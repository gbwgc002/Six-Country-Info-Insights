#!/usr/bin/env python3
"""Collect daily candidates and publish bilingual weekly country reports.

All requested countries share one RSS collection pass. Daily runs only update a
persistent candidate pool. Weekly runs merge that pool with a final collection,
then give each country its own AI review, PDF, archive folder and destination.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from collectors.base import NewsItem
from country_candidate_store import (
    DEFAULT_STORE_PATH,
    items_for_period,
    merge_and_save,
)
from email_sender import EmailSender, WEASYPRINT_AVAILABLE
from main import collect_all_sources, load_config, report_now
from processors import (
    GeminiSummarizer,
    finalize_categories,
    infer_country,
    item_matches_country,
    process_items,
)
from publishers.feishu_publisher import FeishuPublisher
from publishers.country_report_archive import CountryReportArchiveManager
from reporting import (
    CATEGORY_BILINGUAL_NAMES,
    COUNTRY_REPORT_METADATA,
    build_source_appendix,
)


COUNTRY_REPORTS = (
    "india",
    "indonesia",
    "nigeria",
    "pakistan",
    "bangladesh",
)
REPORT_TIMEZONE = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class ReportPeriod:
    start: date
    end: date

    @property
    def zh_label(self) -> str:
        return f"{self.start:%Y年%m月%d日} 至 {self.end:%Y年%m月%d日}"

    @property
    def en_label(self) -> str:
        return f"{self.start:%b %d, %Y} – {self.end:%b %d, %Y}"

    @property
    def filename_label(self) -> str:
        return f"{self.start:%Y-%m-%d}_{self.end:%Y-%m-%d}"


def get_report_period(
    now: datetime | None = None,
    *,
    previous_week: bool = False,
) -> ReportPeriod:
    current = now or report_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=REPORT_TIMEZONE)
    today = current.astimezone(REPORT_TIMEZONE).date()
    if previous_week:
        end = today - timedelta(days=today.weekday() + 1)
        start = end - timedelta(days=6)
    else:
        end = today
        start = end - timedelta(days=6)
    return ReportPeriod(start=start, end=end)


def _service_account_path() -> str | None:
    for candidate in Path(__file__).parent.glob("*-sa-*.json"):
        return str(candidate)
    return None


def _country_chat_id(country: str, *, allow_fallback: bool) -> str:
    specific = os.environ.get(f"FEISHU_CHAT_ID_{country.upper()}", "").strip()
    if specific:
        return specific
    if allow_fallback:
        return os.environ.get("FEISHU_BOT_CHAT_ID", "").strip()
    return ""


def _country_items(all_items, country: str):
    return [item for item in all_items if item_matches_country(item, country)]


def _country_collection_config(config: dict, countries: list[str]) -> dict:
    """Keep requested-country and shared sources for one collection pass."""
    country_config = copy.deepcopy(config)
    requested = set(countries)
    for source in country_config.get("rss_sources", {}).values():
        source_country = str(source.get("country") or "multi").lower()
        if source_country not in requested and source_country != "multi":
            source["enabled"] = False
            continue
        if source.get("enabled", True):
            source["max_items"] = max(int(source.get("max_items", 10)), 20)
    return country_config


def _deduplicate(items: list[NewsItem]) -> list[NewsItem]:
    selected: dict[str, NewsItem] = {}
    for item in items:
        key = item.url.strip().casefold() if item.url else item.title.casefold()
        if key:
            selected[key] = item
    return list(selected.values())


def _items_in_period(
    items: list[NewsItem],
    period: ReportPeriod,
    *,
    collected_at: datetime,
) -> list[NewsItem]:
    filtered = []
    for item in items:
        event_time = item.published or collected_at
        if event_time.tzinfo is None:
            event_time = event_time.replace(tzinfo=timezone.utc)
        local_date = event_time.astimezone(REPORT_TIMEZONE).date()
        if period.start <= local_date <= period.end:
            filtered.append(item)
    return filtered


def _relevant_country_candidates(
    items: list[NewsItem],
    countries: list[str],
) -> list[NewsItem]:
    return [
        item
        for item in items
        if any(item_matches_country(item, country) for country in countries)
    ]


async def _prepare_country_categories(
    all_items,
    country: str,
    config: dict,
    summarizer: GeminiSummarizer,
):
    output_config = config.get("output", {})
    max_per_category = int(
        output_config.get("country_report_max_per_category", 8)
    )
    pre_ai_max = int(
        output_config.get("country_report_pre_ai_max_per_category", 24)
    )
    category_order = output_config.get("category_order", [])

    candidates = _country_items(all_items, country)
    print(f"🌍 {country}: {len(candidates)} raw candidates from shared collection")
    categories = process_items(
        candidates,
        max_per_category=pre_ai_max,
        apply_date_filter=False,
    )
    categories = await summarizer.semantic_deduplicate(categories)

    for category, items in list(categories.items()):
        valid_items, _ = await summarizer.process_and_filter_items(items)
        categories[category] = valid_items

    categories = finalize_categories(
        categories,
        max_per_category=max_per_category,
        category_order=category_order,
    )

    # The model may reclassify a cross-market article. Keep only material that
    # still belongs to this country after AI review.
    filtered = {}
    for category, items in categories.items():
        selected = [
            item
            for item in items
            if infer_country(item) == country or item_matches_country(item, country)
        ]
        if selected:
            filtered[category] = selected
    return filtered


async def collect_daily_candidates(
    countries: list[str],
    *,
    store_path: Path = DEFAULT_STORE_PATH,
) -> int:
    config_path = Path(__file__).parent / "config" / "sources.yaml"
    config = load_config(str(config_path))
    print("📡 Collecting one shared daily candidate pool (no group push)...")
    all_items = await collect_all_sources(
        _country_collection_config(config, countries)
    )
    if not all_items:
        raise RuntimeError("No source items were collected; candidate pool unchanged")
    candidates = _relevant_country_candidates(all_items, countries)
    total = merge_and_save(candidates, path=store_path)
    print(
        f"✅ Collected {len(all_items)} raw items; "
        f"stored {total} deduplicated five-country candidates"
    )
    return 0


async def generate_and_publish(
    countries: list[str],
    *,
    previous_week: bool = False,
    store_path: Path = DEFAULT_STORE_PATH,
    now: datetime | None = None,
) -> int:
    if not WEASYPRINT_AVAILABLE:
        raise RuntimeError("WeasyPrint is required for bilingual country PDFs")

    config_path = Path(__file__).parent / "config" / "sources.yaml"
    config = load_config(str(config_path))

    current = now or report_now()
    period = get_report_period(current, previous_week=previous_week)
    cached_items = items_for_period(
        start=period.start,
        end=period.end,
        path=store_path,
        timezone_info=REPORT_TIMEZONE,
    )
    print(
        "📚 Loaded "
        f"{len(cached_items)} daily candidates for {period.start}–{period.end}"
    )

    print("📡 Running one final shared collection for all requested countries...")
    fresh_items = await collect_all_sources(
        _country_collection_config(config, countries)
    )
    fresh_items = _items_in_period(
        _relevant_country_candidates(fresh_items, countries),
        period,
        collected_at=current,
    )
    all_items = _deduplicate(cached_items + fresh_items)
    if not all_items:
        raise RuntimeError("No source items were available for the weekly period")
    print(
        f"   Weekly shared pool contains {len(all_items)} items "
        f"({len(fresh_items)} from final collection)"
    )

    sa_path = _service_account_path()
    if not sa_path and not os.environ.get("GOOGLE_SA_JSON"):
        raise RuntimeError("GOOGLE_SA_JSON is required for bilingual reports")
    summarizer = GeminiSummarizer(service_account_file=sa_path)
    publisher = FeishuPublisher()
    if not publisher.is_configured():
        raise RuntimeError("Feishu credentials are required for country report delivery")
    archive = CountryReportArchiveManager(publisher)

    date_label = f"{period.zh_label} / {period.en_label}"
    output_dir = Path(__file__).parent / "output" / "pdf"
    output_dir.mkdir(parents=True, exist_ok=True)
    renderer = EmailSender()

    for country in countries:
        metadata = COUNTRY_REPORT_METADATA[country]
        categories = await _prepare_country_categories(
            all_items,
            country,
            config,
            summarizer,
        )
        if not categories:
            raise RuntimeError(f"No qualified items remained for {country}")

        highlights = await summarizer.generate_country_highlights(
            categories,
            CATEGORY_BILINGUAL_NAMES,
            metadata["zh"],
            metadata["en"],
        )
        report_title = (
            f"{metadata['zh']}用研洞察周报 / "
            f"{metadata['en']} Weekly User Research Insights"
        )
        title = (
            f"🔍 {metadata['zh']}洞察周报 / {metadata['en']} Weekly Insights - "
            f"{period.start.isoformat()} to {period.end.isoformat()}"
        )
        html = renderer.render_email(
            categories=categories,
            category_names=CATEGORY_BILINGUAL_NAMES,
            highlights=highlights,
            date_label=date_label,
            report_title=report_title,
            report_subtitle=(
                f"{metadata['flag']} {metadata['en']} · 中英双语 / Chinese-English Bilingual"
            ),
            highlights_title="⚡ 本周要点 / Weekly Highlights",
            toc_title="📑 本周目录 / Contents",
            footer_title=(
                f"{metadata['zh']}用研洞察 ({metadata['en']} User Research Insights)"
            ),
            footer_description=(
                "数据来源：本地媒体、官方机构及行业研究 / "
                "Sources: local media, official institutions and industry research"
            ),
            source_appendix=build_source_appendix(
                config,
                country,
                report_days=7,
                max_per_category=int(
                    config.get("output", {}).get(
                        "country_report_max_per_category",
                        8,
                    )
                ),
                pre_ai_max_per_category=int(
                    config.get("output", {}).get(
                        "country_report_pre_ai_max_per_category",
                        24,
                    )
                ),
            ),
            bilingual=True,
        )
        pdf_path = output_dir / (
            f"{metadata['en']}_Weekly_Insights_Bilingual_"
            f"{period.filename_label}.pdf"
        )
        if not renderer.generate_pdf(html, str(pdf_path)):
            raise RuntimeError(f"PDF generation failed for {country}")

        chat_id = _country_chat_id(country, allow_fallback=len(countries) == 1)
        if not chat_id:
            raise RuntimeError(
                f"Missing FEISHU_CHAT_ID_{country.upper()} for country delivery"
            )
        pdf_url = await archive.upload_country_pdf(
            str(pdf_path),
            title,
            chat_id,
            country,
        )
        if not pdf_url:
            raise RuntimeError(f"Feishu PDF upload failed for {country}")
        await publisher.send_digest_card(
            chat_id,
            title,
            highlights,
            categories,
            CATEGORY_BILINGUAL_NAMES,
            pdf_url,
            bilingual=True,
        )
        print(f"✅ Published bilingual {metadata['en']} report to its Feishu group")

    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=("collect", "publish"),
        default="publish",
        help="Daily collection updates the candidate pool; publish sends PDFs.",
    )
    parser.add_argument(
        "--countries",
        nargs="+",
        choices=("all",) + COUNTRY_REPORTS,
        default=["all"],
        help="One or more country codes. All share one collection pass.",
    )
    parser.add_argument(
        "--previous-week",
        action="store_true",
        help="Publish the previous Monday–Sunday natural week.",
    )
    parser.add_argument(
        "--store-path",
        type=Path,
        default=DEFAULT_STORE_PATH,
        help="Persistent daily candidate pool JSON path.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    countries = list(COUNTRY_REPORTS) if "all" in args.countries else args.countries
    if args.mode == "collect":
        result = collect_daily_candidates(countries, store_path=args.store_path)
    else:
        result = generate_and_publish(
            countries,
            previous_week=args.previous_week,
            store_path=args.store_path,
        )
    sys.exit(asyncio.run(result))


if __name__ == "__main__":
    main()
