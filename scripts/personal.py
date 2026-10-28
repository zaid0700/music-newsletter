"""
Personal taste from your Last.fm account (used by rank.py). Optional: if there's
no Last.fm username in config/ranking.yaml or no LASTFM_API_KEY secret, ranking
works without it.

  Your artists:     your top artists for the last 12 months and all time.
                    An artist near the top of your list scores close to 1.0,
                    one near the bottom about 0.5.
  Similar artists:  for release artists you don't listen to, Last.fm's list of
                    similar artists is checked against yours. A close match to one
                    of your favourites scores up to 0.7, so artists you actually
                    play always rank above ones that are merely similar.

Similar-artist lists are cached in data/lastfm.json for 30 days.
"""

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from rank import main_artist, norm

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "lastfm.json"
API = "https://ws.audioscrobbler.com/2.0/"
TOP_LIMIT = 500
SIMILAR_LIMIT = 50
SIMILAR_MAX = 0.7
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
    """{normalised name: {"name", "affinity"}} from your top artists."""
    mine: dict[str, dict] = {}
    for period in ("12month", "overall"):
        artists = call(session, "user.gettopartists", user=user, period=period,
                       limit=TOP_LIMIT)["topartists"]["artist"]
        for rank, a in enumerate(artists):
            affinity = 1.0 - 0.5 * rank / max(len(artists) - 1, 1)
            key = norm(a["name"])
            if key and affinity > mine.get(key, {}).get("affinity", 0):
                mine[key] = {"name": a["name"], "affinity": round(affinity, 3)}
    return mine


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


def add_personal(releases: list[dict], user: str) -> bool:
    """Adds rel["personal"] (0-1) and rel["personal_reason"] to each release.
    Returns False (and changes nothing) if Last.fm isn't set up or can't be reached."""
    if not user or not os.environ.get("LASTFM_API_KEY"):
        print("Personal: Last.fm not set up; ranking without it")
        return False

    session = requests.Session()
    try:
        mine = your_artists(session, user)
    except (requests.RequestException, KeyError, ValueError) as err:
        print(f"Personal: couldn't read Last.fm for {user} ({err}); ranking without it")
        return False

    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    direct = similar_count = 0
    for rel in releases:
        name = main_artist(rel["artist"])
        hit = mine.get(norm(name)) or mine.get(norm(rel["artist"]))
        if hit:
            rel["personal"] = hit["affinity"]
            rel["personal_reason"] = "In your Last.fm top artists"
            direct += 1
            continue

        best, because = 0.0, []
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
    print(f"Personal: {len(mine)} of your Last.fm artists loaded; {direct} releases by artists "
          f"you listen to, {similar_count} by similar artists")
    return True
