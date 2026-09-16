"""Run a NaVILA x Agno Supervisor-Executor demo mission."""
from __future__ import annotations

import argparse
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # optional dependency; mock demo does not need it
    def load_dotenv(*_args: object, **_kwargs: object) -> None:
        return None

from navila_agno.contracts import ExecutionEvent
from navila_agno.memory import EpisodeMemory
from navila_agno.robot import Go2HttpRobot, MockRobot
from navila_agno.runtime import MissionRuntime
from navila_agno.supervisor import AgnoSupervisor, MockSupervisor
from navila_agno.tools import set_runtime_robot
from navila_agno.vla import VLAExecutor, make_vla


def _on_event(event: ExecutionEvent) -> None:
    print(f"  [event] {event.type.value} @ {event.subgoal_id}: {event.message}")


def _make_supervisor(kind: str) -> object:
    if kind == "mock":
        return MockSupervisor()

    try:
        supervisor = AgnoSupervisor(
            model_provider=os.getenv("MODEL_PROVIDER", "openai"),
            model_id=os.getenv("MODEL_ID", "gpt-4o-mini"),
            api_key=os.getenv("OPENAI_API_KEY"),
            base_url=os.getenv("OPENAI_BASE_URL"),
            db_file=os.getenv("DB_FILE", "data/supervisor_memory.db"),
        )
    except ImportError:
        if kind == "agno":
            raise
        print("[demo] agno 未安装，使用 MockSupervisor。")
        return MockSupervisor()

    if not supervisor.use_llm:
        print("[demo] AgnoSupervisor 已构建，但未配置模型，使用确定性规划。")
    else:
        print("[demo] 使用 AgnoSupervisor + LLM 规划。")
    return supervisor


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="NaVILA x Agno Supervisor-Executor demo")
    parser.add_argument(
        "mission",
        nargs="?",
        default="去 5 楼会议室 A",
        help="高层导航任务，例如：去 5 楼会议室 A",
    )
    parser.add_argument(
        "--supervisor",
        choices=["auto", "mock", "agno"],
        default="auto",
        help="高层监督者实现",
    )
    parser.add_argument(
        "--vla",
        choices=["mock", "http", "navila", "lightnav"],
        default="mock",
        help="中层 VLA 策略实现",
    )
    parser.add_argument(
        "--navila-endpoint",
        default=os.getenv("NAVILA_ENDPOINT", "http://127.0.0.1:8011"),
        help="NaVILA bridge HTTP 地址，--vla=http/navila 时使用",
    )
    parser.add_argument(
        "--lightnav-endpoint",
        default=os.getenv("LIGHTNAV_ENDPOINT", "ws://127.0.0.1:8050"),
        help="LightNav-0 WebSocket 地址，--vla=lightnav 时使用",
    )
    parser.add_argument(
        "--robot",
        choices=["mock", "go2"],
        default="mock",
        help="机器人接口实现",
    )
    parser.add_argument(
        "--go2-endpoint",
        default=os.getenv("GO2_ENDPOINT", "http://127.0.0.1:8013"),
        help="Go2 bridge HTTP 地址，--robot=go2 时使用",
    )
    parser.add_argument(
        "--go2-dry-run",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Go2 默认只打印运动指令；使用 --no-go2-dry-run 才允许真实控制",
    )
    parser.add_argument(
        "--fail-subgoal",
        default=None,
        help="模拟某个子目标被阻塞，例如 go-to-elevator",
    )
    parser.add_argument(
        "--frame-buffer-size",
        type=int,
        default=8,
        help="NaVILA 最近观测帧窗口大小",
    )
    parser.add_argument(
        "--memory",
        default="data/episode_memory.json",
        help="任务记忆落盘路径",
    )
    args = parser.parse_args()

    if args.robot == "go2":
        robot = Go2HttpRobot(
            endpoint=args.go2_endpoint,
            dry_run=args.go2_dry_run,
        )
    else:
        robot = MockRobot(fail_subgoal_id=args.fail_subgoal)
    set_runtime_robot(robot)

    vla_endpoint = (
        args.lightnav_endpoint if args.vla == "lightnav" else args.navila_endpoint
    )
    policy = make_vla(args.vla, endpoint=vla_endpoint)
    memory = EpisodeMemory(args.memory)
    executor = VLAExecutor(
        policy=policy,
        robot=robot,
        frame_buffer_size=args.frame_buffer_size,
        on_event=_on_event,
    )
    supervisor = _make_supervisor(args.supervisor)
    runtime = MissionRuntime(supervisor=supervisor, executor=executor, memory=memory)

    print(f"任务：{args.mission}")
    print(f"supervisor={type(supervisor).__name__} vla={type(policy).__name__} robot={type(robot).__name__}\n")

    report = runtime.run(args.mission)

    print("\n执行摘要")
    print(f"  成功：{report.success}")
    print(f"  完成子目标：{report.completed_subgoals}/{report.subgoals_total}")
    print(f"  事件数：{len(report.events)}")
    print(f"  记忆文件：{Path(args.memory).resolve()}")


if __name__ == "__main__":
    main()
