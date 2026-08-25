"""Shared helpers for user-facing seven-country and country report output."""

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
    "bangladesh": "孟加拉",
    "multi": "跨国",
}

COUNTRY_REPORT_METADATA = {
    "russia": {"zh": "EE1", "en": "EE1", "flag": "🇷🇺"},
    "india": {"zh": "印度", "en": "India", "flag": "🇮🇳"},
    "indonesia": {"zh": "印尼", "en": "Indonesia", "flag": "🇮🇩"},
    "nigeria": {"zh": "尼日利亚", "en": "Nigeria", "flag": "🇳🇬"},
    "kenya": {"zh": "肯尼亚", "en": "Kenya", "flag": "🇰🇪"},
    "pakistan": {"zh": "巴基斯坦", "en": "Pakistan", "flag": "🇵🇰"},
    "bangladesh": {"zh": "孟加拉", "en": "Bangladesh", "flag": "🇧🇩"},
}

COUNTRY_ORDER = {
    country: index
    for index, country in enumerate(
        (
            "russia",
            "india",
            "indonesia",
            "nigeria",
            "kenya",
            "pakistan",
            "bangladesh",
            "multi",
        )
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

CATEGORY_BILINGUAL_NAMES = {
    "macro_infra": "🏛️ 宏观环境与基础设施 / Macro & Infrastructure",
    "commerce_economy": "💰 商业流转与消费气象 / Commerce & Consumption",
    "digital_ecosystem": "🚀 数字生态与本土创投 / Digital Ecosystem",
    "pop_culture": "🎭 流行文化与公共情绪 / Digital Lifestyle & Sentiment",
    "mobile_market": "📱 手机与硬件市场 / Mobile & Hardware Market",
    "country_news": "🌍 国家要闻速递 / Country Headlines",
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


def build_source_appendix(
    config: dict,
    country_code: str | None = None,
    report_days: int = 1,
    max_per_category: int = 15,
    pre_ai_max_per_category: int = 30,
) -> dict:
    """Build the compact, auditable source appendix used by the PDF report."""
    configured_sources = config.get("rss_sources", {})
    enabled_sources = []
    applicable_count = 0

    for source_id, source in configured_sources.items():
        source_country = str(source.get("country") or "multi").lower()
        if country_code and source_country not in {country_code, "multi"}:
            continue
        applicable_count += 1
        if not source.get("enabled", True):
            continue

        source_country = str(source.get("country") or "multi").lower()
        category_code = str(source.get("category") or "country_news").lower()
        priority = float(source.get("priority", 1.0))
        freshness_days = max(
            int(source.get("freshness_days", 1)),
            int(report_days),
        )
        enabled_sources.append(
            {
                "id": source_id,
                "name": sanitize_public_text(str(source.get("name") or source_id)),
                "country": COUNTRY_DISPLAY_NAMES.get(source_country, "跨国"),
                "country_code": source_country,
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
        "disabled_count": applicable_count - len(enabled_sources),
        "columns": [enabled_sources[:midpoint], enabled_sources[midpoint:]],
        "weight_rules": [
            {
                "range": "W3.0",
                "meaning": "官方监管机构及权威行业研究，排序优先级最高",
                "meaning_en": "Official regulators and authoritative research; highest source priority.",
            },
            {
                "range": "W1.9-2.7",
                "meaning": "垂直科技、商业、调查及产业媒体，强调专业相关性",
                "meaning_en": "Specialist technology, business, survey and industry sources.",
            },
            {
                "range": "W0.9-1.8",
                "meaning": "综合新闻与舆情补充源，需更强内容相关性",
                "meaning_en": "General news and sentiment sources subject to stricter relevance checks.",
            },
        ],
        "filter_rules": [
            (
                "源级过滤：仅采集启用 RSS，并执行来源关键词；周报统一保留近 7 天合格内容。"
                if report_days >= 7
                else "源级过滤：仅采集启用 RSS，并执行来源关键词；常规源保留 1 天，低频官方/行业源保留 7 天。"
            ),
            (
                f"去重与候选池：按链接、标题及语义去重；AI 处理前每类最多保留 {pre_ai_max_per_category} 条，并仅保留与本国直接相关的候选。"
                if country_code
                else f"去重与候选池：按链接、标题及语义去重；AI 处理前每类最多保留 {pre_ai_max_per_category} 条，并优先保障七国候选覆盖。"
            ),
            "AI 复核：过滤低质、不安全及弱相关内容，按文章实际价值重分类，并给出 1-5 分用研重要性。",
            (
                f"最终排序：依次参考重要性、来源权重与时效；再次核验本国相关性，最终每类最多 {max_per_category} 条。"
                if country_code
                else f"最终排序：依次参考重要性、来源权重与时效；每类对有合格内容的国家优先保留 1 条，最终每类最多 {max_per_category} 条。"
            ),
        ],
        "filter_rules_en": [
            (
                "Source filtering: collect enabled feeds only, apply source keywords, and retain qualified content from the latest seven days for the weekly report."
                if report_days >= 7
                else "Source filtering: collect enabled feeds only, apply source keywords, use a 1-day window for regular feeds and 7 days for low-frequency official or industry feeds."
            ),
            (
                f"Deduplication and candidate pool: deduplicate by URL, title and semantics; retain up to {pre_ai_max_per_category} pre-AI candidates per category and keep only material directly relevant to this country."
                if country_code
                else f"Deduplication and candidate pool: deduplicate by URL, title and semantics; retain up to {pre_ai_max_per_category} pre-AI candidates per category while protecting seven-country coverage."
            ),
            "AI review: remove low-quality, unsafe and weakly relevant content, reclassify by actual insight value, and assign a 1-5 research-importance score.",
            (
                f"Final ranking: combine importance, source weight and recency; revalidate country relevance and cap each category at {max_per_category}."
                if country_code
                else f"Final ranking: combine importance, source weight and recency; reserve coverage for countries with qualified items and cap each category at {max_per_category}."
            ),
        ],
    }
