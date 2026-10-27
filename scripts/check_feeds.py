"""
Step 2 check: confirms every feed in config/sources.yaml is reachable and readable.

Run it with:   python scripts/check_feeds.py
It prints one line per feed: OK / FAIL, how many items it holds, and the newest item.
"""

from pathlib import Path

import feedparser
import requests
import yaml

CONFIG = Path(__file__).resolve().parent.parent / "config" / "sources.yaml"
HEADERS = {"User-Agent": "Mozilla/5.0 (personal music newsletter; RSS reader)"}


def check(source: dict) -> None:
    name, url = source["name"], source["url"]
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as err:
        print(f"FAIL  {name}\n      {url}\n      {err}\n")
        return

    feed = feedparser.parse(resp.content)
    if not feed.entries:
        print(f"FAIL  {name}\n      {url}\n      Reachable, but no items found (not a valid feed?)\n")
        return

    newest = feed.entries[0]
    date = newest.get("published") or newest.get("updated") or "no date"
    print(
        f"OK    {name}  ({len(feed.entries)} items)\n"
        f"      feed title: {feed.feed.get('title', '?')}\n"
        f"      newest: {newest.get('title', '?')}  [{date}]\n"
    )


def main() -> None:
    config = yaml.safe_load(CONFIG.read_text())
    for source in config["sources"]:
        check(source)


if __name__ == "__main__":
    main()
