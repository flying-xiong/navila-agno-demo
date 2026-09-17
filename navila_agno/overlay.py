"""Frame annotation helpers for run videos.

The demo videos are used in slide decks, so every frame carries the mission,
the active subgoal, the VLA sentence and the mapped robot command.

Text is burned into the pixels with Pillow instead of ffmpeg ``drawtext``.
That matters here because the usual CJK fallback font on this machine
(Droid Sans Fallback) contains *no* Latin or digit glyphs, which is what turns
every English character into a box.  :class:`MixedFont` therefore draws Latin
and CJK runs with two different fonts on a shared baseline.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional, Sequence

from PIL import Image, ImageDraw, ImageFont

#: Fonts providing Latin letters and digits, in order of preference.
LATIN_FONT_CANDIDATES: tuple[str, ...] = (
    str(Path(__file__).resolve().parents[1] / "assets/fonts/NotoSansCJKsc-Regular.otf"),
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    "/usr/share/fonts/truetype/lato/Lato-Regular.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "C:/Windows/Fonts/arial.ttf",
)

#: Fonts providing CJK glyphs, in order of preference.
CJK_FONT_CANDIDATES: tuple[str, ...] = (
    str(Path(__file__).resolve().parents[1] / "assets/fonts/NotoSansCJKsc-Regular.otf"),
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "C:/Windows/Fonts/msyh.ttc",
)

BG_COLOR = (12, 14, 20)
PANEL_COLOR = (10, 12, 18)
LABEL_COLOR = (255, 196, 87)
TEXT_COLOR = (240, 243, 248)
DIM_COLOR = (168, 178, 195)
VLA_COLOR = (198, 224, 255)
OK_COLOR = (110, 220, 140)
WARN_COLOR = (255, 138, 128)

#: Characters at or above this code point are routed to the CJK font.
_CJK_THRESHOLD = 0x2E80


def _first_existing(candidates: Sequence[str]) -> Optional[str]:
    for candidate in candidates:
        if Path(candidate).is_file():
            return candidate
    return None


@lru_cache(maxsize=1)
def find_latin_font_path() -> Optional[str]:
    return _first_existing(LATIN_FONT_CANDIDATES)


@lru_cache(maxsize=1)
def find_cjk_font_path() -> Optional[str]:
    return _first_existing(CJK_FONT_CANDIDATES)


def overlay_available() -> bool:
    """True when both a Latin and a CJK font are available."""
    return find_latin_font_path() is not None and find_cjk_font_path() is not None


@lru_cache(maxsize=64)
def _load_font(path: str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()


class MixedFont:
    """Draw Latin and CJK text together on one baseline.

    The usual CJK fallback font has no Latin coverage, so a single
    ``truetype()`` call renders every ASCII character as a missing-glyph box.
    This wrapper picks the right face per character instead.
    """

    def __init__(self, size: int, bold: bool = False) -> None:
        self.size = max(int(size), 8)
        self.bold = bold
        latin_path = find_latin_font_path()
        cjk_path = find_cjk_font_path()
        self.latin = (
            _load_font(latin_path, self.size)
            if latin_path
            else ImageFont.load_default()
        )
        self.cjk = _load_font(cjk_path, self.size) if cjk_path else self.latin
        latin_metrics = self.latin.getmetrics()
        cjk_metrics = self.cjk.getmetrics()
        self.ascent = max(latin_metrics[0], cjk_metrics[0])
        self.descent = max(latin_metrics[1], cjk_metrics[1])
        self.stroke_width = max(1, self.size // 22) if bold else 0

    @property
    def line_height(self) -> int:
        return self.ascent + self.descent

    def face_for(self, char: str) -> ImageFont.FreeTypeFont:
        if ord(char) >= _CJK_THRESHOLD:
            return self.cjk
        return self.latin

    def advance(self, text: str) -> float:
        return sum(self.face_for(char).getlength(char) for char in text)

    def draw(
        self,
        draw: ImageDraw.ImageDraw,
        xy: tuple[float, float],
        text: str,
        fill: tuple[int, int, int],
        limit: Optional[float] = None,
    ) -> float:
        """Draw ``text`` with its baseline at ``xy``; return the end x."""
        x, baseline = xy
        for char in text:
            face = self.face_for(char)
            width = face.getlength(char)
            if limit is not None and x + width > limit:
                break
            draw.text(
                (x, baseline),
                char,
                font=face,
                fill=fill,
                anchor="ls",
                stroke_width=self.stroke_width,
                stroke_fill=fill,
            )
            x += width
        return x


def wrap_text(text: str, font: MixedFont, max_width: float, max_lines: int = 2) -> list[str]:
    """Character-wise greedy wrap, so unspaced CJK text also breaks."""
    text = " ".join(str(text).split())
    if not text:
        return []
    lines: list[str] = []
    current = ""
    for char in text:
        if font.advance(current + char) <= max_width or not current:
            current += char
            continue
        lines.append(current)
        current = char
        if len(lines) == max_lines:
            break
    if current and len(lines) < max_lines:
        lines.append(current)
    consumed = sum(len(line) for line in lines)
    if consumed < len(text) and lines:
        last = lines[-1]
        while last and font.advance(f"{last}...") > max_width:
            last = last[:-1]
        lines[-1] = f"{last}..."
    return lines


@dataclass
class FrameAnnotation:
    """Everything burned into one video frame."""

    mission: str = ""
    subgoal_label: str = ""
    subgoal_instruction: str = ""
    step_label: str = ""
    vla_text: str = ""
    action_text: str = ""
    command_text: str = ""
    status_text: str = ""
    status_color: tuple[int, int, int] = TEXT_COLOR
    step_progress: float = 0.0
    remaining_text: str = ""


def _panel(image: Image.Image, top: int, bottom: int, alpha: int) -> Image.Image:
    base = image.convert("RGBA")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    ImageDraw.Draw(overlay).rectangle(
        [0, top, base.width, bottom], fill=(*PANEL_COLOR, alpha)
    )
    return Image.alpha_composite(base, overlay)


def annotate_frame(
    source: str | Path,
    destination: str | Path,
    annotation: FrameAnnotation,
) -> Path:
    """Draw the annotation banner onto ``source`` and save to ``destination``."""
    image = Image.open(source).convert("RGB")
    width, height = image.size
    unit = max(12, height // 40)
    label_font = MixedFont(int(unit * 0.82), bold=True)
    body_font = MixedFont(int(unit * 0.92))
    small_font = MixedFont(int(unit * 0.78))
    pad = int(unit * 0.8)

    top_h = int(unit * 2.0)
    line = int(unit * 1.25)
    track_h = max(4, unit // 4)
    # Worst case: subgoal header, a two-line instruction, VLA, action, command.
    bottom_h = pad + 6 * line + track_h
    bottom_top = height - bottom_h

    composed = _panel(image, 0, top_h, 175)
    composed = _panel(composed, bottom_top, height, 210)
    draw = ImageDraw.Draw(composed)

    top_baseline = int(top_h * 0.72)
    mission_line = wrap_text(f"任务  {annotation.mission}", label_font, width * 0.64, 1)
    if mission_line:
        label_font.draw(draw, (pad, top_baseline), mission_line[0], LABEL_COLOR)
    if annotation.status_text:
        status_width = label_font.advance(annotation.status_text)
        label_font.draw(
            draw,
            (width - pad - status_width, top_baseline),
            annotation.status_text,
            annotation.status_color,
        )

    # Bottom-anchored so short subgoals do not leave a hole above the panel.
    command_y = height - track_h - pad // 2
    action_y = command_y - line
    vla_y = action_y - line
    instruction_lines = wrap_text(
        annotation.subgoal_instruction, body_font, width - 2 * pad, 2
    ) or [""]
    instruction_first_y = vla_y - line * len(instruction_lines)
    header_y = instruction_first_y - line

    header_end = label_font.draw(
        draw, (pad, header_y), annotation.subgoal_label, LABEL_COLOR
    )
    if annotation.step_label:
        small_font.draw(draw, (header_end + pad, header_y), annotation.step_label, DIM_COLOR)

    for offset, text in enumerate(instruction_lines):
        body_font.draw(
            draw,
            (pad, instruction_first_y + offset * line),
            text,
            TEXT_COLOR,
        )

    if annotation.vla_text:
        vla_x = label_font.draw(draw, (pad, vla_y), "VLA", LABEL_COLOR) + pad * 0.5
        lines = wrap_text(
            f'"{annotation.vla_text}"', body_font, width - vla_x - pad, 1
        )
        if lines:
            body_font.draw(draw, (vla_x, vla_y), lines[0], VLA_COLOR)

    if annotation.action_text:
        action_x = label_font.draw(draw, (pad, action_y), "动作", LABEL_COLOR) + pad * 0.5
        body_font.draw(draw, (action_x, action_y), annotation.action_text, OK_COLOR)
        if annotation.remaining_text:
            small_font.draw(
                draw,
                (int(width * 0.62), action_y),
                annotation.remaining_text,
                DIM_COLOR,
            )

    if annotation.command_text:
        small_font.draw(draw, (pad, command_y), annotation.command_text, DIM_COLOR)

    progress = max(0.0, min(1.0, annotation.step_progress))
    track_y = height - track_h
    draw.rectangle([0, track_y, width, height], fill=(52, 58, 72))
    if progress > 0:
        draw.rectangle([0, track_y, int(width * progress), height], fill=LABEL_COLOR)

    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    composed.convert("RGB").save(destination, quality=92)
    return destination


def render_card(
    destination: str | Path,
    title: str,
    subtitle: str = "",
    lines: Sequence[str] = (),
    size: tuple[int, int] = (1280, 720),
) -> Path:
    """Render a full-screen title/summary card used at the video ends."""
    image = Image.new("RGB", size, BG_COLOR)
    draw = ImageDraw.Draw(image)
    width, height = size
    unit = max(14, height // 26)
    title_font = MixedFont(int(unit * 1.25), bold=True)
    sub_font = MixedFont(int(unit * 0.82))
    body_font = MixedFont(int(unit * 0.68))
    left = int(width * 0.07)

    y = int(height * 0.20)
    for line in wrap_text(title, title_font, width * 0.86, 2):
        title_font.draw(draw, (left, y), line, LABEL_COLOR)
        y += int(unit * 1.6)
    if subtitle:
        y += int(unit * 0.2)
        for line in wrap_text(subtitle, sub_font, width * 0.86, 3):
            sub_font.draw(draw, (left, y), line, TEXT_COLOR)
            y += int(unit * 1.15)
    y += int(unit * 0.5)
    for line in lines:
        for wrapped in wrap_text(line, body_font, width * 0.86, 2):
            body_font.draw(draw, (left, y), wrapped, DIM_COLOR)
            y += int(unit * 0.98)

    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination, quality=92)
    return destination
