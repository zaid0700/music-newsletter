"""
Step 4: find the releases in the collected items.

  1. Filter:  a quick keyword check drops obvious non-music news (tours, lawsuits...).
  2. Extract: Claude reads the remaining items and lists every release they mention
              (artist, title, album/EP/single, genre, release date, review score).
              Results are cached in data/extracted.json, so each item is only ever
              sent to Claude once.
  3. Merge:   the same release covered by several sources becomes one entry with
              several mentions.
  4. Dates:   albums/EPs with no release date are looked up on MusicBrainz
              (free, official API). Lookups are cached in data/musicbrainz.json.

Output: data/releases.json

Run it with:   python scripts/analyse.py
Needs the ANTHROPIC_API_KEY environment variable (a GitHub secret).
"""

import json
import os
import re
import sys
import time
import unicodedata
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "sources.yaml"
ITEMS = ROOT / "data" / "items.json"
EXTRACTED = ROOT / "data" / "extracted.json"
MB_CACHE = ROOT / "data" / "musicbrainz.json"
RELEASES = ROOT / "data" / "releases.json"

MODEL = "claude-haiku-5-5"
BATCH_SIZE = 20

# --------------------------------------------------------------------------- 1. filter

# For general news feeds, only keep items whose headline looks like it's about music
# coming out. Review and video feeds are always kept.
RELEASE_HINTS = re.compile(
    r"\b(album|ep|lp|mixtape|single|tape|record|deluxe|debut|out now|out today|"
    r"out friday|announces?|announced|shares?|hear|listen|stream|premiere|"
    r"new (song|music|track|video)|review|drops?|released?|surprise)\b"
    r"|[–—-]\s*[“\"‘']",  # Stereogum's  Artist – “Song”  format
    re.IGNORECASE,
)


def looks_like_release(item: dict) -> bool:
    if item["source_type"] in ("reviews", "video", "allmusic"):
        return True
    return bool(RELEASE_HINTS.search(item["title"]))


# --------------------------------------------------------------------------- 2. extract

INSTRUCTIONS = """You read music publication feed items and list the NEW MUSIC RELEASES each one is about.

For every item, return its item_id and a list of releases (an empty list if it isn't about a new release).

What counts as a release:
- New albums, EPs, mixtapes, and singles/songs (a new song or video for a new song counts as a single).
- Include it whether the item is a review, an announcement, a new-song post, or news about the release.
- An announcement often covers two releases: "X announces album Y: hear 'Z'" = album Y (with its announced
  release date) AND single Z (out on the item's published date).

Not a release (return nothing for these):
- Tours, concerts, live TV performances, interviews, lawsuits, awards, gossip, merchandise.
- Anniversary pieces, reissues, remasters, box sets, and old music.
- Lists or roundups where the item doesn't name both the artist and the title.

Fields:
- artist: as credited; join collaborators with " & ". Don't put featured artists here.
- title: the release title only, without "(feat. ...)" or quote marks.
- type: album, ep, mixtape or single.
- genre: the best fit of: {genres}, or "Other" (country, jazz, classical, folk, etc.).
  Metal, punk and emo count as Rock / Alternative / Indie. R&B and soul count as Hip-hop / R&B.
- release_date: YYYY-MM-DD when the release came out or comes out. Work out phrases like "out Friday" or
  "next month" from the item's published date. A new song shared today is out on the published date.
  Use null if you can't tell.
- mention: review, announcement, new_song, release_news or list.
- score: a review score converted to a 0-10 scale (e.g. 8.1, or 7 for "7/10"), only if the item states one. Otherwise null.

Only use what the item says. Don't guess artists or titles that aren't in the text."""


def release_schema(genres: list[str]) -> dict:
    release = {
        "type": "object",
        "properties": {
            "artist": {"type": "string"},
            "title": {"type": "string"},
            "type": {"type": "string", "enum": ["album", "ep", "mixtape", "single"]},
            "genre": {"type": "string", "enum": genres + ["Other"]},
            "release_date": {"type": ["string", "null"]},
            "mention": {
                "type": "string",
                "enum": ["review", "announcement", "new_song", "release_news", "list"],
            },
            "score": {"type": ["number", "null"]},
        },
        "required": ["artist", "title", "type", "genre", "release_date", "mention", "score"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "item_id": {"type": "string"},
                        "releases": {"type": "array", "items": release},
                    },
                    "required": ["item_id", "releases"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["items"],
        "additionalProperties": False,
    }


def describe(item: dict) -> str:
    published = item.get("published") or item["collected"]
    day = datetime.fromisoformat(published)
    lines = [
        f"item_id: {item['id']}",
        f"source: {item['source']}",
        f"published: {day:%Y-%m-%d} ({day:%A})",
        f"title: {item['title']}",
    ]
    if item.get("summary"):
        lines.append(f"summary: {item['summary'][:600]}")
    if item.get("categories"):
        lines.append(f"tags: {', '.join(item['categories'][:12])}")
    return "\n".join(lines)


def extract_batch(client, batch: list[dict], genres: list[str]) -> dict:
    """Send one batch of items to Claude. Returns {item_id: [releases]}."""
    prompt = "\n\n---\n\n".join(describe(i) for i in batch)
    response = client.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=INSTRUCTIONS.format(genres=", ".join(genres)),
        messages=[{"role": "user", "content": f"Feed items:\n\n{prompt}"}],
        output_config={"format": {"type": "json_schema", "schema": release_schema(genres)}},
    )
    if response.stop_reason in ("max_tokens", "refusal"):
        raise RuntimeError(f"Claude stopped early ({response.stop_reason})")
    text = next(block.text for block in response.content if block.type == "text")
    wanted = {i["id"] for i in batch}
    return {
        row["item_id"]: row["releases"]
        for row in json.loads(text)["items"]
        if row["item_id"] in wanted
    }


