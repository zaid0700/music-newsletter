"""
Album covers and Spotify links for the newsletter (used by newsletter.py).

  Cover:    found on Deezer (free, no key). Albums/EPs are searched as albums,
            singles as tracks (their cover is the single's artwork).
  Spotify:  the Deezer link is converted to a direct Spotify link with Odesli
            (song.link, free, no key). Odesli's free API is being retired, so if it
            doesn't answer, the link opens a Spotify search for the release instead.

Results are cached in data/media.json. Anything not found is retried after 3 days,
since new releases sometimes reach Deezer or Odesli late.
"""

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import requests

from rank import main_artist, norm

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "media.json"
RETRY_AFTER_DAYS = 3
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


def deezer_find(rel: dict, session: requests.Session) -> dict | None:
    """Return {"deezer_url", "cover"} for the release, or None."""
    artist, title = main_artist(rel["artist"]), rel["title"]
    if rel["type"] == "single":
        path, query = "track", f'artist:"{artist}" track:"{title}"'
    else:
        path, query = "album", f'artist:"{artist}" album:"{title}"'

    resp = session.get(f"https://api.deezer.com/search/{path}",
                       params={"q": query, "limit": 10}, timeout=20)
    resp.raise_for_status()
    for hit in resp.json().get("data", []):
        if not (title_matches(hit.get("title", ""), title)
                and artist_matches(hit.get("artist", {}).get("name", ""), rel["artist"])):
            continue
        album = hit.get("album", hit)  # a track result carries its album's artwork
        cover = album.get("cover_big") or album.get("cover_medium")
        return {"deezer_url": hit.get("link"), "cover": cover}
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
            return None
        return resp.json().get("linksByPlatform", {}).get("spotify", {}).get("url")


def add_media(releases: list[dict]) -> None:
    """Adds rel["cover"] (may be None) and rel["spotify"] (always set) to each release."""
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    now = datetime.now(timezone.utc)
    retry = (now - timedelta(days=RETRY_AFTER_DAYS)).isoformat()
    session = requests.Session()
    odesli = Odesli(session)
    covers = direct = 0

    for rel in releases:
        key = f"{norm(rel['artist'])}|{norm(rel['title'])}|{rel['type']}"
        entry = cache.get(key, {})
        complete = entry.get("cover") and entry.get("spotify")

        if not complete and entry.get("checked", "") < retry:
            if not entry.get("deezer_url"):
                try:
                    entry.update(deezer_find(rel, session) or {})
                except requests.RequestException as err:
                    print(f"  Deezer lookup failed for {rel['artist']} - {rel['title']}: {err}")
                time.sleep(0.2)
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
