#!/usr/bin/env python3
"""Independent daily-collection / weekly-publish pipeline for AI Insights."""

from __future__ import annotations

import argparse
import asyncio
import html
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
    CATEGORY_NAMES,
    ScoredInsight,
    WeeklyDigest,
    extract_urls,
)
from processors.deduper import deduplicate_items
from publishers.feishu_archive import (
    AI_INSIGHTS,
    FeishuArchiveError,
    FeishuArchiveManager,
)
from publishers.feishu_publisher import FeishuPublisher
from email_sender import EmailSender, WEASYPRINT_AVAILABLE

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
    archive: FeishuArchiveManager | None = None,
) -> dict | None:
    if archive and archive.is_enabled:
        try:
            archived = await archive.find_report_by_title(
                AI_INSIGHTS,
                period.title,
            )
            if archived:
                return archived
        except FeishuArchiveError as exc:
            print(
                "Archive folder lookup failed; falling back to the app root: "
                f"{exc}"
            )
    return await publisher.find_document_by_title(period.title)


async def _create_week_document(
    publisher: FeishuPublisher,
    period: WeekPeriod,
    first_chat_id: str,
    archive: FeishuArchiveManager | None = None,
) -> dict:
    archive_ready = False
    if archive and archive.is_enabled:
        try:
            await archive.configure_publisher_folder(AI_INSIGHTS)
            archive_ready = True
        except FeishuArchiveError as exc:
            print(
                "Archive folder setup failed; creating in the existing location: "
                f"{exc}"
            )
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
    if archive_ready:
        await archive.archive_created_document(document_id, strict=False)
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


def render_highlights_html(items: list[str]) -> str:
    """Render trusted presentation markup from escaped AI-generated text."""
    return "\n".join(
        (
            '<div class="highlight-item">'
            f'<span class="highlight-number">{index}</span>'
            f'<span class="highlight-text">{html.escape(item)}</span>'
            "</div>"
        )
        for index, item in enumerate(items[:3], start=1)
    )


def generate_ai_insights_pdf(
    digest: WeeklyDigest,
    period: WeekPeriod,
) -> str | None:
    """Render the weekly digest with the existing six-country visual system."""
    if not WEASYPRINT_AVAILABLE:
        print("WeasyPrint is unavailable; AI Insights PDF cannot be generated.")
        return None

    renderer = EmailSender()
    categories = digest.to_report_categories()
    html_content = renderer.render_email(
        categories=categories,
        category_names=CATEGORY_NAMES,
        highlights=render_highlights_html(digest.core_judgments),
        date_label=(
            f"{period.start:%Y年%m月%d日} 至 "
            f"{period.end:%Y年%m月%d日}"
        ),
        report_icon="",
        report_title="AI洞察资讯周报",
        report_subtitle=(
            "AI Insights · User Research · Consumer Insights · Mobile AI"
        ),
        highlights_title="本周核心判断",
        toc_title="本期目录",
        recommendations=[
            html.escape(advice) for advice in digest.team_advice
        ],
        recommendations_title="本周给团队的三条建议",
        footer_title="AI洞察资讯周报 (AI Insights)",
        footer_description=(
            "聚焦用户研究、消费者洞察、研究工作流与手机 AI"
        ),
    )

    pdf_dir = Path(__file__).parent / "output"
    pdf_dir.mkdir(exist_ok=True)
    pdf_path = pdf_dir / (
        f"AI_Insights_{period.start:%Y-%m-%d}_{period.end:%Y-%m-%d}.pdf"
    )
    if not renderer.generate_pdf(html_content, str(pdf_path)):
        return None
    return str(pdf_path)


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
    archive = FeishuArchiveManager(publisher)
    document = None
    existing_text = ""
    if not dry_run:
        if not publisher.is_configured():
            print("Feishu credentials are missing.")
            return 1
        document = await _find_week_document(publisher, period, archive)
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
            archive,
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
    archive = FeishuArchiveManager(publisher)
    if not publisher.is_configured():
        print("Feishu credentials are missing.")
        return 1
    document = await _find_week_document(publisher, period, archive)
    if not document:
        print("No document exists for the previous natural week.")
        return 0

    collected_text = await publisher.read_document_text(document["document_id"])
    if "AI洞察PDF：" in collected_text:
        print("This weekly PDF was already published; skipping duplicate push.")
        return 0
    if "每日收集" not in collected_text:
        print("The weekly document contains no daily candidates.")
        return 0
    if not _service_account_available():
        print("Gemini service account credentials are missing.")
        return 1

    summarizer = create_summarizer()
    digest = await summarizer.generate_weekly_digest(
        collected_text=collected_text,
        period_label=f"{period.start:%Y-%m-%d} 至 {period.end:%Y-%m-%d}",
        max_weekly_items=int(settings.get("max_weekly_items", 10)),
        vendor_weekly_cap=int(settings.get("vendor_weekly_cap", 3)),
    )
    final_markdown = digest.to_markdown()
    highlights = digest.card_highlights
    category_titles = digest.card_category_titles(limit=2)

    if dry_run:
        print("\nDRY RUN — no document or group message was changed:\n")
        print(final_markdown)
        print("\nCard highlights:\n")
        print(highlights)
        print("\nCard category titles:\n")
        print(category_titles)
        return 0

    chat_ids = _chat_ids(config)
    if not chat_ids:
        print("FEISHU_BOT_CHAT_ID is missing.")
        return 1

    pdf_path = generate_ai_insights_pdf(digest, period)
    if not pdf_path:
        return 1
    try:
        pdf_url = await archive.upload_pdf(
            pdf_path,
            period.title,
            chat_ids[0],
            AI_INSIGHTS,
        )
    except FeishuArchiveError as exc:
        print(
            "Archive upload failed; using the existing Feishu upload path so "
            f"the weekly push can continue: {exc}"
        )
        pdf_url = await publisher.upload_pdf(
            pdf_path,
            period.title,
            chat_ids[0],
        )
    if not pdf_url:
        print("AI Insights PDF upload failed; no group message was sent.")
        return 1

    # Keep the weekly Feishu document as the collection/evidence store. Add
    # the editorial summary once, while the user-facing card opens the PDF.
    if "本周最终精选" not in collected_text:
        await publisher.write_content(
            document["document_id"],
            publisher._markdown_to_blocks(final_markdown),
            index=0,
        )
    for chat_id in chat_ids:
        await publisher.send_ai_insights_card(
            chat_id=chat_id,
            highlights=highlights,
            categories=category_titles,
            doc_url=pdf_url,
        )
    pdf_marker = f"## PDF版本\n\nAI洞察PDF：[查看完整周报]({pdf_url})"
    await publisher.write_content(
        document["document_id"],
        publisher._markdown_to_blocks(pdf_marker),
        index=0,
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
