"""Shared helpers for user-facing six-country report output."""

from __future__ import annotations

from math import ceil


PUBLIC_LABEL_REPLACEMENTS = {
    "俄罗斯": "EE1",
}

COUNTRY_DISPLAY_NAMES = {
    "russia": "EE1",
    "india": "印度",
    "indonesia": "印尼",
    "nigeria": "尼日利亚",
    "kenya": "肯尼亚",
    "pakistan": "巴基斯坦",
    "multi": "跨国",
}

COUNTRY_ORDER = {
    country: index
    for index, country in enumerate(
        ("russia", "india", "indonesia", "nigeria", "kenya", "pakistan", "multi")
    )
}

CATEGORY_DISPLAY_NAMES = {
    "macro_infra": "宏观/基建",
    "commerce_economy": "商业/消费",
    "digital_ecosystem": "数字生态",
    "pop_culture": "流行文化",
    "mobile_market": "手机市场",
    "country_news": "各国要闻",
}

CATEGORY_ORDER = {
    category: index
    for index, category in enumerate(
        (
            "macro_infra",
            "commerce_economy",
            "digital_ecosystem",
            "pop_culture",
            "mobile_market",
            "country_news",
        )
    )
}


def sanitize_public_text(value: str | None) -> str:
    """Apply mandatory public-facing label replacements."""
    text = value or ""
    for original, replacement in PUBLIC_LABEL_REPLACEMENTS.items():
        text = text.replace(original, replacement)
    return text


def build_source_appendix(config: dict) -> dict:
    """Build the compact, auditable source appendix used by the PDF report."""
    configured_sources = config.get("rss_sources", {})
    enabled_sources = []

    for source_id, source in configured_sources.items():
        if not source.get("enabled", True):
            continue

        country_code = str(source.get("country") or "multi").lower()
        category_code = str(source.get("category") or "country_news").lower()
        priority = float(source.get("priority", 1.0))
        freshness_days = int(source.get("freshness_days", 1))
        enabled_sources.append(
            {
                "id": source_id,
                "name": sanitize_public_text(str(source.get("name") or source_id)),
                "country": COUNTRY_DISPLAY_NAMES.get(country_code, "跨国"),
                "country_code": country_code,
                "category": CATEGORY_DISPLAY_NAMES.get(category_code, category_code),
                "category_code": category_code,
                "priority": f"{priority:.1f}",
                "freshness_days": freshness_days,
            }
        )

    enabled_sources.sort(
        key=lambda source: (
            COUNTRY_ORDER.get(source["country_code"], len(COUNTRY_ORDER)),
            CATEGORY_ORDER.get(source["category_code"], len(CATEGORY_ORDER)),
            -float(source["priority"]),
            source["name"].casefold(),
        )
    )

    midpoint = ceil(len(enabled_sources) / 2)
    return {
        "enabled_count": len(enabled_sources),
        "disabled_count": len(configured_sources) - len(enabled_sources),
        "columns": [enabled_sources[:midpoint], enabled_sources[midpoint:]],
        "weight_rules": [
            {"range": "W3.0", "meaning": "官方监管机构及权威行业研究，排序优先级最高"},
            {"range": "W1.9-2.4", "meaning": "垂直科技、商业及产业媒体，强调专业相关性"},
            {"range": "W0.9-1.6", "meaning": "综合新闻与舆情补充源，需更强内容相关性"},
        ],
        "filter_rules": [
            "源级过滤：仅采集启用 RSS，并执行来源关键词；常规源保留 1 天，低频官方/行业源保留 7 天。",
            "去重与候选池：按链接、标题及语义去重；AI 处理前每类最多保留 30 条，并优先保障六国候选覆盖。",
            "AI 复核：过滤低质、不安全及弱相关内容，按文章实际价值重分类，并给出 1-5 分用研重要性。",
            "最终排序：依次参考重要性、来源权重与时效；每类对有合格内容的国家优先保留 1 条，最终每类最多 15 条。",
        ],
    }
