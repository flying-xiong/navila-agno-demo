"""True closed-loop demo: Habitat simulator + NaVILA bridge + Agno supervisor.

Run inside the NaVILA `navila-eval` conda environment:

    python3 closed_loop_runner.py

Required external services:
    - NaVILA bridge   : http://127.0.0.1:8011/v1/navigate
    - Agno supervisor : http://127.0.0.1:8012/plan|replan
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import urllib.request
from collections import deque
from pathlib import Path
from typing import Any, Optional

import numpy as np
from PIL import Image, ImageDraw

PROJECT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_DIR))

from navila_agno.contracts import EventType, SubGoal  # noqa: E402
from navila_agno.vla import parse_action  # noqa: E402


def _http_json(url: str, payload: dict[str, Any], timeout: float = 360.0) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _save_frame(frame_dir: Path, observations: dict[str, Any], index: int) -> str:
    rgb = np.asarray(observations["rgb"])
    if rgb.ndim == 4:
        rgb = rgb[0]
    rgb = rgb[:, :, :3].astype(np.uint8)
    path = frame_dir / f"frame_{index:05d}.jpg"
    Image.fromarray(rgb).save(path, quality=92)
    return str(path)


def _annotate_frame(
    frame: np.ndarray,
    mission: str,
    subgoal: Optional[SubGoal],
    action_text: str,
    font_path: str,
) -> np.ndarray:
    img = Image.fromarray(frame.astype(np.uint8)).convert("RGB")
    draw = ImageDraw.Draw(img)
    font_small = None
    try:
        from PIL import ImageFont

        font_small = ImageFont.truetype(font_path, 20)
    except Exception:
        pass

    label = "Agno: 等待规划" if subgoal is None else f"Agno: {subgoal.note_zh or subgoal.instruction}"
    lines = [f"Mission: {mission[:80]}", label, f"NaVILA: {action_text[:80]}"]
    if font_small is not None:
        y = 8
        for line in lines:
            draw.rectangle((0, y, img.width, y + 30), fill=(0, 0, 0))
            draw.text((10, y + 6), line, fill=(255, 255, 255), font=font_small)
            y += 32
    return np.asarray(img)


def _action_index(action_text: str) -> tuple[int, int]:
    action = parse_action(action_text)
    if action.action_type == "stop":
        return 0, 0
    if action.action_type == "turn_left":
        return 2, max(1, round(action.angle_deg / 15.0))
    if action.action_type == "turn_right":
        return 3, max(1, round(action.angle_deg / 15.0))
    return 1, max(1, round(action.distance_m / 0.25))


def _build_env(config_module: Any, config: Any, episode: str, split: str) -> tuple[Any, Any]:
    from habitat_baselines.common.environments import get_env_class
    from vlnce_baselines.common.env_utils import construct_envs_auto_reset_false

    config.defrost()
    config.TASK_CONFIG.DATASET.SPLIT = split
    config.TASK_CONFIG.DATASET.ROLES = ["guide"]
    config.TASK_CONFIG.DATASET.LANGUAGES = config.EVAL.LANGUAGES
    config.TASK_CONFIG.TASK.NDTW.SPLIT = split
    config.TASK_CONFIG.ENVIRONMENT.ITERATOR_OPTIONS.SHUFFLE = False
    config.TASK_CONFIG.ENVIRONMENT.ITERATOR_OPTIONS.MAX_SCENE_REPEAT_STEPS = -1
    config.TASK_CONFIG.DATASET.NUM_CHUNKS = 1
    config.TASK_CONFIG.DATASET.CHUNK_IDX = 0
    config.TASK_CONFIG.DATASET.EPISODES_ALLOWED = [episode]
    if "TOP_DOWN_MAP_VLNCE" not in config.TASK_CONFIG.TASK.MEASUREMENTS:
        config.TASK_CONFIG.TASK.MEASUREMENTS.append("TOP_DOWN_MAP_VLNCE")
    config.freeze()

    envs = construct_envs_auto_reset_false(config, get_env_class(config.ENV_NAME))
    return envs, config


def run_closed_loop(
    navila_root: Path,
    episode: str,
    split: str,
    output_dir: Path,
    supervisor_url: str,
    navila_url: str,
    font_path: str,
    max_outer_steps: int = 60,
) -> None:
    navila_root = navila_root.resolve()
    output_dir = output_dir.resolve()
    sys.path.insert(0, str(navila_root / "evaluation"))
    from habitat_extensions.utils import generate_video, observations_to_image
    from vlnce_baselines.config.default import get_config

    os.chdir(str(navila_root / "evaluation"))
    exp_config = str(navila_root / "evaluation/vlnce_baselines/config/r2r_baselines/navila.yaml")
    config = get_config(exp_config)
    envs, config = _build_env(None, config, episode, split)

    observations = envs.reset()
    current_episodes = envs.current_episodes()
    mission = current_episodes[0].instruction.instruction_text
    ep_id = current_episodes[0].episode_id

    print(f"[closed-loop] episode={ep_id} mission={mission}")
    plan = _http_json(f"{supervisor_url}/plan", {"mission": mission})
    subgoals = deque(SubGoal(**item) for item in plan["subgoals"])
    print(subgoals)
    current_subgoal = subgoals.popleft() if subgoals else None

    frame_dir = Path(tempfile.mkdtemp(prefix="navila_agno_frames_"))
    past_paths: list[str] = []
    rgb_frames: list[np.ndarray] = []
    queue_actions: deque[int] = deque()

    current_path = _save_frame(frame_dir, observations[0], 0)

    done = False
    outer_step = 0
    subgoal_step = 0
    last_action_text = ""

    while not done and outer_step < max_outer_steps:
        if current_subgoal is None:
            break

        if queue_actions:
            action_idx = queue_actions.popleft()
        else:
            frames = past_paths[-8:] + [current_path]
            payload = {
                "instruction": current_subgoal.instruction,
                "image_paths": frames,
                "num_video_frames": len(frames),
            }
            response = _http_json(f"{navila_url}/v1/navigate", payload)
            last_action_text = response.get("action", "stop")
            print(f"[closed-loop] subgoal={current_subgoal.id} step={subgoal_step} -> {last_action_text}")
            action_idx, substeps = _action_index(last_action_text)

            if action_idx == 0:
                if subgoals:
                    current_subgoal = subgoals.popleft()
                    subgoal_step = 0
                    continue
                outputs = envs.step([0])
                observations, _, dones, infos = [list(x) for x in zip(*outputs)]
                done = bool(dones[0])
                rgb_frames.append(
                    _annotate_frame(
                        observations_to_image(observations[0], infos[0]),
                        mission,
                        current_subgoal,
                        last_action_text,
                        font_path,
                    )
                )
                break

            for _ in range(max(0, substeps - 1)):
                queue_actions.append(action_idx)

        outputs = envs.step([action_idx])
        observations, _, dones, infos = [list(x) for x in zip(*outputs)]
        done = bool(dones[0])

        past_paths.append(current_path)
        current_path = _save_frame(frame_dir, observations[0], outer_step + 1)
        rgb_frames.append(
            _annotate_frame(
                observations_to_image(observations[0], infos[0]),
                mission,
                current_subgoal,
                last_action_text,
                font_path,
            )
        )
        subgoal_step += 1
        outer_step += 1

        if done:
            break

        if current_subgoal is not None and subgoal_step >= current_subgoal.max_steps:
            print(f"[closed-loop] subgoal failed after max_steps: {current_subgoal.id}")
            replan = _http_json(
                f"{supervisor_url}/replan",
                {
                    "mission": mission,
                    "event": {
                        "type": EventType.SUBGOAL_FAILED.value,
                        "subgoal_id": current_subgoal.id,
                        "message": "max_steps exceeded",
                        "step_count": subgoal_step,
                    },
                },
            )
            replacement = [SubGoal(**item) for item in replan.get("subgoals", [])]
            if not replacement:
                print("[closed-loop] supervisor 未给出替代路径，停止任务。")
                break
            for sg in reversed(replacement):
                subgoals.appendleft(sg)
            current_subgoal = subgoals.popleft()
            subgoal_step = 0

    spl = float(infos[0].get("spl", 0.0)) if infos else 0.0
    output_dir.mkdir(parents=True, exist_ok=True)
    generate_video(
        video_option=["disk"],
        video_dir=str(output_dir),
        images=rgb_frames,
        episode_id=ep_id,
        checkpoint_idx=0,
        metrics={"spl": spl},
        tb_writer=None,
        fps=10,
    )
    envs.close()
    print(f"[closed-loop] video saved to {output_dir}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a true NaVILA x Agno closed loop in Habitat")
    parser.add_argument(
        "--navila-root",
        default=os.getenv("NAVILA_ROOT", str(PROJECT_DIR.parent / "NaVILA")),
    )
    parser.add_argument("--episode", default="86")
    parser.add_argument("--split", default="val_unseen")
    parser.add_argument("--output", default=str(PROJECT_DIR / "runs" / "habitat"))
    parser.add_argument("--supervisor-url", default="http://127.0.0.1:8012")
    parser.add_argument("--navila-url", default="http://127.0.0.1:8011")
    parser.add_argument(
        "--font",
        default=str(PROJECT_DIR / "assets/fonts/NotoSansCJKsc-Regular.otf"),
    )
    parser.add_argument("--max-outer-steps", type=int, default=120)
    args = parser.parse_args()

    run_closed_loop(
        navila_root=Path(args.navila_root),
        episode=args.episode,
        split=args.split,
        output_dir=Path(args.output),
        supervisor_url=args.supervisor_url.rstrip("/"),
        navila_url=args.navila_url.rstrip("/"),
        font_path=args.font,
        max_outer_steps=args.max_outer_steps,
    )


if __name__ == "__main__":
    main()
