"""Shared contracts between the supervisor, executor, VLA and robot layers."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

#: The only stop rules the executor knows how to evaluate.
STOP_KINDS = ("vla_stop", "vla_stop_after_distance", "distance", "steps")

#: How far short of the declared distance a stop still counts as arriving.
#: Distance estimates come from the supervisor's guess and odometry drifts, so
#: requiring the exact number would make every distance rule fail.
STOP_DISTANCE_TOLERANCE_RATIO = 0.2

#: How far past its declared distance a `vla_stop_after_distance` segment may
#: run before the executor stops waiting for a VLA `stop` and hands the segment
#: back to the supervisor. Without this ceiling a coarse merged segment whose
#: target never gets recognised would burn its whole step budget.
STOP_DISTANCE_OVERSHOOT_RATIO = 0.3

#: Hard ceiling on one subgoal's step budget.
MAX_SUBGOAL_STEPS = 30

#: Kinds whose "arrived" verdict comes from the VLA itself.
VLA_STOP_KINDS = ("vla_stop", "vla_stop_after_distance")

#: A VLA `stop` on the very first step, while the supervisor still thinks there
#: is more than this much ground to cover, is treated as a mis-specified
#: subgoal rather than an arrival.
FIRST_STEP_STOP_MARGIN_M = 1.0

#: Aliases the supervisor model tends to invent for the canonical kinds.
_STOP_ALIASES = {
    "stop": "vla_stop",
    "vla": "vla_stop",
    "vla_stop": "vla_stop",
    "stop_after_distance": "vla_stop_after_distance",
    "vla_stop_after_distance": "vla_stop_after_distance",
    "distance_then_stop": "vla_stop_after_distance",
    "distance": "distance",
    "odometry": "distance",
    "steps": "steps",
    "max_steps": "steps",
}


@dataclass
class StopDecision:
    """Result of evaluating a subgoal's stop condition for one step."""

    met: bool = False
    #: The VLA asked to stop, but the condition says it is not there yet.
    premature: bool = False
    reason: str = ""


