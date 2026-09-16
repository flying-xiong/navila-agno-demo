"""High-level supervisor implementations.

`MockSupervisor` is dependency-free and deterministic, so the demo can run
without an LLM. `AgnoSupervisor` wraps an Agno Agent with skills, memory and
tools, and falls back to the deterministic planner when no model is configured.
"""
from __future__ import annotations

import json
import os
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from .contracts import EventType, ExecutionEvent, RoutePlan, SubGoal
from .tools import ask_human, map_route, robot_status

SKILLS_DIR = Path(__file__).resolve().parent / "skills"


class Supervisor(ABC):
    @abstractmethod
    def plan(self, mission: str) -> RoutePlan:
        """Decompose a mission into NaVILA-friendly subgoals."""

    @abstractmethod
    def replan(self, mission: str, event: ExecutionEvent) -> Optional[list[SubGoal]]:
        """Return replacement subgoals, or None to stop and ask a human."""


class MockSupervisor(Supervisor):
    """Deterministic planner used for offline demos and tests."""

    def __init__(self) -> None:
        self._replanned_elevator = False

    def plan(self, mission: str) -> RoutePlan:
        self._replanned_elevator = False
        return RoutePlan(mission=mission, subgoals=self._build_plan(mission))

    def _build_plan(self, mission: str) -> list[SubGoal]:
        if "5" in mission and "会议" in mission:
            return [
                SubGoal(
                    id="go-to-elevator",
                    instruction="走到电梯 C 门口并停下",
                    context="当前在 3 楼走廊，靠右行驶",
                    constraints=["避开楼梯间", "遇到施工区停下"],
                    completion_criteria="距电梯门口小于 0.5 米且正对电梯门",
                    max_steps=8,
                    estimated_distance_m=2.0,
                    note_zh="沿走廊走到电梯 C 门口",
                ),
                SubGoal(
                    id="take-elevator",
                    instruction="进入电梯并选择 5 楼",
                    context="电梯已到达",
                    constraints=["等待门完全打开"],
                    completion_criteria="电梯到达 5 楼且门已打开",
                    max_steps=3,
                    estimated_distance_m=0.2,
                    note_zh="进入电梯并选择 5 楼",
                ),
                SubGoal(
                    id="go-to-meeting-room",
                    instruction="沿走廊走到 502 会议室并停下",
                    context="当前在 5 楼电梯厅",
                    constraints=["优先右侧通行"],
                    completion_criteria="到达 502 会议室门口",
                    max_steps=10,
                    estimated_distance_m=2.5,
                    note_zh="沿走廊走到 502 会议室",
                ),
            ]

        return [
            SubGoal(
                id="navigate",
                instruction=mission,
                context="单段导航任务",
                max_steps=10,
                estimated_distance_m=3.0,
                note_zh="执行单段导航任务",
            )
        ]

    def replan(self, mission: str, event: ExecutionEvent) -> Optional[list[SubGoal]]:
        if event.type not in {EventType.BLOCKED, EventType.SUBGOAL_FAILED}:
            return None

        if event.subgoal_id == "go-to-elevator" and not self._replanned_elevator:
            self._replanned_elevator = True
            return [
                SubGoal(
                    id="reroute-via-stairs",
                    instruction="绕行消防楼梯到 5 楼电梯厅",
                    context="主路径被阻塞",
                    constraints=["走消防楼梯", "保持低速"],
                    completion_criteria="到达 5 楼电梯厅",
                    max_steps=12,
                    estimated_distance_m=3.0,
                    note_zh="绕行消防楼梯到 5 楼电梯厅",
                )
            ]
        return None


