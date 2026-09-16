"""External tools available to the Agno supervisor.

Deliberately, this module does *not* expose a `run_navila` tool. NaVILA is
called by the mid-level VLAExecutor, not directly by the high-level agent.
"""
from __future__ import annotations

from typing import Optional

from .contracts import RobotState
from .robot import RobotInterface

_ROBOT: Optional[RobotInterface] = None


def set_runtime_robot(robot: RobotInterface) -> None:
    """Inject the current robot into tools that need live status."""
    global _ROBOT
    _ROBOT = robot


def map_route(origin: str, destination: str, avoid_stairs: bool = False) -> dict:
    """Return a normalized route plan with waypoints and floor changes.

    In production, replace this with a map API call and normalize the response
    into the same schema.

    Args:
        origin: Start location, e.g. \"3 楼实验室\".
        destination: Target location, e.g. \"5 楼会议室 A\".
        avoid_stairs: Prefer elevators and accessible routes.

    Returns:
        dict: Route with `waypoints` and `floor_changes`.
    """
    waypoints = [origin, "电梯 C", destination]
    if avoid_stairs:
        waypoints = [origin, "无障碍电梯 C", destination]
    return {
        "origin": origin,
        "destination": destination,
        "waypoints": waypoints,
        "floor_changes": ["3F -> 5F"] if "5" in destination else [],
    }


def robot_status() -> RobotState:
    """Return the live robot state used by the demo runtime."""
    if _ROBOT is None:
        return RobotState(
            position={"x": 0, "floor": "unknown"},
            battery=0.82,
            mode="idle",
        )
    return _ROBOT.observe()


def ask_human(question: str) -> str:
    """Request human clarification or takeover for safety-critical choices."""
    return f"HUMAN_RESPONSE:{question}"
