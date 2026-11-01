"""
Reads Sputnikmusic's New Releases page (used by collect.py).

Sputnikmusic has no RSS feed, so this reads the page directly, once per run, as a
plain script. The page is grouped by month; only albums under the current or
previous month's heading are kept. If Sputnikmusic blocks the request or changes
its layout, collect.py skips it and carries on with the other sources.

Albums are found by Sputnikmusic's album / review links (/album/123456/... or
/review/123456/...) and artists by their band links (/bands/Name/1234/).
"""

import re
from datetime import date, datetime, timezone

import requests

from allmusic import Blocked, parse_links, text_of

URL = "https://www.sputnikmusic.com/newreleases.php"
BASE = "https://www.sputnikmusic.com"
HEADERS = {
    "User-Agent": "music-newsletter/1.0 (personal weekly newsletter; one request per day)",
    "Accept": "text/html",
}

ALBUM = re.compile(r'<a[^>]+href="(?P<url>(?:https?://(?:www\.)?sputnikmusic\.com)?/(?:album|review)/\d+/[^"#?]*)"[^>]*>(?P<text>.*?)</a>',
                   re.S | re.I)
ARTIST = re.compile(r'<a[^>]+href="(?:https?://(?:www\.)?sputnikmusic\.com)?/bands/[^"]+?/\d+/?"[^>]*>(?P<text>.*?)</a>',
                    re.S | re.I)
MONTH = re.compile(r">\s*(January|February|March|April|May|June|July|August|September|"
                   r"October|November|December)\s+(\d{4})\s*<")
RATING = re.compile(r"\b([0-5]\.\d)\b")


def fetch_page(session: requests.Session) -> str:
    resp = session.get(URL, headers=HEADERS, timeout=30)
    if resp.status_code in (403, 429, 503) or "Just a moment" in resp.text[:2000]:
        raise Blocked(f"Sputnikmusic refused the request (HTTP {resp.status_code})")
    resp.raise_for_status()
    return resp.text


def parse(page: str, today: date | None = None) -> list[dict]:
    today = today or datetime.now(timezone.utc).date()
    this_month = (today.year, today.month)
    last_month = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)

    headings = [(m.start(), (int(m.group(2)),
                             datetime.strptime(m.group(1), "%B").month))
                for m in MONTH.finditer(page)]

    albums = []
    for a in parse_links(page, ALBUM, ARTIST, BASE):
        # Month heading this album sits under (if the page has headings)
        month = None
        for pos, ym in headings:
            if pos < a["pos"]:
                month = ym
        if headings and month not in (this_month, last_month):
            continue

        artist, title = a["artist"], a["title"]
        if not artist and " - " in title:          # "Artist - Album" as one link
            artist, title = (p.strip() for p in title.split(" - ", 1))
        if not artist:
            continue
        rating = RATING.search(a["blurb"][:40])
        albums.append({"artist": artist, "title": title, "link": a["link"],
                       "rating": rating.group(1) if rating else None,
                       "month": f"{month[0]}-{month[1]:02d}" if month else None})
    return albums


def fetch_releases() -> list[dict]:
    """Fetch and parse the page. Raises Blocked or requests.RequestException."""
    with requests.Session() as session:
        return parse(fetch_page(session))


def as_items(albums: list[dict], source: dict, now: datetime) -> list[dict]:
    """Turn albums into the same item format the RSS feeds produce."""
    items = []
    for a in albums:
        summary = "Listed on Sputnikmusic's New Releases page"
        if a["month"]:
            summary += f" ({datetime.strptime(a['month'], '%Y-%m'):%B %Y})"
        summary += "."
        items.append({
            "link": a["link"],
            "title": f"{a['artist']} - {a['title']} (new release)",
            "summary": summary,
            "published": now.isoformat(),
        })
    return items