class AgnoSupervisor(Supervisor):
    """Agno-backed supervisor with a deterministic fallback planner."""

    def __init__(
        self,
        model_provider: str = "openai",
        model_id: str = "gpt-4o-mini",
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        db_file: str = "data/supervisor_memory.db",
    ) -> None:
        try:
            from agno.agent import Agent
            from agno.db.sqlite import SqliteDb
            from agno.models.openai import OpenAIChat, OpenAILike
            from agno.skills import LocalSkills, Skills
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise ImportError(
                "AgnoSupervisor requires `agno`. Install with `pip install -r requirements.txt`."
            ) from exc

        self.model_provider = model_provider.lower()
        self.model_id = model_id
        self.base_url = base_url
        self.use_llm = bool(
            api_key
            or base_url
            or self.model_provider == "ollama"
        )
        self._fallback = MockSupervisor()

        if self.model_provider == "openai_like":
            model = OpenAILike(
                id=model_id,
                api_key=api_key or "not-needed",
                base_url=base_url or "http://localhost:11434/v1",
            )
        elif self.model_provider == "ollama":
            from agno.models.ollama import Ollama

            model = Ollama(id=model_id)
        else:
            model = OpenAIChat(id=model_id)

        db = SqliteDb(db_file=db_file)
        self.agent = Agent(
            name="navila-agno-supervisor",
            model=model,
            db=db,
            skills=Skills(loaders=[LocalSkills(str(SKILLS_DIR))]),
            tools=[map_route, robot_status, ask_human],
            instructions=[
                "你是 NaVILA 导航系统的高层监督者。",
                "把任务分解为短、具体、可验证的子目标，供 NaVILA 执行。",
                "子目标不要包含长期历史，只保留当前段所需的上下文和约束。",
                "当地图、机器人状态或异常事件可用时，优先调用对应工具。",
                "遇到失败或阻塞时给出替换子目标；无法决策时调用 ask_human。",
            ],
            markdown=True,
        )

    def plan(self, mission: str) -> RoutePlan:
        if not self.use_llm:
            return self._fallback.plan(mission)

        prompt = (
            "你是导航监督者。请把任务分解成 NaVILA 可执行的子目标，只返回 JSON 数组。"
            "每个元素包含 id, instruction（英文，直接给 NaVILA）、note_zh（中文一句说明，用于 PPT 字幕）、"
            "context, constraints, completion_criteria, estimated_distance_m, max_steps。\n任务："
            + mission
        )
        try:
            raw = self.agent.run(prompt).content
            subgoals = self._parse_subgoals(raw)
            if subgoals:
                return RoutePlan(mission=mission, subgoals=subgoals)
        except Exception as exc:  # noqa: BLE001 - fallback is expected
            print(f"[AgnoSupervisor] 模型规划失败，使用确定性规划：{type(exc).__name__}: {exc}")
        return self._fallback.plan(mission)

    def replan(self, mission: str, event: ExecutionEvent) -> Optional[list[SubGoal]]:
        if not self.use_llm:
            return self._fallback.replan(mission, event)

        prompt = (
            "导航执行发生异常。请给出替代子目标 JSON 数组；如果无法自主决策，"
            f"返回空数组。\n事件：{event.type.value} {event.message}\n原任务：{mission}"
        )
        try:
            raw = self.agent.run(prompt).content
            subgoals = self._parse_subgoals(raw)
            if subgoals:
                return subgoals
        except Exception as exc:  # noqa: BLE001 - fallback is expected
            print(f"[AgnoSupervisor] 重规划失败，使用确定性重规划：{type(exc).__name__}: {exc}")
        return self._fallback.replan(mission, event)

    @staticmethod
    def _parse_subgoals(raw: str) -> Optional[list[SubGoal]]:
        match = re.search(r"\[.*\]", raw, flags=re.S)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None

        subgoals: list[SubGoal] = []
        for item in data:
            if not isinstance(item, dict) or "instruction" not in item:
                continue
            constraints_raw = item.get("constraints", [])
            if isinstance(constraints_raw, str):
                constraints = [constraints_raw]
            else:
                constraints = [str(x) for x in constraints_raw]
            subgoals.append(
                SubGoal(
                    id=str(item.get("id", f"sg-{len(subgoals)}")),
                    instruction=str(item["instruction"]),
                    context=str(item.get("context", "")),
                    constraints=constraints,
                    completion_criteria=str(item.get("completion_criteria", "")),
                    estimated_distance_m=item.get("estimated_distance_m"),
                    max_steps=int(item.get("max_steps", 12)),
                    note_zh=str(item.get("note_zh", "")),
                )
            )
        return subgoals or None


def make_supervisor(
    kind: str,
    model_provider: str = "openai",
    model_id: str = "gpt-4o-mini",
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    db_file: str = "data/supervisor_memory.db",
) -> Supervisor:
    """Create a supervisor by kind: `mock` or `agno`."""
    if kind == "mock":
        return MockSupervisor()
    return AgnoSupervisor(
        model_provider=model_provider,
        model_id=model_id,
        api_key=api_key,
        base_url=base_url,
        db_file=db_file,
    )
