"""
RSS feed collector - handles all RSS-based sources.
"""

import asyncio
import re
from datetime import datetime, timezone
from typing import Optional
import aiohttp
import feedparser
from .base import BaseCollector, NewsItem


class RSSCollector(BaseCollector):
    """Collect news from RSS feeds."""

    def __init__(self, source_id: str, source_config: dict):
        super().__init__(source_config)
        self.source_id = source_id
        self.feed_url = source_config["url"]
        self.source_name = source_config["name"]
        self.category = source_config.get("category", "general")
        self.keywords = source_config.get("keywords", [])
        self.require_keywords = source_config.get("require_keywords", [])
        self.max_items = source_config.get("max_items", 10)
        self.country = source_config.get("country")
        self.source_priority = float(source_config.get("priority", 1.0))
        freshness_days = source_config.get("freshness_days")
        self.freshness_days = (
            float(freshness_days) if freshness_days is not None else None
        )

    async def collect(self) -> list[NewsItem]:
        """Fetch and parse RSS feed."""
        if not self.is_enabled():
            return []

        try:
            # Use a browser-like User-Agent to avoid being blocked (e.g. by 36Kr)
            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"
            }
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    self.feed_url,
                    timeout=aiohttp.ClientTimeout(total=30, connect=10),
                    headers=headers,
                    allow_redirects=True
                ) as response:
                    if response.status != 200:
                        print(f"[{self.source_name}] HTTP {response.status}")
                        return []

                    try:
                        content = await response.text()
                    except aiohttp.ClientPayloadError:
                        # Fallback for partial payloads - some servers are buggy
                        content = await response.read()
                        content = content.decode('utf-8', errors='replace')
        except Exception as e:
            print(f"[{self.source_name}] Fetch error: {type(e).__name__}: {e}")
            return []

        # Parse feed
        feed = feedparser.parse(content)
        items = []

        # Some search/official feeds are relevance-sorted instead of date-sorted.
        # Normalising here prevents an old high-ranking entry from crowding out a
        # new regulator or market update before the downstream date filter runs.
        entries = list(feed.entries)
        entries.sort(
            key=lambda entry: self._parse_date(entry)
            or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )

        for entry in entries[:self.max_items * 5]:  # Fetch extra for keyword filtering
            title = entry.get("title", "")

            # 优先使用 content (通常包含完整文章), 其次是 summary/description
            content_list = entry.get("content", [])
            full_content = ""
            if content_list:
                # 寻找 text/html 或 text/plain
                for c in content_list:
                    if c.get("type") in ["text/html", "text/plain"]:
                        full_content = c.get("value", "")
                        break

            # Check if content is invalid (anti-bot)
            if self._is_invalid_content(full_content):
                # Fallback to summary if content is invalid
                full_content = ""

            # 如果 content 为空 (或无效)，尝试使用 summary_detail 或 summary
            if not full_content:
                full_content = entry.get("summary", entry.get("description", ""))

            # Clean HTML tags for filtering and display
            clean_content = self._clean_html(full_content)

            # Filter out invalid content (anti-bot responses) - Final check
            if self._is_invalid_content(clean_content):
                print(f"[{self.source_name}] Skipped invalid content: {title}")
                continue

            # Apply the primary OR-list. If a second list is configured, at
            # least one term from that list must match as well. Six-country
            # sources do not use require_keywords, so their behaviour is
            # unchanged.
            combined_text = f"{title} {clean_content}"
            if not self.filter_by_keywords(combined_text, self.keywords):
                continue
            if not self.filter_by_required_keywords(
                combined_text,
                self.require_keywords,
            ):
                continue

            # Parse publish date
            published = self._parse_date(entry)

            # Extract image URL
            image_url = self._extract_image(entry, full_content)

            item = NewsItem(
                title=title,
                url=entry.get("link", ""),
                source=self.source_name,
                category=self.category,
                published=published,
                summary=clean_content[:1000],  # 保留更多内容给 LLM 总结
                content=clean_content,         # 保存完整内容
                author=entry.get("author"),
                tags=[tag.term for tag in entry.get("tags", [])][:5],
                image_url=image_url,
                country=self.country,
                source_priority=self.source_priority,
                freshness_days=self.freshness_days,
            )
            items.append(item)

            if len(items) >= self.max_items:
                break

        print(f"[{self.source_name}] Collected {len(items)} items")
        return items

    def _extract_image(self, entry, summary: str) -> Optional[str]:
        """从RSS条目中提取图片URL"""
        # 方法1: media:content 或 media:thumbnail
        if hasattr(entry, 'media_content') and entry.media_content:
            for media in entry.media_content:
                if media.get('type', '').startswith('image/') or media.get('medium') == 'image':
                    return media.get('url')

        if hasattr(entry, 'media_thumbnail') and entry.media_thumbnail:
            return entry.media_thumbnail[0].get('url')

        # 方法2: enclosure
        if hasattr(entry, 'enclosures') and entry.enclosures:
            for enc in entry.enclosures:
                if enc.get('type', '').startswith('image/'):
                    return enc.get('href') or enc.get('url')

        # 方法3: 从 content 中提取 <img> 标签
        content = entry.get('content', [{}])[0].get('value', '') if entry.get('content') else ''
        full_text = summary + content

        img_match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', full_text)
        if img_match:
            img_url = img_match.group(1)
            # 过滤掉太小的图片（通常是图标）
            if not any(x in img_url.lower() for x in ['icon', 'logo', 'avatar', '1x1', 'pixel']):
                return img_url

        # 方法4: image 字段
        if hasattr(entry, 'image') and entry.image:
            if isinstance(entry.image, dict):
                return entry.image.get('href') or entry.image.get('url')
            elif isinstance(entry.image, str):
                return entry.image

        return None

    def _parse_date(self, entry) -> Optional[datetime]:
        """Parse date from feed entry."""
        for date_field in ["published_parsed", "updated_parsed", "created_parsed"]:
            time_struct = entry.get(date_field)
            if time_struct:
                try:
                    return datetime(*time_struct[:6], tzinfo=timezone.utc)
                except:
                    pass
        return None

    def _clean_html(self, text: str) -> str:
        """Remove HTML tags from text but preserve some structure."""
        if not text:
            return ""

        # Replace block elements and breaks with newlines to preserve structure
        text = re.sub(r'<(p|div|br|li|h[1-6]|tr)[^>]*>', '\n', text, flags=re.IGNORECASE)

        # Remove all other tags
        text = re.sub(r'<[^>]+>', '', text)

        # Collapse multiple spaces but preserve newlines
        lines = []
        for line in text.split('\n'):
            cleaned_line = re.sub(r'\s+', ' ', line).strip()
            if cleaned_line:
                lines.append(cleaned_line)

        return '\n'.join(lines)


    def _is_invalid_content(self, text: str) -> bool:
        """Check if content is an anti-bot response or invalid."""
        if not text:
            return False
        invalid_markers = [
            "request result",
            "enable javascript",
            "javascript is disabled",
            "please enable js",
            "access denied",
            "security check"
        ]
        text_lower = text.lower()
        return any(marker in text_lower for marker in invalid_markers)


async def collect_all_rss(rss_config: dict) -> list[NewsItem]:
    """Collect from all configured RSS sources."""
    collectors = []

    for source_id, source_config in rss_config.items():
        if source_config.get("enabled", True):
            collectors.append(RSSCollector(source_id, source_config))

    # Run all collectors concurrently
    tasks = [c.collect() for c in collectors]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    all_items = []
    for result in results:
        if isinstance(result, list):
            all_items.extend(result)
        elif isinstance(result, Exception):
            print(f"Collector error: {result}")

    return all_items
