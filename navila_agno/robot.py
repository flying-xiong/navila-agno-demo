"""Robot interfaces plus deterministic simulated and Go2 HTTP adapters."""
from __future__ import annotations

import math
import os
import time
from collections import deque
from pathlib import Path
from typing import Any, Protocol

from .contracts import MidLevelAction, RobotState, SubGoal


class RobotTransportError(RuntimeError):
    """Raised when the HTTP bridge cannot be reached after retries."""


class RobotCommandError(RobotTransportError):
    """Raised when the Go2 SDK rejects a motion command."""


class RobotInterface(Protocol):
    def begin_subgoal(self, subgoal: SubGoal) -> None: ...

    def observe(self) -> RobotState: ...

    def execute(self, action: MidLevelAction) -> RobotState: ...

    def recent_frames(self, n: int) -> list[str]: ...

    def is_complete(self, subgoal: SubGoal, state: RobotState) -> bool: ...

    def mark_complete(self) -> None: ...


class MockRobot:
    """A tiny 1-D navigation simulation.

    It keeps a frame ring buffer and can be told to become blocked on a given
    subgoal to demonstrate event-driven replanning.
    """

    def __init__(self, fail_subgoal_id: str | None = None, fail_after_steps: int = 2) -> None:
        self.fail_subgoal_id = fail_subgoal_id
        self.fail_after_steps = fail_after_steps
        self.current_subgoal: SubGoal | None = None
        self.step = 0
        self.remaining_distance_m = 0.0
        self.complete = False
        self._frames: deque[str] = deque([f"mock://frame/{self.step}"], maxlen=32)

    def begin_subgoal(self, subgoal: SubGoal) -> None:
        self.current_subgoal = subgoal
        self.step = 0
        self.complete = False
        self.remaining_distance_m = subgoal.estimated_distance_m or 1.0
        self._frames.clear()
        self._frames.append(f"mock://frame/{self.step}")

    def observe(self) -> RobotState:
        obstacle = (
            self.fail_subgoal_id is not None
            and self.current_subgoal is not None
            and self.current_subgoal.id == self.fail_subgoal_id
            and self.step >= self.fail_after_steps
        )
        return RobotState(
            position={"x": self.step, "floor": "unknown"},
            battery=0.82,
            mode="navigating" if not self.complete else "idle",
            frame_path=self._frames[-1],
            obstacle=obstacle,
            remaining_distance_m=self.remaining_distance_m,
        )

    def execute(self, action: MidLevelAction) -> RobotState:
        if action.action_type in {"move_forward", "move"}:
            self.remaining_distance_m = max(
                0.0, self.remaining_distance_m - max(action.distance_m, 0.0)
            )
        self.step += 1
        self._frames.append(f"mock://frame/{self.step}")
        return self.observe()

    def recent_frames(self, n: int) -> list[str]:
        return list(self._frames)[-n:]

    def is_complete(self, subgoal: SubGoal, state: RobotState) -> bool:
        return self.complete or state.remaining_distance_m <= 0.05

    def mark_complete(self) -> None:
        self.complete = True
        self.remaining_distance_m = 0.0


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def action_to_go2_command(
    action: MidLevelAction,
    forward_speed_mps: float = 0.30,
    max_linear_speed_mps: float = 0.50,
    max_lateral_speed_mps: float = 0.30,
    max_yaw_speed_deg_per_s: float = 80.0,
    waypoint_duration_s: float = 0.50,
) -> dict[str, float]:
    """Convert a mid-level action to a Go2 SportClient twist command.

    ``move`` actions are waypoint displacements in LightNav-0 convention:
    forward meters, left-positive lateral meters, left-positive yaw degrees.
    The official LightNav deployment normalises displacements by 0.375 m and
    yaw by 9 degrees, which is preserved here before velocity clamping.
    """
    if action.action_type == "stop":
        return {"vx": 0.0, "vy": 0.0, "vyaw": 0.0, "duration_sec": 0.0}

    if action.action_type == "move":
        vx = max(-1.0, min(1.0, action.distance_m / 0.375)) * max_linear_speed_mps
        vy = max(-1.0, min(1.0, action.lateral_m / 0.375)) * max_lateral_speed_mps
        vyaw = max(-1.0, min(1.0, action.angle_deg / 9.0)) * max_yaw_speed_deg_per_s
        return {
            "vx": vx,
            "vy": vy,
            "vyaw": vyaw,
            "duration_sec": waypoint_duration_s,
        }

    if action.action_type == "move_forward":
        distance = max(action.distance_m, 0.0)
        speed = max(0.05, min(forward_speed_mps, max_linear_speed_mps))
        return {
            "vx": speed,
            "vy": 0.0,
            "vyaw": 0.0,
            "duration_sec": max(0.1, min(5.0, distance / speed)),
        }

    if action.action_type in {"turn_left", "turn_right"}:
        direction = -1.0 if action.action_type == "turn_left" else 1.0
        angle_deg = max(abs(action.angle_deg), 0.1)
        yaw_speed = max(5.0, min(max_yaw_speed_deg_per_s, max_yaw_speed_deg_per_s))
        return {
            "vx": 0.0,
            "vy": 0.0,
            "vyaw": direction * yaw_speed,
            "duration_sec": max(0.1, min(5.0, angle_deg / yaw_speed)),
        }

    return {"vx": 0.0, "vy": 0.0, "vyaw": 0.0, "duration_sec": 0.0}


