"""Turn a finished run into an annotated, slow-paced PPT video."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from .artifacts import compose_timeline
from .contracts import MidLevelAction
from .overlay import (
    DIM_COLOR,
    OK_COLOR,
    WARN_COLOR,
    FrameAnnotation,
    annotate_frame,
    overlay_available,
    render_card,
)

#: Frames showing a stop/finish action stay on screen a little longer.
STOP_HOLD_MULTIPLIER = 1.6


def _as_action(record: Mapping[str, Any]) -> MidLevelAction:
    return MidLevelAction(
        action_type=str(record.get("action_type") or "stop"),
        distance_m=float(record.get("action_distance_m") or 0.0),
        angle_deg=float(record.get("action_angle_deg") or 0.0),
        lateral_m=float(record.get("action_lateral_m") or 0.0),
    )


def _command_text(record: Mapping[str, Any]) -> str:
    command = record.get("command")
    if not isinstance(command, Mapping):
        return ""
    parts = [
        f"vx={float(command.get('vx', 0.0)):.2f}",
        f"vy={float(command.get('vy', 0.0)):.2f}",
        f"vyaw={float(command.get('vyaw', 0.0)):.2f} rad/s",
        f"dur={float(command.get('duration_sec', 0.0)):.2f}s",
    ]
    if command.get("dry_run"):
        parts.append("dry-run")
    response = record.get("command_response")
    if isinstance(response, Mapping) and "rc" in response:
        parts.append(f"rc={response.get('rc')}")
    return "Go2 指令  " + "  ".join(parts)


def _status(record: Mapping[str, Any]) -> tuple[str, tuple[int, int, int]]:
    state = record.get("robot_state")
    if isinstance(state, Mapping) and state.get("obstacle"):
        return "障碍：不可通行", WARN_COLOR
    if record.get("action_type") == "stop":
        return "VLA: 到达，停止", OK_COLOR
    if isinstance(state, Mapping):
        return f"mode={state.get('mode', '-')}", DIM_COLOR
    return "", DIM_COLOR


def annotation_for(
    mission: str,
    record: Mapping[str, Any],
    subgoal_meta: Mapping[str, Mapping[str, Any]],
) -> FrameAnnotation:
    """Build the on-screen annotation for one step record."""
    subgoal_id = str(record.get("subgoal_id") or "")
    meta = subgoal_meta.get(subgoal_id, {})
    index = meta.get("index")
    total = meta.get("total")
    label = f"子目标 {subgoal_id}"
    if isinstance(index, int) and isinstance(total, int):
        label = f"{label}  ({index}/{total})"

    step = int(record.get("step") or 0)
    max_steps = int(meta.get("max_steps") or 0)
    step_label = f"step {step}/{max_steps}" if max_steps else f"step {step}"

    action = _as_action(record)
    remaining = 0.0
    position_text = ""
    state = record.get("robot_state")
    if isinstance(state, Mapping):
        remaining = float(state.get("remaining_distance_m") or 0.0)
        position = state.get("position")
        if isinstance(position, Mapping):
            position_text = (
                f"  |  x={float(position.get('x', 0.0)):.2f} "
                f"y={float(position.get('y', 0.0)):.2f}"
            )

    status_text, status_color = _status(record)
    return FrameAnnotation(
        mission=mission,
        subgoal_label=label,
        subgoal_instruction=str(record.get("instruction") or ""),
        step_label=step_label,
        vla_text=str(record.get("raw_vla") or ""),
        action_text=action.describe(),
        command_text=_command_text(record),
        status_text=status_text,
        status_color=status_color,
        step_progress=(step / max_steps) if max_steps else 0.0,
        remaining_text=f"剩余 {remaining:.2f} m{position_text}" if remaining else position_text,
    )


def build_timeline(
    frame_paths: Iterable[str | Path],
    step_records: Sequence[Mapping[str, Any]],
    subgoal_meta: Mapping[str, Mapping[str, Any]],
    mission: str,
    *,
    hold_s: float = 1.2,
) -> list[tuple[str | Path, float, Optional[FrameAnnotation]]]:
    """Align recorded frames with step records and assign per-frame durations."""
    by_frame: dict[str, Mapping[str, Any]] = {}
    for record in step_records:
        frame = record.get("frame_path")
        if frame:
            by_frame[str(Path(str(frame)).resolve())] = record

    keys = [str(Path(str(frame)).resolve()) for frame in frame_paths]
    # Frames captured outside a step (for example the pre-mission snapshot)
    # borrow the nearest step annotation so no frame renders blank.
    borrowed: list[Optional[str]] = [None] * len(keys)
    previous: Optional[str] = None
    for index, key in enumerate(keys):
        if key in by_frame:
            previous = key
        borrowed[index] = previous
    following: Optional[str] = None
    for index in range(len(keys) - 1, -1, -1):
        if keys[index] in by_frame:
            following = keys[index]
        if borrowed[index] is None:
            borrowed[index] = following

    timeline: list[tuple[str | Path, float, Optional[FrameAnnotation]]] = []
    for frame, key, fallback in zip(frame_paths, keys, borrowed):
        record = by_frame.get(key)
        if record is None and fallback is not None:
            record = by_frame.get(fallback)
        if record is None:
            timeline.append(
                (
                    frame,
                    hold_s,
                    FrameAnnotation(mission=mission, status_text="任务开始"),
                )
            )
            continue
        duration = hold_s
        if record.get("action_type") == "stop":
            duration = hold_s * STOP_HOLD_MULTIPLIER
        timeline.append(
            (frame, duration, annotation_for(mission, record, subgoal_meta))
        )
    return timeline


def render_video(
    timeline: Sequence[tuple[str | Path, float, Optional[FrameAnnotation]]],
    video_path: str | Path,
    annotated_dir: str | Path,
    *,
    intro: Optional[tuple[str, str, Sequence[str]]] = None,
    intro_s: float = 3.0,
    outro: Optional[tuple[str, str, Sequence[str]]] = None,
    outro_s: float = 4.0,
) -> Optional[Path]:
    """Burn annotations into every frame and stitch the mp4."""
    if not overlay_available():
        raise RuntimeError(
            "未找到支持中英文的字体，无法渲染字幕。请安装 fonts-droid-fallback "
            "或 Noto Sans CJK。"
        )

    annotated_dir = Path(annotated_dir)
    annotated_dir.mkdir(parents=True, exist_ok=True)
    for stale in annotated_dir.iterdir():
        if stale.is_file():
            stale.unlink()
    entries: list[tuple[str | Path, float]] = []

    if intro is not None:
        card = render_card(annotated_dir / "0000_intro.jpg", intro[0], intro[1], intro[2])
        entries.append((card, intro_s))

    for index, (frame, duration, annotation) in enumerate(timeline, start=1):
        target = annotated_dir / f"{index:04d}_{Path(str(frame)).name}"
        annotate_frame(frame, target, annotation or FrameAnnotation())
        entries.append((target, duration))

    if outro is not None:
        card = render_card(annotated_dir / "9999_outro.jpg", outro[0], outro[1], outro[2])
        entries.append((card, outro_s))

    return compose_timeline(entries, video_path)
