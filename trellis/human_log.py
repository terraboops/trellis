"""LLM-narrated human-readable log handler for Trellis.

Records are batched and sent to Claude, which decides what (if anything)
to print.  Formats are named system-prompt specs — built-in ones live
here; project-level overrides go in {project_root}/log-formats/{name}.md.

Usage:
    handler = HumanReadableHandler(format_name="explanatory")
    logging.getLogger().addHandler(handler)
"""

from __future__ import annotations

import logging
import queue
import sys
import threading
import time
from typing import NamedTuple

# ── Built-in format definitions ───────────────────────────────────────────────
# Each entry is a system prompt.  The LLM receives a batch of log lines and
# should output natural-language narration (or nothing, if there's nothing
# worth saying).

BUILTIN_FORMATS: dict[str, str] = {
    "explanatory": """\
You are a concise, senior-engineer log narrator for a multi-agent AI pipeline called Trellis.
You receive batches of structured log lines.  Your job is to write short, plain-English
explanations of what is happening — WITHOUT being overwhelming.

Rules:
- Batch similar or repeated lines into a single sentence.  Never emit one sentence per line.
- Skip lines that are routine noise (HTTP 200s, heartbeats, pool ticks) unless something changed.
- Skip lines you have already explained in recent output (they will be marked [seen]).
- If everything is routine and unchanged, output nothing at all.
- When something important happens (agent starts/stops, error, cost milestone, phase change,
  sandbox failure, pool crash), explain it clearly and note any risk.
- Be cautious about errors: explain what went wrong and what the user might want to do.
- Max ~3 sentences per batch.  No bullet lists.  No markdown headers.
- Write for someone watching a terminal who is comfortable with software but not reading every log.
""",
    "terse": """\
You are a terse log narrator for Trellis.  One short sentence max per batch.
Only output when something meaningfully changed (agent state, error, phase).
Skip all routine noise.  No markdown.
""",
    "debug": """\
You are a detailed log narrator for Trellis.  Explain every meaningful log line,
including internal state changes, timing, and potential issues.  Group related lines.
Use technical language appropriate for a developer debugging the system.
""",
    "ops": """\
You are an on-call SRE narrating Trellis logs.  Focus on:
- Errors and failures (always explain these)
- Resource pressure (queue depth, budget, timeouts)
- Agent activity (start/stop, cost incurred)
Skip normal info noise entirely.  Flag anything that could page someone.
No markdown.  Be direct.
""",
}


# ── Format loading ─────────────────────────────────────────────────────────────


def load_format(name: str) -> str:
    """Return the system prompt for the given format name.

    Checks project-level log-formats/{name}.md first, then built-ins.
    Raises ValueError if not found.
    """
    # Project override
    try:
        from trellis.config import find_project_root

        override = find_project_root() / "log-formats" / f"{name}.md"
        if override.exists():
            return override.read_text().strip()
    except Exception:
        pass

    if name in BUILTIN_FORMATS:
        return BUILTIN_FORMATS[name]

    available = list(BUILTIN_FORMATS.keys())
    raise ValueError(
        f"Unknown log format '{name}'. "
        f"Built-ins: {available}. "
        f"Or create log-formats/{name}.md in your project root."
    )


def list_formats() -> dict[str, str]:
    """Return all available format names → first-line description."""
    result: dict[str, str] = {}

    # Built-ins
    for name, prompt in BUILTIN_FORMATS.items():
        first_line = prompt.strip().splitlines()[0] if prompt.strip() else ""
        result[name] = first_line

    # Project overrides
    try:
        from trellis.config import find_project_root

        fmt_dir = find_project_root() / "log-formats"
        if fmt_dir.is_dir():
            for f in sorted(fmt_dir.glob("*.md")):
                name = f.stem
                lines = f.read_text().strip().splitlines()
                result[name] = lines[0] if lines else "(custom)"
    except Exception:
        pass

    return result


# ── Log record ─────────────────────────────────────────────────────────────────


class _Record(NamedTuple):
    ts: str
    level: str
    logger: str
    msg: str
    seen: bool  # True if this exact message was recently narrated


# ── Background narration thread ────────────────────────────────────────────────