def extract(items: list[dict], genres: list[str]) -> dict:
    cache = json.loads(EXTRACTED.read_text()) if EXTRACTED.exists() else {}
    todo = [i for i in items if i["id"] not in cache and looks_like_release(i)]
    print(f"Extract: {len(todo)} new items to read "
          f"({len(items)} stored, {len(cache)} already read)")
    if not todo:
        return cache

    import anthropic  # imported here so the rest of the script works without it

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    for start in range(0, len(todo), BATCH_SIZE):
        batch = todo[start:start + BATCH_SIZE]
        try:
            found = extract_batch(client, batch, genres)
        except Exception as err:  # noqa: BLE001 - one bad batch shouldn't stop the rest
            print(f"  batch {start // BATCH_SIZE + 1}: FAILED ({err}); will retry next run")
            continue
        cache.update(found)
        count = sum(len(r) for r in found.values())
        print(f"  batch {start // BATCH_SIZE + 1}: {len(found)} items read, {count} releases found")
        EXTRACTED.write_text(json.dumps(cache, indent=1, ensure_ascii=False))

    # Forget items that are no longer stored, so the cache doesn't grow forever.
    stored = {i["id"] for i in items}
    cache = {k: v for k, v in cache.items() if k in stored}
    EXTRACTED.write_text(json.dumps(cache, indent=1, ensure_ascii=False))
    return cache


# --------------------------------------------------------------------------- 3. merge

