"""
Deduplication and ranking utilities.
"""

from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Optional
from collectors.base import NewsItem


def deduplicate_items(items: list[NewsItem]) -> list[NewsItem]:
    """Remove duplicate items based on URL and similar titles."""
    seen_urls = set()
    seen_titles = set()
    unique_items = []

    for item in items:
        # Check URL
        if item.url in seen_urls:
            continue

        # Check for very similar titles (simple approach)
        title_key = item.title.lower()[:50]
        if title_key in seen_titles:
            continue

        seen_urls.add(item.url)
        seen_titles.add(title_key)
        unique_items.append(item)

    return unique_items


def filter_by_date(
    items: list[NewsItem],
    days: float = 1.0
) -> list[NewsItem]:
    """Filter items to only include recent ones."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    # Allow longer window for papers (ArXiv often has delays)
    # and china sources (WayToAGI etc. use Beijing time, midnight+08:00
    # easily falls outside a strict 24h UTC window)
    extended_cutoff = datetime.now(timezone.utc) - timedelta(days=2.0)

    filtered = []
    for item in items:
        if item.published:
            # Handle naive datetime by assuming UTC
            pub_date = item.published
            if pub_date.tzinfo is None:
                pub_date = pub_date.replace(tzinfo=timezone.utc)

            # Use extended window for papers and china sources
            target_cutoff = (
                extended_cutoff
                if item.category in ('papers', 'china')
                else cutoff
            )

            if pub_date >= target_cutoff:
                filtered.append(item)
        else:
            # Include items without date (might be recent)
            filtered.append(item)

    return filtered


def sort_items(
    items: list[NewsItem],
    by: str = "published"
) -> list[NewsItem]:
    """Sort items by specified field."""
    if by == "published":
        return sorted(
            items,
            key=lambda x: x.published or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True
        )
    elif by == "score":
        return sorted(items, key=lambda x: x.score, reverse=True)
    return items


def group_by_category(
    items: list[NewsItem]
) -> dict[str, list[NewsItem]]:
    """Group items by category."""
    grouped = defaultdict(list)
    for item in items:
        grouped[item.category].append(item)
    return dict(grouped)


def process_items(
    items: list[NewsItem],
    max_per_category: int = 5,
    days: float = 1.0  # Reduced to 1.0 (24 hours) for strict daily filtering
) -> dict[str, list[NewsItem]]:
    """Full processing pipeline: dedupe, filter, sort, group."""
    # Deduplicate
    items = deduplicate_items(items)

    # Filter by date (strictly recent items)
    items = filter_by_date(items, days=days)

    # Sort by date
    items = sort_items(items, by="published")

    # Group by category
    grouped = group_by_category(items)

    # Limit per category
    for category in grouped:
        grouped[category] = grouped[category][:max_per_category]

    return grouped
