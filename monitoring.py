"""Sanitized monitoring receipts for the Seven-Country delivery pipeline."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


RECEIPT_SCHEMA_VERSION = 1
DEFAULT_RECEIPT_PATH = Path("output/monitor/receipt.json")


def utc_now_iso() -> str:
    """Return a stable UTC timestamp suitable for machine processing."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def new_run_receipt() -> dict[str, Any]:
    """Create the initial receipt before any potentially failing work starts."""
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "pipeline": "seven-country-daily",
        "recorded_at": utc_now_iso(),
        "run": {
            "id": os.environ.get("GITHUB_RUN_ID") or None,
            "attempt": _safe_int(os.environ.get("GITHUB_RUN_ATTEMPT")),
            "event": os.environ.get("GITHUB_EVENT_NAME") or "local",
        },
        "feishu_send": empty_feishu_receipt("pending"),
        "feishu_sends": [],
    }


def empty_feishu_receipt(status: str) -> dict[str, Any]:
    """Return the public, whitelisted shape used for every send outcome."""
    return {
        "request_started_at": None,
        "api_ack_at": None,
        "feishu_create_time": None,
        "http_status": None,
        "provider_code": None,
        "message_ref": None,
        "attempt_count": 0,
        "status": status,
        "error_code": None,
    }


def write_receipt_atomic(receipt: dict[str, Any], path: str | Path | None = None) -> Path:
    """Atomically persist the strictly sanitized receipt JSON."""
    target = Path(path or os.environ.get("MONITOR_RECEIPT_PATH") or DEFAULT_RECEIPT_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    receipt["recorded_at"] = utc_now_iso()
    temp = target.with_name(f".{target.name}.tmp-{os.getpid()}")
    temp.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temp, target)
    return target


def require_confirmed_delivery(receipt: dict[str, Any], required: bool) -> None:
    """Fail closed when a formal run has no Feishu API acknowledgement."""
    if not required:
        return
    status = receipt.get("feishu_send", {}).get("status")
    if status != "acknowledged":
        raise RuntimeError(
            "Required Feishu delivery was not acknowledged "
            f"(sanitized status: {status or 'missing'})"
        )


def _safe_int(value: str | None) -> int | None:
    try:
        return int(value) if value else None
    except (TypeError, ValueError):
        return None
