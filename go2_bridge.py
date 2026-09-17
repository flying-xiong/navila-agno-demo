"""HTTP bridge for Unitree Go2 using the official ``unitree_sdk2_python`` SDK.

Run this on the robot-side computer or the host wired to the Go2:

    python3 go2_bridge.py \
        --network-interface eth0 --host 0.0.0.0 --port 8013

The bridge owns all DDS/SDK imports so the Agno and VLA processes never need
``unitree_sdk2py`` installed. Motion is disabled until ``ENABLE_MOTION=1`` is
set explicitly, and every request carries ``dry_run`` as an extra guard.
"""
from __future__ import annotations

import argparse
import os
import threading
import time
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

app = FastAPI(title="Unitree Go2 Bridge", version="0.1.0")

OBSTACLE_STOP_DISTANCE_M = float(os.getenv("GO2_OBSTACLE_STOP_DISTANCE_M", "0.6"))
FRAME_MAX_WIDTH = int(os.getenv("GO2_FRAME_MAX_WIDTH", "1280"))
FRAME_JPEG_QUALITY = int(os.getenv("GO2_FRAME_JPEG_QUALITY", "80"))
MOVE_WATCHDOG_GRACE_S = float(os.getenv("GO2_MOVE_WATCHDOG_GRACE_S", "0.5"))
MOVE_REPEAT_PERIOD_S = float(os.getenv("GO2_MOVE_REPEAT_PERIOD_S", "0.05"))


class MoveRequest(BaseModel):
    vx: float = Field(default=0.0, ge=-1.0, le=1.0)
    vy: float = Field(default=0.0, ge=-1.0, le=1.0)
    vyaw: float = Field(default=0.0, ge=-5.0, le=5.0)
    duration_sec: float = Field(default=0.5, ge=0.0, le=10.0)
    dry_run: bool = True


