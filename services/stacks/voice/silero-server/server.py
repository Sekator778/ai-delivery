from __future__ import annotations

import io
import logging
import os
import time
from typing import Any

import soundfile as sf
import torch
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel


# Configured unconditionally (not gated behind `if __name__ == "__main__"`):
# the Dockerfile CMD invokes `uvicorn server:app` directly, which never runs
# the __main__ block, so without this, INFO logs (model load) and even the
# traceback from logger.exception() below never reach container stdout.
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("silero-server")

LANGUAGE = os.getenv("SILERO_LANGUAGE", "ru")
MODEL_ID = os.getenv("SILERO_MODEL_ID", "v4_ru")
HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8768"))
ALLOWED_VOICES: list[str] = ["eugene", "aidar", "baya", "kseniya", "xenia"]

MAX_TEXT_LENGTH = 8000

logger.info("Loading Silero TTS model (language=%s, model_id=%s)...", LANGUAGE, MODEL_ID)
_start = time.monotonic()
# NOTE: for v3/v4/v5 speakers (which MODEL_ID="v4_ru" is), the silero_tts hub
# entrypoint returns a (model, example_text) tuple, not the model object
# itself — see snakers4/silero-models src/silero/silero.py:silero_tts(). Using
# the tuple directly as `_model` makes every /synthesize call fail with
# "'tuple' object has no attribute 'apply_tts'" regardless of architecture.
_model: Any
_model, _example_text = torch.hub.load(
    repo_or_dir="snakers4/silero-models",
    model="silero_tts",
    language=LANGUAGE,
    speaker=MODEL_ID,
)
_elapsed = time.monotonic() - _start
logger.info("Silero TTS model loaded in %.1fs", _elapsed)

app = FastAPI()


class SynthesizeRequest(BaseModel):
    text: str
    voice: str = "eugene"
    sample_rate: int = 48000


@app.get("/health")
async def health() -> dict[str, object]:
    return {
        "ok": True,
        "model": MODEL_ID,
        "sample_rate": 48000,
        "voices": ALLOWED_VOICES,
    }


@app.post("/synthesize")
async def synthesize(req: SynthesizeRequest) -> Response:
    if req.voice not in ALLOWED_VOICES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown voice '{req.voice}'. Allowed: {ALLOWED_VOICES}",
        )

    if len(req.text) > MAX_TEXT_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"Text too long ({len(req.text)} chars, max {MAX_TEXT_LENGTH})",
        )

    try:
        audio_tensor: torch.Tensor = _model.apply_tts(
            text=req.text,
            speaker=req.voice,
            sample_rate=req.sample_rate,
        )
        audio_np = audio_tensor.cpu().numpy()

        buf = io.BytesIO()
        sf.write(buf, audio_np, req.sample_rate, format="WAV", subtype="PCM_16")
        buf.seek(0)
    except Exception as exc:
        logger.exception(
            "Synthesis failed (voice=%s, sample_rate=%d, text_len=%d)",
            req.voice,
            req.sample_rate,
            len(req.text),
        )
        return JSONResponse(
            status_code=500,
            content={"error": str(exc), "type": type(exc).__name__},
        )

    return Response(content=buf.getvalue(), media_type="audio/wav")


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT)
