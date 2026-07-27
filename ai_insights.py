#!/usr/bin/env python3
"""Independent daily-collection / weekly-publish pipeline for AI Insights."""

from __future__ import annotations

import argparse
import asyncio
import os
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv

load_dotenv()

from collectors import NewsItem, collect_all_rss, collect_arxiv
from processors.ai_insights_summarizer import (
    AIInsightsSummarizer,
    ScoredInsight,
    extract_urls,
)
from processors.deduper import deduplicate_items
from publishers.feishu_publisher import FeishuPublisher

DEFAULT_CONFIG = Path(__file__).parent / "config" / "ai_insights_sources.yaml"


@dataclass(frozen=True)
class WeekPeriod:
    start: date
    end: date

    @property
    def label(self) -> str:
        return f"{self.start:%Y.%m.%d}–{self.end:%m.%d}"

    @property
    def title(self) -> str:
        return f"AI洞察资讯周报｜{self.label}"


def load_config(path: Path = DEFAULT_CONFIG) -> dict:
    with path.open("r", encoding="utf-8") as file:
        return yaml.safe_load(file)


def get_week_period(
    now: datetime | None = None,
    timezone_name: str = "Asia/Shanghai",
    previous: bool = False,
) -> WeekPeriod:
    zone = ZoneInfo(timezone_name)
    current = now or datetime.now(tz=zone)
    if current.tzinfo is None:
        current = current.replace(tzinfo=zone)
    local_date = current.astimezone(zone).date()
    monday = local_date - timedelta(days=local_date.weekday())
    if previous:
        monday -= timedelta(days=7)
    return WeekPeriod(start=monday, end=monday + timedelta(days=6))


def _all_rss_sources(config: dict) -> dict:
    """Merge only the AI pipeline's two RSS sections."""
    return {
        **config.get("rss_sources", {}),
        **config.get("rss_major_sources", {}),
    }


def _source_metadata(config: dict) -> dict[str, dict]:
    metadata = {}
    for source in _all_rss_sources(config).values():
        metadata[source["name"]] = {
            "source_kind": source.get("source_kind", "media"),
            "source_tier": source.get("source_tier", 2),
        }
    metadata["arXiv"] = {"source_kind": "research", "source_tier": 1}
    return metadata


def _chat_ids(config: dict) -> list[str]:
    bot_config = config.get("publishers", {}).get("feishu_bot", {})
    raw = bot_config.get("chat_id") or os.environ.get("FEISHU_BOT_CHAT_ID", "")
    return [chat_id.strip() for chat_id in raw.split(",") if chat_id.strip()]


async def collect_sources(config: dict) -> list[NewsItem]:
    tasks = [collect_all_rss(_all_rss_sources(config))]
    arxiv_config = config.get("arxiv", {})
    if arxiv_config.get("enabled", False) and collect_arxiv:
        tasks.append(collect_arxiv(arxiv_config))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    items: list[NewsItem] = []
    for result in results:
        if isinstance(result, Exception):
            print(f"Collector error: {result}")
        else:
            items.extend(result)
    return items


