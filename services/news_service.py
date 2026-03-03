"""
World + tech news aggregator (RSS) with a curated source list and 5-minute cache.

This stays deliberately "source-driven": we don't try to "decide truth" algorithmically,
we show the headline, source, timestamp, and link so you can cross-check quickly.

If you want to tune sources, edit `NEWS_FEEDS` below or override via data/news_sources.json.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Any
import datetime as _dt
import feedparser  # lightweight pure-python dependency

from services.settings_store import load_settings

DEFAULT_SOURCES_PATH = Path("data") / "news_sources.json"

# Categories: tech, world, security, science, business
NEWS_FEEDS: Dict[str, List[Dict[str, str]]] = {
    "tech": [
        {"name": "Ars Technica", "url": "https://feeds.arstechnica.com/arstechnica/index"},
        {"name": "The Verge", "url": "https://www.theverge.com/rss/index.xml"},
        {"name": "Wired", "url": "https://www.wired.com/feed/rss"},
        {"name": "MIT Technology Review", "url": "https://www.technologyreview.com/feed/"},
        {"name": "Hacker News", "url": "https://news.ycombinator.com/rss"},
    ],
    "world": [
        {"name": "BBC World", "url": "http://feeds.bbci.co.uk/news/world/rss.xml"},
        {"name": "Reuters World", "url": "https://feeds.reuters.com/Reuters/worldNews"},
        {"name": "AP News", "url": "https://apnews.com/apf-topnews?output=1"},  # may fail; RSS-ish
        {"name": "DW", "url": "https://rss.dw.com/rdf/rss-en-world"},
        {"name": "Al Jazeera", "url": "https://www.aljazeera.com/xml/rss/all.xml"},
        {"name": "The Guardian World", "url": "https://www.theguardian.com/world/rss"},
    ],
    "security": [
        {"name": "KrebsOnSecurity", "url": "https://krebsonsecurity.com/feed/"},
        {"name": "The Hacker News", "url": "https://thehackernews.com/feeds/posts/default"},
        {"name": "BleepingComputer", "url": "https://www.bleepingcomputer.com/feed/"},
    ],
    "science": [
        {"name": "ScienceDaily", "url": "https://www.sciencedaily.com/rss/top/science.xml"},
        {"name": "Nature", "url": "https://www.nature.com/subjects/science/rss"},
    ],
    "business": [
        {"name": "Reuters Business", "url": "https://feeds.reuters.com/reuters/businessNews"},
        {"name": "The Economist", "url": "https://www.economist.com/rss/the-world-this-week"},
    ],
}


def _load_override_sources() -> Optional[Dict[str, List[Dict[str, str]]]]:
    if not DEFAULT_SOURCES_PATH.exists():
        return None
    try:
        data = json.loads(DEFAULT_SOURCES_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        return None
    return None


@dataclass
class NewsItem:
    title: str
    source: str
    url: str
    published: Optional[str] = None
    summary: Optional[str] = None


class NewsCache:
    def __init__(self):
        self._cache: Dict[str, Dict[str, Any]] = {}

    def get(self, category: str) -> Optional[List[NewsItem]]:
        s = load_settings()
        ttl = max(30, int(s.news_refresh_seconds))
        entry = self._cache.get(category)
        if not entry:
            return None
        if time.time() - entry["ts"] > ttl:
            return None
        return entry["items"]

    def set(self, category: str, items: List[NewsItem]) -> None:
        self._cache[category] = {"ts": time.time(), "items": items}


_cache = NewsCache()


def fetch_news(category: str, limit: int = 40) -> List[NewsItem]:
    category = (category or "tech").lower()
    cached = _cache.get(category)
    if cached is not None:
        return cached[:limit]

    feeds = (_load_override_sources() or NEWS_FEEDS).get(category, NEWS_FEEDS["tech"])
    items: List[NewsItem] = []

    for f in feeds:
        try:
            parsed = feedparser.parse(f["url"])
            for e in parsed.entries[: max(10, limit)]:
                title = getattr(e, "title", "").strip()
                link = getattr(e, "link", "").strip()
                if not title or not link:
                    continue
                published = getattr(e, "published", None) or getattr(e, "updated", None)
                summary = getattr(e, "summary", None)
                items.append(NewsItem(title=title, source=f["name"], url=link, published=published, summary=summary))
        except Exception:
            continue

    # de-dup by url
    seen = set()
    dedup = []
    for it in items:
        if it.url in seen:
            continue
        seen.add(it.url)
        dedup.append(it)

    # Sort: if published parseable use it, else keep insertion order
    def _sort_key(it: NewsItem):
        if not it.published:
            return 0.0
        try:
            # feedparser may provide parsed time
            # if not, attempt ISO-ish parse
            return _dt.datetime.fromisoformat(it.published.replace("Z", "+00:00")).timestamp()
        except Exception:
            return 0.0

    dedup.sort(key=_sort_key, reverse=True)

    _cache.set(category, dedup)
    return dedup[:limit]