def norm(text: str) -> str:
    """Simplify a name for matching: 'The Weeknd (feat. X)' -> 'weeknd'."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = re.sub(r"[(\[].*?(feat|ft\.|with|deluxe|edition|version|remix)[^)\]]*[)\]]", "", text)
    text = re.sub(r"\s(feat\.?|ft\.?)\s.*$", "", text)
    text = text.replace("&", " and ").replace("$", "s")
    text = re.sub(r"^the\s+", "", text.strip())
    return re.sub(r"[^a-z0-9]", "", text)


def same_artist(a: str, b: str) -> bool:
    # "andersonpaakandcordae" vs "andersonpaak" -> same release
    return a == b or (min(len(a), len(b)) >= 3 and (a in b or b in a))


LONG_FORM = ("album", "ep", "mixtape")


def dates_close(a, b) -> bool:
    if not (valid_date(a) and valid_date(b)):
        return True
    gap = abs(datetime.fromisoformat(a) - datetime.fromisoformat(b))
    return gap <= timedelta(days=7)


def same_release(rel: dict, other: dict) -> bool:
    """Same title (already checked) plus a matching artist. For albums/EPs with a
    distinctive title, a different artist credit still counts if the dates agree,
    e.g. 'Shabazz Palaces' vs its frontman 'Ishmael Butler' for the same album."""
    if same_artist(norm(rel["artist"]), norm(other["artist"])):
        return True
    return (rel["type"] in LONG_FORM and other["type"] in LONG_FORM
            and len(norm(rel["title"])) >= 6
            and dates_close(rel["release_date"], other["release_date"]))


def most_common(values, prefer_not=None):
    values = [v for v in values if v]
    if prefer_not:
        better = [v for v in values if v != prefer_not]
        values = better or values
    return Counter(values).most_common(1)[0][0] if values else None


def merge(items: list[dict], extracted: dict) -> list[dict]:
    by_id = {i["id"]: i for i in items}
    groups: dict[str, list[list[tuple]]] = {}  # title key -> clusters of (release, item)

    for item_id, releases in extracted.items():
        item = by_id.get(item_id)
        if not item:
            continue
        for rel in releases:
            tkey, akey = norm(rel["title"]), norm(rel["artist"])
            if not tkey or not akey:
                continue
            clusters = groups.setdefault(tkey, [])
            for cluster in clusters:
                if same_release(rel, cluster[0][0]):
                    cluster.append((rel, item))
                    break
            else:
                clusters.append([(rel, item)])

    merged = []
    for clusters in groups.values():
        for cluster in clusters:
            rels = [r for r, _ in cluster]
            seen_links = set()
            mentions = []
            for rel, item in cluster:
                if item["link"] in seen_links and rel["mention"] != "review":
                    continue
                seen_links.add(item["link"])
                mentions.append({
                    "source": item["source"],
                    "source_type": item["source_type"],
                    "weight": item["weight"],
                    "mention": rel["mention"],
                    "score": rel["score"],
                    "headline": item["title"],
                    "link": item["link"],
                    "published": item.get("published") or item["collected"],
                })
            # Credit used most often wins; a review's credit counts double.
            credits = Counter()
            for r in rels:
                credits[r["artist"]] += 2 if r["mention"] == "review" else 1
            merged.append({
                "artist": credits.most_common(1)[0][0],
                "title": most_common([r["title"] for r in rels]),
                "type": most_common([r["type"] for r in rels]),
                "genre": most_common([r["genre"] for r in rels], prefer_not="Other"),
                "release_date": most_common([r["release_date"] for r in rels
                                             if valid_date(r["release_date"])]),
                "date_source": "feeds",
                # one score per article (a Pitchfork review also listed in Best New Albums
                # is the same article, so its score only counts once)
                "scores": list({m["link"]: m["score"] for m in mentions
                                if m["score"] is not None}.values()),
                "sources": sorted({m["source"] for m in mentions}),
                "mentions": sorted(mentions, key=lambda m: m["published"], reverse=True),
            })
    return merged


def valid_date(value) -> bool:
    return bool(value) and bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value))


# --------------------------------------------------------------------------- 4. dates

def mb_lookup(artist: str, title: str, session: requests.Session) -> str | None:
    """Ask MusicBrainz for a release's first release date (YYYY-MM-DD) or None."""
    def q(s):
        return s.replace("\\", "").replace('"', "")
    query = f'releasegroup:"{q(title)}" AND artist:"{q(artist)}"'
    resp = session.get(
        "https://musicbrainz.org/ws/2/release-group/",
        params={"query": query, "fmt": "json", "limit": 5},
        timeout=20,
    )
    resp.raise_for_status()
    for group in resp.json().get("release-groups", []):
        date = group.get("first-release-date", "")
        if group.get("score", 0) >= 90 and norm(group.get("title", "")) == norm(title) \
                and valid_date(date):
            return date
    return None


def fill_dates(releases: list[dict]) -> None:
    cache = json.loads(MB_CACHE.read_text()) if MB_CACHE.exists() else {}
    recent = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
    todo = [
        r for r in releases
        if not r["release_date"] and r["type"] in ("album", "ep", "mixtape")
        and r["mentions"][0]["published"] >= recent
    ]
    repo = os.environ.get("GITHUB_REPOSITORY", "personal-project")
    session = requests.Session()
    session.headers["User-Agent"] = f"music-newsletter/1.0 ( https://github.com/{repo} )"

    looked_up = 0
    for rel in todo:
        key = f"{norm(rel['artist'])}|{norm(rel['title'])}"
        if key not in cache:
            try:
                cache[key] = mb_lookup(rel["artist"], rel["title"], session)
            except requests.RequestException as err:
                print(f"  MusicBrainz lookup failed for {rel['artist']} - {rel['title']}: {err}")
                continue
            looked_up += 1
            time.sleep(1.1)  # MusicBrainz allows 1 request per second
        if cache[key]:
            rel["release_date"] = cache[key]
            rel["date_source"] = "musicbrainz"

    MB_CACHE.write_text(json.dumps(cache, indent=1, ensure_ascii=False))
    filled = sum(1 for r in todo if r["release_date"])
    print(f"Dates: {len(todo)} albums/EPs had no date; "
          f"{looked_up} looked up on MusicBrainz, {filled} filled")


# --------------------------------------------------------------------------- main

def main() -> None:
    if not ITEMS.exists():
        sys.exit("No data/items.json yet. Run scripts/collect.py first.")
    genres = yaml.safe_load(CONFIG.read_text())["genres"]
    items = json.loads(ITEMS.read_text())

    extracted = extract(items, genres)
    releases = merge(items, extracted)
    fill_dates(releases)

    releases.sort(key=lambda r: (r["release_date"] or "", len(r["mentions"])), reverse=True)
    RELEASES.write_text(json.dumps(releases, indent=2, ensure_ascii=False))

    dated = sum(1 for r in releases if r["release_date"])
    multi = sum(1 for r in releases if len(r["sources"]) > 1)
    print(f"\nReleases: {len(releases)} found ({dated} with a release date, "
          f"{multi} covered by more than one source). Saved to data/releases.json")


if __name__ == "__main__":
    main()
