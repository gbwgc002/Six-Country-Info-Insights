#!/usr/bin/env python3
"""Aggregate sanitized multi-target receipts into the public monitor feed."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

BEIJING = ZoneInfo("Asia/Shanghai")
SCHEMA_VERSION = 2
MAX_RECORDS = 720
ON_TIME_MINUTES = 15

WORKFLOWS: dict[str, dict[str, Any]] = {
    "Seven-Country Info Insights": {
        "pipeline": "seven-country-daily",
        "targets": {"seven-country-daily": "软件用研 · 七国日报"},
        "schedule": {"weekday": None, "hour": 6, "minute": 25},
    },
    "AI Insights Weekly Publish": {
        "pipeline": "ai-insights-weekly",
        "targets": {"ai-insights-weekly": "软件用研 · AI洞察周报"},
        "schedule": {"weekday": 0, "hour": 16, "minute": 47},
    },
    "AI Design + Insights Weekly Push": {
        "pipeline": "ux-combined-weekly",
        "targets": {"ux-combined-weekly": "SW用户体验部 · AI设计与洞察周报"},
        "schedule": None,
    },
    "Five-Country Weekly Insights": {
        "pipeline": "country-weekly",
        "targets": {
            "country-weekly-india": "印度站点 · 用研周报",
            "country-weekly-indonesia": "印尼站点 · 用研周报",
            "country-weekly-nigeria": "尼日利亚站点 · 用研周报",
            "country-weekly-pakistan": "巴基斯坦站点 · 用研周报",
            "country-weekly-bangladesh": "孟加拉站点 · 用研周报",
        },
        "schedule": {"weekday": 0, "hour": 6, "minute": 25},
    },
}
SAFE_DELIVERY_STATUSES = {
    "pending", "acknowledged", "failed", "unknown", "not_attempted",
    "blocked", "already_delivered",
}
SAFE_ERROR_CODES = {
    "auth_token_failed", "response_unreadable", "provider_rejected",
    "transport_error", "credentials_missing", "destination_missing",
    "content_missing", "archive_failed", "generation_failed",
    "prerequisite_failed", "upstream_failed", "feed_invalid",
    "state_save_failed", "delivery_not_attempted", "receipt_missing",
    "delivery_missing", "delivery_unconfirmed", "workflow_not_successful",
    "workflow_failed_after_delivery",
}
ERROR_SUMMARIES = {
    "auth_token_failed": "飞书鉴权失败，发送请求未发出。",
    "response_unreadable": "飞书响应无法解析，确认状态未知。",
    "provider_rejected": "飞书接口拒绝了发送请求。",
    "transport_error": "发送请求发生网络异常，确认状态未知。",
    "credentials_missing": "正式推送凭据缺失。",
    "destination_missing": "正式推送目标配置缺失。",
    "content_missing": "正式推送内容未准备完成。",
    "archive_failed": "报告归档失败，正式消息未发送。",
    "generation_failed": "报告或卡片生成失败。",
    "prerequisite_failed": "正式推送的前置条件未满足。",
    "upstream_failed": "上游周报任务未成功，组合推送被阻断。",
    "feed_invalid": "设计资讯数据源未通过校验。",
    "state_save_failed": "正式消息已发送，但去重状态保存失败。",
    "delivery_not_attempted": "正式消息未尝试发送。",
    "receipt_missing": "工作流没有上传安全回执，无法确认推送结果。",
    "delivery_missing": "安全回执缺少该正式目标，无法确认推送结果。",
    "delivery_unconfirmed": "没有取得飞书接口确认回执。",
    "workflow_not_successful": "工作流未成功完成，请查看受控 Actions 日志。",
    "workflow_failed_after_delivery": "飞书已确认消息，但工作流后续步骤失败。",
}


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip() or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
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


def workflow_config(workflow_run: dict[str, Any], receipt: dict[str, Any]) -> dict[str, Any]:
    name = workflow_run.get("name")
    if name in WORKFLOWS:
        return WORKFLOWS[name]
    # Legacy events in tests and the existing data branch may omit name.
    pipeline = receipt.get("pipeline")
    return next(
        (value for value in WORKFLOWS.values() if value["pipeline"] == pipeline),
        WORKFLOWS["Seven-Country Info Insights"],
    )


def planned_time(
    workflow_run: dict[str, Any], receipt: dict[str, Any], config: dict[str, Any]
) -> datetime | None:
    # For the UX combined workflow, this is the upstream AI workflow completion
    # recorded by the producer. Its own creation time is a safe missing-artifact
    # fallback because workflow_run dispatch occurs at upstream completion.
    if config["pipeline"] == "ux-combined-weekly":
        return parse_timestamp(receipt.get("planned_at")) or parse_timestamp(
            workflow_run.get("created_at")
        )
    if workflow_run.get("event") != "schedule" or not config.get("schedule"):
        return None
    created = parse_timestamp(workflow_run.get("created_at"))
    if created is None:
        return None
    local = created.astimezone(BEIJING)
    schedule = config["schedule"]
    candidate = datetime(
        local.year, local.month, local.day,
        schedule["hour"], schedule["minute"], tzinfo=BEIJING,
    )
    weekday = schedule.get("weekday")
    if weekday is not None:
        candidate -= timedelta(days=(candidate.weekday() - weekday) % 7)
        if candidate > local:
            candidate -= timedelta(days=7)
    elif candidate > local:
        candidate -= timedelta(days=1)
    return candidate.astimezone(timezone.utc)


def safe_delivery(raw: dict[str, Any]) -> dict[str, Any]:
    status = raw.get("status")
    aliases = {"not_sent": "not_attempted", "not_configured": "blocked", "sending": "pending"}
    status = aliases.get(status, status)
    if status not in SAFE_DELIVERY_STATUSES:
        status = "unknown"
    error_code = raw.get("error_code")
    return {
        "request_started_at": timestamp_iso(parse_timestamp(raw.get("request_started_at"))),
        "api_ack_at": timestamp_iso(parse_timestamp(raw.get("api_ack_at"))),
        "feishu_create_time": provider_create_time(raw.get("feishu_create_time")),
        "attempt_count": raw.get("attempt_count") if isinstance(raw.get("attempt_count"), int) else 0,
        "status": status,
        "error_code": error_code if error_code in SAFE_ERROR_CODES else None,
    }


def receipt_deliveries(
    receipt: dict[str, Any], config: dict[str, Any]
) -> tuple[dict[str, dict[str, Any]], bool]:
    deliveries: dict[str, dict[str, Any]] = {}
    raw = receipt.get("deliveries")
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            key = item.get("target_key")
            if key in config["targets"] and item.get("role", "primary") == "primary":
                deliveries[key] = safe_delivery(item)
    # Schema v1 compatibility is intentionally limited to the daily pipeline.
    if not deliveries and config["pipeline"] == "seven-country-daily" and isinstance(
        receipt.get("feishu_send"), dict
    ):
        deliveries["seven-country-daily"] = safe_delivery(receipt["feishu_send"])
    if config["pipeline"] == "country-weekly" and isinstance(raw, list) and deliveries:
        # Countries are sent serially. Preserve the first unresolved target,
        # then mark every later untouched target as not attempted instead of
        # misreporting five independent "missing receipts".
        stopped = False
        for key in config["targets"]:
            item = deliveries.get(key)
            status = item.get("status") if item else None
            if stopped and status in {None, "pending", "unknown"}:
                deliveries[key] = {
                    "request_started_at": None,
                    "api_ack_at": None,
                    "feishu_create_time": None,
                    "attempt_count": 0,
                    "status": "not_attempted",
                    "error_code": "delivery_not_attempted",
                }
                continue
            if item is None or status not in {"acknowledged", "already_delivered"}:
                stopped = True
    return deliveries, bool(receipt)


def build_record(
    receipt: dict[str, Any],
    workflow_run: dict[str, Any],
    target_key: str,
    target_label: str,
    delivery: dict[str, Any] | None,
    *,
    receipt_present: bool,
) -> dict[str, Any]:
    config = workflow_config(workflow_run, receipt)
    planned = planned_time(workflow_run, receipt, config)
    created = parse_timestamp(workflow_run.get("created_at"))
    started = parse_timestamp(workflow_run.get("run_started_at"))
    completed = parse_timestamp(workflow_run.get("updated_at"))
    missing_code = "delivery_missing" if receipt_present else "receipt_missing"
    send = delivery or {
        "request_started_at": None, "api_ack_at": None,
        "feishu_create_time": None, "attempt_count": 0,
        "status": "unknown", "error_code": missing_code,
    }
    acknowledged = parse_timestamp(send.get("api_ack_at"))
    delay_minutes = (
        round((acknowledged - planned).total_seconds() / 60, 2)
        if planned and acknowledged else None
    )
    conclusion = workflow_run.get("conclusion") or "unknown"
    delivery_status = send["status"]
    errors: list[dict[str, str]] = []
    if delivery_status == "acknowledged":
        status = "delayed" if delay_minutes is not None and delay_minutes > ON_TIME_MINUTES else "normal"
    elif delivery_status == "already_delivered":
        status = "already_delivered"
    elif delivery_status in {"failed", "blocked", "not_attempted"}:
        status = delivery_status
    else:
        status = "unconfirmed"
    error_code = send.get("error_code")
    if delivery_status in {"acknowledged", "already_delivered"} and error_code:
        errors.append({
            "stage": "post_delivery_workflow",
            "code": error_code,
            "summary": ERROR_SUMMARIES.get(
                error_code, "正式消息已确认，但后续处理未完成。"
            ),
        })
    if delivery_status not in {"acknowledged", "already_delivered"}:
        code = error_code or missing_code
        errors.append({
            "stage": "formal_delivery",
            "code": code,
            "summary": ERROR_SUMMARIES.get(code, "没有取得飞书接口确认回执。"),
        })
    if conclusion != "success":
        code = (
            "workflow_failed_after_delivery"
            if delivery_status in {"acknowledged", "already_delivered"}
            else "workflow_not_successful"
        )
        errors.append({"stage": "workflow", "code": code, "summary": ERROR_SUMMARIES[code]})

    run_id = str(workflow_run.get("id") or receipt.get("run", {}).get("id") or "unknown")
    attempt = workflow_run.get("run_attempt") or receipt.get("run", {}).get("attempt") or 1
    local_reference = planned or created or started
    event_name = workflow_run.get("event") or receipt.get("run", {}).get("event")
    trigger_scope = {
        "schedule": "scheduled-production",
        "workflow_dispatch": "manual-production",
        "workflow_run": "upstream-production",
    }.get(event_name, "production")
    return {
        "id": f"{run_id}-{attempt}-{target_key}",
        "run_id": run_id,
        "run_attempt": attempt,
        "workflow_name": workflow_run.get("name"),
        "pipeline": config["pipeline"],
        "target_key": target_key,
        "target_label": target_label,
        "report_date": local_reference.astimezone(BEIJING).date().isoformat() if local_reference else None,
        "trigger": event_name,
        "trigger_scope": trigger_scope,
        "destination_tier": "production",
        "status": status,
        "delivery_status": delivery_status,
        "workflow_conclusion": conclusion,
        "planned_at": timestamp_iso(planned),
        "workflow_created_at": timestamp_iso(created),
        "workflow_started_at": timestamp_iso(started),
        "push_requested_at": send["request_started_at"],
        "feishu_api_acked_at": send["api_ack_at"],
        "feishu_created_at": send["feishu_create_time"],
        "workflow_completed_at": timestamp_iso(completed),
        "delay_minutes": delay_minutes,
        "feishu_send": send,
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


def normalize_existing_record(item: dict[str, Any]) -> dict[str, Any]:
    """Add v2 target identity to historical Seven-Country feed records."""
    if item.get("target_key"):
        return item
    normalized = dict(item)
    normalized.update({
        "workflow_name": normalized.get("workflow_name") or "Seven-Country Info Insights",
        "pipeline": normalized.get("pipeline") or "seven-country-daily",
        "target_key": "seven-country-daily",
        "target_label": "软件用研 · 七国日报",
        "delivery_status": normalized.get("delivery_status")
        or (normalized.get("feishu_send") or {}).get("status")
        or "unknown",
    })
    return normalized


def aggregate(receipt_path: Path, event_path: Path, output_path: Path) -> dict[str, Any]:
    event = load_json(event_path, {})
    workflow_run = event.get("workflow_run") if isinstance(event.get("workflow_run"), dict) else event
    receipt = load_json(receipt_path, {})
    config = workflow_config(workflow_run, receipt)
    event_name = workflow_run.get("event")
    destination_tier = receipt.get("destination_tier")
    if event_name == "workflow_dispatch" and (
        not receipt or destination_tier != "production"
    ):
        # Missing manual receipts are ambiguous, while test/custom manual runs
        # must never enter the formal-delivery feed.
        existing = load_json(output_path, {})
        if existing:
            return existing
        payload = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": timestamp_iso(datetime.now(timezone.utc)),
            "timezone": "Asia/Shanghai",
            "acknowledgement_definition": "Feishu API accepted; not member read confirmation",
            "targets": [],
            "records": [],
        }
        write_atomic(output_path, payload)
        return payload
    if event_name == "workflow_dispatch" and isinstance(receipt.get("deliveries"), list):
        declared_primary = {
            item.get("target_key")
            for item in receipt["deliveries"]
            if isinstance(item, dict) and item.get("role", "primary") == "primary"
        }
        selected_targets = {
            key: label
            for key, label in config["targets"].items()
            if key in declared_primary
        }
        if selected_targets:
            config = {**config, "targets": selected_targets}
    deliveries, receipt_present = receipt_deliveries(receipt, config)
    new_records = [
        build_record(
            receipt, workflow_run, key, label, deliveries.get(key),
            receipt_present=receipt_present,
        )
        for key, label in config["targets"].items()
    ]
    existing = load_json(output_path, {"records": []})
    records = existing.get("records") if isinstance(existing.get("records"), list) else []
    records = [normalize_existing_record(item) for item in records if isinstance(item, dict)]
    new_ids = {record["id"] for record in new_records}
    records = [item for item in records if item.get("id") not in new_ids]
    records.extend(new_records)
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
        "targets": [
            {
                "target_key": key,
                "target_label": label,
                "pipeline": workflow["pipeline"],
            }
            for workflow in WORKFLOWS.values()
            for key, label in workflow["targets"].items()
        ],
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
