"""NaVILA's history-frame memory, mirrored from the official evaluation loop.

NaVILA is stateless: every forward pass consumes a fixed-size clip of
``num_video_frames`` images (8 in the released checkpoint) built from

    7 uniformly sampled history frames + the current observation

and it expects black padding frames at the front while the episode is shorter
than that.  This is ``sample_and_pad_images`` in
``evaluation/vlnce_baselines/navila_trainer.py`` of the NaVILA release.

The key property that a naive "last N frames" window loses: the samples span
the *whole* trajectory, so landmarks seen at the start of a corridor or a
building stay in context for the rest of the mission.

This module is dependency-free so the bridge, the client and the tests all
share one implementation.
"""
from __future__ import annotations

from typing import Optional, Sequence, TypeVar

#: History length the NaVILA checkpoint was trained and evaluated with.
DEFAULT_NUM_VIDEO_FRAMES = 8

#: Size of the black padding frames used by the official sampler.
PADDING_FRAME_SIZE = 512

T = TypeVar("T")


def plan_frame_selection(length: int, num_frames: int) -> list[Optional[int]]:
    """Map ``num_frames`` model inputs onto a history of ``length`` frames.

    Returns one entry per model input: an index into the history, or ``None``
    for a black padding frame.  Mirrors ``sample_and_pad_images``:

    * fewer history frames than ``num_frames`` -> pad the front with black and
      keep every real frame;
    * otherwise -> ``num_frames - 1`` frames spaced uniformly across the whole
      history (``linspace(0, length - 1, num=num_frames - 1, endpoint=False)``)
      followed by the most recent frame.
    """
    if length <= 0:
        raise ValueError("history 不能为空")
    if num_frames < 1:
        raise ValueError("num_frames 至少为 1")

    if length < num_frames:
        return [None] * (num_frames - length) + list(range(length))

    if num_frames == 1:
        return [length - 1]

    divisor = num_frames - 1
    indices = [int(i * (length - 1) / divisor) for i in range(divisor)]
    return indices + [length - 1]


def sample_and_pad_paths(
    paths: Sequence[T], num_frames: int = DEFAULT_NUM_VIDEO_FRAMES
) -> list[Optional[T]]:
    """Select history entries for one NaVILA call; ``None`` means black padding."""
    history = list(paths)
    return [
        None if index is None else history[index]
        for index in plan_frame_selection(len(history), num_frames)
    ]


def describe_selection(selection: Sequence[Optional[int]]) -> dict[str, int]:
    """Summarise a selection for logs and step records."""
    return {
        "slots": len(selection),
        "padding": sum(1 for index in selection if index is None),
        "history": sum(1 for index in selection if index is not None),
    }