class Go2SDK:
    """Lazy holder for the official Unitree Python SDK objects."""

    def __init__(self, network_interface: str = "") -> None:
        self.network_interface = network_interface
        self.motion_enabled = os.getenv("ENABLE_MOTION", "0") == "1"
        self.video_enabled = os.getenv("ENABLE_VIDEO", "1") == "1"
        self._lock = threading.Lock()
        self._frame_lock = threading.Lock()
        self._initialized = False
        self._sport_client: Any = None
        self._video_client: Any = None
        self._frame_thread: Optional[threading.Thread] = None
        self._frame_stop = threading.Event()
        self._latest_frame: Optional[bytes] = None
        self._latest_frame_time = 0.0
        self._state: dict[str, Any] = {
            "position": [0.0, 0.0, 0.0],
            "velocity": [0.0, 0.0, 0.0],
            "mode": "unknown",
            "battery": None,
            "obstacle": False,
            "range_obstacle": [0.0, 0.0, 0.0, 0.0],
        }
        self.sdk_available = False

    def ensure_initialized(self) -> bool:
        if self._initialized:
            return self.sdk_available
        with self._lock:
            if self._initialized:
                return self.sdk_available
            try:
                self._initialize()
                self.sdk_available = True
            except Exception as exc:  # noqa: BLE001 - bridge must stay alive
                self.sdk_available = False
                print(f"[go2-bridge] SDK 初始化失败：{type(exc).__name__}: {exc}")
            self._initialized = True
            return self.sdk_available

    def _initialize(self) -> None:
        from unitree_sdk2py.core.channel import (
            ChannelFactoryInitialize,
            ChannelSubscriber,
        )
        from unitree_sdk2py.idl.default import unitree_go_msg_dds__SportModeState_
        from unitree_sdk2py.idl.unitree_go.msg.dds_ import SportModeState_
        from unitree_sdk2py.go2.sport.sport_client import SportClient

        if self.network_interface:
            ChannelFactoryInitialize(0, self.network_interface)
        else:
            ChannelFactoryInitialize(0)

        self._sport_client = SportClient()
        self._sport_client.SetTimeout(10.0)
        self._sport_client.Init()

        if self.video_enabled:
            try:
                from unitree_sdk2py.go2.video.video_client import VideoClient

                self._video_client = VideoClient()
                self._video_client.SetTimeout(3.0)
                self._video_client.Init()
                self._frame_thread = threading.Thread(
                    target=self._frame_loop,
                    name="go2-frame-cache",
                    daemon=True,
                )
                self._frame_thread.start()
            except Exception as exc:  # noqa: BLE001 - camera is optional
                print(f"[go2-bridge] 相机初始化失败（继续无图像模式）：{exc}")
                self._video_client = None

        def _on_state(msg: Any) -> None:
            self._state.update(
                position=_to_list(getattr(msg, "position", None), 3),
                velocity=_to_list(getattr(msg, "velocity", None), 3),
                mode=_sport_mode_name(getattr(msg, "mode", None)),
                battery=_battery_from_state(msg),
                obstacle=_obstacle_from_state(msg),
                range_obstacle=_to_list(getattr(msg, "range_obstacle", None), 4),
            )

        self._state_subscriber = ChannelSubscriber("rt/sportmodestate", SportModeState_)
        self._state_subscriber.Init(_on_state, 10)

    def move(self, req: MoveRequest) -> dict[str, Any]:
        if req.dry_run:
            return _move_response(req, dry_run=True)

        if not self.motion_enabled:
            raise HTTPException(
                status_code=403,
                detail="ENABLE_MOTION 未开启；拒绝真实运动指令。",
            )
        if not self.ensure_initialized() or self._sport_client is None:
            raise HTTPException(status_code=503, detail="Go2 SDK 不可用")

        move_rc = -1
        stop_rc = -1
        watchdog = threading.Timer(
            max(req.duration_sec, 0.1) + MOVE_WATCHDOG_GRACE_S,
            self._stop_move_safely,
        )
        watchdog.daemon = True
        watchdog.start()
        try:
            deadline = time.monotonic() + max(req.duration_sec, 0.0)
            while True:
                move_rc = int(self._sport_client.Move(req.vx, req.vy, req.vyaw))
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(MOVE_REPEAT_PERIOD_S, remaining))
        except Exception as exc:  # noqa: BLE001 - report SDK failure to client
            raise HTTPException(status_code=502, detail=f"Go2 Move 调用失败：{exc}") from exc
        finally:
            watchdog.cancel()
            stop_rc = self._stop_move_safely()
        return _move_response(
            req,
            dry_run=False,
            move_rc=move_rc,
            stop_rc=stop_rc,
        )

    def _stop_move_safely(self) -> int:
        try:
            if self._sport_client is None:
                return -1
            return int(self._sport_client.StopMove())
        except Exception:  # noqa: BLE001 - best effort safety stop
            return -1

    def stop(self) -> dict[str, Any]:
        self.ensure_initialized()
        return {"rc": self._stop_move_safely(), "msg": "ok"}

    def stand_up(self) -> dict[str, Any]:
        if not self.motion_enabled:
            raise HTTPException(status_code=403, detail="ENABLE_MOTION 未开启")
        if self.ensure_initialized() and self._sport_client is not None:
            return {"rc": int(self._sport_client.StandUp()), "msg": "ok"}
        return {"rc": 0, "msg": "ok"}

    def damp(self) -> dict[str, Any]:
        if self.ensure_initialized() and self._sport_client is not None:
            return {"rc": int(self._sport_client.Damp()), "msg": "ok"}
        return {"rc": 0, "msg": "ok"}

    def get_state(self) -> dict[str, Any]:
        self.ensure_initialized()
        return {
            "available": self.sdk_available,
            "state": dict(self._state),
            "motion_enabled": self.motion_enabled,
        }

    def get_frame(self) -> bytes:
        self.ensure_initialized()
        if not self.video_enabled or self._video_client is None:
            raise HTTPException(status_code=503, detail="相机不可用")
        with self._frame_lock:
            frame = self._latest_frame
        if frame is None:
            raise HTTPException(status_code=503, detail="相机帧尚未就绪")
        return frame

    def _frame_loop(self) -> None:
        while not self._frame_stop.is_set():
            try:
                if self._video_client is None:
                    return
                code, data = self._video_client.GetImageSample()
                if code == 0 and data:
                    frame = _encode_frame(bytes(data))
                    with self._frame_lock:
                        self._latest_frame = frame
                        self._latest_frame_time = time.time()
                else:
                    time.sleep(0.1)
            except Exception as exc:  # noqa: BLE001 - keep video loop alive
                print(f"[go2-bridge] 取帧失败：{type(exc).__name__}: {exc}")
                time.sleep(0.5)


