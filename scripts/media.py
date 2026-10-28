"""
Album covers for the newsletter (used by newsletter.py).

Covers come from Deezer (free, no key). Albums/EPs are searched as albums and
singles as tracks, with looser searches if the exact one finds nothing (singles
are often listed on Deezer as one-track albums).

Results are cached in data/media.json. Anything not found is retried on the next
run, since new releases sometimes reach Deezer late.
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from rank import main_artist, norm

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "media.json"


def title_matches(found: str, wanted: str) -> bool:
    # "FUDGE (Deluxe Edition)" still matches "FUDGE"
    f, w = norm(found), norm(wanted)
    return bool(f and w) and (f == w or f.startswith(w) or w.startswith(f))


def artist_matches(found: str, wanted: str) -> bool:
    f, w = norm(found), norm(main_artist(wanted))
    return bool(f and w) and (f == w or f in w or w in f)


def deezer_search(path: str, query: str, session: requests.Session) -> list[dict]:
    resp = session.get(f"https://api.deezer.com/search/{path}",
                       params={"q": query, "limit": 15}, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:  # Deezer reports errors (e.g. quota) with a 200 status
        raise requests.RequestException(f"Deezer error: {data['error']}")
    return data.get("data", [])


def deezer_cover(rel: dict, session: requests.Session) -> str | None:
    """Return the cover image URL for the release, or None."""
    artist, title = main_artist(rel["artist"]), rel["title"]
    loose = f"{artist} {title}"
    if rel["type"] == "single":
        attempts = [("track", f'artist:"{artist}" track:"{title}"'),
                    ("track", loose), ("album", loose)]
    else:
        attempts = [("album", f'artist:"{artist}" album:"{title}"'),
                    ("album", loose), ("track", loose)]

    for path, query in attempts:
        for hit in deezer_search(path, query, session):
            if not (title_matches(hit.get("title", ""), title)
                    and artist_matches(hit.get("artist", {}).get("name", ""), rel["artist"])):
                continue
            album = hit.get("album", hit)  # a track result carries its album's artwork
            cover = album.get("cover_big") or album.get("cover_medium")
            if cover:
                return cover
        time.sleep(0.2)
    return None


def add_media(releases: list[dict]) -> None:
    """Adds rel["cover"] (an image URL, or None) to each release."""
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    now = datetime.now(timezone.utc)
    session = requests.Session()
    found_count = 0

    for rel in releases:
        key = f"{norm(rel['artist'])}|{norm(rel['title'])}|{rel['type']}"
        entry = cache.get(key, {})

        if not entry.get("cover"):  # missing covers are looked up again every run
            try:
                cover = deezer_cover(rel, session)
                if not cover:
                    print(f"  No cover on Deezer (yet): {rel['artist']} - {rel['title']} ({rel['type']})")
            except requests.RequestException as err:
                print(f"  Deezer lookup failed for {rel['artist']} - {rel['title']}: {err}")
                cover = None
            entry = {"cover": cover, "checked": now.isoformat()}
            cache[key] = entry

        rel["cover"] = entry.get("cover")
        found_count += bool(rel["cover"])

    CACHE.parent.mkdir(exist_ok=True)
    CACHE.write_text(json.dumps(cache, indent=1, ensure_ascii=False))
    print(f"Covers: {found_count}/{len(releases)} found")
