#!/usr/bin/env python3
"""
AI Daily Digest - Main entry point.

Collects AI news from multiple sources, summarizes with Gemini,
and sends a beautifully formatted email digest.
"""

import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

import yaml
from dotenv import load_dotenv

# Load environment variables from .env file if it exists
load_dotenv()

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from collectors import (
    collect_all_rss,
    collect_arxiv,
    collect_twitter,
    collect_hackernews,
    NewsItem,
)
from processors import GeminiSummarizer, process_items
from email_sender import send_digest_email, EmailSender, WEASYPRINT_AVAILABLE
from publishers.feishu_publisher import FeishuPublisher


def load_config(config_path: str = "config/sources.yaml") -> dict:
    """Load configuration from YAML file."""
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


async def collect_all_sources(config: dict) -> list[NewsItem]:
    """Collect news from all configured sources."""
    tasks = []

    # RSS sources
    if config.get("rss_sources"):
        tasks.append(collect_all_rss(config["rss_sources"]))

    # arXiv papers
    if config.get("arxiv", {}).get("enabled", True):
        tasks.append(collect_arxiv(config.get("arxiv", {})))

    # Twitter/X
    if config.get("twitter", {}).get("enabled", True):
        tasks.append(collect_twitter(config.get("twitter", {})))

    # Hacker News
    if config.get("hackernews", {}).get("enabled", True):
        tasks.append(collect_hackernews(config.get("hackernews", {})))

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
    print(f"\n{'='*60}")
    print(f"🤖 AI Daily Digest - {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}\n")

    # Load config
    config_path = Path(__file__).parent / "config" / "sources.yaml"
    config = load_config(str(config_path))

    # Get output settings
    output_config = config.get("output", {})
    category_names = output_config.get("category_names", {})
    max_per_category = output_config.get("max_per_category", 5)

    # Collect from all sources
    print("📡 Collecting from sources...")
    all_items = await collect_all_sources(config)
    print(f"   Total collected: {len(all_items)} items\n")

    if not all_items:
        print("❌ No items collected. Check your configuration and network.")
        return 1

    # Process items (dedupe, filter, group)
    print("🔄 Processing items...")
    categories = process_items(all_items, max_per_category=max_per_category)
    total_items = sum(len(items) for items in categories.values())
    print(f"   After processing: {total_items} items in {len(categories)} categories\n")

    # Initialize summarizer
    summarizer = None
    highlights = ""

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        # Fallback check
        if os.environ.get("ANTHROPIC_API_KEY"):
            print("⚠️  ANTHROPIC_API_KEY found but GEMINI_API_KEY is missing.")
            print("    The project has migrated to Google Gemini. Please set GEMINI_API_KEY.")

    if api_key:
        print("🧠 Initializing Gemini AI...")
        try:
            summarizer = GeminiSummarizer(api_key=api_key)

            # Semantic dedup BEFORE translation (saves API calls)
            print("🔍 Semantic deduplication...")
            categories = await summarizer.semantic_deduplicate(categories)
            total_items = sum(len(items) for items in categories.values())
            print(f"   After dedup: {total_items} items\n")

            # Translate items in each category (Processing categories sequentially, items parallel)
            for cat_name, items in categories.items():
                valid_items, _ = await summarizer.process_and_filter_items(items)
                categories[cat_name] = valid_items

            # Generate highlights
            print("✨ Generating daily highlights...")
            highlights = await summarizer.generate_daily_highlights(categories, category_names)
            print("   Highlights generated\n")
        except Exception as e:
            print(f"   AI error: {e}\n")
    else:
        print("⚠️  GEMINI_API_KEY not set, skipping AI translation and summaries\n")

    # Send email
    to_email = os.environ.get("TO_EMAIL", "rillahai@gmail.com")
    print(f"📧 Sending email to {to_email}...")

    # Generate PDF for both email and Feishu
    email_sender = EmailSender()
    html_content = email_sender.render_email(categories, category_names, highlights)
    date_str = datetime.now().strftime("%Y-%m-%d")
    pdf_path = None

    if WEASYPRINT_AVAILABLE:
        pdf_dir = Path(__file__).parent / "output"
        pdf_dir.mkdir(exist_ok=True)
        pdf_path = str(pdf_dir / f"AI_Daily_Digest_{date_str}.pdf")
        email_sender.generate_pdf(html_content, pdf_path)

    # Send email with PDF attachment
    subject = f"🤖 AI Daily Digest - {datetime.now().strftime('%m/%d')}"
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
        if publisher.is_configured():
            title = feishu_config.get("title_format", "AI Daily Digest - {date}").format(date=date_str)

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
                            doc_url = await publisher.upload_pdf(pdf_path, title, first_chat_id)
                            if doc_url:
                                print(f"   PDF available at: {doc_url}")
                        else:
                            print("   ⚠️ PDF not available, skipping Feishu upload")

                        print(f"\n🤖 Pushing to {len(chat_ids)} Feishu Bot Group(s)...")
                        for cid in chat_ids:
                            await publisher.send_digest_card(cid, title, highlights, categories, category_names, doc_url)

                        # Cleanup old documents (older than 180 days)
                        print("\n🧹 Checking for old documents to clean up...")
                        await publisher.cleanup_old_documents()
                    else:
                        print("   ⚠️ Feishu bot enabled but no valid chat IDs found")
                else:
                    print("   ⚠️ Feishu bot enabled but FEISHU_BOT_CHAT_ID not set")
        else:
            print("   ⚠️ Feishu publisher enabled but credentials not found (FEISHU_APP_ID/SECRET)")

    print("\n✅ Daily digest completed!")
    return 0


def main():
    """Wrapper for async main."""
    sys.exit(asyncio.run(main_async()))


if __name__ == "__main__":
    sys.exit(main())