def _move_response(
    req: MoveRequest,
    dry_run: bool,
    move_rc: int = 0,
    stop_rc: int = 0,
) -> dict[str, Any]:
    return {
        "rc": move_rc,
        "stop_rc": stop_rc,
        "msg": "dry_run" if dry_run else "ok",
        "dry_run": dry_run,
        "vx": req.vx,
        "vy": req.vy,
        "vyaw": req.vyaw,
        "duration_sec": req.duration_sec,
    }


def _encode_frame(data: bytes) -> bytes:
    if FRAME_MAX_WIDTH <= 0:
        return data
    try:
        import cv2
        import numpy as np
    except Exception:  # noqa: BLE001 - optional dependency
        return data
    try:
        image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            return data
        height, width = image.shape[:2]
        if width > FRAME_MAX_WIDTH:
            scale = FRAME_MAX_WIDTH / float(width)
            image = cv2.resize(
                image,
                (FRAME_MAX_WIDTH, int(height * scale)),
                interpolation=cv2.INTER_AREA,
            )
        ok, encoded = cv2.imencode(
            ".jpg",
            image,
            [int(cv2.IMWRITE_JPEG_QUALITY), FRAME_JPEG_QUALITY],
        )
        return encoded.tobytes() if ok else data
    except Exception:  # noqa: BLE001 - fall back to raw JPEG
        return data


def _to_list(value: Any, size: int) -> list[float]:
    if value is None:
        return [0.0] * size
    try:
        return [float(x) for x in value][:size]
    except (TypeError, ValueError):
        return [0.0] * size


def _sport_mode_name(mode: Any) -> str:
    if mode is None:
        return "unknown"
    if isinstance(mode, str):
        return mode
    names = {
        0: "idle",
        1: "stand_up",
        2: "stand_down",
        3: "walk",
        4: "trot",
        5: "run",
        6: "climb",
        7: "damp",
        8: "recovery",
    }
    try:
        return names.get(int(mode), str(mode))
    except (TypeError, ValueError):
        return str(mode)


def _battery_from_state(msg: Any) -> Optional[float]:
    bms = getattr(msg, "bms_state", None)
    soc = getattr(bms, "soc", None)
    if soc is None:
        return None
    try:
        value = float(soc)
        return value if value <= 1.0 else value / 100.0
    except (TypeError, ValueError):
        return None


def _obstacle_from_state(msg: Any) -> bool:
    range_obstacle = getattr(msg, "range_obstacle", None)
    if range_obstacle is None:
        return False
    try:
        values = [float(x) for x in range_obstacle]
    except (TypeError, ValueError):
        return False
    return any(0.0 < value <= OBSTACLE_STOP_DISTANCE_M for value in values)


_sdk = Go2SDK()


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "sdk_available": _sdk.sdk_available,
        "motion_enabled": _sdk.motion_enabled,
        "video_enabled": _sdk.video_enabled,
    }


@app.get("/state")
def state() -> dict[str, Any]:
    return _sdk.get_state()


@app.post("/move")
def move(req: MoveRequest) -> dict[str, Any]:
    return _sdk.move(req)


@app.post("/stop")
def stop() -> dict[str, Any]:
    return _sdk.stop()


@app.post("/stand")
def stand_up() -> dict[str, Any]:
    return _sdk.stand_up()


@app.post("/damp")
def damp() -> dict[str, Any]:
    return _sdk.damp()


@app.get("/frame")
def frame() -> Response:
    data = _sdk.get_frame()
    return Response(content=data, media_type="image/jpeg")


def main() -> None:
    parser = argparse.ArgumentParser(description="Unitree Go2 HTTP bridge")
    parser.add_argument("--host", default=os.getenv("GO2_BRIDGE_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("GO2_BRIDGE_PORT", "8013")))
    parser.add_argument("--network-interface", default=os.getenv("GO2_NETWORK_INTERFACE", ""))
    args = parser.parse_args()

    global _sdk
    _sdk = Go2SDK(network_interface=args.network_interface)

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
