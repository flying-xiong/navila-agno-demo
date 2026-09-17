"""Run-scoped artifact management for frames, videos and metadata."""
from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


def slugify(text: str, max_length: int = 32) -> str:
    """Create a safe, stable directory name from a mission string."""
    value = (text or "").strip().lower()
    value = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-")
    return (value[:max_length] or "mission").strip("-")


@dataclass
class RunArtifacts:
    """Filesystem layout for one supervisor/executor run."""

    root: Path
    kind: str
    mission: str
    run_id: str
    run_dir: Path
    frames_dir: Path
    video_path: Path
    metadata_path: Path
    steps_path: Path
    started_at: str
    frame_paths: list[str] = field(default_factory=list)

    @classmethod
    def create(
        cls,
        kind: str,
        mission: str,
        root: str | Path = "runs",
        run_name: str | None = None,
    ) -> "RunArtifacts":
        root_path = Path(root)
        run_id = (
            f"{time.strftime('%Y%m%d-%H%M%S')}_"
            f"{slugify(run_name or mission)}"
        )
        run_dir = root_path / kind / run_id
        frames_dir = run_dir / "frames"
        frames_dir.mkdir(parents=True, exist_ok=True)
        return cls(
            root=root_path,
            kind=kind,
            mission=mission,
            run_id=run_id,
            run_dir=run_dir,
            frames_dir=frames_dir,
            video_path=run_dir / "video.mp4",
            metadata_path=run_dir / "run.json",
            steps_path=run_dir / "steps.jsonl",
            started_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        )

    def add_frame(self, path: str | Path) -> None:
        self.frame_paths.append(str(path))

    def write_metadata(self, payload: dict[str, Any]) -> Path:
        data: dict[str, Any] = {
            "run_id": self.run_id,
            "kind": self.kind,
            "mission": self.mission,
            "started_at": self.started_at,
            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "frames": len(self.frame_paths),
            "video": str(self.video_path) if self.video_path.exists() else None,
        }
        data.update(payload)
        self.metadata_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return self.metadata_path

    def write_steps(self, steps: Iterable[dict[str, Any]]) -> Path:
        with self.steps_path.open("w", encoding="utf-8") as handle:
            for step in steps:
                handle.write(json.dumps(step, ensure_ascii=False) + "\n")
        return self.steps_path


def compose_video(
    frame_paths: Iterable[str | Path],
    output_path: str | Path,
    fps: float = 4.0,
) -> Path | None:
    """Compose an mp4 from JPEG/PNG frames at a fixed frame rate."""
    default = 1.0 / max(fps, 0.1)
    return compose_timeline(
        [(path, default) for path in frame_paths],
        output_path,
    )


def compose_timeline(
    entries: Iterable[tuple[str | Path, float]],
    output_path: str | Path,
) -> Path | None:
    """Compose an mp4 where every image keeps its own on-screen duration.

    A slower per-image duration is what makes the PPT videos pausable: each
    annotated step stays visible long enough to talk over it.
    """
    frames: list[tuple[Path, float]] = []
    for path, duration in entries:
        candidate = Path(path)
        if candidate.is_file():
            frames.append((candidate, max(float(duration), 0.04)))
    if not frames:
        return None

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    concat_file = output.with_suffix(".ffconcat")

    with concat_file.open("w", encoding="utf-8") as handle:
        for frame, duration in frames:
            handle.write(f"file '{frame.resolve().as_posix()}'\n")
            handle.write(f"duration {duration:.4f}\n")
        handle.write(f"file '{frames[-1][0].resolve().as_posix()}'\n")

    command = [
        "ffmpeg",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_file),
        "-vsync",
        "vfr",
        "-pix_fmt",
        "yuv420p",
        "-c:v",
        "libx264",
        str(output),
    ]
    try:
        subprocess.run(
            command,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError as exc:
        concat_file.unlink(missing_ok=True)
        message = exc.stderr.decode("utf-8", errors="replace")[-500:]
        raise RuntimeError(f"ffmpeg 合成视频失败：{message}") from exc
    finally:
        concat_file.unlink(missing_ok=True)
    return output
