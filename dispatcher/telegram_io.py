"""Telegram / bot notification side-channels.

Extracted from stage_runner_agent.py (god-module split, 2026-06-04). Both
helpers are best-effort: a failure is logged to stderr but never aborts the
pipeline. _notify_bot POSTs a signal to bot.py's HTTP /notify endpoint (which
drives Telegram actions); _send_telegram shells out to the botctl-send-text
helper for a direct status line. Imports only stdlib.
"""
from __future__ import annotations

import json
import os as _os
import subprocess
import sys
from pathlib import Path


BOTCTL_SEND_TEXT = Path.home() / "projects" / "ai-delivery" / "bin" / "botctl-send-text"


BOT_HTTP_URL = _os.environ.get("BOT_HTTP_URL", "http://127.0.0.1:8766")


def _notify_bot(signal: str, task_id: str, **extra: object) -> None:
    """POST to bot.py's /notify endpoint to trigger Telegram actions."""
    import urllib.request

    body: dict[str, object] = {"signal": signal, "task_id": task_id}
    body.update(extra)
    payload = json.dumps(body).encode()
    try:
        req = urllib.request.Request(
            f"{BOT_HTTP_URL}/notify",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception as exc:
        print(f"warn: failed to notify bot (signal={signal}): {exc}",
              file=sys.stderr)


def _send_telegram(text: str) -> None:
    """Send a status message to Telegram via botctl-send-text."""
    if not BOTCTL_SEND_TEXT.exists():
        print(f"warn: botctl-send-text not found at {BOTCTL_SEND_TEXT}",
              file=sys.stderr)
        return
    try:
        subprocess.run(
            [str(BOTCTL_SEND_TEXT), text],
            timeout=10,
            capture_output=True,
        )
    except Exception as exc:
        print(f"warn: botctl-send-text failed: {exc}", file=sys.stderr)
