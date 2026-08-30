#!/usr/bin/env python3
"""
Seven-Country Info Insights (七国用研洞察)

Collects user-research insights from Russia, India, Indonesia,
Nigeria, Kenya, Pakistan, and Bangladesh — covering macro environment, commerce,
digital ecosystems, pop culture, and mobile markets.

Summarises with Gemini AI and pushes via Feishu bot / email.
"""

import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv

# Load environment variables from .env file if it exists
load_dotenv()

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from collectors import (
    collect_all_rss,
    NewsItem,
)
from processors import (
    GeminiSummarizer,
    finalize_categories,
    infer_country,
    process_items,
)
from email_sender import send_digest_email, EmailSender, WEASYPRINT_AVAILABLE
from publishers.feishu_archive import (
    SIX_COUNTRY,
    FeishuArchiveError,
    FeishuArchiveManager,
)
from publishers.feishu_publisher import FeishuPublisher
from publishers.feishu_publisher import FeishuSendError
from reporting import build_source_appendix
from monitoring import (
    empty_feishu_receipt,
    new_run_receipt,
    require_confirmed_delivery,
    write_receipt_atomic,
)


REPORT_TIMEZONE = ZoneInfo("Asia/Shanghai")


def report_now() -> datetime:
    """Use Beijing time for report dates, including the 23:00 UTC schedule."""
    return datetime.now(REPORT_TIMEZONE)


def load_config(config_path: str = "config/sources.yaml") -> dict:
    """Load configuration from YAML file."""
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


async def collect_all_sources(config: dict) -> list[NewsItem]:
    """Collect news from all configured sources."""
    tasks = []

    # RSS sources (primary collection method)
    if config.get("rss_sources"):
        tasks.append(collect_all_rss(config["rss_sources"]))

    # Run all collectors concurrently
    results = await asyncio.gather(*tasks, return_exceptions=True)

    all_items = []
    for result in results:
        if isinstance(result, list):
            all_items.extend(result)
        elif isinstance(result, Exception):
            print(f"Collector error: {result}")

    return all_items


