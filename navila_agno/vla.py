"""VLA policy adapters and the stateful mid-level executor.

This module deliberately keeps model-specific transport concerns behind a
small ``VLAPolicy`` interface. The executor only needs:

* current subgoal instruction
* recent image paths
* optional robot state

and receives either a NaVILA-style free-form action string or an already
structured :class:`MidLevelAction` from newer waypoint models such as
LightNav-0.
"""
from __future__ import annotations

import asyncio
import base64
import json
import math
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional, Protocol

from .contracts import (
    EventType,
    ExecutionEvent,
    ExecutionOutcome,
    MidLevelAction,
    RobotState,
    StepRecord,
    SubGoal,
)
from .navila_memory import DEFAULT_NUM_VIDEO_FRAMES
from .robot import RobotInterface, RobotTransportError


class VLAPolicy(Protocol):
    """Anything that turns an instruction + frames into a navigation action."""

    def predict(
        self,
        instruction: str,
        image_paths: list[str],
        state: Optional[RobotState] = None,
    ) -> str | MidLevelAction: ...


PayloadBuilder = Callable[[str, list[str], Optional[RobotState]], dict[str, Any]]
ResponseParser = Callable[[dict[str, Any]], str]


def _extract_number(text: str, default: float) -> float:
    match = re.search(r"(-?\d+(?:\.\d+)?)", text)
    return float(match.group(1)) if match else default


def parse_action(raw: str) -> MidLevelAction:
    """Parse NaVILA's free-form language output into a structured action."""
    text = raw.lower()
    if "stop" in text or "complete" in text or "arrived" in text:
        return MidLevelAction(action_type="stop")

    if "left" in text and "right" not in text:
        angle = _extract_number(text, default=30.0)
        return MidLevelAction(action_type="turn_left", angle_deg=angle)

    if "right" in text:
        angle = _extract_number(text, default=30.0)
        return MidLevelAction(action_type="turn_right", angle_deg=angle)

    if "forward" in text or "move" in text or "ahead" in text:
        # Prefer centimeters, otherwise meters.
        if "cm" in text:
            distance = _extract_number(text, default=25.0) / 100.0
        else:
            distance = _extract_number(text, default=0.25)
        return MidLevelAction(action_type="move_forward", distance_m=distance)

    return MidLevelAction(action_type="move_forward", distance_m=0.25)


class MockVLA:
    """Deterministic VLA used for offline demo and tests."""

    def __init__(self, default_step_m: float = 0.5) -> None:
        self.default_step_m = default_step_m

    def predict(
        self,
        instruction: str,
        image_paths: list[str],
        state: Optional[RobotState] = None,
    ) -> str:
        remaining = state.remaining_distance_m if state else 0.0
        if remaining <= 0.05:
            return "stop, the subgoal is completed."
        distance = min(self.default_step_m, remaining)
        return f"move forward {distance:.2f} m."


def build_navila_payload(
    instruction: str,
    image_paths: list[str],
    state: Optional[RobotState] = None,
) -> dict[str, Any]:
    """Request body understood by the NaVILA bridge.

    ``image_paths`` is the whole recorded history; the bridge samples it down
    to ``num_video_frames`` slots the way the official evaluation loop does.
    """
    return {
        "instruction": instruction,
        "image_paths": image_paths,
        "num_video_frames": navila_video_frames(),
    }


def navila_video_frames() -> int:
    """Clip length NaVILA consumes; the checkpoint ships with 8."""
    raw = os.getenv("NAVILA_NUM_VIDEO_FRAMES")
    try:
        value = int(raw) if raw is not None else DEFAULT_NUM_VIDEO_FRAMES
    except ValueError:
        value = DEFAULT_NUM_VIDEO_FRAMES
    return max(1, value)


def parse_navila_response(data: dict[str, Any]) -> str:
    return str(data.get("action", "stop"))


