#!/usr/bin/env python3
"""Generate bilingual daily insight PDFs for one or more target countries.

All requested countries share one RSS collection pass. Each country then gets
its own candidate pool, AI review, ranking, PDF and Feishu destination.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import os
import sys
from pathlib import Path

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


DAILY_COUNTRY_REPORTS = (
    "india",
    "indonesia",
    "nigeria",
    "pakistan",
    "bangladesh",
)


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


def _daily_collection_config(config: dict, countries: list[str]) -> dict:
    """Keep requested-country and shared sources for one daily collection pass."""
    daily_config = copy.deepcopy(config)
    requested = set(countries)
    for source in daily_config.get("rss_sources", {}).values():
        source_country = str(source.get("country") or "multi").lower()
        if source_country not in requested and source_country != "multi":
            source["enabled"] = False
            continue
        if source.get("enabled", True):
            source["max_items"] = max(int(source.get("max_items", 10)), 20)
    return daily_config


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
        days=1.0,
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


async def generate_and_publish(countries: list[str]) -> int:
    if not WEASYPRINT_AVAILABLE:
        raise RuntimeError("WeasyPrint is required for bilingual country PDFs")

    config_path = Path(__file__).parent / "config" / "sources.yaml"
    config = load_config(str(config_path))

    print("📡 Collecting one shared daily source pool...")
    all_items = await collect_all_sources(
        _daily_collection_config(config, countries)
    )
    if not all_items:
        raise RuntimeError("No source items were collected")
    print(f"   Shared collection contains {len(all_items)} items")

    sa_path = _service_account_path()
    if not sa_path and not os.environ.get("GOOGLE_SA_JSON"):
        raise RuntimeError("GOOGLE_SA_JSON is required for bilingual reports")
    summarizer = GeminiSummarizer(service_account_file=sa_path)
    publisher = FeishuPublisher()
    if not publisher.is_configured():
        raise RuntimeError("Feishu credentials are required for country report delivery")
    archive = CountryReportArchiveManager(publisher)

    now = report_now()
    period_end = now.date()
    date_label = (
        f"{period_end.strftime('%Y年%m月%d日')} / "
        f"{period_end.strftime('%b %d, %Y')}"
    )
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
            f"{metadata['zh']}用研洞察 / {metadata['en']} User Research Insights"
        )
        title = (
            f"🔍 {metadata['zh']}洞察日报 / {metadata['en']} Daily Insights - "
            f"{period_end.isoformat()}"
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
            highlights_title="⚡ 今日要点 / Daily Highlights",
            toc_title="📑 今日目录 / Contents",
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
                report_days=1,
                max_per_category=int(
                    config.get("output", {}).get(
                        "country_report_max_per_category",
                        8,
                    )
                ),
            ),
            bilingual=True,
        )
        pdf_path = output_dir / (
            f"{metadata['en']}_Insights_Bilingual_"
            f"{period_end.isoformat()}.pdf"
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
        "--countries",
        nargs="+",
        choices=DAILY_COUNTRY_REPORTS,
        default=["india"],
        help="One or more country codes. All share one collection pass.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    sys.exit(asyncio.run(generate_and_publish(args.countries)))


if __name__ == "__main__":
    main()
