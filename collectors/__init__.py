"""
Collectors package - all news collection modules.
"""

from .base import NewsItem, BaseCollector
from .rss_collector import RSSCollector, collect_all_rss
from .arxiv_collector import ArxivCollector, collect_arxiv
from .twitter_collector import TwitterCollector, collect_twitter
from .hackernews_collector import HackerNewsCollector, collect_hackernews
from .waytoagi_collector import WayToAGICollector, collect_waytoagi

__all__ = [
    "NewsItem",
    "BaseCollector",
    "RSSCollector",
    "collect_all_rss",
    "ArxivCollector",
    "collect_arxiv",
    "TwitterCollector",
    "collect_twitter",
    "HackerNewsCollector",
    "collect_hackernews",
    "WayToAGICollector",
    "collect_waytoagi",
]
