"""Lightweight episode memory used to record demo execution traces."""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from .contracts import EpisodeMemoryRecord, ExecutionEvent, RoutePlan


class EpisodeMemory:
    """In-memory episode log with optional JSON persistence."""

    def __init__(self, path: Optional[str | Path] = None) -> None:
        self.path = Path(path) if path else None
        self.records: list[EpisodeMemoryRecord] = []
        self.current_mission = ""

    def start_mission(self, plan: RoutePlan) -> None:
        self.current_mission = plan.mission
        self.records.clear()

    def record(self, event: ExecutionEvent) -> None:
        self.records.append(
            EpisodeMemoryRecord(
                mission=self.current_mission,
                subgoal_id=event.subgoal_id,
                event_type=event.type.value,
                message=event.message,
                step_count=event.step_count,
            )
        )

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {"mission": self.current_mission, "records": [asdict(r) for r in self.records]},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def summary(self) -> dict:
        completed = sum(1 for r in self.records if r.event_type == "subgoal_completed")
        return {
            "mission": self.current_mission,
            "events": len(self.records),
            "completed": completed,
        }
