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

from .contracts import (
    MAX_SUBGOAL_STEPS,
    EventType,
    ExecutionEvent,
    RoutePlan,
    StopCondition,
    SubGoal,
    merge_subgoals,
)
from .tools import ask_human, map_route, robot_status

SKILLS_DIR = Path(__file__).resolve().parent / "skills"

#: Decomposition policy shared by the planner and the re-planner.
DECOMPOSITION_POLICY = (
    "分解原则：**尽量少拆**。NaVILA 是按多子句指令训练的，能直接执行"
    "“穿过大门后右转，走到走廊尽头，在消防栓前停下”这类复合指令。\n"
    "- 只有当你能为切分点写出一个明确的停止条件（一个地标、一个路口、一次转向）"
    "时才拆分；\n"
    "- 转向不要单独成段：把 turn left/right 写进相邻移动段的 instruction，"
    "让 NaVILA 在一次推理里完成转向和前进；\n"
    "- 如果一个段落写不出可判定的停止条件，就不要单独成段，把它和相邻段落"
    "合并成一句更长的英文指令；\n"
    "- 宁可 1~3 个粗粒度子目标，也不要 5~8 个细碎子目标。每个切分点都是一次"
    "可能停不下来的风险。\n"
)

#: Shared with the planner and the re-planner so both produce judgeable rules.
STOP_CONDITION_SPEC = (
    "每个子目标必须给出 stop_condition（执行器判断该子目标何时结束的唯一依据），"
    "它是 stop_condition 对象的机器可判定版本，kind 只能取以下四种之一：\n"
    '  {"kind": "vla_stop_after_distance", "distance_m": 3.0, "description": "中文一句话"}\n'
    '  {"kind": "distance", "distance_m": 5.0, "description": "..."}\n'
    '  {"kind": "steps", "steps": 4, "description": "..."}\n'
    '  {"kind": "vla_stop", "description": "..."}\n'
    "选择规则：\n"
    "- 需要识别地标再停（门口、消防栓、电梯前）：vla_stop_after_distance，"
    "distance_m 填这段的预期前进米数；\n"
    "- 沿走廊直行固定一段、无需识别地标：distance；\n"
    "- 既不需要识别地标、也不用判断转向是否到位时：steps，steps 填不超过 max_steps 的整数；\n"
    "- 目标很近且外观非常明确（几米内的门）：vla_stop。\n"
    "distance_m 必须与 estimated_distance_m 一致；纯转向子目标不要用距离类条件。"
    "注意 vla_stop_after_distance 的 distance_m 同时是下限和上限：走完这个距离还没"
    "识别到目标，执行器会把该段交回重规划，所以估计要偏保守（宁可略大）。"
    "不要用 steps 表示“转向完成”：步数无法证明转到位，这类段落会被执行器"
    "判定为不可判定并并入前一段。"
)

#: Words that mark a change of heading in an instruction.
_TURN_MARKERS = ("turn ", "turning", "rotate", "spin ", "veer ")


