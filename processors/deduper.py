"""
Deduplication and ranking utilities.
"""

from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Optional
from collectors.base import NewsItem


TARGET_COUNTRIES = (
    "russia",
    "india",
    "indonesia",
    "nigeria",
    "kenya",
    "pakistan",
    "bangladesh",
)

COUNTRY_ALIASES = {
    "russia": ("russia", "russian", "moscow", "ruble", "rouble", "俄罗斯", "莫斯科"),
    "india": (
        "india", "indian", "delhi", "mumbai", "bengaluru", "bangalore",
        "chennai", "hyderabad", "kolkata", "upi", "印度", "孟买", "德里",
    ),
    "indonesia": (
        "indonesia", "indonesian", "jakarta", "surabaya", "bandung",
        "qris", "印尼", "印度尼西亚", "雅加达",
    ),
    "nigeria": (
        "nigeria", "nigerian", "lagos", "abuja", "kano", "naira",
        "尼日利亚", "拉各斯", "阿布贾",
    ),
    "kenya": (
        "kenya", "kenyan", "nairobi", "mombasa", "m-pesa", "mpesa",
        "肯尼亚", "内罗毕", "蒙巴萨",
    ),
    "pakistan": (
        "pakistan", "pakistani", "karachi", "islamabad", "lahore",
        "peshawar", "巴基斯坦", "卡拉奇", "伊斯兰堡", "拉合尔",
    ),
    "bangladesh": (
        "bangladesh", "bangladeshi", "dhaka", "chattogram", "chittagong",
        "taka", "btrc", "孟加拉", "达卡", "吉大港",
    ),
}


def infer_country(item: NewsItem) -> str | None:
    """Infer one target country from configured metadata or article text."""
    configured = (item.country or "").strip().lower()
    if configured in TARGET_COUNTRIES or configured == "multi":
        return configured

    text = " ".join(
        value for value in (item.title, item.summary, item.content) if value
    ).lower()
    matches = [
        country
        for country, aliases in COUNTRY_ALIASES.items()
        if any(alias in text for alias in aliases)
    ]
    if len(matches) == 1:
        item.country = matches[0]
        return matches[0]
    if len(matches) > 1:
        item.country = "multi"
        return "multi"
    return None


def item_matches_country(item: NewsItem, country: str) -> bool:
    """Return whether an item should enter one country's candidate pool."""
    country = country.strip().lower()
    if country not in TARGET_COUNTRIES:
        return False

    configured = (item.country or "").strip().lower()
    if configured == country:
        return True
    if configured and configured not in {"multi"}:
        return False

    text = " ".join(
        value for value in (item.title, item.summary, item.content) if value
    ).lower()
    return any(alias in text for alias in COUNTRY_ALIASES[country])


def _item_rank_key(item: NewsItem) -> tuple[float, float, float]:
    published = item.published
    if published and published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    timestamp = published.timestamp() if published else 0.0
    return (
        float(getattr(item, "relevance_score", 0.0) or 0.0),
        float(getattr(item, "source_priority", 1.0) or 1.0),
        timestamp,
    )


def balanced_limit(items: list[NewsItem], limit: int | None) -> list[NewsItem]:
    """Keep at least one item per available target country, then rank the rest."""
    ranked = sorted(items, key=_item_rank_key, reverse=True)
    if not limit or len(ranked) <= limit:
        return ranked

    buckets: dict[str, list[NewsItem]] = defaultdict(list)
    for item in ranked:
        buckets[infer_country(item) or "unassigned"].append(item)

    selected: list[NewsItem] = []
    selected_ids: set[int] = set()

    # One guaranteed slot for every country represented in this category.
    for country in TARGET_COUNTRIES:
        if len(selected) >= limit:
            break
        if buckets.get(country):
            item = buckets[country][0]
            selected.append(item)
            selected_ids.add(id(item))

    remaining = [item for item in ranked if id(item) not in selected_ids]
    selected.extend(remaining[: max(0, limit - len(selected))])
    return selected


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
    extended_cutoff = datetime.now(timezone.utc) - timedelta(days=max(days, 2.0))

    filtered = []
    for item in items:
        if item.published:
            # Handle naive datetime by assuming UTC
            pub_date = item.published
            if pub_date.tzinfo is None:
                pub_date = pub_date.replace(tzinfo=timezone.utc)

            # Low-frequency official/industry sources can define a longer window.
            if item.freshness_days is not None:
                target_cutoff = datetime.now(timezone.utc) - timedelta(
                    days=item.freshness_days
                )
            else:
                target_cutoff = (
                    extended_cutoff
                    if item.category in ('macro_infra', 'country_news', 'pop_culture')
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
    days: float = 1.0,  # Reduced to 1.0 (24 hours) for strict daily filtering
    apply_date_filter: bool = True,
) -> dict[str, list[NewsItem]]:
    """Full processing pipeline: dedupe, filter, sort, group."""
    # Deduplicate
    items = deduplicate_items(items)

    # Filter by date (strictly recent items)
    if apply_date_filter:
        items = filter_by_date(items, days=days)

    # Attach country metadata before category quotas are applied.
    for item in items:
        infer_country(item)

    # Sort by date before grouping. balanced_limit applies the final composite rank.
    items = sort_items(items, by="published")

    # Group by category
    grouped = group_by_category(items)

    # Limit per category
    for category in grouped:
        grouped[category] = balanced_limit(
            grouped[category],
            max_per_category,
        )

    return grouped


def finalize_categories(
    categories: dict[str, list[NewsItem]],
    max_per_category: int,
    category_order: list[str] | None = None,
) -> dict[str, list[NewsItem]]:
    """Regroup AI-classified items, balance countries, and apply final caps."""
    regrouped: dict[str, list[NewsItem]] = defaultdict(list)
    for items in categories.values():
        for item in items:
            infer_country(item)
            regrouped[item.category].append(item)

    ordered_categories = list(category_order or [])
    ordered_categories.extend(
        category for category in regrouped if category not in ordered_categories
    )

    result: dict[str, list[NewsItem]] = {}
    for category in ordered_categories:
        items = regrouped.get(category, [])
        if not items:
            continue
        result[category] = balanced_limit(items, max_per_category)
    return result
