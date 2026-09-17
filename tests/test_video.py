"""Tests for the annotated PPT video pipeline."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

try:
    from PIL import Image, ImageDraw
except ImportError:  # pragma: no cover - Pillow is optional at runtime
    Image = None

if Image is not None:
    from navila_agno.overlay import (
        FrameAnnotation,
        MixedFont,
        annotate_frame,
        find_cjk_font_path,
        find_latin_font_path,
    )
    from navila_agno.video import build_timeline, render_video


def _make_frame(path: Path, size: tuple[int, int] = (320, 180)) -> str:
    Image.new("RGB", size, (30, 60, 90)).save(path, quality=90)
    return str(path)


@unittest.skipIf(Image is None, "Pillow 未安装")
class OverlayFontTests(unittest.TestCase):
    def test_mixed_font_covers_latin_and_cjk(self) -> None:
        self.assertIsNotNone(find_latin_font_path())
        self.assertIsNotNone(find_cjk_font_path())
        font = MixedFont(24)
        self.assertGreater(font.advance("step 2/4"), 0)

        def ink(char: str) -> bytes:
            canvas = Image.new("L", (96, 96), 0)
            font.draw(ImageDraw.Draw(canvas), (12, 72), char, 255)
            return canvas.tobytes()

        # U+FFFF is a noncharacter, so it always renders as the missing-glyph
        # box.  Latin letters and digits must not look like that box, which is
        # exactly the "all English turned into squares" bug.
        notdef = ink("\uffff")
        for char in ("A", "7", "z", "中", "，"):
            with self.subTest(char=char):
                self.assertNotEqual(ink(char), notdef)

    def test_annotation_banner_is_drawn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "frame.jpg"
            _make_frame(source)
            raw = Image.open(source).convert("RGB")
            raw_top = raw.getpixel((5, 5))
            raw_bottom = raw.getpixel((5, raw.height - 40))

            target = Path(tmp) / "annotated.jpg"
            annotate_frame(
                source,
                target,
                FrameAnnotation(
                    mission="测试任务",
                    subgoal_label="子目标 1  (1/1)",
                    subgoal_instruction="walk forward and stop",
                    step_label="step 2/4",
                    vla_text="The next action is move forward 75 cm.",
                    action_text="move_forward 0.75m",
                    command_text="Go2 指令  vx=0.30",
                    step_progress=0.5,
                ),
            )
            self.assertTrue(target.is_file())
            painted = Image.open(target).convert("RGB")
            # Both the top mission bar and the bottom panel must be painted.
            self.assertNotEqual(painted.getpixel((5, 5)), raw_top)
            self.assertNotEqual(painted.getpixel((5, painted.height - 40)), raw_bottom)


@unittest.skipIf(Image is None, "Pillow 未安装")
class TimelineTests(unittest.TestCase):
    def test_frames_align_with_steps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = _make_frame(root / "sg-1_step0000.jpg")
            second = _make_frame(root / "sg-1_step0001.jpg")
            third = _make_frame(root / "sg-1_step0002.jpg")
            steps = [
                {
                    "subgoal_id": "sg-1",
                    "instruction": "go forward",
                    "step": 1,
                    "frame_path": first,
                    "raw_vla": "move forward 75 cm",
                    "action_type": "move_forward",
                    "action_distance_m": 0.75,
                    "robot_state": {"position": {"x": 1.0, "y": 2.0}, "remaining_distance_m": 3.0},
                    "command": {"vx": 0.3, "vy": 0.0, "vyaw": 0.0, "duration_sec": 2.5},
                },
                {
                    "subgoal_id": "sg-1",
                    "instruction": "go forward",
                    "step": 2,
                    "frame_path": second,
                    "raw_vla": "stop",
                    "action_type": "stop",
                    "robot_state": {"position": {"x": 1.7, "y": 2.0}, "remaining_distance_m": 0.0},
                },
            ]
            meta = {"sg-1": {"index": 1, "total": 1, "max_steps": 4, "instruction": "go forward"}}

            # `third` was captured outside a step and must borrow an annotation.
            timeline = build_timeline([first, second, third], steps, meta, "去会议室", hold_s=1.0)
            self.assertEqual(len(timeline), 3)
            self.assertEqual(timeline[0][2].subgoal_label, "子目标 sg-1  (1/1)")
            self.assertEqual(timeline[0][2].vla_text, "move forward 75 cm")
            self.assertIn("move_forward", timeline[0][2].action_text)
            self.assertGreater(timeline[1][1], timeline[0][1])  # stop frame held longer
            self.assertIsNotNone(timeline[2][2])

    @unittest.skipIf(shutil.which("ffmpeg") is None, "ffmpeg 未安装")
    def test_render_video_writes_mp4(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            frame = _make_frame(root / "sg-1_step0000.jpg")
            steps = [
                {
                    "subgoal_id": "sg-1",
                    "instruction": "go forward",
                    "step": 1,
                    "frame_path": frame,
                    "raw_vla": "move forward 75 cm",
                    "action_type": "move_forward",
                    "robot_state": {"position": {"x": 0.0, "y": 0.0}, "remaining_distance_m": 1.0},
                }
            ]
            meta = {"sg-1": {"index": 1, "total": 1, "max_steps": 1, "instruction": "go forward"}}
            timeline = build_timeline([frame], steps, meta, "去会议室", hold_s=0.5)
            video = render_video(
                timeline,
                root / "video.mp4",
                root / "annotated",
                intro=("去会议室", "1 个子目标", ["test"]),
                intro_s=0.5,
                outro=("任务结束", "去会议室", ["总步数：1"]),
                outro_s=0.5,
            )
            self.assertIsNotNone(video)
            self.assertTrue(video.is_file())
            self.assertGreater(video.stat().st_size, 1000)
            self.assertTrue((root / "annotated" / "0000_intro.jpg").is_file())


if __name__ == "__main__":
    unittest.main()
