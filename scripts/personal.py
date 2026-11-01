"""
Personal taste from Last.fm and/or Spotify (used by rank.py). Both are optional:
Last.fm needs a username in config/ranking.yaml plus the LASTFM_API_KEY secret;
Spotify needs the three SPOTIFY_* secrets (see spotify.py). With neither, ranking
works without taste.

  Spotify playlists: artists on playlists you own or collaborate on. One track
                    scores 0.5; the artist with the most tracks scores 1.0.
                    If an artist is in both Last.fm and Spotify, the higher wins.

  Your artists:     EVERY artist in your Last.fm library, even ones played once.
                    Played once scores 0.5; your most-played artist scores 1.0
                    (log scale, so a handful of plays still counts for a lot).
                    Current favourites (your last-12-months top 200) get +0.1.
  Similar artists:  for release artists you've never played, Last.fm's list of
                    similar artists is checked against yours. A close match to one
                    of your favourites scores up to 0.4, so any artist you've
                    actually played ranks above ones that are merely similar.

Similar-artist lists are cached in data/lastfm.json for 30 days.
"""

import json
import math
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from rank import main_artist, norm

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "lastfm.json"
API = "https://ws.audioscrobbler.com/2.0/"
LIBRARY_PAGE_SIZE = 1000
MAX_LIBRARY_PAGES = 20      # up to 20,000 artists
RECENT_BOOST = 0.1
SIMILAR_LIMIT = 50
SIMILAR_MAX = 0.4           # below the 0.5 floor for artists you've actually played
REFRESH_DAYS = 30


def call(session: requests.Session, method: str, **params) -> dict:
    resp = session.get(API, params={"method": method, "api_key": os.environ["LASTFM_API_KEY"],
                                    "format": "json", **params}, timeout=20)
    data = resp.json()
    if "error" in data:
        raise requests.RequestException(f"Last.fm error {data['error']}: {data.get('message')}")
    resp.raise_for_status()
    time.sleep(0.25)  # Last.fm asks for no more than ~5 requests a second
    return data


def your_artists(session: requests.Session, user: str) -> dict[str, dict]:
    """{normalised name: {"name", "plays", "affinity"}} for EVERY artist in your
    Last.fm library, however few times you've played them.

    Affinity runs from 0.5 (played once) to 1.0 (your most-played artist), on a log
    scale so a few plays still count for a lot. Artists in your last-12-months top
    list get a small boost for being current favourites.
    """
    plays: dict[str, dict] = {}
    page, pages = 1, 1
    while page <= min(pages, MAX_LIBRARY_PAGES):
        data = call(session, "library.getartists", user=user, limit=LIBRARY_PAGE_SIZE, page=page)
        block = data.get("artists", {})
        pages = int(block.get("@attr", {}).get("totalPages", 1) or 1)
        for a in block.get("artist", []):
            key = norm(a["name"])
            if key:
                plays[key] = {"name": a["name"], "plays": int(a.get("playcount") or 0)}
        page += 1

    recent = set()
    try:
        top = call(session, "user.gettopartists", user=user, period="12month", limit=200)
        recent = {norm(a["name"]) for a in top["topartists"]["artist"]}
    except (requests.RequestException, KeyError, ValueError):
        pass

    most = max((p["plays"] for p in plays.values()), default=1) or 1
    for key, p in plays.items():
        affinity = 0.5 + 0.5 * math.log10(1 + p["plays"]) / math.log10(1 + most)
        if key in recent:
            affinity += RECENT_BOOST
        p["affinity"] = round(min(1.0, affinity), 3)
    return plays


def similar_to(session: requests.Session, artist: str, cache: dict) -> list[dict]:
    key = norm(artist)
    stale = (datetime.now(timezone.utc) - timedelta(days=REFRESH_DAYS)).isoformat()
    entry = cache.get(key)
    if entry and entry["checked"] >= stale:
        return entry["similar"]
    try:
        data = call(session, "artist.getsimilar", artist=artist, limit=SIMILAR_LIMIT, autocorrect=1)
        similar = [{"name": a["name"], "match": float(a.get("match", 0))}
                   for a in data.get("similarartists", {}).get("artist", [])]
    except (requests.RequestException, ValueError) as err:
        print(f"  Last.fm similar-artist lookup failed for {artist}: {err}")
        return entry["similar"] if entry else []
    cache[key] = {"similar": similar, "checked": datetime.now(timezone.utc).isoformat()}
    return similar