def _turn_judged_by_steps(instruction: str, condition: StopCondition) -> bool:
    """A turn whose only evidence is a step count cannot be checked."""
    if condition.kind != "steps":
        return False
    text = instruction.lower()
    return any(marker in text for marker in _TURN_MARKERS)


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
                    stop_condition=StopCondition(
                        kind="vla_stop_after_distance",
                        distance_m=2.0,
                        description="走够 2m 且 VLA 说 stop（电梯门口）",
                    ),
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
                    stop_condition=StopCondition(
                        kind="steps",
                        steps=3,
                        description="执行 3 步后结束（进出电梯不是视觉导航）",
                    ),
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
                    stop_condition=StopCondition(
                        kind="vla_stop_after_distance",
                        distance_m=2.5,
                        description="走够 2.5m 且 VLA 说 stop（会议室门口）",
                    ),
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
                stop_condition=StopCondition(
                    kind="vla_stop_after_distance",
                    distance_m=3.0,
                    description="走够 3m 且 VLA 说 stop",
                ),
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
                    stop_condition=StopCondition(
                        kind="vla_stop_after_distance",
                        distance_m=3.0,
                        description="走够 3m 且 VLA 说 stop（5 楼电梯厅）",
                    ),
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
                DECOMPOSITION_POLICY.replace("**", ""),
                "把任务分解为可由 NaVILA 直接执行的物理移动子目标，粒度宁粗勿细。",
                "禁止生成 verify/check/report/ask/confirm 等非移动子目标。",
                "不要生成单独的“到达验证”步骤；最后一个子目标用 walk to ... and stop 表达到达。",
                "如果只需要转向，instruction 写成 turn left/right ... and then continue。",
                "estimated_distance_m 表示该子目标预期前进的米数；纯转向子目标设为 0.5~1.0，不要设为 0。",
                "每个子目标都必须给出 stop_condition，且必须是可判定的四类之一。",
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
            + DECOMPOSITION_POLICY
            + "每个子目标必须是物理移动段，禁止 verify/check/report/ask/confirm 类步骤。"
            + "最后一个子目标直接用 walk to ... and stop 表达到达，不要额外生成验证步骤。"
            + "每个元素包含 id, instruction（必须是英文，因为直接送给 NaVILA）、"
            + "note_zh（中文一句说明，用于 PPT 字幕）、"
            + "context, constraints, completion_criteria, "
            + "estimated_distance_m（预期前进米数，纯转向设 0.5~1.0）, "
            + "max_steps（不超过 30）。\n"
            + STOP_CONDITION_SPEC
            + "\n任务："
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
            + DECOMPOSITION_POLICY
            + "替代子目标同样必须是物理移动段，禁止 verify/check/report/ask/confirm 类步骤。"
            + "字段与首次规划完全一致：id, instruction（必须是英文，因为直接送给 NaVILA）、"
            + "note_zh（中文说明）, completion_criteria, estimated_distance_m, max_steps, "
            + "stop_condition。\n"
            + STOP_CONDITION_SPEC
            + f"\n如果无法给出移动子目标，返回空数组。\n事件：{event.type.value} {event.message}\n原任务：{mission}"
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
        #: Indices of segments whose split the model could not justify.
        unjustified: set[int] = set()
        for item in data:
            if not isinstance(item, dict) or "instruction" not in item:
                continue
            try:
                max_steps = int(item.get("max_steps", 12))
            except (TypeError, ValueError):
                max_steps = 12
            max_steps = max(1, min(max_steps, MAX_SUBGOAL_STEPS))
            constraints_raw = item.get("constraints", [])
            if isinstance(constraints_raw, str):
                constraints = [constraints_raw]
            else:
                constraints = [str(x) for x in constraints_raw]
            try:
                estimated = float(item["estimated_distance_m"])
            except (KeyError, TypeError, ValueError):
                estimated = None
            instruction = str(item["instruction"])
            if any("\u4e00" <= char <= "\u9fff" for char in instruction):
                print(
                    "[AgnoSupervisor] 子目标 instruction 含中文，"
                    f"NaVILA 需要英文指令：{instruction[:40]}"
                )
            stop_condition = StopCondition.from_value(
                item.get("stop_condition"), estimated
            )
            if stop_condition is None:
                unjustified.add(len(subgoals))
                stop_condition = StopCondition()
            elif _turn_judged_by_steps(instruction, stop_condition):
                unjustified.add(len(subgoals))
                print(
                    f"[AgnoSupervisor] 子目标 {item.get('id')} 用步数表示转向完成，"
                    "步数无法证明转到位，已并入前一段"
                )
            subgoals.append(
                SubGoal(
                    id=str(item.get("id", f"sg-{len(subgoals)}")),
                    instruction=instruction,
                    context=str(item.get("context", "")),
                    constraints=constraints,
                    completion_criteria=str(item.get("completion_criteria", "")),
                    estimated_distance_m=estimated,
                    max_steps=max_steps,
                    note_zh=str(item.get("note_zh", "")),
                    stop_condition=stop_condition,
                )
            )
        return _merge_unjustified(subgoals, unjustified) or None


def _merge_unjustified(
    subgoals: list[SubGoal], unjustified: set[int]
) -> list[SubGoal]:
    """Fuse splits the model could not justify with a judgeable stop condition.

    A subgoal without a valid ``stop_condition`` is one the executor cannot
    tell apart from "still busy". Rather than inventing a threshold for it, we
    drop the split point and let NaVILA handle one longer instruction, which is
    what it was trained on.  Consecutive unjustified segments keep fusing, so a
    plan that is split too finely collapses into one coarse subgoal.
    """
    merged: list[SubGoal] = []
    for index, subgoal in enumerate(subgoals):
        if index not in unjustified:
            merged.append(subgoal)
            continue

        if merged:
            previous = merged.pop()
            combined = merge_subgoals(previous, subgoal)
            combined.stop_condition = StopCondition.default_for(
                combined.instruction, combined.estimated_distance_m
            )
            print(
                f"[AgnoSupervisor] 子目标 {subgoal.id} 的切分点不可判定，"
                f"已并入前一段（合并后共 {len(merged) + 1} 段）"
            )
            merged.append(combined)
        else:
            # Nothing to merge into; fall back to a derived condition.
            subgoal.stop_condition = StopCondition.default_for(
                subgoal.instruction, subgoal.estimated_distance_m
            )
            merged.append(subgoal)
    return merged


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
