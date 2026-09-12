#!/usr/bin/env python3
"""Re-emit a captured command's output as GitHub Actions check annotations.

Actions log blobs are not always retrievable (the results endpoint can be
unreachable from a sandboxed network), but the check-runs *annotations* API is.
So a failing CI step pipes its output through this script and the tail of that
output becomes readable without the logs.

    alembic upgrade head 2>&1 | tee /tmp/out.log
    python scripts/ci_annotate.py --file /tmp/out.log --title "alembic upgrade"

GitHub workflow commands need %-escaping, and a single annotation is truncated
by the UI, so the tail is split into numbered chunks.
"""
from __future__ import annotations

import argparse
import pathlib
import sys

MAX_MESSAGE = 1400  # stay well under the annotation limit


def escape(text: str) -> str:
    """Escape for a `::error::` workflow command (order matters: % first)."""
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def chunks(lines: list[str], size: int) -> list[list[str]]:
    return [lines[i:i + size] for i in range(0, len(lines), size)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", required=True, help="captured command output")
    parser.add_argument("--title", default="step failed")
    parser.add_argument("--level", default="error", choices=["error", "warning", "notice"])
    parser.add_argument("--lines", type=int, default=60, help="how many trailing lines to keep")
    parser.add_argument("--chunk", type=int, default=25, help="lines per annotation")
    args = parser.parse_args()

    path = pathlib.Path(args.file)
    if not path.exists():
        print(f"::{args.level} title={escape(args.title)}::no output captured at {path}")
        return 0

    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-args.lines:]
    # Prefer the informative lines: tracebacks, SQL errors, and the failing SQL.
    interesting = [
        ln for ln in lines
        if any(k in ln for k in ("Error", "error", "ERROR", "Exception", "Traceback",
                                 "File \"", "DETAIL", "HINT", "LINE ", "sqlalchemy",
                                 "alembic", "asyncpg", "[SQL", "[parameters"))
    ]
    payload = (interesting or lines)[-args.lines:]

    # Emit the tail first: the last traceback frame is the one that names the
    # failing line, and a single annotation is truncated by the UI.
    parts = list(reversed(chunks(payload, args.chunk)))
    total = len(parts)
    for index, part in enumerate(parts, start=1):
        message = "\n".join(part)[:MAX_MESSAGE]
        title = f"{args.title} [{index}/{total} tail-first]" if total > 1 else args.title
        print(f"::{args.level} title={escape(title)}::{escape(message)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