@dataclass
class HttpVLAClient:
    """Generic HTTP VLA bridge client.

    Backends are configured with small ``PayloadBuilder`` / ``ResponseParser``
    functions, so adding a future HTTP-served VLA does not require changing the
    executor. ``navila`` is the default builder because it is the existing
    bridge contract.
    """

    endpoint: str = "http://127.0.0.1:8011"
    timeout_s: float = 180.0
    backend: str = "navila"
    payload_builder: Optional[PayloadBuilder] = None
    response_parser: Optional[ResponseParser] = None

    def __post_init__(self) -> None:
        if self.payload_builder is None:
            if self.backend == "navila":
                self.payload_builder = build_navila_payload
            else:
                self.payload_builder = build_navila_payload
        if self.response_parser is None:
            self.response_parser = parse_navila_response

    def predict(
        self,
        instruction: str,
        image_paths: list[str],
        state: Optional[RobotState] = None,
    ) -> str:
        import httpx

        if not image_paths:
            # No observation means no information; "stop" is the only safe
            # command but it must be visible in the logs, because the executor
            # cannot tell it apart from a real "arrived" answer.
            print(f"[{self.backend}] 观测帧为空，返回 stop（疑似取帧失败，不是真的到达）")
            return "stop"

        assert self.payload_builder is not None
        assert self.response_parser is not None
        payload = self.payload_builder(instruction, image_paths, state)
        response = httpx.post(
            f"{self.endpoint.rstrip('/')}/v1/navigate",
            json=payload,
            timeout=self.timeout_s,
        )
        response.raise_for_status()
        return self.response_parser(response.json())


class NaVILAHttpClient(HttpVLAClient):
    """Backward-compatible alias around :class:`HttpVLAClient` for NaVILA."""

    def __init__(
        self,
        endpoint: str = "http://127.0.0.1:8011",
        timeout_s: float = 180.0,
    ) -> None:
        super().__init__(endpoint=endpoint, timeout_s=timeout_s, backend="navila")


def lightnav_waypoint_to_action(data: dict[str, Any]) -> MidLevelAction:
    """Convert a LightNav-0 WebSocket ``next`` response to a mid-level action.

    LightNav-0 returns a chunk of SE(2) waypoints. We execute the first
    waypoint as a short robot-frame displacement: forward meters, left-positive
    lateral meters, and left-positive yaw degrees.
    """
    if data.get("stop") or data.get("rc") not in (None, 0):
        return MidLevelAction(action_type="stop")

    actions = (data.get("actions") or {}).get("actions") or []
    if not actions:
        return MidLevelAction(action_type="stop")

    first = actions[0]
    if len(first) < 3:
        return MidLevelAction(action_type="stop")

    dx, dy, dyaw = (float(x) for x in first[:3])
    if abs(dx) < 0.03 and abs(dy) < 0.03 and abs(dyaw) < 0.03:
        return MidLevelAction(action_type="stop")

    if abs(dyaw) >= 0.05:
        angle_deg = math.degrees(abs(dyaw))
        action_type = "turn_left" if dyaw > 0 else "turn_right"
        return MidLevelAction(action_type=action_type, angle_deg=angle_deg)

    return MidLevelAction(
        action_type="move",
        distance_m=dx,
        lateral_m=dy,
        angle_deg=math.degrees(dyaw),
    )


