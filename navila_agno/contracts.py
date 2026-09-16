"""Shared contracts between the supervisor, executor, VLA and robot layers."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


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


@dataclass
class EpisodeMemoryRecord:
    """A lightweight episode memory entry."""

    mission: str
    subgoal_id: str
    event_type: str
    message: str
    step_count: int
