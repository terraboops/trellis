"""Structured logging setup for Trellis.

Detects runtime environment and configures the appropriate log format:
  - k8s / Loki / VictoriaMetrics: JSON Lines to stdout
  - systemd / journald: syslog priority prefix + JSON Lines
  - terminal: standard Python text format (unchanged)

Pass human_readable=<format_name> to switch to LLM-narrated output.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone

# Standard LogRecord attributes that shouldn't be re-emitted as extras.
_STDLIB_ATTRS = frozenset(
    (
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "message",
        "taskName",
    )
)

# Syslog priority codes mapped from Python log levels.
_SYSLOG_PRI = {
    logging.CRITICAL: 2,  # CRIT
    logging.ERROR: 3,  # ERR
    logging.WARNING: 4,  # WARNING
    logging.INFO: 6,  # INFO
    logging.DEBUG: 7,  # DEBUG
}


def detect_runtime() -> str:
    """Return 'k8s', 'systemd', or 'plain'."""
    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        return "k8s"
    if os.environ.get("INVOCATION_ID") or os.environ.get("JOURNAL_STREAM"):
        return "systemd"
    return "plain"


class JSONLinesFormatter(logging.Formatter):
    """Loki / VictoriaMetrics / k8s-compatible JSON Lines formatter."""

    def format(self, record: logging.LogRecord) -> str:
        record.getMessage()  # populate record.message
        doc: dict = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            doc["exc"] = self.formatException(record.exc_info)
        if record.stack_info:
            doc["stack"] = record.stack_info
        # Attach any extra fields the caller passed in.
        for key, val in record.__dict__.items():
            if key not in _STDLIB_ATTRS and not key.startswith("_"):
                doc[key] = val
        return json.dumps(doc, default=str)


class SystemdFormatter(logging.Formatter):
    """Journald-compatible formatter: syslog-priority prefix + JSON Lines.

    When stdout is connected to journald (JOURNAL_STREAM is set), the
    ``<N>`` prefix is stripped and used as the log level; the JSON payload
    is then available as MESSAGE in the journal.
    """

    def format(self, record: logging.LogRecord) -> str:
        pri = _SYSLOG_PRI.get(record.levelno, 6)
        payload = JSONLinesFormatter().format(record)
        return f"<{pri}>{payload}"


def configure_logging(human_readable: str | None = None) -> None:
    """Configure the root logger for the detected runtime.

    Args:
        human_readable: Format name for LLM narration (e.g. "explanatory").
                        None means structured JSON to stdout.
    """
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # Remove any handlers added by basicConfig earlier.
    for h in list(root.handlers):
        root.removeHandler(h)

    if human_readable is not None:
        from trellis.human_log import HumanReadableHandler

        handler = HumanReadableHandler(format_name=human_readable)
        handler.setLevel(logging.DEBUG)
        root.addHandler(handler)
        return

    runtime = detect_runtime()

    if runtime == "systemd":
        formatter: logging.Formatter = SystemdFormatter()
    else:
        # k8s and plain terminal: JSON Lines to stdout (Loki/VictoriaMetrics-compatible)
        formatter = JSONLinesFormatter()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root.addHandler(handler)
