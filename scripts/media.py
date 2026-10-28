"""
Album covers and Spotify links for the newsletter (used by newsletter.py).

  Cover:    found on Deezer (free, no key). Albums/EPs are searched as albums,
            singles as tracks (their cover is the single's artwork).
  Spotify:  the Deezer link is converted to a direct Spotify link with Odesli
            (song.link, free, no key). Odesli's free API is being retired, so if it
            doesn't answer, the link opens a Spotify search for the release instead.

Results are cached in data/media.json. Anything not found is retried on the next
run, since new releases sometimes reach Deezer or Odesli late.
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests

from rank import main_artist, norm

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "media.json"
ODESLI_GAP_SECONDS = 6.5  # Odesli allows 10 requests a minute without a key


def spotify_search_link(artist: str, title: str) -> str:
    return "https://open.spotify.com/search/" + quote(f"{main_artist(artist)} {title}")


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


def deezer_find(rel: dict, session: requests.Session) -> dict | None:
    """Return {"deezer_url", "cover"} for the release, or None.
    Tries the strict search first, then looser ones. Singles are often listed on
    Deezer as one-track 'albums', so both kinds of search are tried."""
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
            return {"deezer_url": hit.get("link"), "cover": cover}
        time.sleep(0.2)
    return None


class Odesli:
    """Deezer link -> Spotify link. Switches itself off for the run if the
    service is gone (410) or rate-limits us (429)."""

    def __init__(self, session: requests.Session):
        self.session = session
        self.available = True
        self.last_call = 0.0

    def spotify(self, deezer_url: str) -> str | None:
        if not self.available:
            return None
        wait = ODESLI_GAP_SECONDS - (time.monotonic() - self.last_call)
        if wait > 0:
            time.sleep(wait)
        self.last_call = time.monotonic()
        try:
            resp = self.session.get("https://api.song.link/v1-alpha.1/links",
                                    params={"url": deezer_url, "userCountry": "AE"},
                                    timeout=20)
        except requests.RequestException as err:
            print(f"  Odesli unreachable ({err}); using Spotify search links")
            self.available = False
            return None
        if resp.status_code in (410, 429):
            print(f"  Odesli returned {resp.status_code}; using Spotify search links")
            self.available = False
            return None
        if resp.status_code != 200:
            print(f"  Odesli returned {resp.status_code} for {deezer_url}")
            return None
        return resp.json().get("linksByPlatform", {}).get("spotify", {}).get("url")


def add_media(releases: list[dict]) -> None:
    """Adds rel["cover"] (may be None) and rel["spotify"] (always set) to each release."""
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    now = datetime.now(timezone.utc)
    session = requests.Session()
    odesli = Odesli(session)
    covers = direct = 0

    for rel in releases:
        key = f"{norm(rel['artist'])}|{norm(rel['title'])}|{rel['type']}"
        entry = cache.get(key, {})

        # Anything still missing is looked up again on every run.
        if not entry.get("deezer_url"):
            try:
                found = deezer_find(rel, session)
                if not found:
                    print(f"  Not on Deezer (yet): {rel['artist']} - {rel['title']} ({rel['type']})")
            except requests.RequestException as err:
                print(f"  Deezer lookup failed for {rel['artist']} - {rel['title']}: {err}")
                found = None
            if found:
                entry.update(found)
        if entry.get("deezer_url") and not entry.get("spotify"):
            entry["spotify"] = odesli.spotify(entry["deezer_url"])
        entry["checked"] = now.isoformat()
        cache[key] = entry

        rel["cover"] = entry.get("cover")
        rel["spotify"] = entry.get("spotify") or spotify_search_link(rel["artist"], rel["title"])
        rel["spotify_direct"] = bool(entry.get("spotify"))
        covers += bool(rel["cover"])
        direct += rel["spotify_direct"]

    CACHE.parent.mkdir(exist_ok=True)
    CACHE.write_text(json.dumps(cache, indent=1, ensure_ascii=False))
    print(f"Media: {covers}/{len(releases)} covers found; "
          f"{direct}/{len(releases)} direct Spotify links (the rest open a Spotify search)")
