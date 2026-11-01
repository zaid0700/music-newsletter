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
        if source.get("type") in ("allmusic", "sputnik"):
            check_page(source)
        else:
            check(source)


def check_page(source: dict) -> None:
    import allmusic, sputnik  # same folder
    reader = {"allmusic": allmusic, "sputnik": sputnik}[source["type"]]
    try:
        albums = reader.fetch_releases()
    except (allmusic.Blocked, requests.RequestException) as err:
        print(f"FAIL  {source['name']}\n      {err}\n")
        return
    if not albums:
        print(f"FAIL  {source['name']}\n      Page loaded but no albums found\n")
        return
    first = albums[0]
    when = first.get("week") or first.get("month") or "?"
    print(f"OK    {source['name']}  ({len(albums)} albums, {when})\n"
          f"      first: {first['artist']} - {first['title']}\n")


if __name__ == "__main__":
    main()