def _as_position(data: Any) -> dict[str, Any]:
    if isinstance(data, dict):
        return dict(data)
    if isinstance(data, (list, tuple)):
        keys = ("x", "y", "z")
        return {keys[i]: data[i] for i in range(min(len(keys), len(data)))}
    return {"x": 0.0, "y": 0.0, "z": 0.0}


def _distance_between(first: dict[str, Any], second: dict[str, Any]) -> float:
    try:
        dx = float(second.get("x", 0.0)) - float(first.get("x", 0.0))
        dy = float(second.get("y", 0.0)) - float(first.get("y", 0.0))
        return math.hypot(dx, dy)
    except (TypeError, ValueError):
        return 0.0


class Go2HttpRobot:
    """Robot adapter for the standalone Go2 bridge service.

    The bridge owns the official ``unitree_sdk2py`` imports and all DDS
    initialisation. This package only speaks HTTP so it can run from the Agno
    environment or any other host that can reach the robot bridge.
    """

    def __init__(
        self,
        endpoint: str = "http://127.0.0.1:8013",
        timeout_s: float = 10.0,
        dry_run: bool = True,
        frame_dir: str | Path = "data/go2_frames",
        frame_fetch: bool = True,
        forward_speed_mps: float = 0.30,
        max_linear_speed_mps: float = 0.50,
        max_lateral_speed_mps: float = 0.30,
        max_yaw_speed_deg_per_s: float = 80.0,
        waypoint_duration_s: float = 0.50,
        max_retries: int = 3,
        retry_backoff_s: float = 0.5,
        connect_timeout_s: float = 5.0,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.timeout_s = timeout_s
        self.dry_run = dry_run
        self.frame_fetch = frame_fetch
        self.frame_dir = Path(frame_dir)
        self.frame_dir.mkdir(parents=True, exist_ok=True)
        self.forward_speed_mps = forward_speed_mps
        self.max_linear_speed_mps = max_linear_speed_mps
        self.max_lateral_speed_mps = max_lateral_speed_mps
        self.max_yaw_speed_deg_per_s = max_yaw_speed_deg_per_s
        self.waypoint_duration_s = waypoint_duration_s
        self.max_retries = max(1, max_retries)
        self.retry_backoff_s = max(0.0, retry_backoff_s)
        self.connect_timeout_s = max(0.5, connect_timeout_s)

        self.current_subgoal: SubGoal | None = None
        self.step = 0
        self.remaining_distance_m = 0.0
        self._estimated_distance_m = 0.0
        self._start_position: dict[str, Any] | None = None
        self.complete = False
        self._frames: deque[str] = deque(maxlen=32)
        self.recorded_frames: list[str] = []
        self.last_command: dict[str, Any] | None = None
        self.last_command_response: dict[str, Any] | None = None

    def begin_subgoal(self, subgoal: SubGoal) -> None:
        self.current_subgoal = subgoal
        self.step = 0
        self.complete = False
        self._estimated_distance_m = (
            float(subgoal.estimated_distance_m)
            if subgoal.estimated_distance_m is not None
            else 1.0
        )
        self.remaining_distance_m = self._estimated_distance_m
        self._start_position = None
        try:
            data = self._get_json("/state")
            state = data.get("state") if isinstance(data, dict) else None
            if isinstance(state, dict):
                self._start_position = _as_position(state.get("position"))
        except Exception:  # noqa: BLE001 - fall back to action-distance estimate
            self._start_position = None
        self._frames.clear()

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        import httpx

        url = f"{self.endpoint}{path}"
        timeout = httpx.Timeout(self.timeout_s, connect=self.connect_timeout_s)
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = httpx.request(
                    method,
                    url,
                    json=payload,
                    timeout=timeout,
                )
                response.raise_for_status()
                return response
            except (
                httpx.ConnectError,
                httpx.ConnectTimeout,
                httpx.ReadTimeout,
                httpx.RemoteProtocolError,
                httpx.WriteError,
            ) as exc:
                last_error = exc
                if attempt < self.max_retries:
                    time.sleep(self.retry_backoff_s * attempt)
        raise RobotTransportError(
            f"{method} {path} 失败（已重试 {self.max_retries} 次）：{last_error}"
        ) from last_error

    def _get_json(self, path: str) -> dict[str, Any]:
        return self._request("GET", path).json()

    def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", path, payload).json()

    def _fetch_frame(self) -> str:
        if not self.frame_fetch:
            return ""
        try:
            response = self._request("GET", "/frame")
            if not response.content:
                return ""
            name = (
                f"{self.current_subgoal.id if self.current_subgoal else 'mission'}"
                f"_step{self.step:04d}.jpg"
            )
            path = self.frame_dir / name
            path.write_bytes(response.content)
            resolved = str(path.resolve())
            self._frames.append(resolved)
            if not self.recorded_frames or self.recorded_frames[-1] != resolved:
                self.recorded_frames.append(resolved)
            return resolved
        except Exception:
            return ""

    def observe(self) -> RobotState:
        state_data: dict[str, Any] = {}
        try:
            data = self._get_json("/state")
            if isinstance(data, dict):
                state_data = data.get("state") or data
        except Exception:
            state_data = {}

        frame_path = self._fetch_frame() or (self._frames[-1] if self._frames else "")
        position = _as_position(state_data.get("position", {"x": self.step}))
        if self._start_position is not None and not self.dry_run:
            traveled = _distance_between(self._start_position, position)
            self.remaining_distance_m = max(
                0.0,
                self._estimated_distance_m - traveled,
            )
        try:
            battery = float(state_data.get("battery", 1.0) or 1.0)
        except (TypeError, ValueError):
            battery = 1.0
        mode = str(state_data.get("mode") or ("navigating" if not self.complete else "idle"))
        obstacle = bool(
            state_data.get("obstacle")
            or state_data.get("safety_stop")
            or state_data.get("blocked")
        )
        return RobotState(
            position=position,
            battery=battery,
            mode=mode,
            frame_path=frame_path,
            obstacle=obstacle,
            remaining_distance_m=self.remaining_distance_m,
        )

    def _send_action(self, action: MidLevelAction) -> None:
        command = action_to_go2_command(
            action,
            forward_speed_mps=self.forward_speed_mps,
            max_linear_speed_mps=self.max_linear_speed_mps,
            max_lateral_speed_mps=self.max_lateral_speed_mps,
            max_yaw_speed_deg_per_s=self.max_yaw_speed_deg_per_s,
            waypoint_duration_s=self.waypoint_duration_s,
        )
        command["dry_run"] = self.dry_run
        self.last_command = dict(command)
        self.last_command_response = None
        if self.dry_run:
            print(
                f"[go2:dry-run] {action.describe()} -> "
                f"vx={command['vx']:.2f} vy={command['vy']:.2f} "
                f"vyaw={command['vyaw']:.2f} dur={command['duration_sec']:.2f}s"
            )
            self.last_command_response = {"rc": 0, "msg": "dry-run"}
            return

        if action.action_type == "stop":
            self.last_command_response = self._post_json("/stop", {})
            return
        try:
            response = self._post_json("/move", command)
            self.last_command_response = dict(response)
            rc = int(response.get("rc", 0) or 0)
            if rc != 0:
                raise RobotCommandError(f"Go2 Move 返回错误码 rc={rc}")
        except RobotTransportError:
            self._stop_safely()
            raise

    def _stop_safely(self) -> None:
        try:
            self.last_command_response = self._post_json("/stop", {})
        except Exception:  # noqa: BLE001 - best effort safety stop
            return

    def execute(self, action: MidLevelAction) -> RobotState:
        self._send_action(action)
        if (
            self.dry_run
            or self._start_position is None
        ) and action.action_type in {"move_forward", "move"}:
            self.remaining_distance_m = max(
                0.0, self.remaining_distance_m - max(action.distance_m, 0.0)
            )
        self.step += 1
        return self.observe()

    def recent_frames(self, n: int) -> list[str]:
        return list(self._frames)[-n:]

    def is_complete(self, subgoal: SubGoal, state: RobotState) -> bool:
        return self.complete or state.remaining_distance_m <= 0.05

    def mark_complete(self) -> None:
        self.complete = True
        self.remaining_distance_m = 0.0