async def main_async():
    """Main entry point (Async)."""
    monitor_receipt = new_run_receipt()
    write_receipt_atomic(monitor_receipt)
    delivery_required = os.environ.get("REQUIRE_FEISHU_DELIVERY", "").lower() in {
        "1", "true", "yes", "on"
    }
    now = report_now()
    print(f"\n{'='*60}")
    print(f"🔍 七国用研洞察 - {now.strftime('%Y-%m-%d %H:%M')}")
    print(f"   EE1 · India · Indonesia · Nigeria · Kenya · Pakistan · Bangladesh")
    print(f"{'='*60}\n")

    # Load config
    config_path = Path(__file__).parent / "config" / "sources.yaml"
    config = load_config(str(config_path))

    # Get output settings
    output_config = config.get("output", {})
    category_names = output_config.get("category_names", {})
    max_per_category = output_config.get("max_per_category", 15)
    pre_ai_max_per_category = output_config.get(
        "pre_ai_max_per_category",
        max_per_category * 2,
    )
    category_order = output_config.get("category_order", [])

    # Collect from all sources
    print("📡 Collecting from sources...")
    all_items = await collect_all_sources(config)
    print(f"   Total collected: {len(all_items)} items\n")

    if not all_items:
        print("❌ No items collected. Check your configuration and network.")
        return 1

    # Process items (dedupe, filter, group)
    print("🔄 Processing items...")
    categories = process_items(
        all_items,
        max_per_category=pre_ai_max_per_category,
    )
    total_items = sum(len(items) for items in categories.values())
    print(f"   After processing: {total_items} items in {len(categories)} categories\n")

    # Initialize summarizer (service account file or GOOGLE_SA_JSON env var)
    highlights = ""
    sa_file = None
    # Look for any service account JSON file in project root
    for f in Path(__file__).parent.glob("*-sa-*.json"):
        sa_file = f
        break
    sa_available = (sa_file and sa_file.exists()) or os.environ.get("GOOGLE_SA_JSON")

    if sa_available:
        print("🧠 Initializing Gemini AI...")
        try:
            sa_path = str(sa_file) if (sa_file and sa_file.exists()) else None
            summarizer = GeminiSummarizer(service_account_file=sa_path)

            # Semantic dedup BEFORE translation (saves API calls)
            print("🔍 Semantic deduplication...")
            categories = await summarizer.semantic_deduplicate(categories)
            total_items = sum(len(items) for items in categories.values())
            print(f"   After dedup: {total_items} items\n")

            # Translate items in each category
            for cat_name, items in categories.items():
                valid_items, _ = await summarizer.process_and_filter_items(items)
                categories[cat_name] = valid_items

            # Regroup using AI's actual category, balance countries, then cap.
            categories = finalize_categories(
                categories,
                max_per_category=max_per_category,
                category_order=category_order,
            )

            # Generate highlights
            print("✨ Generating daily highlights...")
            highlights = await summarizer.generate_daily_highlights(categories, category_names)
            print("   Highlights generated\n")
        except Exception as e:
            print(f"   AI error: {e}\n")
    else:
        print("⚠️  Service account not found (no file or GOOGLE_SA_JSON), skipping AI processing\n")

    # Also enforce final caps if AI was unavailable or failed partway through.
    categories = finalize_categories(
        categories,
        max_per_category=max_per_category,
        category_order=category_order,
    )
    country_counts: dict[str, int] = {}
    for items in categories.values():
        for item in items:
            country = infer_country(item) or "unassigned"
            country_counts[country] = country_counts.get(country, 0) + 1
    print(f"🌍 Final country coverage: {country_counts}\n")

    # Send email
    to_email = os.environ.get("TO_EMAIL", "")
    if to_email:
        print(f"📧 Sending email to {to_email}...")
    else:
        print("📧 TO_EMAIL not set, skipping email...")

    # Generate PDF for both email and Feishu
    email_sender = EmailSender()
    source_appendix = build_source_appendix(config)
    html_content = email_sender.render_email(
        categories,
        category_names,
        highlights,
        date_label=now.strftime("%Y年%m月%d日"),
        source_appendix=source_appendix,
    )
    date_str = now.strftime("%Y-%m-%d")
    pdf_path = None

    if WEASYPRINT_AVAILABLE:
        pdf_dir = Path(__file__).parent / "output"
        pdf_dir.mkdir(exist_ok=True)
        pdf_path = str(pdf_dir / f"Seven_Country_Insights_{date_str}.pdf")
        email_sender.generate_pdf(html_content, pdf_path)

    # Send email with PDF attachment
    email_success = False
    if to_email:
        subject = f"🔍 七国用研洞察 - {now.strftime('%m/%d')}"
        email_success = email_sender.send(to_email, subject, html_content, pdf_path)

        if email_success:
            print("✅ Email sent successfully!")
        else:
            print("❌ Failed to send email. Check SMTP configuration.")

    # Publish to Feishu (independent of email)
    publishers_config = config.get("publishers", {})
    feishu_config = publishers_config.get("feishu", {})

    if feishu_config.get("enabled", False):
        print("\n🚀 Publishing to Feishu...")
        publisher = FeishuPublisher()
        archive = FeishuArchiveManager(publisher)
        if publisher.is_configured():
            title = feishu_config.get("title_format", "🔍 七国用研洞察 - {date}").format(date=date_str)

            # Publish to Feishu Bot (Push)
            bot_config = publishers_config.get("feishu_bot", {})
            if bot_config.get("enabled", False):
                chat_id_str = bot_config.get("chat_id") or os.environ.get("FEISHU_BOT_CHAT_ID")
                if chat_id_str:
                    chat_ids = [cid.strip() for cid in chat_id_str.split(',') if cid.strip()]

                    if chat_ids:
                        first_chat_id = chat_ids[0]
                        doc_url = None

                        # Upload PDF to Feishu (same content as email)
                        if pdf_path and Path(pdf_path).exists():
                            try:
                                doc_url = await archive.upload_pdf(
                                    pdf_path,
                                    title,
                                    first_chat_id,
                                    SIX_COUNTRY,
                                )
                            except FeishuArchiveError as exc:
                                print(
                                    "   ⚠️ Archive upload failed; using the existing "
                                    f"Feishu upload path: {exc}"
                                )
                                doc_url = await publisher.upload_pdf(
                                    pdf_path,
                                    title,
                                    first_chat_id,
                                )
                            if doc_url:
                                print(f"   PDF available at: {doc_url}")
                        else:
                            print("   ⚠️ PDF not available, skipping Feishu upload")

                        print(f"\n🤖 Pushing to {len(chat_ids)} Feishu Bot Group(s)...")
                        for cid in chat_ids:
                            try:
                                send_receipt = await publisher.send_digest_card(
                                    cid,
                                    title,
                                    highlights,
                                    categories,
                                    category_names,
                                    doc_url,
                                )
                            except FeishuSendError as exc:
                                monitor_receipt["feishu_send"] = exc.receipt
                                monitor_receipt["feishu_sends"].append(exc.receipt)
                                write_receipt_atomic(monitor_receipt)
                                raise
                            monitor_receipt["feishu_send"] = send_receipt
                            monitor_receipt["feishu_sends"].append(send_receipt)
                            write_receipt_atomic(monitor_receipt)

                        # Cleanup old documents (older than 180 days)
                        print("\n🧹 Checking for old documents to clean up...")
                        await publisher.cleanup_old_documents()
                    else:
                        print("   ⚠️ Feishu bot enabled but no valid chat IDs found")
                        monitor_receipt["feishu_send"] = empty_feishu_receipt("not_sent")
                        write_receipt_atomic(monitor_receipt)
                else:
                    print("   ⚠️ Feishu bot enabled but FEISHU_BOT_CHAT_ID not set")
                    monitor_receipt["feishu_send"] = empty_feishu_receipt("not_configured")
                    write_receipt_atomic(monitor_receipt)
            else:
                monitor_receipt["feishu_send"] = empty_feishu_receipt("not_sent")
                write_receipt_atomic(monitor_receipt)
        else:
            print("   ⚠️ Feishu publisher enabled but credentials not found (FEISHU_APP_ID/SECRET)")
            monitor_receipt["feishu_send"] = empty_feishu_receipt("not_configured")
            write_receipt_atomic(monitor_receipt)
    else:
        monitor_receipt["feishu_send"] = empty_feishu_receipt("not_sent")
        write_receipt_atomic(monitor_receipt)

    require_confirmed_delivery(monitor_receipt, delivery_required)

    print("\n✅ Daily insights digest completed!")
    return 0


def main():
    """Wrapper for async main."""
    sys.exit(asyncio.run(main_async()))


if __name__ == "__main__":
    sys.exit(main())
