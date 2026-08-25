"""Persistent candidate pool for the five country-specific weekly reports."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from collectors.base import NewsItem


STORE_VERSION = 1
DEFAULT_STORE_PATH = (
    Path(__file__).parent / "data" / "country_insights" / "candidates.json"
)


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _item_key(item: NewsItem) -> str:
    if item.url:
        return f"url:{item.url.strip().casefold()}"
    title = re.sub(r"\W+", "", item.title).casefold()
    return f"title:{title[:160]}"


def serialize_item(item: NewsItem, collected_at: datetime) -> dict:
    payload = item.to_dict()
    payload.pop("id", None)
    payload["content"] = item.content
    payload["collected_at"] = collected_at.isoformat()
    return payload


def deserialize_item(payload: dict) -> tuple[NewsItem, datetime]:
    collected_at = _parse_datetime(payload.get("collected_at")) or datetime.now(
        timezone.utc
    )
    item = NewsItem(
        title=str(payload.get("title") or ""),
        url=str(payload.get("url") or ""),
        source=str(payload.get("source") or ""),
        category=str(payload.get("category") or "country_news"),
        published=_parse_datetime(payload.get("published")),
        summary=payload.get("summary"),
        content=payload.get("content"),
        author=payload.get("author"),
        tags=list(payload.get("tags") or []),
        score=float(payload.get("score") or 0.0),
        is_translated=bool(payload.get("is_translated", False)),
        image_url=payload.get("image_url"),
        organization=payload.get("organization"),
        country=payload.get("country"),
        source_priority=float(payload.get("source_priority") or 1.0),
        relevance_score=float(payload.get("relevance_score") or 0.0),
        freshness_days=(
            float(payload["freshness_days"])
            if payload.get("freshness_days") is not None
            else None
        ),
        title_en=payload.get("title_en"),
        summary_en=payload.get("summary_en"),
    )
    return item, collected_at


def load_store(path: Path = DEFAULT_STORE_PATH) -> list[tuple[NewsItem, datetime]]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if int(payload.get("version", 0)) != STORE_VERSION:
        raise ValueError(f"Unsupported country candidate store: {path}")
    return [deserialize_item(entry) for entry in payload.get("items", [])]


def merge_and_save(
    items: Iterable[NewsItem],
    *,
    path: Path = DEFAULT_STORE_PATH,
    collected_at: datetime | None = None,
    retention_days: int = 21,
    max_items: int = 5000,
) -> int:
    """Merge a daily collection into the pool and retain a short rolling history."""
    current = collected_at or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    cutoff = current.astimezone(timezone.utc) - timedelta(days=retention_days)

    merged: dict[str, tuple[NewsItem, datetime]] = {}
    for item, seen_at in load_store(path):
        if seen_at.astimezone(timezone.utc) >= cutoff:
            merged[_item_key(item)] = (item, seen_at)
    for item in items:
        if item.title and item.url:
            merged[_item_key(item)] = (item, current)

    def sort_timestamp(pair: tuple[NewsItem, datetime]) -> float:
        event_time = pair[0].published or pair[1]
        if event_time.tzinfo is None:
            event_time = event_time.replace(tzinfo=timezone.utc)
        return event_time.timestamp()

    ranked = sorted(merged.values(), key=sort_timestamp, reverse=True)[:max_items]
    payload = {
        "version": STORE_VERSION,
        "updated_at": current.isoformat(),
        "retention_days": retention_days,
        "max_items": max_items,
        "items": [serialize_item(item, seen_at) for item, seen_at in ranked],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return len(ranked)


def items_for_period(
    *,
    start,
    end,
    path: Path = DEFAULT_STORE_PATH,
    timezone_info,
) -> list[NewsItem]:
    """Read items whose publication (or collection) date overlaps a report period."""
    selected = []
    for item, collected_at in load_store(path):
        event_time = item.published or collected_at
        if event_time.tzinfo is None:
            event_time = event_time.replace(tzinfo=timezone.utc)
        if start <= event_time.astimezone(timezone_info).date() <= end:
            selected.append(item)
    return selected