def filter_recent_candidates(
    items: list[NewsItem],
    lookback_hours: int = 48,
    max_raw_items: int = 80,
    now: datetime | None = None,
) -> list[NewsItem]:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    cutoff = current.astimezone(timezone.utc) - timedelta(hours=lookback_hours)

    recent = []
    for item in deduplicate_items(items):
        if not item.url or not item.title:
            continue
        if item.published:
            published = item.published
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
            if published.astimezone(timezone.utc) < cutoff:
                continue
        recent.append(item)

    recent.sort(
        key=lambda item: item.published or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return recent[:max_raw_items]


def _service_account_available() -> bool:
    if os.environ.get("GOOGLE_SA_JSON"):
        return True
    if os.environ.get("GOOGLE_SA_FILE"):
        return Path(os.environ["GOOGLE_SA_FILE"]).exists()
    return any(Path(__file__).parent.glob("*-sa-*.json"))


def create_summarizer() -> AIInsightsSummarizer:
    service_account_file = None
    for candidate in Path(__file__).parent.glob("*-sa-*.json"):
        service_account_file = str(candidate)
        break
    return AIInsightsSummarizer(service_account_file=service_account_file)


async def _find_week_document(
    publisher: FeishuPublisher,
    period: WeekPeriod,
) -> dict | None:
    return await publisher.find_document_by_title(period.title)


async def _create_week_document(
    publisher: FeishuPublisher,
    period: WeekPeriod,
    first_chat_id: str,
) -> dict:
    document_id = await publisher.create_document(period.title)
    await publisher.set_document_public_permission(document_id, first_chat_id)
    intro = "\n".join(
        [
            "## 周报说明",
            (
                f"统计周期：{period.start:%Y-%m-%d} 至 "
                f"{period.end:%Y-%m-%d}。本页由每日任务增量收集，"
                "周一生成最终精选并推送。"
            ),
        ]
    )
    await publisher.write_content(
        document_id,
        publisher._markdown_to_blocks(intro),
    )
    return {
        "document_id": document_id,
        "title": period.title,
        "url": f"https://feishu.cn/docx/{document_id}",
    }


def render_daily_section(
    insights: list[ScoredInsight],
    local_date: date,
) -> str:
    parts = [f"## {local_date:%Y-%m-%d} 每日收集"]
    parts.extend(insight.to_markdown() for insight in insights)
    return "\n\n".join(parts)


async def collect_daily(config: dict, dry_run: bool = False) -> int:
    settings = config.get("settings", {})
    timezone_name = settings.get("timezone", "Asia/Shanghai")
    zone = ZoneInfo(timezone_name)
    now = datetime.now(tz=zone)
    period = get_week_period(now, timezone_name=timezone_name)

    print(f"\nAI Insights daily collection · {now:%Y-%m-%d %H:%M}")
    print(f"Target document: {period.title}")

    all_items = await collect_sources(config)
    candidates = filter_recent_candidates(
        all_items,
        lookback_hours=int(settings.get("daily_lookback_hours", 48)),
        max_raw_items=int(settings.get("max_raw_items", 80)),
    )
    print(
        f"Collected {len(all_items)} items; "
        f"{len(candidates)} remain after date and URL filtering."
    )

    if not candidates:
        print("No recent candidates. Nothing to append.")
        return 0

    publisher = FeishuPublisher()
    document = None
    existing_text = ""
    if not dry_run:
        if not publisher.is_configured():
            print("Feishu credentials are missing.")
            return 1
        document = await _find_week_document(publisher, period)
        if document:
            existing_text = await publisher.read_document_text(document["document_id"])

    existing_urls = extract_urls(existing_text)
    candidates = [item for item in candidates if item.url not in existing_urls]
    if not candidates:
        print("All recent candidates already exist in this week's document.")
        return 0

    if not _service_account_available():
        if dry_run:
            counts = Counter(item.source for item in candidates)
            print("Gemini credentials unavailable; raw dry-run source counts:")
            for source, count in counts.most_common():
                print(f"- {source}: {count}")
            return 0
        print("Gemini service account credentials are missing.")
        return 1

    summarizer = create_summarizer()
    insights = await summarizer.score_items(
        candidates,
        source_metadata=_source_metadata(config),
        existing_context=existing_text[
            -int(settings.get("existing_context_chars", 12000)) :
        ],
        max_daily_items=int(settings.get("max_daily_items", 8)),
    )
    if not insights:
        print("No candidate met the AI Insights quality threshold.")
        return 0

    section = render_daily_section(insights, now.date())
    if dry_run:
        print("\nDRY RUN — no Feishu document was changed:\n")
        print(section)
        return 0

    chat_ids = _chat_ids(config)
    if not chat_ids:
        print("FEISHU_BOT_CHAT_ID is missing.")
        return 1

    if not document:
        document = await _create_week_document(
            publisher,
            period,
            chat_ids[0],
        )

    await publisher.write_content(
        document["document_id"],
        publisher._markdown_to_blocks(section),
    )
    print(
        f"Appended {len(insights)} candidates to "
        f"{document['url']}. No group message was sent."
    )
    return 0


async def publish_weekly(config: dict, dry_run: bool = False) -> int:
    settings = config.get("settings", {})
    timezone_name = settings.get("timezone", "Asia/Shanghai")
    now = datetime.now(tz=ZoneInfo(timezone_name))
    period = get_week_period(
        now,
        timezone_name=timezone_name,
        previous=True,
    )
    print(f"\nAI Insights weekly publish · {now:%Y-%m-%d %H:%M}")
    print(f"Publishing: {period.title}")

    publisher = FeishuPublisher()
    if not publisher.is_configured():
        print("Feishu credentials are missing.")
        return 1
    document = await _find_week_document(publisher, period)
    if not document:
        print("No document exists for the previous natural week.")
        return 0

    collected_text = await publisher.read_document_text(document["document_id"])
    if "本周最终精选" in collected_text:
        print("This weekly document is already finalized; skipping duplicate push.")
        return 0
    if "每日收集" not in collected_text:
        print("The weekly document contains no daily candidates.")
        return 0
    if not _service_account_available():
        print("Gemini service account credentials are missing.")
        return 1

    summarizer = create_summarizer()
    final_markdown, highlights = await summarizer.generate_weekly_digest(
        collected_text=collected_text,
        period_label=f"{period.start:%Y-%m-%d} 至 {period.end:%Y-%m-%d}",
        max_weekly_items=int(settings.get("max_weekly_items", 10)),
        vendor_weekly_cap=int(settings.get("vendor_weekly_cap", 3)),
    )

    if dry_run:
        print("\nDRY RUN — no document or group message was changed:\n")
        print(final_markdown)
        print("\nCard highlights:\n")
        print(highlights)
        return 0

    chat_ids = _chat_ids(config)
    if not chat_ids:
        print("FEISHU_BOT_CHAT_ID is missing.")
        return 1

    # Insert the final editorial summary at the top. Existing daily evidence
    # remains below it for traceability.
    await publisher.write_content(
        document["document_id"],
        publisher._markdown_to_blocks(final_markdown),
        index=0,
    )
    for chat_id in chat_ids:
        await publisher.send_ai_insights_card(
            chat_id=chat_id,
            title=period.title,
            highlights=highlights,
            doc_url=document["url"],
        )
    print(f"Published weekly digest to {len(chat_ids)} Feishu group(s).")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("collect", "publish"),
        help="collect daily candidates or publish the previous week's digest",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="path to the independent AI Insights YAML configuration",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="perform read/AI work but do not write or send",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    if args.command == "collect":
        exit_code = asyncio.run(collect_daily(config, dry_run=args.dry_run))
    else:
        exit_code = asyncio.run(publish_weekly(config, dry_run=args.dry_run))
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
