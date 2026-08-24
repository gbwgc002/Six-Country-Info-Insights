"""
Base collector interface and common utilities.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
import hashlib


@dataclass
class NewsItem:
    """Represents a single news/article item."""
    title: str
    url: str
    source: str
    category: str
    published: Optional[datetime] = None
    summary: Optional[str] = None
    content: Optional[str] = None
    author: Optional[str] = None
    tags: list[str] = field(default_factory=list)
    score: float = 0.0  # For ranking/importance
    is_translated: bool = False  # 是否已翻译成中文
    image_url: Optional[str] = None  # 配图URL
    organization: Optional[str] = None  # 机构/公司标签
    country: Optional[str] = None  # 归属国家；多国内容使用 "multi"
    source_priority: float = 1.0  # 来源权威度/专业度，配置驱动
    relevance_score: float = 0.0  # AI 判断的用研价值分
    freshness_days: Optional[float] = None  # 官方/行业低频源可使用更长窗口

    @property
    def id(self) -> str:
        """Generate unique ID based on URL."""
        return hashlib.md5(self.url.encode()).hexdigest()[:12]

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "url": self.url,
            "source": self.source,
            "category": self.category,
            "published": self.published.isoformat() if self.published else None,
            "summary": self.summary,
            "author": self.author,
            "tags": self.tags,
            "score": self.score,
            "is_translated": self.is_translated,
            "image_url": self.image_url,
            "organization": self.organization,
            "country": self.country,
            "source_priority": self.source_priority,
            "relevance_score": self.relevance_score,
            "freshness_days": self.freshness_days,
        }


class BaseCollector(ABC):
    """Abstract base class for all collectors."""

    def __init__(self, config: dict):
        self.config = config
        self.name = self.__class__.__name__

    @abstractmethod
    async def collect(self) -> list[NewsItem]:
        """Collect news items from the source."""
        pass

    def is_enabled(self) -> bool:
        """Check if this collector is enabled."""
        return self.config.get("enabled", True)

    def filter_by_keywords(self, text: str, keywords: list[str]) -> bool:
        """Check if text contains any of the keywords."""
        if not keywords:
            return True  # No filter = accept all
        text_lower = text.lower()
        return any(kw.lower() in text_lower for kw in keywords)

    def filter_by_required_keywords(
        self,
        text: str,
        required_keywords: list[str],
    ) -> bool:
        """Check a second OR-list that is ANDed with the primary list."""
        if not required_keywords:
            return True
        return self.filter_by_keywords(text, required_keywords)
