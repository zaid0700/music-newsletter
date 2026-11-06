"""
Spotify for the newsletter (used by personal.py and media.py). Optional: without
the three SPOTIFY_* secrets, everything here quietly does nothing.

  Taste:  the artists on your own Spotify playlists (ones you own or collaborate
          on; Spotify doesn't let apps read the contents of other people's
          playlists). The more tracks of theirs across your playlists, the higher.
  Links:  a direct Spotify link for each release, found with Spotify search.
  Art:    the album/single artwork from that same search result.

Needs (GitHub secrets):
  SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET  from your app at developer.spotify.com
  SPOTIFY_REFRESH_TOKEN                     from running scripts/get_spotify_token.py once

Spotify's 2026 rules for apps in Development Mode apply: the account that owns the
app needs Premium, and search returns at most 10 results per request.
"""

import base64
import os
import time

import requests

from rank import main_artist, norm

API = "https://api.spotify.com/v1"
TOKEN_URL = "https://accounts.spotify.com/api/token"
MAX_PLAYLISTS = 200


class Spotify:
    def __init__(self):
        self.client_id = os.environ.get("SPOTIFY_CLIENT_ID", "").strip()
        self.client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET", "").strip()
        self.refresh_token = os.environ.get("SPOTIFY_REFRESH_TOKEN", "").strip()
        self.session = requests.Session()
        self.token = None
        self.user_id = None

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self.refresh_token)

    # ------------------------------------------------------------------ auth
    def login(self) -> bool:
        """Swap the long-lived refresh token for a one-hour access token."""
        if not self.configured:
            return False
        if self.token:
            return True
        auth = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        try:
            resp = self.session.post(TOKEN_URL, timeout=20,
                                     headers={"Authorization": f"Basic {auth}"},
                                     data={"grant_type": "refresh_token",
                                           "refresh_token": self.refresh_token})
            resp.raise_for_status()
        except requests.RequestException as err:
            print(f"  Spotify login failed ({err}); continuing without Spotify")
            self.client_id = ""  # don't keep retrying this run
            return False
        self.token = resp.json()["access_token"]
        self.session.headers["Authorization"] = f"Bearer {self.token}"
        return True

    def get(self, path_or_url: str, **params) -> dict:
        url = path_or_url if path_or_url.startswith("http") else API + path_or_url
        for attempt in range(4):
            resp = self.session.get(url, params=params or None, timeout=20)
            if resp.status_code == 429:  # rate limited: wait as told, then retry
                time.sleep(min(int(resp.headers.get("Retry-After", "2")), 30))
                continue
            resp.raise_for_status()
            return resp.json()
        resp.raise_for_status()
        return {}

    # ------------------------------------------------------------------ taste
    def playlist_artists(self) -> dict[str, dict]:
        """{normalised name: {"name", "tracks"}} for artists on your own playlists."""
        if not self.login():
            return {}
        me = self.get("/me")
        self.user_id = me.get("id")

        playlists, url = [], "/me/playlists"
        params = {"limit": 50}
        while url and len(playlists) < MAX_PLAYLISTS:
            page = self.get(url, **params)
            playlists += page.get("items", [])
            url, params = page.get("next"), {}

        counts: dict[str, dict] = {}
        read = 0
        for pl in playlists:
            mine = (pl.get("owner") or {}).get("id") == self.user_id or pl.get("collaborative")
            if not mine:
                continue  # Spotify only lets apps read the contents of your own playlists
            url, params = f"/playlists/{pl['id']}/items", {"limit": 50}
            while url:
                try:
                    page = self.get(url, **params)
                except requests.RequestException:
                    break
                for entry in page.get("items", []):
                    track = entry.get("item") or entry.get("track") or {}
                    for artist in track.get("artists", []) or []:
                        key = norm(artist.get("name", ""))
                        if key:
                            c = counts.setdefault(key, {"name": artist["name"], "tracks": 0})
                            c["tracks"] += 1
                url, params = page.get("next"), {}
            read += 1
        print(f"  Spotify: {read} of your playlists read, {len(counts)} artists")
        return counts

    # ------------------------------------------------------------------ links + art
    def find(self, rel: dict) -> dict | None:
        """Return {"url", "image"} for the release, or None if Spotify has no match."""
        if not self.login():
            return None
        artist, title = main_artist(rel["artist"]), rel["title"]
        kinds = ["album", "track"] if rel["type"] == "single" else ["album"]
        for kind in kinds:
            data = self.get("/search", q=f'artist:"{artist}" {kind}:"{title}"',
                            type=kind, limit=10)
            for hit in data.get(f"{kind}s", {}).get("items", []) or []:
                if not hit:
                    continue
                names = [a.get("name", "") for a in hit.get("artists", [])]
                if not _title_ok(hit.get("name", ""), title) or not any(_artist_ok(n, rel["artist"]) for n in names):
                    continue
                album = hit.get("album", hit)
                images = album.get("images") or []
                artist_id = next((a.get("id") for a in hit.get("artists", [])
                                  if _artist_ok(a.get("name", ""), rel["artist"])), None)
                return {"url": (hit.get("external_urls") or {}).get("spotify"),
                        "image": images[0]["url"] if images else None,
                        "artist_id": artist_id}
        return None

    def artist_genres(self, artist_id: str) -> list[str]:
        """Spotify's genres for an artist, e.g. ["rage rap", "plugg"]. Often empty
        for new or very niche artists."""
        if not artist_id or not self.login():
            return []
        return self.get(f"/artists/{artist_id}").get("genres", []) or []


def _title_ok(found: str, wanted: str) -> bool:
    f, w = norm(found), norm(wanted)
    return bool(f and w) and (f == w or f.startswith(w) or w.startswith(f))


def _artist_ok(found: str, wanted: str) -> bool:
    f, w = norm(found), norm(main_artist(wanted))
    return bool(f and w) and (f == w or f in w or w in f)


_client = None


def client() -> Spotify:
    """One shared Spotify client per run."""
    global _client
    if _client is None:
        _client = Spotify()
    return _client
