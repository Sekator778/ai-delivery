#!/usr/bin/env python3
"""Markdown → Telegram-HTML preprocessor used by bin/botctl-send-text.

Lives as a separate file (not an inline heredoc inside the shell script)
because `python3 - <<'EOF'` consumes its own stdin as the program source,
leaving nothing for `sys.stdin.read()` to receive from the upstream pipe.
That was the bug the meta-agent diagnosed in its PS note 2026-05-25.

Reads the raw chunk on stdin, writes the converted HTML to stdout.

Conversion rules:

1. Stash fenced ```code``` blocks before HTML-escaping so their content
   is escaped exactly once.
2. Stash inline `code` for the same reason.
3. HTML-escape `&<>` in the remaining body.
4. `**bold**` → `<b>bold</b>`.
5. `*italic*` → `<i>italic</i>`, but ONLY when the asterisks are not
   adjacent to word chars — preserves identifiers like `mcp__mem0__*`
   and Markdown list bullets like `* foo`.
6. Restore inline code as `<code>…</code>` with body HTML-escaped.
7. Restore fenced blocks as `<pre>…</pre>` with body HTML-escaped, and
   strip a leading language tag like `python\n` (Telegram <pre> has no
   syntax highlighting, the language word would just appear as text).

Telegram HTML supports: <b> <i> <u> <s> <code> <pre> <a> — nothing
else, per https://core.telegram.org/bots/api#formatting-options.
"""
from __future__ import annotations

import re
import sys


def main() -> int:
    text = sys.stdin.read()

    fences: list[str] = []

    def stash_fence(m: re.Match) -> str:
        fences.append(m.group(1))
        return f"\x00FENCE{len(fences) - 1}\x00"

    text = re.sub(r"```([^`]*?)```", stash_fence, text, flags=re.DOTALL)

    inlines: list[str] = []

    def stash_inline(m: re.Match) -> str:
        inlines.append(m.group(1))
        return f"\x00INLINE{len(inlines) - 1}\x00"

    text = re.sub(r"`([^`\n]+)`", stash_inline, text)

    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    text = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", text)
    text = re.sub(
        r"(?<![*\w])\*([^*\s][^*\n]*[^*\s]|[^*\s])\*(?![*\w])",
        r"<i>\1</i>",
        text,
    )

    def restore_inline(m: re.Match) -> str:
        idx = int(m.group(1))
        body = (
            inlines[idx]
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        return f"<code>{body}</code>"

    text = re.sub(r"\x00INLINE(\d+)\x00", restore_inline, text)

    def restore_fence(m: re.Match) -> str:
        idx = int(m.group(1))
        body = (
            fences[idx]
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        body = re.sub(r"^[a-zA-Z0-9_+-]{1,20}\n", "", body)
        return f"<pre>{body}</pre>"

    text = re.sub(r"\x00FENCE(\d+)\x00", restore_fence, text)

    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