@dataclass
class StopCondition:
    """A stop rule the executor can judge on its own, without a human.

    ``kind`` is a closed vocabulary so every subgoal coming out of the
    supervisor is machine-checkable:

    ``vla_stop``
        完成条件是 VLA 自己说 stop。适合外观明确、距离短的目标
        （例如"正对电梯门停下"），也是唯一不依赖距离估计的规则。
    ``vla_stop_after_distance``
        VLA 说 stop **且**已经走够 ``distance_m``。早于阈值的 stop 会被报成
        uncertain 交给 supervisor 重规划，而不是当成到达。这是目标类子目标的
        默认规则，因为"第一步就 stop"是我们反复踩到的失败模式。
    ``distance``
        走过 ``distance_m`` 就停，不等 VLA。适合"沿走廊直行 5 米"这种
        不需要识别地标的段落。
    ``steps``
        执行满 ``steps`` 步就停。只适合既不需要识别地标、也不用判断转向是否
        到位的段落；用步数表示"转向完成"是不可判定的，解析阶段会被并入前一段。
    """

    kind: str = "vla_stop"
    distance_m: Optional[float] = None
    steps: Optional[int] = None
    #: 自然语言说明，只用于日志和 PPT 字幕。
    description: str = ""

    def declared_distance_m(
        self, estimated_distance_m: Optional[float]
    ) -> Optional[float]:
        """The distance this condition talks about, if it talks about one."""
        if self.distance_m is not None:
            return float(self.distance_m)
        if estimated_distance_m is not None:
            return float(estimated_distance_m)
        return None

    def threshold_m(self, estimated_distance_m: Optional[float]) -> float:
        """Distance the robot must cover before a stop counts as arrival."""
        declared = self.declared_distance_m(estimated_distance_m)
        if declared is None:
            return 0.0
        return max(0.0, declared * (1.0 - STOP_DISTANCE_TOLERANCE_RATIO))

    def hand_back_after_m(
        self, estimated_distance_m: Optional[float]
    ) -> Optional[float]:
        """Distance budget after which an unconfirmed segment goes back upstream.

        Only ``vla_stop_after_distance`` has one: it is the kind used for
        coarse, landmark-terminated segments, and it is the kind that can hang
        forever when the landmark is never recognised.
        """
        if self.kind != "vla_stop_after_distance":
            return None
        declared = self.declared_distance_m(estimated_distance_m)
        if declared is None or declared <= 0:
            return None
        return declared * (1.0 + STOP_DISTANCE_OVERSHOOT_RATIO)

    def describe(self) -> str:
        if self.description:
            return self.description
        if self.kind == "vla_stop_after_distance":
            return f"走够 {self.distance_m}m 后 VLA 说 stop"
        if self.kind == "distance":
            return f"前进 {self.distance_m}m 后停下"
        if self.kind == "steps":
            return f"执行 {self.steps} 步后结束"
        return "VLA 说 stop"

    def evaluate(
        self,
        *,
        action_type: Optional[str],
        traveled_m: float,
        step: int,
        estimated_distance_m: Optional[float] = None,
    ) -> StopDecision:
        """Judge the condition for one step.

        ``action_type`` is the action the VLA just produced, or ``None`` for
        the pre-action check where no new answer exists yet.
        """
        stopped = action_type == "stop"

        if self.kind == "steps":
            limit = int(self.steps or 0)
            if limit > 0 and step >= limit:
                return StopDecision(met=True, reason=f"已执行 {step} 步（条件 {limit} 步）")
            return StopDecision()

        if self.kind == "distance":
            target = self.threshold_m(estimated_distance_m)
            if traveled_m >= target:
                return StopDecision(
                    met=True, reason=f"已前进 {traveled_m:.2f}m（条件 {target:.2f}m）"
                )
            return StopDecision()

        # Both remaining kinds wait for the VLA to say stop.
        if not stopped:
            return StopDecision()

        if self.kind == "vla_stop":
            return StopDecision(met=True, reason="VLA 返回 stop")

        target = self.threshold_m(estimated_distance_m)
        if traveled_m >= target:
            return StopDecision(
                met=True,
                reason=f"VLA 返回 stop，且已前进 {traveled_m:.2f}m（阈值 {target:.2f}m）",
            )
        return StopDecision(
            premature=True,
            reason=(
                f"VLA 提前返回 stop：只前进了 {traveled_m:.2f}m，"
                f"未达到 {target:.2f}m"
            ),
        )

    @classmethod
    def from_value(
        cls,
        value: Any,
        estimated_distance_m: Optional[float] = None,
    ) -> Optional["StopCondition"]:
        """Parse the supervisor's JSON into a canonical condition."""
        if value is None:
            return None
        if isinstance(value, str):
            data: dict[str, Any] = {"kind": value}
        elif isinstance(value, dict):
            data = dict(value)
        else:
            return None

        raw_kind = str(data.get("kind") or data.get("type") or "").strip().lower()
        kind = _STOP_ALIASES.get(raw_kind)
        if kind is None:
            return None

        distance = data.get("distance_m", data.get("distance"))
        steps = data.get("steps", data.get("max_steps"))
        try:
            distance_m = float(distance) if distance is not None else None
        except (TypeError, ValueError):
            distance_m = None
        try:
            steps_value = int(steps) if steps is not None else None
        except (TypeError, ValueError):
            steps_value = None

        condition = cls(
            kind=kind,
            distance_m=distance_m,
            steps=steps_value,
            description=str(data.get("description") or ""),
        )
        if kind == "distance" and condition.distance_m is None:
            condition.distance_m = estimated_distance_m
        if kind == "steps" and not condition.steps:
            return None
        return condition

    @classmethod
    def default_for(
        cls,
        instruction: str,
        estimated_distance_m: Optional[float],
    ) -> "StopCondition":
        """Fallback used when the supervisor does not declare a condition."""
        lowered = (instruction or "").lower()
        moving_words = ("walk", "move", "go ", "proceed", "forward", "approach", "enter")
        turn_only = "turn" in lowered and not any(word in lowered for word in moving_words)
        if turn_only:
            return cls(kind="vla_stop", description="纯转向子目标，VLA 说 stop 即结束")
        if estimated_distance_m:
            return cls(
                kind="vla_stop_after_distance",
                distance_m=float(estimated_distance_m),
                description="VLA 说 stop 且走够预期距离",
            )
        return cls(kind="vla_stop", description="VLA 说 stop")


class EventType(str, Enum):
    """Executor events that wake the supervisor."""

    SUBGOAL_COMPLETED = "subgoal_completed"
    SUBGOAL_FAILED = "subgoal_failed"
    BLOCKED = "blocked"
    UNCERTAIN = "uncertain"
    SAFETY_STOP = "safety_stop"
    NEED_HELP = "need_help"


@dataclass
class SubGoal:
    """A short, NaVILA-friendly goal emitted by the Agno supervisor."""

    id: str
    instruction: str
    context: str = ""
    constraints: list[str] = field(default_factory=list)
    completion_criteria: str = ""
    max_steps: int = 12
    estimated_distance_m: Optional[float] = None
    note_zh: str = ""
    #: Machine-checkable rule the executor uses to end this subgoal.
    stop_condition: StopCondition = field(default_factory=StopCondition)


