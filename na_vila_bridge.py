"""Standalone NaVILA inference bridge for the closed-loop demo.

Run inside the NaVILA `navila-eval` conda environment:

    NAVILA_ROOT=/path/to/NaVILA \
    NAVILA_MODEL_PATH=/path/to/NaVILA/ckpt \
    python3 na_vila_bridge.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import List

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

NAVILA_ROOT = Path(os.getenv("NAVILA_ROOT", str(Path(__file__).resolve().parent.parent / "NaVILA")))
sys.path.insert(0, str(NAVILA_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from navila_agno.navila_memory import (  # noqa: E402
    DEFAULT_NUM_VIDEO_FRAMES,
    PADDING_FRAME_SIZE,
    plan_frame_selection,
)

MODEL_PATH = os.getenv("NAVILA_MODEL_PATH", str(NAVILA_ROOT / "ckpt"))
CUDA_VISIBLE_DEVICES = os.getenv("NAVILA_CUDA_VISIBLE_DEVICES", "0")
os.environ["CUDA_VISIBLE_DEVICES"] = CUDA_VISIBLE_DEVICES
DEVICE = os.getenv("NAVILA_DEVICE", "cuda:0")


class NavigateRequest(BaseModel):
    instruction: str
    image_paths: List[str] = Field(default_factory=list)
    #: Target clip length. The bridge samples the history down to this many
    #: slots, so ``image_paths`` may hold the whole trajectory.
    num_video_frames: int = DEFAULT_NUM_VIDEO_FRAMES
    temperature: float = 0.0
    max_new_tokens: int = 64


class NavigateResponse(BaseModel):
    action: str
    model_path: str = MODEL_PATH
    num_video_frames: int = DEFAULT_NUM_VIDEO_FRAMES
    history_frames: int = 0
    sampled_frame_paths: List[str] = Field(default_factory=list)


app = FastAPI(title="NaVILA Bridge", version="0.1.0")
_state: dict = {}


def load_model() -> None:
    import torch

    from llava.constants import IMAGE_TOKEN_INDEX
    from llava.conversation import conv_templates
    from llava.mm_utils import get_model_name_from_path
    from llava.model.builder import load_pretrained_model

    model_name = get_model_name_from_path(MODEL_PATH)
    tokenizer, model, image_processor, context_len = load_pretrained_model(
        MODEL_PATH, model_name, None, device=DEVICE
    )
    model.eval()
    _state.update(
        tokenizer=tokenizer,
        model=model,
        image_processor=image_processor,
        context_len=context_len,
        conv=conv_templates["llama_3"].copy(),
        sep=conv_templates["llama_3"].sep,
        sep2=conv_templates["llama_3"].sep2,
    )


@app.on_event("startup")
def startup() -> None:
    load_model()


@app.get("/health")
def health() -> dict:
    return {
        "status": "ready" if "model" in _state else "loading",
        "model_path": MODEL_PATH,
        "device": DEVICE,
    }


def _build_inputs(req: NavigateRequest) -> dict:
    import torch
    from PIL import Image

    from llava.constants import IMAGE_TOKEN_INDEX
    from llava.conversation import SeparatorStyle
    from llava.mm_utils import KeywordsStoppingCriteria, process_images, tokenizer_image_token

    if not req.image_paths:
        raise HTTPException(status_code=400, detail="image_paths 不能为空")

    num_frames = int(req.num_video_frames or DEFAULT_NUM_VIDEO_FRAMES)
    selection = plan_frame_selection(len(req.image_paths), num_frames)
    missing = [
        req.image_paths[index]
        for index in selection
        if index is not None and not Path(req.image_paths[index]).is_file()
    ]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"抽中的历史帧不存在：{missing[:3]}",
        )

    images = []
    for index in selection:
        if index is None:
            images.append(
                Image.new(
                    "RGB",
                    (PADDING_FRAME_SIZE, PADDING_FRAME_SIZE),
                    color=(0, 0, 0),
                )
            )
        else:
            images.append(Image.open(req.image_paths[index]).convert("RGB"))

    image_token = "<image>\n"
    question = (
        "Imagine you are a robot programmed for navigation tasks. You have been given a video "
        f"of historical observations {image_token * (len(images) - 1)}, and current observation <image>\n. "
        f'Your assigned task is: "{req.instruction}" '
        "Analyze this series of images to decide your next action, which could be turning left or right by a specific "
        "degree, moving forward a certain distance, or stop if the task is completed."
    )

    conv = _state["conv"].copy()
    conv.append_message(conv.roles[0], question)
    conv.append_message(conv.roles[1], None)
    prompt = conv.get_prompt()

    tokenizer = _state["tokenizer"]
    model = _state["model"]
    image_processor = _state["image_processor"]

    images_tensor = process_images(images, image_processor, model.config).to(
        model.device, dtype=torch.float16
    )
    input_ids = (
        tokenizer_image_token(prompt, tokenizer, IMAGE_TOKEN_INDEX, return_tensors="pt")
        .unsqueeze(0)
        .to(model.device)
    )

    stop_str = (
        _state["sep"]
        if _state["sep"] is not None and _state["sep"] != SeparatorStyle.TWO
        else _state["sep2"]
    )
    keywords = [stop_str]
    stopping_criteria = KeywordsStoppingCriteria(keywords, tokenizer, input_ids)
    return {
        "input_ids": input_ids,
        "images_tensor": images_tensor.half(),
        "stopping_criteria": [stopping_criteria],
        "tokenizer": tokenizer,
        "stop_str": stop_str,
        "selection": selection,
    }


@app.post("/v1/navigate", response_model=NavigateResponse)
def navigate(req: NavigateRequest) -> NavigateResponse:
    import torch

    if "model" not in _state:
        raise HTTPException(status_code=503, detail="模型尚未加载完成")

    inputs = _build_inputs(req)
    tokenizer = inputs["tokenizer"]
    with torch.inference_mode():
        output_ids = _state["model"].generate(
            inputs["input_ids"],
            images=inputs["images_tensor"],
            do_sample=req.temperature > 0,
            temperature=req.temperature,
            max_new_tokens=req.max_new_tokens,
            use_cache=True,
            stopping_criteria=inputs["stopping_criteria"],
            pad_token_id=tokenizer.eos_token_id,
        )

    outputs = tokenizer.batch_decode(output_ids, skip_special_tokens=True)[0].strip()
    if outputs.endswith(inputs["stop_str"]):
        outputs = outputs[: -len(inputs["stop_str"])].strip()
    selection = inputs["selection"]
    return NavigateResponse(
        action=outputs,
        num_video_frames=len(selection),
        history_frames=len(req.image_paths),
        sampled_frame_paths=[
            req.image_paths[index] for index in selection if index is not None
        ],
    )


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("NAVILA_HOST", "0.0.0.0")
    port = int(os.getenv("NAVILA_PORT", "8011"))
    uvicorn.run("na_vila_bridge:app", host=host, port=port, workers=1)
