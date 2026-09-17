#!/usr/bin/env python3
"""Re-render an annotated PPT video from a finished run directory.

Usage:
    python scripts/render_run_video.py runs/go2/<run_id>
    python scripts/render_run_video.py runs/go2/<run_id> --hold-s 2.0
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from navila_agno.video import build_timeline, render_video  # noqa: E402


def _frame_sort_key(path: str) -> tuple[int, str, int]:
    """Put pre-mission frames first and keep `sg_stepNNNN` in numeric order."""
    name = Path(path).stem
    prefix = 0 if name.startswith("mission") else 1
    head, _, tail = name.rpartition("_step")
    try:
        return (prefix, head, int(tail))
    except ValueError:
        return (prefix, name, 0)


def load_run(run_dir: Path) -> tuple[str, list[dict], list[str], dict]:
    metadata: dict = {}
    run_json = run_dir / "run.json"
    if run_json.is_file():
        metadata = json.loads(run_json.read_text(encoding="utf-8"))

    steps: list[dict] = []
    steps_file = run_dir / "steps.jsonl"
    if steps_file.is_file():
        for line in steps_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                steps.append(json.loads(line))

    frames_dir = run_dir / "frames"
    on_disk = sorted(str(p) for p in frames_dir.glob("*.jpg"))
    on_disk += sorted(str(p) for p in frames_dir.glob("*.png"))

    # Capture order is persisted in run.json; fall back to a sort that keeps
    # the pre-mission snapshot first and step numbers in numeric order.
    recorded = metadata.get("frame_order") or []
    frames: list[str] = []
    seen: set[str] = set()
    for path in recorded:
        resolved = str(Path(str(path)).resolve())
        if Path(resolved).is_file() and resolved not in seen:
            seen.add(resolved)
            frames.append(resolved)
    leftovers = [p for p in on_disk if p not in seen]
    frames.extend(sorted(leftovers, key=_frame_sort_key))
    return metadata.get("mission", run_dir.name), steps, frames, metadata


def main() -> None:
    parser = argparse.ArgumentParser(description="从已有 run 目录重新合成带字幕的视频")
    parser.add_argument("run_dir", help="例如 runs/go2/20260917-122956_向前走到红色消防栓停下")
    parser.add_argument("--hold-s", type=float, default=1.2, help="每步画面停留秒数")
    parser.add_argument("--intro-s", type=float, default=3.0)
    parser.add_argument("--outro-s", type=float, default=4.0)
    parser.add_argument("--output", default=None, help="输出视频路径，默认 <run_dir>/video_annotated.mp4")
    args = parser.parse_args()

    run_dir = Path(args.run_dir).resolve()
    if not run_dir.is_dir():
        raise SystemExit(f"目录不存在：{run_dir}")

    mission, steps, frames, metadata = load_run(run_dir)
    if not frames:
        raise SystemExit(f"{run_dir}/frames 下没有可用图片")

    subgoal_meta: dict[str, dict] = metadata.get("subgoal_meta") or {}
    if not subgoal_meta:
        for record in steps:
            sid = str(record.get("subgoal_id") or "")
            if sid and sid not in subgoal_meta:
                subgoal_meta[sid] = {
                    "index": len(subgoal_meta) + 1,
                    "instruction": record.get("instruction"),
                    "max_steps": max(
                        [
                            int(r.get("step") or 0)
                            for r in steps
                            if str(r.get("subgoal_id")) == sid
                        ]
                        or [0]
                    ),
                    "estimated_distance_m": None,
                }
    for meta in subgoal_meta.values():
        meta["total"] = len(subgoal_meta)

    timeline = build_timeline(
        frames,
        steps,
        subgoal_meta,
        mission,
        hold_s=args.hold_s,
    )
    output = Path(args.output) if args.output else run_dir / "video_annotated.mp4"
    video = render_video(
        timeline,
        output,
        run_dir / "annotated",
        intro=(
            mission,
            f"{len(subgoal_meta)} 个子目标  |  {len(steps)} 步",
            ["由 runs/ 目录离线重渲染，未连接机器人。"],
        ),
        intro_s=args.intro_s,
        outro=(
            "任务结束",
            mission,
            [f"总步数：{len(steps)}"],
        ),
        outro_s=args.outro_s,
    )
    print(f"视频：{video}")
    print(f"带字幕帧：{run_dir / 'annotated'}")


if __name__ == "__main__":
    main()
