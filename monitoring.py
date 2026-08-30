"""Strictly sanitized delivery receipts shared by all production pushes."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

RECEIPT_SCHEMA_VERSION = 2
DEFAULT_RECEIPT_PATH = Path("output/monitor/receipt.json")

# Public target labels live only here. Receipts never include chat IDs, raw
# message IDs, provider messages, or report content.
TARGETS: dict[str, dict[str, Any]] = {
    "seven-country-daily": {"label": "软件用研 · 七国日报", "role": "primary"},
    "ai-insights-weekly": {"label": "软件用研 · AI洞察周报", "role": "primary"},
    "ux-combined-weekly": {"label": "SW用户体验部 · AI设计与洞察周报", "role": "primary"},
    "country-weekly-india": {"label": "印度站点 · 用研周报", "role": "primary"},
    "country-weekly-indonesia": {"label": "印尼站点 · 用研周报", "role": "primary"},
    "country-weekly-nigeria": {"label": "尼日利亚站点 · 用研周报", "role": "primary"},
    "country-weekly-pakistan": {"label": "巴基斯坦站点 · 用研周报", "role": "primary"},
    "country-weekly-bangladesh": {"label": "孟加拉站点 · 用研周报", "role": "primary"},
    "ux-combined-alert": {"label": "AI设计作业测试群 · 异常提醒", "role": "alert"},
}
DELIVERY_STATUSES = {
    "pending", "acknowledged", "failed", "unknown", "not_attempted",
    "blocked", "already_delivered",
}
SUCCESS_STATUSES = {"acknowledged", "already_delivered"}
SAFE_ERROR_CODES = {
    "auth_token_failed", "response_unreadable", "provider_rejected",
    "transport_error", "credentials_missing", "destination_missing",
    "content_missing", "archive_failed", "generation_failed",
    "prerequisite_failed", "upstream_failed", "feed_invalid",
    "state_save_failed", "delivery_not_attempted",
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def empty_feishu_receipt(status: str) -> dict[str, Any]:
    """Return the legacy-compatible, whitelisted provider receipt shape."""
    return {
        "request_started_at": None,
        "api_ack_at": None,
        "feishu_create_time": None,
        "http_status": None,
        "provider_code": None,
        "message_ref": None,
        "attempt_count": 0,
        "status": _normalize_status(status),
        "error_code": None,
    }


def new_delivery(target_key: str, *, required: bool = True) -> dict[str, Any]:
    target = _target(target_key)
    return {
        "target_key": target_key,
        "target_label": target["label"],
        "role": target["role"],
        "required": bool(required and target["role"] == "primary"),
        **empty_feishu_receipt("pending"),
    }


def new_run_receipt(
    pipeline: str = "seven-country-daily",
    target_keys: Iterable[str] | None = None,
    *,
    planned_at: str | None = None,
    destination_tier: str | None = None,
) -> dict[str, Any]:
    """Create a schema-v2 receipt before potentially failing work starts."""
    deliveries = [new_delivery(key) for key in list(target_keys or ["seven-country-daily"])]
    legacy = _send_fields(deliveries[0]) if deliveries else empty_feishu_receipt("pending")
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "pipeline": _safe_pipeline(pipeline),
        "destination_tier": _safe_destination_tier(
            destination_tier or os.environ.get("MONITOR_DESTINATION_TIER")
        ),
        "planned_at": _safe_timestamp(planned_at),
        "recorded_at": utc_now_iso(),
        "run": {
            "id": _safe_run_id(os.environ.get("GITHUB_RUN_ID")),
            "attempt": _safe_int(os.environ.get("GITHUB_RUN_ATTEMPT")),
            "event": _safe_event(os.environ.get("GITHUB_EVENT_NAME") or "local"),
        },
        "deliveries": deliveries,
        # Schema-v1 compatibility during the migration window.
        "feishu_send": legacy,
        "feishu_sends": [],
    }


def record_delivery(
    receipt: dict[str, Any],
    target_key: str,
    send_receipt: dict[str, Any] | None = None,
    *,
    status: str | None = None,
    error_code: str | None = None,
    required: bool | None = None,
) -> dict[str, Any]:
    """Upsert one whitelisted delivery without accepting provider text."""
    target = _target(target_key)
    deliveries = receipt.setdefault("deliveries", [])
    if not isinstance(deliveries, list):
        deliveries = receipt["deliveries"] = []
    delivery = next(
        (item for item in deliveries if isinstance(item, dict) and item.get("target_key") == target_key),
        None,
    )
    if delivery is None:
        delivery = new_delivery(target_key, required=required if required is not None else True)
        deliveries.append(delivery)
    if isinstance(send_receipt, dict):
        delivery.update(_sanitize_send(send_receipt))
    elif send_receipt is not None:
        delivery.update(_sanitize_send({}))
    if status is not None:
        delivery["status"] = _normalize_status(status)
    if error_code is not None:
        delivery["error_code"] = error_code if error_code in SAFE_ERROR_CODES else None
    if required is not None:
        delivery["required"] = bool(required and target["role"] == "primary")
    delivery.update({
        "target_key": target_key,
        "target_label": target["label"],
        "role": target["role"],
    })
    primary = next(
        (item for item in deliveries if isinstance(item, dict) and item.get("role") == "primary"),
        delivery,
    )
    receipt["feishu_send"] = _send_fields(primary)
    receipt.setdefault("feishu_sends", []).append(_send_fields(delivery))
    return delivery


def write_receipt_atomic(receipt: dict[str, Any], path: str | Path | None = None) -> Path:
    """Atomically write only the schema's allowlisted fields."""
    target = Path(path or os.environ.get("MONITOR_RECEIPT_PATH") or DEFAULT_RECEIPT_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    receipt["recorded_at"] = utc_now_iso()
    clean = sanitize_receipt(receipt)
    receipt.clear()
    receipt.update(clean)
    temp = target.with_name(f".{target.name}.tmp-{os.getpid()}")
    temp.write_text(
        json.dumps(clean, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temp, target)
    return target


def sanitize_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    """Build a strict public representation, accepting v1 and v2 inputs."""
    raw_deliveries = receipt.get("deliveries")
    deliveries: list[dict[str, Any]] = []
    if isinstance(raw_deliveries, list):
        seen: set[str] = set()
        for raw in raw_deliveries:
            if not isinstance(raw, dict) or raw.get("target_key") not in TARGETS:
                continue
            key = raw["target_key"]
            if key in seen:
                continue
            seen.add(key)
            target = TARGETS[key]
            deliveries.append({
                "target_key": key,
                "target_label": target["label"],
                "role": target["role"],
                "required": bool(raw.get("required") and target["role"] == "primary"),
                **_sanitize_send(raw),
            })
    legacy_raw = receipt.get("feishu_send")
    if (
        deliveries
        and isinstance(legacy_raw, dict)
        and deliveries[0]["status"] == "pending"
        and _normalize_status(legacy_raw.get("status")) != "pending"
    ):
        # Old call sites updated ``feishu_send`` in place. Honor that shape
        # until every external consumer has migrated to ``deliveries``.
        deliveries[0].update(_sanitize_send(legacy_raw))
    if not deliveries and isinstance(receipt.get("feishu_send"), dict):
        deliveries = [{
            **new_delivery("seven-country-daily"),
            **_sanitize_send(receipt["feishu_send"]),
        }]
    legacy = _send_fields(next((item for item in deliveries if item["role"] == "primary"), {}))
    raw_run = receipt.get("run") if isinstance(receipt.get("run"), dict) else {}
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "pipeline": _safe_pipeline(receipt.get("pipeline")),
        "destination_tier": _safe_destination_tier(
            receipt.get("destination_tier")
        ),
        "planned_at": _safe_timestamp(receipt.get("planned_at")),
        "recorded_at": _safe_timestamp(receipt.get("recorded_at")) or utc_now_iso(),
        "run": {
            "id": _safe_run_id(raw_run.get("id")),
            "attempt": _safe_int(raw_run.get("attempt")),
            "event": _safe_event(raw_run.get("event")),
        },
        "deliveries": deliveries,
        "feishu_send": legacy,
        "feishu_sends": [_send_fields(item) for item in deliveries],
    }


def require_all_required_primary(receipt: dict[str, Any], required: bool) -> None:
    """Fail closed unless every required formal target has a safe success status."""
    if not required:
        return
    clean = sanitize_receipt(receipt)
    required_primary = [
        item for item in clean["deliveries"]
        if item["role"] == "primary" and item["required"]
    ]
    unsuccessful = [
        item
        for item in required_primary
        if item["status"] not in SUCCESS_STATUSES
        or (item["status"] == "acknowledged" and not item.get("api_ack_at"))
    ]
    if not required_primary or unsuccessful:
        summary = ", ".join(
            f"{item['target_key']}={item['status']}" for item in unsuccessful
        ) or "no required primary deliveries declared"
        raise RuntimeError(f"Required formal deliveries were not acknowledged ({summary})")


def require_confirmed_delivery(receipt: dict[str, Any], required: bool) -> None:
    """Backward-compatible alias for the original one-target call site."""
    require_all_required_primary(receipt, required)


def _sanitize_send(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "request_started_at": _safe_timestamp(raw.get("request_started_at")),
        "api_ack_at": _safe_timestamp(raw.get("api_ack_at")),
        "feishu_create_time": _safe_create_time(raw.get("feishu_create_time")),
        "http_status": raw.get("http_status") if isinstance(raw.get("http_status"), int) else None,
        "provider_code": _safe_provider_code(raw.get("provider_code")),
        "message_ref": raw.get("message_ref")
        if isinstance(raw.get("message_ref"), str)
        and raw["message_ref"].startswith("sha256:")
        and len(raw["message_ref"]) <= 80 else None,
        "attempt_count": max(0, _safe_int(raw.get("attempt_count")) or 0),
        "status": _normalize_status(raw.get("status")),
        "error_code": raw.get("error_code") if raw.get("error_code") in SAFE_ERROR_CODES else None,
    }


def _send_fields(value: dict[str, Any]) -> dict[str, Any]:
    return _sanitize_send(value if isinstance(value, dict) else {})


def _target(key: str) -> dict[str, Any]:
    if key not in TARGETS:
        raise ValueError(f"Unknown monitoring target: {key}")
    return TARGETS[key]


def _normalize_status(value: Any) -> str:
    aliases = {
        "not_sent": "not_attempted", "not_configured": "blocked",
        "sending": "pending", "unconfirmed": "unknown",
    }
    normalized = aliases.get(value, value)
    return normalized if normalized in DELIVERY_STATUSES else "unknown"


def _safe_timestamp(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip() or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _safe_create_time(value: Any) -> str | int | None:
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str) and value.isdigit() and len(value) <= 16:
        return value
    return _safe_timestamp(value)


def _safe_provider_code(value: Any) -> str | int | float | None:
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str) and len(value) <= 32 and value.replace("_", "").replace("-", "").isalnum():
        return value
    return None


def _safe_pipeline(value: Any) -> str:
    allowed = {
        "seven-country-daily", "ai-insights-weekly",
        "ux-combined-weekly", "country-weekly",
    }
    return value if isinstance(value, str) and value in allowed else "seven-country-daily"


def _safe_destination_tier(value: Any) -> str:
    return value if value in {"production", "test", "custom"} else "custom"


def _safe_event(value: Any) -> str:
    allowed = {"schedule", "workflow_dispatch", "workflow_run", "local"}
    return value if isinstance(value, str) and value in allowed else "local"


def _safe_run_id(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text.isdigit() and len(text) <= 32 else None


def _safe_int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None