def spotify_artists() -> dict[str, dict]:
    """Artists on your own Spotify playlists, scored like Last.fm: one track 0.5,
    the artist with the most tracks across your playlists 1.0 (log scale)."""
    import spotify  # optional; does nothing without the SPOTIFY_* secrets
    sp = spotify.client()
    if not sp.configured:
        return {}
    try:
        counts = sp.playlist_artists()
    except (requests.RequestException, KeyError, ValueError) as err:
        print(f"  Spotify: couldn't read your playlists ({err})")
        return {}
    most = max((c["tracks"] for c in counts.values()), default=1) or 1
    for c in counts.values():
        c["affinity"] = round(0.5 + 0.5 * math.log10(1 + c["tracks"]) / math.log10(1 + most), 3)
        n = c["tracks"]
        c["reason"] = f"On your Spotify playlists ({n} track{'s' if n != 1 else ''})"
    return counts


def add_personal(releases: list[dict], user: str) -> bool:
    """Adds rel["personal"] (0-1) and rel["personal_reason"] to each release, from
    Last.fm and/or Spotify playlists. Returns False (and changes nothing) if neither
    is set up or reachable."""
    session = requests.Session()
    lastfm = {}
    if user and os.environ.get("LASTFM_API_KEY"):
        try:
            lastfm = your_artists(session, user)
        except (requests.RequestException, KeyError, ValueError) as err:
            print(f"Personal: couldn't read Last.fm for {user} ({err})")
    for a in lastfm.values():
        n = a["plays"]
        a["reason"] = f"You've played {a['name']} {n:,} time{'s' if n != 1 else ''}"
    playlists = spotify_artists()

    # One combined list: if an artist is in both, the higher score wins.
    mine: dict[str, dict] = {}
    for source in (lastfm, playlists):
        for key, a in source.items():
            if key not in mine or a["affinity"] > mine[key]["affinity"]:
                mine[key] = a
    if not mine:
        print("Personal: neither Last.fm nor Spotify set up (or reachable); ranking without taste")
        return False

    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    direct = similar_count = 0
    for rel in releases:
        name = main_artist(rel["artist"])
        # Check every credited artist ("A & B", "A x B", "A, B and C")
        credited = [rel["artist"]] + [p for p in re.split(r"\s+(?:&|and|x|with|feat\.?|ft\.?)\s+|,\s*",
                                                     rel["artist"]) if p.strip()]
        hits = [mine[norm(n)] for n in credited if norm(n) in mine]
        hit = max(hits, key=lambda h: h["affinity"]) if hits else None
        if hit:
            rel["personal"] = hit["affinity"]
            rel["personal_reason"] = hit["reason"]
            direct += 1
            continue

        best, because = 0.0, []
        if lastfm:  # similar-artist lookups use Last.fm
            for s in similar_to(session, name, cache):
                yours = mine.get(norm(s["name"]))
                if yours:
                    score = SIMILAR_MAX * s["match"] * yours["affinity"]
                    because.append((score, yours["name"]))
                    best = max(best, score)
        rel["personal"] = round(best, 3)
        names = [n for _, n in sorted(because, reverse=True)[:2]]
        rel["personal_reason"] = f"Similar to {', '.join(names)}" if names else ""
        similar_count += bool(names)

    CACHE.parent.mkdir(exist_ok=True)
    CACHE.write_text(json.dumps(cache, indent=1, ensure_ascii=False))
    print(f"Personal: {len(lastfm)} Last.fm artists + {len(playlists)} Spotify playlist artists; "
          f"{direct} releases by artists you listen to, {similar_count} by similar artists")
    return True
