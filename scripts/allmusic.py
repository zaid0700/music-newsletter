"""
Reads AllMusic's weekly New Releases page (used by collect.py).

AllMusic has no RSS feed, so this reads the web page directly, once per run, as a
plain script (no browser disguise). If AllMusic blocks the request or changes its
page layout, collect.py skips it and carries on with the other sources.

The page is read by looking for AllMusic's album and artist links, which have a
stable format (/album/<name>-mw0001234567 and /artist/<name>-mn0001234567),
rather than relying on the page's styling.
"""

import html
import re
from datetime import datetime, timezone

import requests

URL = "https://www.allmusic.com/newreleases"
HEADERS = {
    "User-Agent": "music-newsletter/1.0 (personal weekly newsletter; one request per day)",
    "Accept": "text/html",
}

ALBUM = re.compile(r'<a[^>]+href="(?P<url>(?:https://www\.allmusic\.com)?/album/[^"#?]*?-mw\d{10})[^"]*"[^>]*>(?P<text>.*?)</a>',
                   re.S | re.I)
ARTIST = re.compile(r'<a[^>]+href="(?:https://www\.allmusic\.com)?/artist/[^"]*?-mn\d{10}[^"]*"[^>]*>(?P<text>.*?)</a>',
                    re.S | re.I)
DATE = re.compile(r"(January|February|March|April|May|June|July|August|September|October|"
                  r"November|December)\s+\d{1,2},\s+\d{4}")


class Blocked(Exception):
    pass


def text_of(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", " ", fragment)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def fetch_page(session: requests.Session) -> str:
    resp = session.get(URL, headers=HEADERS, timeout=30)
    if resp.status_code in (403, 429, 503) or "Just a moment" in resp.text[:2000]:
        raise Blocked(f"AllMusic refused the request (HTTP {resp.status_code})")
    resp.raise_for_status()
    return resp.text


def parse(page: str) -> list[dict]:
    """Return [{artist, title, link, blurb, week}] for each album on the page."""
    week = None
    m = DATE.search(text_of(page[:20000]))
    if m:
        week = datetime.strptime(m.group(0), "%B %d, %Y").date().isoformat()

    # Group album links by album: each album usually has a cover link (no text)
    # and a title link. A block runs from its first link to the next album's first link.
    blocks: list[dict] = []
    for m in ALBUM.finditer(page):
        url = m.group("url")
        if url.startswith("/"):
            url = "https://www.allmusic.com" + url
        if blocks and blocks[-1]["link"] == url:
            blocks[-1]["last"] = m
        else:
            blocks.append({"link": url, "first": m, "last": m})
        title = text_of(m.group("text"))
        if title and not blocks[-1].get("title"):
            blocks[-1]["title"] = title

    # Work out the page layout from the first album: does the artist link come
    # before the album title, or after it? Then use that for every album.
    titled = [b for b in blocks if b.get("title")]
    if not titled:
        return []
    first_artist = ARTIST.search(page)
    title_link = next(m for m in ALBUM.finditer(page) if text_of(m.group("text")))
    artist_first = bool(first_artist) and first_artist.start() < title_link.start()

    albums, seen = [], set()
    for i, b in enumerate(blocks):
        if not b.get("title") or b["link"] in seen:
            continue
        seen.add(b["link"])
        prev_end = blocks[i - 1]["last"].end() if i > 0 else 0
        next_start = blocks[i + 1]["first"].start() if i + 1 < len(blocks) else b["last"].end() + 3000

        if artist_first:   # artist ... title
            found = ARTIST.findall(page[prev_end:b["last"].start()])
            artist = text_of(found[-1]) if found else ""
        else:              # title ... artist
            found = ARTIST.findall(page[b["last"].end():next_start])
            artist = text_of(found[0]) if found else ""

        rest = ARTIST.sub("", page[b["last"].end():next_start])
        blurb = re.sub(r"^(by|from)\s+", "", text_of(rest), flags=re.I)[:600]
        albums.append({"artist": artist, "title": b["title"], "link": b["link"],
                       "blurb": blurb, "week": week})
    return [a for a in albums if a["artist"]]


def fetch_releases() -> list[dict]:
    """Fetch and parse the page. Raises Blocked or requests.RequestException."""
    with requests.Session() as session:
        return parse(fetch_page(session))


def as_items(albums: list[dict], source: dict, now: datetime) -> list[dict]:
    """Turn albums into the same item format the RSS feeds produce."""
    items = []
    for a in albums:
        summary = (f"Listed on AllMusic's New Releases page"
                   + (f" for the week of {a['week']}" if a["week"] else "")
                   + f". {a['blurb']}")
        published = (datetime.fromisoformat(a["week"]).replace(tzinfo=timezone.utc).isoformat()
                     if a["week"] else now.isoformat())
        items.append({
            "link": a["link"],
            "title": f"{a['artist']} - {a['title']} (new release)",
            "summary": summary[:1000],
            "published": published,
        })
    return items