@dataclass
class LightNavWSClient:
    """LightNav-0 WebSocket policy adapter.

    The official ``lightnav-serve`` protocol accepts one JPEG frame per
    ``next`` message and returns a waypoint chunk. This adapter re-opens a
    session per executor step, pre-fills older frames with empty instructions,
    and runs inference only on the newest frame.
    """

    endpoint: str = "ws://127.0.0.1:8050"
    timeout_s: float = 120.0

    def predict(
        self,
        instruction: str,
        image_paths: list[str],
        state: Optional[RobotState] = None,
    ) -> MidLevelAction:
        if not image_paths:
            return MidLevelAction(action_type="stop")

        try:
            import websockets
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise ImportError(
                "LightNav-0 后端需要 `websockets`：pip install websockets"
            ) from exc

        async def _run_session() -> MidLevelAction:
            async with websockets.connect(
                self.endpoint,
                max_size=None,
                open_timeout=self.timeout_s,
            ) as ws:
                await ws.send(json.dumps({"action": "reset", "data": {}}))
                await ws.recv()

                last_index = len(image_paths) - 1
                prediction: dict[str, Any] = {}
                for seq, path in enumerate(image_paths):
                    with open(path, "rb") as fh:
                        image_b64 = base64.b64encode(fh.read()).decode("ascii")
                    step_instruction = instruction if seq == last_index else ""
                    await ws.send(
                        json.dumps(
                            {
                                "action": "next",
                                "data": {
                                    "seq": seq,
                                    "image": image_b64,
                                    "instruction": step_instruction,
                                },
                            },
                            ensure_ascii=False,
                        )
                    )
                    response = json.loads(await ws.recv())
                    data = response.get("data") or {}
                    if seq == last_index:
                        prediction = data

                return lightnav_waypoint_to_action(prediction)

        return asyncio.run(_run_session())


