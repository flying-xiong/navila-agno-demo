"""Generate a PPT-ready demo video: NaVILA simulator replay + Agno plan overlay.

Workflow
--------
1. Pick a successful NaVILA VLN-CE episode (default: R2R val_unseen episode 86).
2. Ask the Agno supervisor (DeepSeek-V4-Pro) to decompose the episode
   instruction into NaVILA-friendly subgoals, including a Chinese PPT note.
3. Compose a 1920x1080 video with ffmpeg:
   - left: NaVILA first-person/top-down simulator replay;
   - right: Agno high-level plan panel;
   - top/bottom: mission and synchronized subgoal subtitles.

Run from the project root:
    python ppt_demo.py
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import shutil
import subprocess
from dataclasses import asdict
from pathlib import Path

from dotenv import load_dotenv

from navila_agno.contracts import RoutePlan, SubGoal
from navila_agno.supervisor import AgnoSupervisor

PROJECT_DIR = Path(__file__).resolve().parent
FONT = str(PROJECT_DIR / "assets/fonts/NotoSansCJKsc-Regular.otf")


def _load_instruction(navila_root: Path, split: str, episode: str) -> str:
    dataset = (
        navila_root
        / "evaluation/data/datasets/R2R_VLNCE_v1-3_preprocessed"
        / split
        / f"{split}.json.gz"
    )
    if not dataset.exists():
        raise FileNotFoundError(f"R2R 数据不存在：{dataset}")
    with gzip.open(dataset, "rt", encoding="utf-8") as f:
        data = json.load(f)
    for ep in data["episodes"]:
        if str(ep["episode_id"]) == str(episode):
            return ep["instruction"]["instruction_text"]
    raise ValueError(f"在 {split} 中找不到 episode={episode}")


def _find_video(
    navila_root: Path, split: str, run_name: str, episode: str
) -> Path:
    video_dir = navila_root / "evaluation/eval_out/ckpt/VLN-CE-v1" / split / run_name / "videos"
    if not video_dir.exists():
        raise FileNotFoundError(f"NaVILA 视频目录不存在：{video_dir}")
    matches = sorted(video_dir.glob(f"episode={episode}-ckpt=0-*.mp4"))
    if not matches:
        raise FileNotFoundError(f"找不到 episode={episode} 的视频：{video_dir}")
    return matches[-1]


def _load_plan(plan_file: Path, mission: str, supervisor: AgnoSupervisor) -> RoutePlan:
    if plan_file.exists():
        raw = json.loads(plan_file.read_text(encoding="utf-8"))
        subgoals = [
            SubGoal(
                id=item.get("id", f"sg-{i}"),
                instruction=item.get("instruction", ""),
                context=item.get("context", ""),
                constraints=item.get("constraints", []),
                completion_criteria=item.get("completion_criteria", ""),
                estimated_distance_m=item.get("estimated_distance_m"),
                max_steps=int(item.get("max_steps", 12)),
                note_zh=item.get("note_zh", ""),
            )
            for i, item in enumerate(raw.get("subgoals", []))
        ]
        if subgoals:
            print(f"[ppt-demo] 复用已缓存的 Agno 规划：{plan_file}")
            return RoutePlan(mission=mission, subgoals=subgoals)

    print("[ppt-demo] 正在请求 DeepSeek-V4-Pro 规划（可能约 1 分钟）……")
    plan = supervisor.plan(mission)
    plan_file.parent.mkdir(parents=True, exist_ok=True)
    plan_file.write_text(
        json.dumps(
            {"mission": mission, "subgoals": [asdict(sg) for sg in plan.subgoals]},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return plan


def _probe_duration(video: Path) -> float:
    out = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nk=1:nw=1",
            str(video),
        ],
        text=True,
    )
    return float(out.strip())


def _wrap_text(text: str, width: int) -> str:
    if not text:
        return ""
    paragraphs = text.split("\n")
    wrapped: list[str] = []
    for para in paragraphs:
        if not para.strip():
            wrapped.append("")
            continue
        words = para.split()
        if len(words) > 1:
            line = ""
            for word in words:
                candidate = f"{line} {word}".strip()
                if len(candidate) <= width or not line:
                    line = candidate
                else:
                    wrapped.append(line)
                    line = word
            wrapped.append(line)
        else:
            wrapped.extend(para[i : i + width] for i in range(0, len(para), width))
    return "\n".join(wrapped)


def _write_text(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _segments(subgoals: list[SubGoal], duration: float) -> list[tuple[int, float, float]]:
    if not subgoals:
        return [(0, 0.0, duration)]
    weights = [max(float(sg.estimated_distance_m or 1.0), 0.4) for sg in subgoals]
    total = sum(weights)
    starts = [0.0]
    acc = 0.0
    for weight in weights[:-1]:
        acc += duration * weight / total
        starts.append(acc)
    starts.append(duration)
    return [(i, starts[i], starts[i + 1]) for i in range(len(subgoals))]


def _panel_lines(plan: RoutePlan) -> str:
    lines = ["Agno 高层规划 | DeepSeek-V4-Pro", ""]
    lines += _wrap_text("任务：" + plan.mission, 24).split("\n")
    lines.append("")
    lines.append("子目标分解：")
    for i, sg in enumerate(plan.subgoals, 1):
        note = sg.note_zh or sg.instruction
        lines.extend(_wrap_text(f"{i}. {note}", 24).split("\n"))
    return "\n".join(lines)


def _build_filters(
    src: Path,
    plan: RoutePlan,
    duration: float,
    speed: float,
    workdir: Path,
    font: str,
) -> str:
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    segs = _segments(plan.subgoals, duration)

    label_file = _write_text(workdir / "label.txt", "NaVILA 中层执行 | Habitat 模拟器回放 | 事件回传 Agno")
    top_file = _write_text(
        workdir / "top.txt",
        "NaVILA + Agno 协作导航 Demo\n任务：" + _wrap_text(plan.mission, 66),
    )
    seg_files: list[Path] = []
    for idx, _, _ in segs:
        sg = plan.subgoals[idx]
        note = sg.note_zh or sg.instruction
        seg_files.append(
            _write_text(
                workdir / f"seg{idx}.txt",
                f"Agno → NaVILA：子目标 {idx + 1}/{len(plan.subgoals)}\n{_wrap_text(note, 44)}\nNaVILA → Agno：视觉帧 + running 状态",
            )
        )

    filters = [
        f"[0:v]setpts={speed}*PTS,scale=1280:-2,pad=1280:720:0:(oh-ih)/2:color=black,"
        f"drawtext=fontfile={font}:textfile={label_file}:x=40:y=650:fontsize=30:"
        f"fontcolor=white:box=1:boxcolor=black@0.55:boxborderw=12[lv]",
        f"color=c=0x0f172a:s=640x720:r=10:d={duration}[panel0]",
    ]

    panel_in = "panel0"
    title_file = _write_text(workdir / "panel_title.txt", "Agno 高层规划 | DeepSeek-V4-Pro")
    body_file = _write_text(
        workdir / "panel_body.txt",
        "\n".join(_panel_lines(plan).split("\n")[1:]),
    )
    filters.append(
        f"[{panel_in}]drawtext=fontfile={font}:textfile={title_file}:x=36:y=36:"
        f"fontsize=32:fontcolor=0x9cc7ff[panel1]"
    )
    filters.append(
        f"[panel1]drawtext=fontfile={font}:textfile={body_file}:x=36:y=100:"
        f"fontsize=23:fontcolor=white:line_spacing=8[panel]"
    )

    filters.append("[lv][panel]hstack[middle]")
    filters.append(f"color=c=0x0b1120:s=1920x120:r=10:d={duration}[top0]")
    filters.append(
        f"[top0]drawtext=fontfile={font}:textfile={top_file}:x=48:y=14:"
        f"fontsize=32:fontcolor=white:line_spacing=6[top]"
    )

    filters.append(f"color=c=0x111827:s=1920x240:r=10:d={duration}[bottom0]")
    bottom_in = "bottom0"
    for idx, start, end in segs:
        bottom_out = "bottom" if idx == len(segs) - 1 else f"bottom{idx + 1}"
        filters.append(
            f"[{bottom_in}]drawtext=fontfile={font}:textfile={seg_files[idx]}:x=60:y=16:"
            f"fontsize=38:fontcolor=white:line_spacing=8:"
            f"enable='between(t,{start},{end})'[{bottom_out}]"
        )
        bottom_in = bottom_out

    filters.append("[top][middle][bottom]vstack=3,format=yuv420p[out]")
    return ";".join(filters)


def _run_ffmpeg(cmd: list[str]) -> None:
    print("[ppt-demo] 正在合成视频……")
    subprocess.run(cmd, check=True)


def make_ppt_video(
    navila_root: Path,
    episode: str,
    split: str,
    run_name: str,
    output_dir: Path,
    supervisor: AgnoSupervisor,
    cache_plan: bool = True,
    speed: float = 2.5,
) -> dict[str, Path]:
    mission = _load_instruction(navila_root, split, episode)
    video = _find_video(navila_root, split, run_name, episode)
    print(f"[ppt-demo] 任务指令：{mission}")
    print(f"[ppt-demo] NaVILA 视频：{video}")

    output_dir.mkdir(parents=True, exist_ok=True)
    plan_file = output_dir / "plan.json"
    plan = _load_plan(plan_file, mission, supervisor) if cache_plan else supervisor.plan(mission)
    if not cache_plan:
        plan_file.write_text(
            json.dumps(
                {"mission": mission, "subgoals": [asdict(sg) for sg in plan.subgoals]},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    source_duration = _probe_duration(video)
    duration = source_duration * speed
    workdir = output_dir / "text"
    workdir.mkdir(parents=True, exist_ok=True)
    filter_complex = _build_filters(video, plan, duration, speed, workdir, FONT)

    out_video = output_dir / "ppt_demo.mp4"
    _run_ffmpeg(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-i",
            str(video),
            "-filter_complex",
            filter_complex,
            "-map",
            "[out]",
            "-r",
            "10",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(out_video),
        ]
    )

    cover = output_dir / "cover.jpg"
    cover_text = _write_text(workdir / "cover.txt", "NaVILA + Agno\nAgentic Navigation Demo")
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-i",
            str(video),
            "-frames:v",
            "1",
            "-vf",
            f"scale=1280:720,drawtext=fontfile={FONT}:textfile={cover_text}:"
            "x=(w-text_w)/2:y=(h-text_h)/2:fontsize=60:fontcolor=white:"
            "box=1:boxcolor=black@0.7:boxborderw=24:line_spacing=10",
            "-q:v",
            "3",
            str(cover),
        ],
        check=True,
    )

    print(f"[ppt-demo] 输出视频：{out_video}")
    print(f"[ppt-demo] 封面图：{cover}")
    print(f"[ppt-demo] 规划 JSON：{plan_file}")
    return {"video": out_video, "cover": cover, "plan": plan_file}


def main() -> None:
    load_dotenv(PROJECT_DIR / ".env")
    parser = argparse.ArgumentParser(description="生成 NaVILA x Agno PPT 演示视频")
    parser.add_argument(
        "--navila-root",
        default=os.getenv("NAVILA_ROOT", str(PROJECT_DIR.parent / "NaVILA")),
    )
    parser.add_argument("--episode", default="86")
    parser.add_argument("--split", default="val_unseen")
    parser.add_argument("--run-name", default="submodular_v3")
    parser.add_argument("--output", default=str(PROJECT_DIR / "output"))
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--speed", type=float, default=2.5, help="慢放倍率，让字幕更易读")
    args = parser.parse_args()

    supervisor = AgnoSupervisor(
        model_provider=os.getenv("MODEL_PROVIDER", "openai_like"),
        model_id=os.getenv("MODEL_ID", "deepseek-v4-pro"),
        api_key=os.getenv("OPENAI_API_KEY"),
        base_url=os.getenv("OPENAI_BASE_URL"),
        db_file=os.getenv("DB_FILE", str(PROJECT_DIR / "data/supervisor_memory.db")),
    )
    make_ppt_video(
        navila_root=Path(args.navila_root),
        episode=args.episode,
        split=args.split,
        run_name=args.run_name,
        output_dir=Path(args.output),
        supervisor=supervisor,
        cache_plan=not args.no_cache,
        speed=args.speed,
    )


if __name__ == "__main__":
    main()
