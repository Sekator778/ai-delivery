"""Tests for silero-server /synthesize error-envelope path (issue #15).

silero-server's server.py performs real (network-touching) Silero model
loading at *module import time* via `torch.hub.load(...)`, and imports
`torch`, `soundfile`, and `uvicorn` unconditionally. None of those are
installed in the lightweight host/CI test environment (only
fastapi/pydantic/starlette are), and even where torch IS installed,
importing server.py for real would trigger an actual model download.

So this module injects minimal stand-in modules for torch/soundfile/uvicorn
into sys.modules before importing server.py — this keeps the import hermetic
(no network, no real model, no dependency on torch being installed) while
still exercising the *real* synthesize() handler code, including the
try/except -> logger.exception -> JSONResponse envelope added for issue #15
("silero TTS /synthesize returns 500 on arm64 — no traceback surfaces").

This satisfies "if the code structure allows importing server.py without
torch" by making torch not actually required — only stubbed.
"""
from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parent.parent
_SERVER_PATH = (
    REPO_ROOT / "services" / "stacks" / "voice" / "silero-server" / "server.py"
)
# whisper-server also has a module literally named server.py. Loading both
# under the bare name "server" makes them collide in sys.modules depending
# on test order (whichever imports first "wins" for both test files) — load
# this one under a distinct name so it never touches sys.modules["server"].
_MODULE_NAME = "silero_server_under_test"

_FASTAPI_AVAILABLE = importlib.util.find_spec("fastapi") is not None

_SKIP_REASON = (
    "fastapi is not installed in the host test environment — "
    "silero-server cannot be imported even with torch/soundfile/uvicorn "
    "stubbed out. Install fastapi: pip install fastapi. "
    "The error-envelope logic is documented here for manual verification: "
    "POST /synthesize with a text/voice that makes the model raise must "
    "return HTTP 500 with JSON body {\"error\": <str>, \"type\": <exc class "
    "name>} and log the full traceback via logger.exception()."
)


def _install_stub_modules() -> MagicMock:
    """Install fake torch/soundfile/uvicorn modules and return the fake model.

    Returns the MagicMock standing in for the loaded Silero model (what
    server._model gets bound to), so tests can set .apply_tts.side_effect.
    """
    fake_model = MagicMock(name="silero_model")

    fake_torch_hub = types.ModuleType("torch.hub")
    fake_torch_hub.load = MagicMock(return_value=(fake_model, "example text"))

    fake_torch = types.ModuleType("torch")
    fake_torch.hub = fake_torch_hub
    fake_torch.Tensor = object  # only used as a (deferred) type annotation

    fake_soundfile = types.ModuleType("soundfile")
    fake_soundfile.write = MagicMock(return_value=None)

    fake_uvicorn = types.ModuleType("uvicorn")
    fake_uvicorn.run = MagicMock(return_value=None)

    sys.modules["torch"] = fake_torch
    sys.modules["torch.hub"] = fake_torch_hub
    sys.modules["soundfile"] = fake_soundfile
    sys.modules["uvicorn"] = fake_uvicorn

    return fake_model


def _import_server():
    """Load silero-server's server.py fresh, under a private module name.

    Uses spec_from_file_location (not sys.path + `import server`) so this
    never reads or writes sys.modules["server"] — that key is also used by
    tests/test_whisper_server_format.py for its own, differently-located
    server.py, and sharing the bare name causes order-dependent collisions.
    """
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, _SERVER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(_FASTAPI_AVAILABLE, _SKIP_REASON)
class SileroSynthesizeErrorEnvelopeTests(unittest.TestCase):
    """/synthesize must turn a raising model call into a logged, JSON 500.

    Regression coverage for issue #15: previously the handler either let
    the exception propagate bare (no client-visible detail, no server-side
    traceback) or (after the tuple-unpacking root cause was found) raised
    HTTPException(500, detail=str(exc)) without ever calling
    logger.exception(), so the container had no traceback in its logs.
    """

    def setUp(self) -> None:
        self.fake_model = _install_stub_modules()
        self.server = _import_server()
        # torch.hub.load() ran once during import; server._model is bound
        # to our fake_model already. Reset call state for each test.
        self.fake_model.reset_mock(side_effect=True)

    def tearDown(self) -> None:
        sys.modules.pop(_MODULE_NAME, None)

    def test_model_exception_yields_500_json_with_error_and_type(self) -> None:
        self.fake_model.apply_tts.side_effect = RuntimeError("Numpy is not available")

        req = self.server.SynthesizeRequest(text="test", voice="eugene")
        response = asyncio.run(self.server.synthesize(req))

        self.assertEqual(response.status_code, 500)
        body = json.loads(bytes(response.body))
        self.assertEqual(body["error"], "Numpy is not available")
        self.assertEqual(body["type"], "RuntimeError")

    def test_model_exception_is_logged_with_traceback(self) -> None:
        self.fake_model.apply_tts.side_effect = AttributeError(
            "'tuple' object has no attribute 'apply_tts'"
        )

        req = self.server.SynthesizeRequest(text="test", voice="eugene")
        with self.assertLogs("silero-server", level="ERROR") as captured:
            asyncio.run(self.server.synthesize(req))

        joined = "\n".join(captured.output)
        self.assertIn("Synthesis failed", joined)
        # logger.exception() attaches exc_info=True; the default Formatter
        # used by assertLogs renders it as a "Traceback (most recent call
        # last):" block appended to the record's output.
        self.assertIn("Traceback", joined)
        self.assertIn("AttributeError", joined)

    def test_unknown_voice_still_raises_400_not_swallowed_into_500(self) -> None:
        from fastapi import HTTPException

        req = self.server.SynthesizeRequest(text="test", voice="not-a-voice")
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(self.server.synthesize(req))
        self.assertEqual(ctx.exception.status_code, 400)

    def test_successful_synthesis_returns_wav_response_not_json(self) -> None:
        fake_tensor = MagicMock()
        fake_tensor.cpu.return_value.numpy.return_value = b"\x00\x01"
        self.fake_model.apply_tts.return_value = fake_tensor

        req = self.server.SynthesizeRequest(text="test", voice="eugene")
        response = asyncio.run(self.server.synthesize(req))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.media_type, "audio/wav")


if __name__ == "__main__":
    unittest.main()
