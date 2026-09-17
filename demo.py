"""Run a NaVILA x Agno Supervisor-Executor demo mission."""
from __future__ import annotations

import argparse
import os
from dataclasses import asdict
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # optional dependency; mock demo does not need it
    def load_dotenv(*_args: object, **_kwargs: object) -> None:
        return None

from navila_agno.artifacts import RunArtifacts, compose_video
from navila_agno.video import build_timeline, render_video
from navila_agno.contracts import ExecutionEvent, StepRecord
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
    parser.add_argument(
        "--runs-dir",
        default="runs",
        help="运行产物根目录，每次运行会写入 runs/<kind>/<run_id>/",
    )
    parser.add_argument(
        "--run-name",
        default=None,
        help="自定义运行目录后缀，默认使用 mission",
    )
    parser.add_argument(
        "--record-video",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="运行结束后自动把 Go2 相机帧合成为 MP4",
    )
    parser.add_argument(
        "--video-fps",
        type=float,
        default=4.0,
        help="关闭字幕时的视频帧率，真实推理较慢时建议 3~5",
    )
    parser.add_argument(
        "--video-overlay",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="把任务/子目标/当前动作烧进每一帧（PPT 展示用）",
    )
    parser.add_argument(
        "--video-hold-s",
        type=float,
        default=1.2,
        help="每一步画面停留的秒数，越大越慢，方便暂停讲解",
    )
    parser.add_argument(
        "--video-intro-s",
        type=float,
        default=3.0,
        help="片头任务卡停留秒数",
    )
    parser.add_argument(
        "--video-outro-s",
        type=float,
        default=4.0,
        help="片尾结果卡停留秒数",
    )
    args = parser.parse_args()

    step_records: list[dict[str, object]] = []

    def _on_step(record: StepRecord) -> None:
        step_records.append(asdict(record))
        print(
            f"  [step] {record.subgoal_id} step={record.step} "
            f"vla={record.raw_vla[:60]!r} -> {record.action_type}"
        )

    subgoal_meta: dict[str, dict[str, object]] = {}

    def _on_subgoal(subgoal: object) -> None:
        subgoal_meta[subgoal.id] = {
            "index": len(subgoal_meta) + 1,
            "instruction": subgoal.instruction,
            "max_steps": subgoal.max_steps,
            "estimated_distance_m": subgoal.estimated_distance_m,
        }
        print(
            f"  [subgoal] {subgoal.id}: {subgoal.instruction} "
            f"(max_steps={subgoal.max_steps}, est={subgoal.estimated_distance_m})"
        )

    artifacts = RunArtifacts.create(
        kind=args.robot,
        mission=args.mission,
        root=args.runs_dir,
        run_name=args.run_name,
    )

    if args.robot == "go2":
        robot = Go2HttpRobot(
            endpoint=args.go2_endpoint,
            dry_run=args.go2_dry_run,
            frame_dir=artifacts.frames_dir,
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
        on_step=_on_step,
        on_subgoal=_on_subgoal,
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

    recorded_frames = list(getattr(robot, "recorded_frames", []))
    artifacts.frame_paths = recorded_frames
    artifacts.write_steps(step_records)
    video_path = None
    if args.record_video and recorded_frames:
        total_subgoals = len(subgoal_meta)
        for meta in subgoal_meta.values():
            meta["total"] = total_subgoals
        if args.video_overlay:
            timeline = build_timeline(
                recorded_frames,
                step_records,
                subgoal_meta,
                args.mission,
                hold_s=args.video_hold_s,
            )
            video_path = render_video(
                timeline,
                artifacts.video_path,
                artifacts.run_dir / "annotated",
                intro=(
                    args.mission,
                    f"NaVILA × Agno  |  {total_subgoals} 个子目标  |  {len(step_records)} 步",
                    [
                        f"supervisor={type(supervisor).__name__}"
                        f"   vla={type(policy).__name__}"
                        f"   robot={type(robot).__name__}",
                        "Agno 负责高层任务分解，NaVILA 负责逐步视觉语言导航；"
                        "每步画面下方为当前子目标与 VLA 输出。",
                    ],
                ),
                intro_s=args.video_intro_s,
                outro=(
                    "任务结束" if report.success else "任务未完成",
                    args.mission,
                    [
                        f"成功：{'是' if report.success else '否'}"
                        f"   完成子目标：{report.completed_subgoals}/{report.subgoals_total}",
                        f"总步数：{len(step_records)}   事件数：{len(report.events)}",
                    ]
                    + [
                        f"子目标 {sid}：{meta['instruction']}"
                        for sid, meta in subgoal_meta.items()
                    ],
                ),
                outro_s=args.video_outro_s,
            )
        else:
            video_path = compose_video(
                recorded_frames,
                artifacts.video_path,
                fps=args.video_fps,
            )

    artifacts.write_metadata(
        {
            "supervisor": type(supervisor).__name__,
            "vla": type(policy).__name__,
            "robot": type(robot).__name__,
            "success": report.success,
            "subgoals_completed": report.completed_subgoals,
            "subgoals_total": report.subgoals_total,
            "subgoals": (
                [asdict(subgoal) for subgoal in report.route_plan.subgoals]
                if report.route_plan is not None
                else []
            ),
            "steps": step_records,
            "steps_file": str(artifacts.steps_path),
            "frame_order": recorded_frames,
            "video_overlay": bool(args.video_overlay),
            "video_hold_s": args.video_hold_s,
            "annotated_frames": (
                str((artifacts.run_dir / "annotated").resolve())
                if args.video_overlay
                else None
            ),
            "subgoal_meta": subgoal_meta,
            "events": [
                {
                    "type": event.type.value,
                    "subgoal_id": event.subgoal_id,
                    "message": event.message,
                    "step_count": event.step_count,
                }
                for event in report.events
            ],
        }
    )
    print(f"  运行目录：{artifacts.run_dir.resolve()}")
    if video_path is not None:
        print(f"  视频：{video_path.resolve()}")
    elif args.record_video:
        print("  视频：未生成（没有可用的相机帧）")


if __name__ == "__main__":
    main()
