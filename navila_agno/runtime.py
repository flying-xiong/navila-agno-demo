"""Mission runtime that connects the supervisor and VLAExecutor."""
from __future__ import annotations

from collections import deque

from .contracts import EventType, MissionReport, RoutePlan
from .memory import EpisodeMemory
from .supervisor import Supervisor
from .vla import VLAExecutor


class MissionRuntime:
    def __init__(
        self,
        supervisor: Supervisor,
        executor: VLAExecutor,
        memory: EpisodeMemory | None = None,
        max_replans: int = 2,
    ) -> None:
        self.supervisor = supervisor
        self.executor = executor
        self.memory = memory or EpisodeMemory()
        self.max_replans = max_replans

    def run(self, mission: str) -> MissionReport:
        plan: RoutePlan = self.supervisor.plan(mission)
        self.memory.start_mission(plan)

        queue: deque = deque(plan.subgoals)
        completed = 0
        events = []
        replans = 0
        aborted = False

        while queue:
            subgoal = queue.popleft()
            outcome = self.executor.execute(subgoal)
            self.memory.record(outcome.event)
            events.append(outcome.event)

            if outcome.event.type == EventType.SUBGOAL_COMPLETED:
                completed += 1
                continue

            replacement = self.supervisor.replan(mission, outcome.event)
            if replacement and replans < self.max_replans:
                replans += 1
                queue.extendleft(reversed(replacement))
                continue

            aborted = True
            break

        self.memory.save()
        return MissionReport(
            mission=mission,
            subgoals_total=len(plan.subgoals),
            completed_subgoals=completed,
            events=events,
            success=not aborted,
            route_plan=plan,
        )
