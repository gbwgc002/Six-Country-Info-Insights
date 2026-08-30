#!/usr/bin/env python3
"""Aggregate one sanitized delivery receipt into the monitor-data feed."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


BEIJING = ZoneInfo("Asia/Shanghai")
SCHEMA_VERSION = 1
MAX_RECORDS = 180
ON_TIME_MINUTES = 15
SAFE_ERROR_CODES = {
    "auth_token_failed",
    "response_unreadable",
    "provider_rejected",
    "transport_error",
}
ERROR_SUMMARIES = {
    "auth_token_failed": "飞书鉴权失败，发送请求未发出。",
    "response_unreadable": "飞书已返回响应，但响应无法解析，送达状态未知。",
    "provider_rejected": "飞书接口明确拒绝了发送请求。",
    "transport_error": "发送请求发生网络异常，送达状态未知。",
}


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def timestamp_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def provider_create_time(value: Any) -> str | None:
    """Normalize Feishu's epoch-millisecond create_time when available."""
    if value in (None, ""):
        return None
    try:
        raw = int(value)
    except (TypeError, ValueError):
        return timestamp_iso(parse_timestamp(value))
    if raw > 10_000_000_000:
        raw /= 1000
    try:
        return timestamp_iso(datetime.fromtimestamp(raw, tz=timezone.utc))
    except (OverflowError, OSError, ValueError):
        return None


def planned_time(workflow_run: dict[str, Any]) -> datetime | None:
    if workflow_run.get("event") != "schedule":
        return None
    created = parse_timestamp(workflow_run.get("created_at"))
    if created is None:
        return None
    local = created.astimezone(BEIJING)
    planned_local = datetime(local.year, local.month, local.day, 6, 25, tzinfo=BEIJING)
    if local < planned_local:
        planned_local -= timedelta(days=1)
    return planned_local.astimezone(timezone.utc)


def safe_send(receipt: dict[str, Any]) -> dict[str, Any]:
    raw = receipt.get("feishu_send")
    raw = raw if isinstance(raw, dict) else {}
    return {
        "request_started_at": timestamp_iso(parse_timestamp(raw.get("request_started_at"))),
        "api_ack_at": timestamp_iso(parse_timestamp(raw.get("api_ack_at"))),
        "feishu_create_time": provider_create_time(raw.get("feishu_create_time")),
        "http_status": raw.get("http_status") if isinstance(raw.get("http_status"), int) else None,
        "provider_code": raw.get("provider_code")
        if isinstance(raw.get("provider_code"), (int, float, str))
        else None,
        "message_ref": raw.get("message_ref")
        if isinstance(raw.get("message_ref"), str)
        and raw.get("message_ref", "").startswith("sha256:")
        else None,
        "attempt_count": raw.get("attempt_count")
        if isinstance(raw.get("attempt_count"), int)
        else 0,
        "status": raw.get("status")
        if raw.get("status")
        in {"pending", "sending", "acknowledged", "failed", "unknown", "not_sent", "not_configured"}
        else "unconfirmed",
        "error_code": raw.get("error_code")
        if raw.get("error_code") in SAFE_ERROR_CODES
        else None,
    }


def build_record(receipt: dict[str, Any], workflow_run: dict[str, Any]) -> dict[str, Any]:
    planned = planned_time(workflow_run)
    created = parse_timestamp(workflow_run.get("created_at"))
    started = parse_timestamp(workflow_run.get("run_started_at"))
    completed = parse_timestamp(workflow_run.get("updated_at"))
    send = safe_send(receipt)
    acknowledged = parse_timestamp(send.get("api_ack_at"))
    delay_minutes = (
        round((acknowledged - planned).total_seconds() / 60, 2)
        if planned and acknowledged
        else None
    )

    conclusion = workflow_run.get("conclusion") or "unknown"
    errors = []
    acknowledged_delivery = send["status"] == "acknowledged"
    if acknowledged_delivery:
        status = "delayed" if delay_minutes is not None and delay_minutes > ON_TIME_MINUTES else "normal"
        if conclusion != "success":
            errors.append(
                {
                    "stage": "post_delivery_workflow",
                    "code": "workflow_failed_after_delivery",
                    "summary": "飞书已确认消息，但工作流后续步骤未成功完成。",
                }
            )
    else:
        status = "failed" if send["status"] in {"failed", "not_sent", "not_configured"} else "unconfirmed"
        errors.append(
            {
                "stage": "feishu_send",
                "code": send["error_code"] or f"delivery_{send['status']}",
                "summary": ERROR_SUMMARIES.get(
                    send["error_code"], "没有取得飞书接口确认回执。"
                ),
            }
        )
        if conclusion != "success":
            errors.append(
                {
                    "stage": "workflow",
                    "code": "workflow_not_successful",
                    "summary": "工作流未成功完成，请进入 GitHub Actions 查看受控日志。",
                }
            )

    run_id = str(workflow_run.get("id") or receipt.get("run", {}).get("id") or "unknown")
    attempt = workflow_run.get("run_attempt") or receipt.get("run", {}).get("attempt") or 1
    local_reference = (planned or created or started)
    return {
        "id": f"{run_id}-{attempt}",
        "run_id": run_id,
        "run_attempt": attempt,
        "report_date": local_reference.astimezone(BEIJING).date().isoformat()
        if local_reference
        else None,
        "trigger": workflow_run.get("event") or receipt.get("run", {}).get("event"),
        "status": status,
        "workflow_conclusion": conclusion,
        "planned_at": timestamp_iso(planned),
        "workflow_created_at": timestamp_iso(created),
        "workflow_started_at": timestamp_iso(started),
        "push_requested_at": send["request_started_at"],
        "feishu_api_acked_at": send["api_ack_at"],
        "feishu_created_at": send["feishu_create_time"],
        "workflow_completed_at": timestamp_iso(completed),
        "delay_minutes": delay_minutes,
        "feishu_send": {
            "request_started_at": send["request_started_at"],
            "api_ack_at": send["api_ack_at"],
            "feishu_create_time": send["feishu_create_time"],
            "attempt_count": send["attempt_count"],
            "status": send["status"],
            "error_code": send["error_code"],
        },
        "errors": errors,
        "run_url": workflow_run.get("html_url"),
    }


def load_json(path: Path, fallback: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return fallback
    return value if isinstance(value, dict) else fallback


def write_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temp, path)


def aggregate(receipt_path: Path, event_path: Path, output_path: Path) -> dict[str, Any]:
    event = load_json(event_path, {})
    workflow_run = event.get("workflow_run") if isinstance(event.get("workflow_run"), dict) else event
    receipt = load_json(receipt_path, {})
    record = build_record(receipt, workflow_run)

    existing = load_json(output_path, {"records": []})
    records = existing.get("records") if isinstance(existing.get("records"), list) else []
    records = [item for item in records if isinstance(item, dict) and item.get("id") != record["id"]]
    records.append(record)
    records.sort(
        key=lambda item: item.get("workflow_created_at") or item.get("planned_at") or "",
        reverse=True,
    )
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": timestamp_iso(datetime.now(timezone.utc)),
        "timezone": "Asia/Shanghai",
        "schedule": "06:25",
        "acknowledgement_definition": "Feishu API accepted; not member read confirmation",
        "records": records[:MAX_RECORDS],
    }
    write_atomic(output_path, payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--workflow-run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    aggregate(args.receipt, args.workflow_run, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