_BATCH_SECONDS = 3.0  # flush every N seconds
_MAX_BATCH = 40  # or when N records accumulate
_RECENT_WINDOW = 120  # seconds to track "recently seen" messages
_RECENT_MAX = 200  # max entries in the seen-set


class _NarrationThread(threading.Thread):
    """Drains the record queue, batches, calls Claude, prints output."""

    def __init__(self, system_prompt: str) -> None:
        super().__init__(daemon=True, name="trellis-log-narrator")
        self._q: queue.Queue[_Record | None] = queue.Queue()
        self._system = system_prompt
        self._client = None
        self._recent: list[tuple[float, str]] = []  # (timestamp, msg_hash)
        self._lock = threading.Lock()

    def enqueue(self, record: _Record) -> None:
        self._q.put_nowait(record)

    def stop(self) -> None:
        self._q.put_nowait(None)

    def _get_client(self):
        if self._client is None:
            try:
                import anthropic

                self._client = anthropic.Anthropic()
            except Exception as e:
                print(
                    f"[trellis-log-narrator] Cannot initialise Anthropic client: {e}",
                    file=sys.stderr,
                )
        return self._client

    def _is_recent(self, msg: str) -> bool:
        """Return True if this exact message was narrated recently."""
        key = msg[:120]
        now = time.monotonic()
        with self._lock:
            # Expire old entries
            self._recent = [(t, k) for t, k in self._recent if now - t < _RECENT_WINDOW]
            return any(k == key for _, k in self._recent)

    def _mark_recent(self, msg: str) -> None:
        key = msg[:120]
        now = time.monotonic()
        with self._lock:
            self._recent.append((now, key))
            if len(self._recent) > _RECENT_MAX:
                self._recent = self._recent[-_RECENT_MAX:]

    def _drain(self) -> list[_Record]:
        records: list[_Record] = []
        deadline = time.monotonic() + _BATCH_SECONDS
        while len(records) < _MAX_BATCH:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                item = self._q.get(timeout=min(remaining, 0.2))
                if item is None:
                    records.append(None)  # type: ignore[arg-type]
                    break
                records.append(item)
            except queue.Empty:
                break
        return records

    def _narrate(self, records: list[_Record]) -> None:
        if not records:
            return
        client = self._get_client()
        if client is None:
            # Fallback: just print raw lines
            for r in records:
                print(f"[{r.level.upper():8}] {r.logger}: {r.msg}", flush=True)
            return

        lines = []
        for r in records:
            tag = " [seen]" if r.seen else ""
            lines.append(f"[{r.ts}] [{r.level.upper()}] {r.logger}: {r.msg}{tag}")
        user_msg = "\n".join(lines)

        try:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=300,
                system=self._system,
                messages=[{"role": "user", "content": user_msg}],
            )
            text = (response.content[0].text or "").strip()
            if text:
                print(text, flush=True)
                # Mark the core messages as recently seen
                for r in records:
                    self._mark_recent(r.msg)
        except Exception as e:
            # Degraded: print the batch as plain text
            print(f"[narrator error: {e}]", file=sys.stderr)
            for r in records:
                print(f"[{r.level.upper():8}] {r.logger}: {r.msg}", flush=True)

    def run(self) -> None:
        while True:
            records = self._drain()
            stop = any(r is None for r in records)
            real = [r for r in records if r is not None]
            if real:
                self._narrate(real)
            if stop:
                break


# ── Logging handler ────────────────────────────────────────────────────────────


class HumanReadableHandler(logging.Handler):
    """Logging handler that routes records to the LLM narration thread."""

    def __init__(self, format_name: str = "explanatory") -> None:
        super().__init__()
        system_prompt = load_format(format_name)
        self._thread = _NarrationThread(system_prompt)
        self._thread.start()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            if record.exc_info:
                msg += "\n" + self.formatException(record.exc_info)
            from datetime import datetime, timezone

            ts = datetime.fromtimestamp(record.created, tz=timezone.utc).strftime("%H:%M:%S")
            seen = self._thread._is_recent(msg)
            self._thread.enqueue(
                _Record(
                    ts=ts,
                    level=record.levelname.lower(),
                    logger=record.name,
                    msg=msg,
                    seen=seen,
                )
            )
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        self._thread.stop()
        self._thread.join(timeout=5)
        super().close()
