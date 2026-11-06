"""
Album covers and Spotify links for the newsletter (used by newsletter.py).

If Spotify is set up (see spotify.py), each release's Spotify link and artwork come
from Spotify search. Otherwise, or if Spotify has no match, covers come from Deezer (free, no key). Albums/EPs are searched as albums and
singles as tracks, with looser searches if the exact one finds nothing (singles
are often listed on Deezer as one-track albums). If Deezer has nothing, Apple's
iTunes Search API (also free, no key) is tried.

Results are cached in data/media.json. Anything not found is retried on the next
run, since new releases sometimes reach Deezer late.
"""

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from rank import main_artist, norm

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "media.json"
MAX_TAGS = 3   # Spotify genre tags shown per release


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


ITUNES_GAP_SECONDS = 3.1  # Apple allows about 20 searches a minute
_last_itunes = 0.0


def itunes_cover(rel: dict, session: requests.Session) -> str | None:
    """Fallback: Apple's iTunes Search API (free, no key). Returns a 600x600 cover."""
    global _last_itunes
    artist, title = main_artist(rel["artist"]), rel["title"]
    entities = ["album", "song"] if rel["type"] == "single" else ["album"]
    for entity in entities:
        wait = ITUNES_GAP_SECONDS - (time.monotonic() - _last_itunes)
        if wait > 0:
            time.sleep(wait)
        _last_itunes = time.monotonic()
        resp = session.get("https://itunes.apple.com/search",
                           params={"term": f"{artist} {title}", "media": "music",
                                   "entity": entity, "limit": 15, "country": "US"},
                           timeout=20)
        resp.raise_for_status()
        for hit in resp.json().get("results", []):
            name = hit.get("collectionName", "") if entity == "album" else hit.get("trackName", "")
            name = name.replace(" - Single", "").replace(" - EP", "")
            if (title_matches(name, title) and artist_matches(hit.get("artistName", ""), rel["artist"])
                    and hit.get("artworkUrl100")):
                return hit["artworkUrl100"].replace("100x100bb", "600x600bb")
    return None


def find_cover(rel: dict, session: requests.Session) -> tuple[str | None, str]:
    """Try Deezer, then iTunes. Returns (cover URL or None, what happened)."""
    problems = []
    for name, finder in (("Deezer", deezer_cover), ("iTunes", itunes_cover)):
        try:
            cover = finder(rel, session)
        except (requests.RequestException, ValueError) as err:
            problems.append(f"{name} failed: {err}")
            continue
        if cover:
            return cover, name
        problems.append(f"not on {name}")
    return None, "; ".join(problems)


UPPER = {"r&b": "R&B", "uk": "UK", "us": "US", "edm": "EDM", "idm": "IDM", "lo-fi": "Lo-fi"}


def tag_label(tag: str) -> str:
    """"rage rap" -> "Rage rap", "uk drill" -> "UK drill", "r&b" -> "R&B"."""
    words = [UPPER.get(w, w) for w in tag.split()]
    text = " ".join(words)
    return text[:1].upper() + text[1:]


def add_media(releases: list[dict]) -> None:
    """Adds rel["cover"] (image URL or None) and rel["spotify"] (link or None).
    Spotify is tried first for both (if set up); Deezer then iTunes fill any gaps."""
    import spotify  # optional; does nothing without the SPOTIFY_* secrets
    sp = spotify.client()
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    now = datetime.now(timezone.utc)
    session = requests.Session()
    found_count, links, tagged, by_source = 0, 0, 0, {}
    week_ago = (now - timedelta(days=7)).isoformat()

    for rel in releases:
        key = f"{norm(rel['artist'])}|{norm(rel['title'])}|{rel['type']}"
        entry = cache.get(key, {})

        # Spotify: link + artwork (looked up again each run until found)
        if sp.configured and not entry.get("spotify"):
            try:
                hit = sp.find(rel)
            except requests.RequestException as err:
                print(f"  Spotify search failed for {rel['artist']} - {rel['title']}: {err}")
                hit = None
            if hit:
                entry["spotify"] = hit["url"]
                entry["artist_id"] = hit.get("artist_id")
                if hit["image"]:
                    entry["cover"], entry["source"] = hit["image"], "Spotify"
                    by_source["Spotify"] = by_source.get("Spotify", 0) + 1

        # Spotify genre tags for the artist (rechecked weekly if Spotify had none)
        if sp.configured and (entry.get("tags") is None or
                              (not entry["tags"] and entry.get("tags_checked", "") < week_ago)):
            try:
                if not entry.get("artist_id"):
                    hit = sp.find(rel)
                    entry["artist_id"] = hit.get("artist_id") if hit else None
                entry["tags"] = sp.artist_genres(entry.get("artist_id"))
                entry["tags_checked"] = now.isoformat()
            except requests.RequestException as err:
                print(f"  Spotify genres failed for {rel['artist']}: {err}")

        # Deezer / iTunes artwork if there's still no cover
        if not entry.get("cover"):
            cover, how = find_cover(rel, session)
            if cover:
                by_source[how] = by_source.get(how, 0) + 1
                entry["cover"], entry["source"] = cover, how
            else:
                print(f"  No cover: {rel['artist']} - {rel['title']} ({rel['type']}): {how}")
        entry["checked"] = now.isoformat()
        cache[key] = entry

        rel["cover"] = entry.get("cover")
        rel["spotify"] = entry.get("spotify")
        rel["tags"] = [tag_label(t) for t in (entry.get("tags") or [])][:MAX_TAGS]
        tagged += bool(rel["tags"])
        found_count += bool(rel["cover"])
        links += bool(rel["spotify"])

    CACHE.parent.mkdir(exist_ok=True)
    CACHE.write_text(json.dumps(cache, indent=1, ensure_ascii=False))
    new = ", ".join(f"{n} new from {s}" for s, n in by_source.items())
    print(f"Covers: {found_count}/{len(releases)} found" + (f" ({new})" if new else ""))
    if sp.configured:
        print(f"Spotify links: {links}/{len(releases)}; genre tags: {tagged}/{len(releases)}")
    for rel in releases[:3]:
        if rel["cover"]:
            print(f"  e.g. {rel['artist']} - {rel['title']}: {rel['cover']}")