@dataclass
class VLAExecutor:
    """Stateful mid-level executor that runs one SubGoal to completion."""

    policy: VLAPolicy
    robot: RobotInterface
    frame_buffer_size: int = 8
    #: Which frames the VLA sees: "episode" (whole mission, NaVILA's official
    #: memory), "subgoal" (frames since this subgoal started) or "recent"
    #: (sliding window of ``frame_buffer_size`` frames).
    frame_memory: str = "episode"
    on_event: Optional[Callable[[ExecutionEvent], None]] = None
    on_step: Optional[Callable[[StepRecord], None]] = None
    on_subgoal: Optional[Callable[[SubGoal], None]] = None
    _last_event: Optional[ExecutionEvent] = field(default=None, repr=False)

    def _emit(self, event: ExecutionEvent) -> ExecutionEvent:
        self._last_event = event
        if self.on_event is not None:
            self.on_event(event)
        return event

    def _emit_step(self, record: StepRecord) -> None:
        if self.on_step is not None:
            self.on_step(record)

    def begin_mission(self) -> None:
        begin = getattr(self.robot, "begin_mission", None)
        if callable(begin):
            begin()

    def _observation_frames(self) -> list[str]:
        if self.frame_memory == "recent":
            return self.robot.recent_frames(self.frame_buffer_size)
        if self.frame_memory == "subgoal":
            return self.robot.subgoal_frames()
        return self.robot.mission_frames()

    def _step_record(
        self,
        subgoal: SubGoal,
        step: int,
        state: RobotState,
        raw_action: str,
        action: MidLevelAction,
        history_frames: int = 0,
    ) -> StepRecord:
        return StepRecord(
            subgoal_id=subgoal.id,
            instruction=subgoal.instruction,
            step=step,
            timestamp=datetime.now().isoformat(timespec="seconds"),
            raw_vla=raw_action,
            action_type=action.action_type,
            action_distance_m=action.distance_m,
            action_angle_deg=action.angle_deg,
            action_lateral_m=action.lateral_m,
            frame_path=state.frame_path,
            history_frames=history_frames,
            video_frames=min(history_frames, navila_video_frames()),
            robot_state={
                "position": state.position,
                "mode": state.mode,
                "battery": state.battery,
                "obstacle": state.obstacle,
                "remaining_distance_m": state.remaining_distance_m,
            },
        )

    def execute(self, subgoal: SubGoal) -> ExecutionOutcome:
        self.robot.begin_subgoal(subgoal)
        if self.on_subgoal is not None:
            self.on_subgoal(subgoal)
        trace: list[MidLevelAction] = []

        for step in range(1, subgoal.max_steps + 1):
            state = self.robot.observe()

            if state.obstacle:
                event = self._emit(
                    ExecutionEvent(
                        type=EventType.BLOCKED,
                        subgoal_id=subgoal.id,
                        message="检测到不可通行障碍，请求重规划。",
                        step_count=step,
                        state=state,
                    )
                )
                return ExecutionOutcome(event=event, action_trace=trace)

            if self.robot.is_complete(subgoal, state):
                event = self._emit(
                    ExecutionEvent(
                        type=EventType.SUBGOAL_COMPLETED,
                        subgoal_id=subgoal.id,
                        message=f"完成子目标：{subgoal.instruction}",
                        step_count=step,
                        state=state,
                    )
                )
                return ExecutionOutcome(event=event, action_trace=trace)

            frames = self._observation_frames()
            raw_action = self.policy.predict(subgoal.instruction, frames, state)
            raw_text = (
                raw_action
                if isinstance(raw_action, str)
                else raw_action.describe()
            )
            action = (
                raw_action
                if isinstance(raw_action, MidLevelAction)
                else parse_action(raw_action)
            )
            record = self._step_record(
                subgoal, step, state, raw_text, action, history_frames=len(frames)
            )

            trace.append(action)
            try:
                self.robot.execute(action)
            except RobotTransportError as exc:
                record.command = getattr(self.robot, "last_command", None)
                record.command_response = getattr(
                    self.robot, "last_command_response", None
                )
                self._emit_step(record)
                event = self._emit(
                    ExecutionEvent(
                        type=EventType.SAFETY_STOP,
                        subgoal_id=subgoal.id,
                        message=f"机器人通信失败，已触发安全停止：{exc}",
                        step_count=step,
                        state=state,
                    )
                )
                return ExecutionOutcome(event=event, action_trace=trace)

            record.command = getattr(self.robot, "last_command", None)
            record.command_response = getattr(
                self.robot, "last_command_response", None
            )
            self._emit_step(record)

            if action.action_type == "stop":
                instruction = subgoal.instruction.lower()
                turn_only = "turn" in instruction and not any(
                    keyword in instruction
                    for keyword in ("walk", "move", "go ", "proceed", "forward")
                )
                if (
                    step <= 1
                    and state.remaining_distance_m > 1.0
                    and not turn_only
                ):
                    event = self._emit(
                        ExecutionEvent(
                            type=EventType.UNCERTAIN,
                            subgoal_id=subgoal.id,
                            message=(
                                "VLA 在第一步返回 stop，但估计剩余距离 "
                                f"{state.remaining_distance_m:.2f}m，请求重新规划。"
                            ),
                            step_count=step,
                            state=state,
                        )
                    )
                    return ExecutionOutcome(event=event, action_trace=trace)
                self.robot.mark_complete()
                event = self._emit(
                    ExecutionEvent(
                        type=EventType.SUBGOAL_COMPLETED,
                        subgoal_id=subgoal.id,
                        message=(
                            "VLA 返回 stop，且剩余距离已接近目标，"
                            "子目标完成。"
                        ),
                        step_count=step,
                        state=state,
                    )
                )
                return ExecutionOutcome(event=event, action_trace=trace)

        event = self._emit(
            ExecutionEvent(
                type=EventType.SUBGOAL_FAILED,
                subgoal_id=subgoal.id,
                message=f"超过 max_steps={subgoal.max_steps}，子目标失败。",
                step_count=subgoal.max_steps,
                state=self.robot.observe(),
            )
        )
        return ExecutionOutcome(event=event, action_trace=trace)


def make_vla(
    backend: str,
    endpoint: str = "http://127.0.0.1:8011",
) -> VLAPolicy:
    """Create a VLA policy by backend name.

    Supported names:
    * ``mock``: deterministic offline policy
    * ``http`` / ``navila``: NaVILA HTTP bridge
    * ``lightnav``: LightNav-0 WebSocket server
    """
    name = backend.lower()
    if name in {"mock"}:
        return MockVLA()
    if name in {"http", "navila"}:
        return NaVILAHttpClient(endpoint=endpoint)
    if name == "lightnav":
        return LightNavWSClient(endpoint=endpoint)
    raise ValueError(f"未知 VLA 后端：{backend}")