def merge_subgoals(first: SubGoal, second: SubGoal) -> SubGoal:
    """Fuse two consecutive segments into one coarser subgoal.

    Used when a split cannot be justified by a judgeable stop condition.
    NaVILA is trained on multi-clause R2R instructions, so one longer
    instruction is easier to finish than two segments that each need their own
    termination signal.  The merged segment ends where ``second`` ended, so it
    inherits ``second``'s completion target.
    """
    combined_instruction = f"{first.instruction.rstrip(' .;')}, then {second.instruction.lstrip()}"
    estimated = None
    if first.estimated_distance_m is not None or second.estimated_distance_m is not None:
        estimated = (first.estimated_distance_m or 0.0) + (second.estimated_distance_m or 0.0)

    note = first.note_zh
    if second.note_zh:
        note = f"{note}，然后{second.note_zh}" if note else second.note_zh

    constraints = list(dict.fromkeys(first.constraints + second.constraints))
    criteria = second.completion_criteria or first.completion_criteria
    return SubGoal(
        id=first.id,
        instruction=combined_instruction,
        context=first.context or second.context,
        constraints=constraints,
        completion_criteria=criteria,
        max_steps=min(first.max_steps + second.max_steps, MAX_SUBGOAL_STEPS),
        estimated_distance_m=estimated,
        note_zh=note,
        stop_condition=second.stop_condition,
    )


@dataclass
class MidLevelAction:
    """Structured action shared by VLA backends.

    NaVILA-style language actions use `move_forward`, `turn_left`,
    `turn_right`, and `stop`. Waypoint/trajectory models such as LightNav-0
    use `move` with a small SE(2) displacement expressed in robot-frame
    units: `distance_m` (forward), `lateral_m` (left-positive) and
    `angle_deg` (yaw, left-positive degrees).
    """

    action_type: str  # move_forward | turn_left | turn_right | move | stop
    distance_m: float = 0.0
    angle_deg: float = 0.0
    lateral_m: float = 0.0

    def describe(self) -> str:
        if self.action_type == "move_forward":
            return f"move_forward {self.distance_m:.2f}m"
        if self.action_type in {"turn_left", "turn_right"}:
            return f"{self.action_type} {self.angle_deg:.1f}deg"
        if self.action_type == "move":
            return (
                f"move fwd={self.distance_m:.2f} lat={self.lateral_m:.2f} "
                f"yaw={self.angle_deg:.1f}deg"
            )
        return self.action_type


@dataclass
class RobotState:
    """Structured robot observation passed between execution layers."""

    position: dict[str, Any] = field(default_factory=dict)
    battery: float = 1.0
    mode: str = "idle"
    frame_path: str = ""
    obstacle: bool = False
    remaining_distance_m: float = 0.0
    #: Metres covered since the current subgoal started, measured by the robot.
    traveled_m: float = 0.0


@dataclass
class ExecutionEvent:
    """An event emitted by VLAExecutor to the supervisor."""

    type: EventType
    subgoal_id: str
    message: str = ""
    step_count: int = 0
    state: Optional[RobotState] = None


@dataclass
class ExecutionOutcome:
    """Result of executing one SubGoal."""

    event: ExecutionEvent
    action_trace: list[MidLevelAction] = field(default_factory=list)


@dataclass
class RoutePlan:
    """Planned route decomposed into executable subgoals."""

    mission: str
    subgoals: list[SubGoal] = field(default_factory=list)


@dataclass
class MissionReport:
    """Final summary produced by the demo runtime."""

    mission: str
    subgoals_total: int
    completed_subgoals: int
    events: list[ExecutionEvent] = field(default_factory=list)
    success: bool = False
    route_plan: Optional["RoutePlan"] = None


@dataclass
class StepRecord:
    """Per-step debug record for one supervisor/VLA execution cycle."""

    subgoal_id: str
    instruction: str
    step: int
    timestamp: str
    raw_vla: str = ""
    action_type: str = ""
    action_distance_m: float = 0.0
    action_angle_deg: float = 0.0
    action_lateral_m: float = 0.0
    frame_path: str = ""
    #: Frames the VLA saw this step, and how many of them reached the model.
    history_frames: int = 0
    video_frames: int = 0
    #: Why the subgoal ended, when this step was the last one.
    stop_reason: str = ""
    robot_state: dict[str, Any] = field(default_factory=dict)
    command: Optional[dict[str, Any]] = None
    command_response: Optional[dict[str, Any]] = None


@dataclass
class EpisodeMemoryRecord:
    """A lightweight episode memory entry."""

    mission: str
    subgoal_id: str
    event_type: str
    message: str
    step_count: int
