"""
Collectors package - news/insight collection modules.
"""

from .base import NewsItem, BaseCollector
from .rss_collector import RSSCollector, collect_all_rss

# Legacy collectors kept in repo but disabled for the six-country insights use case.
# Import them lazily only if someone explicitly needs them.
try:
    from .arxiv_collector import ArxivCollector, collect_arxiv
except ImportError:
    ArxivCollector = None
    collect_arxiv = None

try:
    from .twitter_collector import TwitterCollector, collect_twitter
except ImportError:
    TwitterCollector = None
    collect_twitter = None

try:
    from .hackernews_collector import HackerNewsCollector, collect_hackernews
except ImportError:
    HackerNewsCollector = None
    collect_hackernews = None

try:
    from .waytoagi_collector import WayToAGICollector, collect_waytoagi
except ImportError:
    WayToAGICollector = None
    collect_waytoagi = None

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
