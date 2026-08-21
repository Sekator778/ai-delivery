from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile

MODEL_PATH = os.environ.get("WHISPER_MODEL_PATH", "/models/ggml-medium.bin")
WHISPER_BIN = os.environ.get("WHISPER_BIN", "whisper-cli")
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8765"))
# Configurable timeouts (FR-014, FR-015). Applied independently — do not derive
# one from the other (BRD NFR-003, NFR-002 independence requirement).
WHISPER_TIMEOUT_SEC = int(os.environ.get("WHISPER_TIMEOUT_SEC", "600"))
FFMPEG_TIMEOUT_SEC = int(os.environ.get("FFMPEG_TIMEOUT_SEC", "60"))

# Accepted audio filename suffixes for temp-file naming (ADR-004).
# Whitelist: client-supplied extension is used only as a format hint to ffmpeg;
# anything outside this set falls back to .bin (safe, ffmpeg still probes).
_ACCEPTED_AUDIO_SUFFIXES: frozenset[str] = frozenset(
    {".m4a", ".mp3", ".ogg", ".wav", ".aiff", ".flac"}
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("whisper-server")

if not Path(MODEL_PATH).exists():
    logger.warning("Model not found at %s", MODEL_PATH)

app = FastAPI()


async def _run_subprocess(cmd: list[str], timeout: float) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return (proc.returncode or 0, stderr.decode())
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise


def _derive_upload_suffix(filename: Optional[str]) -> str:
    """Return a whitelisted suffix from the uploaded filename (ADR-004).

    Only the extension is used; the rest of the client-supplied filename is
    discarded. Falls back to '.bin' for unknown or missing extensions so that
    ffmpeg's built-in probing can still identify the format.
    """
    ext = os.path.splitext(filename or "")[1].lower()
    return ext if ext in _ACCEPTED_AUDIO_SUFFIXES else ".bin"


@app.get("/health")
async def health() -> dict:
    model_exists = Path(MODEL_PATH).exists()
    if model_exists:
        return {"ok": True, "model": MODEL_PATH}
    return {"ok": False, "reason": "model not found", "model": MODEL_PATH}


@app.post("/transcribe")
async def transcribe(
    file: UploadFile = File(...),
    language: str = Form("ru"),
) -> dict:
    audio_tmp: Optional[str] = None
    wav_tmp: Optional[str] = None
    try:
        # Preserve the original audio extension so ffmpeg gets a correct format
        # hint instead of always assuming .ogg (FR-013, ADR-004).
        suffix = _derive_upload_suffix(file.filename)
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(await file.read())
            audio_tmp = f.name

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            wav_tmp = f.name

        # ffmpeg conversion step — independently bounded by FFMPEG_TIMEOUT_SEC
        # (FR-015, NFR-003). Never coupled to WHISPER_TIMEOUT_SEC.
        try:
            retcode, stderr = await _run_subprocess(
                [
                    "ffmpeg", "-i", audio_tmp,
                    "-ar", "16000", "-ac", "1",
                    "-c:a", "pcm_s16le", wav_tmp,
                    "-y",
                ],
                timeout=FFMPEG_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            logger.error("ffmpeg timed out after %ds", FFMPEG_TIMEOUT_SEC)
            raise HTTPException(
                status_code=500,
                detail={
                    "reason": f"Audio conversion timed out after {FFMPEG_TIMEOUT_SEC}s",
                    "step": "convert",
                    "stderr_tail": "",
                },
            )

        if retcode != 0:
            # ffmpeg non-zero exit = input file is corrupt/unsupported → 422
            # (ADR-001: client-fault status for invalid input).
            stderr_tail = stderr[-400:] if len(stderr) > 400 else stderr
            logger.error("ffmpeg failed (exit %d): %s", retcode, stderr)
            raise HTTPException(
                status_code=422,
                detail={
                    "reason": f"Audio conversion failed (ffmpeg exit {retcode})",
                    "step": "convert",
                    "stderr_tail": stderr_tail,
                },
            )

        wav_stem = wav_tmp.rsplit(".", 1)[0]

        # whisper-cli transcription step — independently bounded by
        # WHISPER_TIMEOUT_SEC (FR-014, NFR-002).
        try:
            retcode, stderr = await _run_subprocess(
                [
                    WHISPER_BIN, "-m", MODEL_PATH,
                    "-l", language,
                    "-f", wav_tmp,
                    "--no-timestamps",
                    "--output-txt",
                    "--output-file", wav_stem,
                ],
                timeout=WHISPER_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            logger.error("whisper-cli timed out after %ds", WHISPER_TIMEOUT_SEC)
            raise HTTPException(
                status_code=500,
                detail={
                    "reason": f"Transcription timed out after {WHISPER_TIMEOUT_SEC}s",
                    "step": "transcribe",
                    "stderr_tail": "",
                },
            )

        if retcode != 0:
            stderr_tail = stderr[-400:] if len(stderr) > 400 else stderr
            logger.error("whisper-cli failed (exit %d): %s", retcode, stderr)
            raise HTTPException(
                status_code=500,
                detail={
                    "reason": f"Transcription failed (whisper-cli exit {retcode})",
                    "step": "transcribe",
                    "stderr_tail": stderr_tail,
                },
            )

        txt_path = wav_stem + ".txt"
        if not Path(txt_path).exists():
            logger.error("whisper-cli output not found: %s", txt_path)
            raise HTTPException(
                status_code=500,
                detail={
                    "reason": "Transcription produced no output file",
                    "step": "transcribe",
                    "stderr_tail": "",
                },
            )

        text = Path(txt_path).read_text(encoding="utf-8").strip()
        logger.info("Transcription complete: %d chars", len(text))
        return {"text": text, "source": "whisper-cpp", "language": language}

    finally:
        for p in (audio_tmp, wav_tmp):
            if p:
                try:
                    Path(p).unlink(missing_ok=True)
                except OSError:
                    pass
        if wav_tmp:
            txt_path = wav_tmp.rsplit(".", 1)[0] + ".txt"
            try:
                Path(txt_path).unlink(missing_ok=True)
            except OSError:
                pass


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT)
