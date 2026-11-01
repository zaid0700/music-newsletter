"""
Step 3: daily collection.

Reads every feed in config/sources.yaml and adds any items it hasn't seen before
to data/items.json. Items older than KEEP_DAYS are dropped so the file stays small.

Run it with:   python scripts/collect.py
GitHub runs it automatically every day (see .github/workflows/collect.yml).
"""

import hashlib
import html
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import requests
import yaml

import allmusic
import sputnik

PAGE_SOURCES = {"allmusic": allmusic, "sputnik": sputnik}

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "sources.yaml"
DATA = ROOT / "data" / "items.json"
KEEP_DAYS = 30
HEADERS = {"User-Agent": "Mozilla/5.0 (personal music newsletter; RSS reader)"}


def clean_text(raw: str, limit: int = 1000) -> str:
    """Strip HTML tags and extra whitespace from a feed summary."""
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def published_utc(entry) -> str | None:
    """Return the entry's publish time as an ISO string in UTC, if it has one."""
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    return datetime(*parsed[:6], tzinfo=timezone.utc).isoformat()


def item_id(source_name: str, entry) -> str:
    """A stable ID per item per source, so the same article is never stored twice
    from one feed, but a review that also appears in "Best New Albums" is kept for
    both (that second appearance is a ranking signal)."""
    key = entry.get("id") or entry.get("link") or entry.get("title", "")
    return hashlib.sha1(f"{source_name}|{key}".encode("utf-8")).hexdigest()[:16]


def fetch(url: str) -> bytes:
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.content


def load_items() -> dict:
    if DATA.exists():
        return {item["id"]: item for item in json.loads(DATA.read_text())}
    return {}


def save_items(items: dict) -> None:
    DATA.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(items.values(), key=lambda i: i.get("published") or "", reverse=True)
    DATA.write_text(json.dumps(ordered, indent=2, ensure_ascii=False))


def collect_page(source: dict, items: dict, now: datetime) -> int:
    """AllMusic and Sputnikmusic have no feeds, so their New Releases pages are
    read directly (see allmusic.py and sputnik.py)."""
    reader = PAGE_SOURCES[source["type"]]
    try:
        albums = reader.fetch_releases()
    except (allmusic.Blocked, requests.RequestException) as err:
        print(f"SKIP  {source['name']}: {err}")
        return 0
    if not albums:
        print(f"SKIP  {source['name']}: page loaded but no albums found "
              "(the site may have changed its layout)")
        return 0

    new_here = 0
    for entry in reader.as_items(albums, source, now):
        iid = item_id(source["name"], {"link": entry["link"]})
        if iid in items:
            continue
        items[iid] = {
            "id": iid,
            "source": source["name"],
            "source_type": "page",
            "weight": source.get("weight", 1.0),
            "title": clean_text(entry["title"], 300),
            "link": entry["link"],
            "summary": clean_text(entry["summary"]),
            "categories": [],
            "published": entry["published"],
            "collected": now.isoformat(),
        }
        new_here += 1
    print(f"OK    {source['name']}: {new_here} new of {len(albums)}")
    return new_here


def main() -> None:
    config = yaml.safe_load(CONFIG.read_text())
    items = load_items()
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=KEEP_DAYS)).isoformat()
    added = 0

    for source in config["sources"]:
        if source.get("type") in PAGE_SOURCES:
            added += collect_page(source, items, now)
            continue
        try:
            feed = feedparser.parse(fetch(source["url"]))
        except requests.RequestException as err:
            # One broken feed shouldn't stop the others.
            print(f"SKIP  {source['name']}: {err}")
            continue

        new_here = 0
        for entry in feed.entries:
            iid = item_id(source["name"], entry)
            published = published_utc(entry)
            if iid in items or (published and published < cutoff):
                continue  # already stored, or too old to matter
            items[iid] = {
                "id": iid,
                "source": source["name"],
                "source_type": source.get("type", "news"),
                "weight": source.get("weight", 1.0),
                "title": clean_text(entry.get("title", ""), 300),
                "link": entry.get("link", ""),
                "summary": clean_text(entry.get("summary", "")),
                "categories": [t.get("term") for t in entry.get("tags", []) if t.get("term")],
                "published": published,
                "collected": now.isoformat(),
            }
            new_here += 1
        added += new_here
        print(f"OK    {source['name']}: {new_here} new of {len(feed.entries)}")

    # Drop anything older than KEEP_DAYS (by publish date, or collection date if none).
    before = len(items)
    items = {k: v for k, v in items.items() if (v["published"] or v["collected"]) >= cutoff}

    save_items(items)
    print(f"\nAdded {added} items, removed {before - len(items)} old ones. Total stored: {len(items)}")


if __name__ == "__main__":
    main()
