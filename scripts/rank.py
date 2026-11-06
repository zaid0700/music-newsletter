"""
Step 5: rank the week's releases.

  1. Week:   keeps releases that came out in the newsletter week, Saturday to
             Friday (UAE time), ending on the Friday the newsletter is sent.
             Reviewed albums with no known date are kept if they were reviewed
             during that week.
  2. Stars:  looks up each artist's fan count on Deezer (free, no key). Cached in
             data/deezer.json and refreshed every 30 days.
  3. Taste:  optional: how close each artist is to what you listen to on Last.fm
             (see personal.py).
  4. Score:  combines taste and popularity using config/ranking.yaml.

Output: data/ranked.json  (top releases overall, plus top releases per genre)

Run it with:             python scripts/rank.py
For a specific week:     python scripts/rank.py --week-ending 2026-10-30   (a Friday)
"""

import argparse
import json
import math
import re
import sys
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
SOURCES = ROOT / "config" / "sources.yaml"
RANKING = ROOT / "config" / "ranking.yaml"
RELEASES = ROOT / "data" / "releases.json"
DEEZER_CACHE = ROOT / "data" / "deezer.json"
RANKED = ROOT / "data" / "ranked.json"
FEATURED = ROOT / "data" / "featured.json"   # releases in each sent issue (newsletter.py)

UAE = ZoneInfo("Asia/Dubai")
FANS_FOR_FULL_STAR_POWER = 10_000_000
DEEZER_REFRESH_DAYS = 30


# --------------------------------------------------------------------------- 1. week

def newsletter_week(week_ending: date | None = None) -> tuple[date, date]:
    """Return (Saturday, Friday) of the newsletter week in UAE time.
    Run on a Friday, that's last Saturday to today. On any other day, it's the
    week that ended on the most recent Friday."""
    if week_ending is None:
        today = datetime.now(UAE).date()
        days_since_friday = (today.weekday() - 4) % 7  # Friday = 4, so 0 on a Friday
        week_ending = today - timedelta(days=days_since_friday)
    if week_ending.weekday() != 4:
        sys.exit(f"--week-ending must be a Friday; {week_ending} is a {week_ending:%A}")
    return week_ending - timedelta(days=6), week_ending


def uae_date(iso: str) -> date:
    return datetime.fromisoformat(iso).astimezone(UAE).date()


LIVE = re.compile(r"\blive(\s+\S+){0,2}\s+(at|from)\b"          # Live at..., Live 25 at...
                  r"|(^|[:\-\u2013\u2014(\[])\s*live\s+(in|on)\b"   # Live in Paris, Album: Live in Tokyo
                  r"|\blive\s+'?\d{2,4}\b"                       # Live 2025, Live '99
                  r"|[(\[]\s*live\b|\blive\s*[)\]]"               # (Live), [Live]
                  r"|\blive\s+(album|recording|session)s?\b|^live$|\bunplugged\b", re.IGNORECASE)


def is_live(rel: dict) -> bool:
    """Live albums are left out of the newsletter."""
    return bool(LIVE.search(rel["title"]))


def release_key(rel: dict) -> str:
    return f"{norm(main_artist(rel['artist']))}|{bare_title(rel['title'])}"


def late_friday(rel: dict, start: date, featured: dict) -> bool:
    """Released on the previous Friday but covered after that issue went out
    (it's sent at 8:00 on Friday), so it wasn't in it. Include it this week."""
    prev_friday = (start - timedelta(days=1)).isoformat()
    if rel["release_date"] != prev_friday or release_key(rel) in set(featured.get(prev_friday, [])):
        return False
    # Only if it was first covered after last week's issue (sent Saturday morning).
    # Anything covered earlier was available to that issue, so it's not "missed".
    first = min((uae_date(m["published"]) for m in rel["mentions"]), default=None)
    return first is not None and first >= start


def in_week(rel: dict, start: date, end: date) -> bool:
    if rel["release_date"]:
        return start <= date.fromisoformat(rel["release_date"]) <= end
    # No known date: keep albums/EPs that were reviewed during the week.
    return rel["type"] != "single" and any(
        m["mention"] == "review" and start <= uae_date(m["published"]) <= end
        for m in rel["mentions"]
    )


# --------------------------------------------------------------------------- 1b. album singles

TRACKLIST_CACHE = ROOT / "data" / "tracklists.json"


def bare_title(title: str) -> str:
    """'Song (feat. X) [Radio Edit]' -> normalised 'song'."""
    return norm(re.sub(r"\s*[(\[].*?[)\]]", "", title))


def same_artist(a: str, b: str) -> bool:
    x, y = norm(main_artist(a)), norm(main_artist(b))
    return bool(x and y) and (x == y or x in y or y in x)


def deezer_tracklist(album: dict, session: requests.Session, cache: dict) -> list[str]:
    """Bare track titles of the album on Deezer ([] if not found). Cached."""
    key = f"{norm(album['artist'])}|{norm(album['title'])}"
    if cache.get(key):
        return cache[key]
    tracks = []
    try:
        resp = session.get("https://api.deezer.com/search/album", timeout=20,
                           params={"q": f'artist:"{main_artist(album["artist"])}" album:"{album["title"]}"',
                                   "limit": 10})
        resp.raise_for_status()
        for hit in resp.json().get("data", []):
            if bare_title(hit.get("title", "")) == bare_title(album["title"]) and \
                    same_artist(hit.get("artist", {}).get("name", ""), album["artist"]):
                t = session.get(f"https://api.deezer.com/album/{hit['id']}/tracks",
                                params={"limit": 100}, timeout=20)
                t.raise_for_status()
                tracks = [bare_title(x.get("title", "")) for x in t.json().get("data", [])]
                break
        time.sleep(0.2)
    except requests.RequestException as err:
        print(f"  Deezer tracklist lookup failed for {album['artist']} - {album['title']}: {err}")
    cache[key] = tracks
    return tracks


def drop_album_singles(releases: list[dict]) -> list[dict]:
    """If an album/EP/mixtape is in this week's list, drop singles from it.
    A single counts as 'from the album' if it was announced in the same article,
    or its title is on the album's Deezer tracklist. Its coverage moves to the album."""
    albums = [r for r in releases if r["type"] != "single"]
    singles = [r for r in releases if r["type"] == "single"]
    if not albums or not singles:
        return releases

    cache = json.loads(TRACKLIST_CACHE.read_text()) if TRACKLIST_CACHE.exists() else {}
    session = requests.Session()
    dropped = []
    for single in singles:
        links = {m["link"] for m in single["mentions"]}
        for album in albums:
            if not same_artist(single["artist"], album["artist"]):
                continue
            same_article = bool(links & {m["link"] for m in album["mentions"]})
            if same_article or bare_title(single["title"]) in deezer_tracklist(album, session, cache):
                known = {m["link"] for m in album["mentions"]}
                album["mentions"] += [m for m in single["mentions"] if m["link"] not in known]
                dropped.append(single)
                print(f"  Dropped single {single['artist']} - {single['title']} "
                      f"(from the album {album['title']})")
                break
    TRACKLIST_CACHE.write_text(json.dumps(cache, indent=1, ensure_ascii=False))
    return [r for r in releases if r not in dropped]


# --------------------------------------------------------------------------- 1c. real release dates

DATES_CACHE = ROOT / "data" / "release_dates.json"


def deezer_release_date(rel: dict, session: requests.Session) -> str | None:
    """The release's actual release date on Deezer (YYYY-MM-DD), or None."""
    resp = session.get("https://api.deezer.com/search/album", timeout=20,
                       params={"q": f'artist:"{main_artist(rel["artist"])}" album:"{rel["title"]}"',
                               "limit": 10})
    resp.raise_for_status()
    for hit in resp.json().get("data", []):
        if bare_title(hit.get("title", "")) == bare_title(rel["title"]) and \
                same_artist(hit.get("artist", {}).get("name", ""), rel["artist"]):
            album = session.get(f"https://api.deezer.com/album/{hit['id']}", timeout=20)
            album.raise_for_status()
            date_ = album.json().get("release_date", "")
            return date_ if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_ or "") else None
    return None


def verify_release_dates(releases: list[dict]) -> None:
    """Replace each release's date with Deezer's where Deezer has it. Dates guessed
    from articles can be wrong (e.g. a review published days after release)."""
    cache = json.loads(DATES_CACHE.read_text()) if DATES_CACHE.exists() else {}
    session = requests.Session()
    changed = 0
    for rel in releases:
        key = release_key(rel)
        if key not in cache:
            try:
                cache[key] = deezer_release_date(rel, session)
            except requests.RequestException as err:
                print(f"  Deezer date lookup failed for {rel['artist']} - {rel['title']}: {err}")
                continue
            time.sleep(0.2)
        real = cache[key]
        if real and real != rel.get("release_date"):
            print(f"  Date corrected: {rel['artist']} - {rel['title']}: "
                  f"{rel.get('release_date') or 'unknown'} -> {real} (Deezer)")
            rel["release_date"], rel["date_source"] = real, "deezer"
            changed += 1
    DATES_CACHE.write_text(json.dumps(cache, indent=1, ensure_ascii=False))
    print(f"Dates: {changed} corrected from Deezer")


# --------------------------------------------------------------------------- 2. stars

def norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = text.replace("&", "and").replace("$", "s")
    text = re.sub(r"^the\s+", "", text.strip())
    return re.sub(r"[^a-z0-9]", "", text)


def main_artist(artist: str) -> str:
    """'Anderson .Paak & Cordae' -> 'Anderson .Paak' (first credited artist)."""
    return re.split(r"\s+(?:&|and|x|with|,)\s+|,\s*", artist, maxsplit=1)[0].strip()


def deezer_fans(name: str, session: requests.Session) -> int | None:
    """Fan count of the Deezer artist whose name matches, or None if no match."""
    resp = session.get("https://api.deezer.com/search/artist",
                       params={"q": name, "limit": 10}, timeout=20)
    resp.raise_for_status()
    wanted = norm(name)
    matches = [a for a in resp.json().get("data", []) if norm(a.get("name", "")) == wanted]
    if not matches:
        return None
    return max(a.get("nb_fan", 0) for a in matches)  # same name twice: take the big one


def add_star_power(releases: list[dict]) -> None:
    cache = json.loads(DEEZER_CACHE.read_text()) if DEEZER_CACHE.exists() else {}
    stale = (datetime.now(timezone.utc) - timedelta(days=DEEZER_REFRESH_DAYS)).isoformat()
    session = requests.Session()
    looked_up = 0

    for rel in releases:
        name = main_artist(rel["artist"])
        key = norm(name)
        entry = cache.get(key)
        if not entry or entry["checked"] < stale:
            try:
                fans = deezer_fans(name, session)
            except requests.RequestException as err:
                print(f"  Deezer lookup failed for {name}: {err}")
                fans = entry["fans"] if entry else None
            else:
                cache[key] = entry = {"name": name, "fans": fans,
                                      "checked": datetime.now(timezone.utc).isoformat()}
                looked_up += 1
                time.sleep(0.2)  # stay well under Deezer's rate limit
        fans = (entry or {}).get("fans") or 0
        rel["fans"] = fans
        rel["star_power"] = round(min(1.0, math.log10(fans + 1) /
                                      math.log10(FANS_FOR_FULL_STAR_POWER)), 3)

    DEEZER_CACHE.write_text(json.dumps(cache, indent=1, ensure_ascii=False))
    print(f"Stars: {looked_up} artists looked up on Deezer, "
          f"{len(releases) - looked_up} from cache")


# --------------------------------------------------------------------------- 3. score

def add_scores(releases: list[dict], cfg: dict, source_weights: dict,
               use_personal: bool = False) -> None:
    """Two signals, each 0-1:
      personal    how close the artist is to your Last.fm listening
      popularity  how widely it was covered this week (number of publications,
                  weighted by sources.yaml) plus the artist's Deezer fans
    Review scores are deliberately not used: most reviews land after release day.
    """
    for rel in releases:
        # One count per publication (Pitchfork's three feeds count as one Pitchfork),
        # using that publication's highest source weight.
        pubs: dict[str, float] = {}
        for m in rel["mentions"]:
            pub = m["source"].split(" (")[0]
            weight = source_weights.get(m["source"], m.get("weight", 1.0))
            pubs[pub] = max(pubs.get(pub, 0.0), weight)
        rel["coverage"] = round(sum(pubs.values()), 2)
        rel["publications"] = sorted(pubs)

    top_coverage = max((r["coverage"] for r in releases), default=0) or 1
    pop = cfg["popularity_mix"]
    w = {"popularity": cfg["weights"]["popularity"]}
    if use_personal:
        w["personal"] = cfg["weights"]["personal"]
    total = sum(w.values()) or 1
    for rel in releases:
        rel["popularity"] = round(pop["coverage"] * rel["coverage"] / top_coverage
                                  + pop["artist_fans"] * rel["star_power"], 3)
        rel.setdefault("personal", 0.0)
        rel.setdefault("personal_reason", "")
        mix = sum(weight * rel[signal] for signal, weight in w.items()) / total
        rel["rank_score"] = round(cfg["type_weight"].get(rel["type"], 1.0) * mix, 3)
        rel["avg_score"] = round(sum(rel["scores"]) / len(rel["scores"]), 1) if rel["scores"] else None


# --------------------------------------------------------------------------- 5. discover

def pick_discoveries(releases: list[dict], cfg: dict) -> list[dict]:
    """Releases by artists you've NEVER played but who are similar to ones you do,
    favouring smaller / underground artists. Needs Last.fm (for similar artists)."""
    max_fans = cfg.get("max_artist_fans", 500_000)
    picks = [r for r in releases
             if r.get("personal_reason", "").startswith("Similar to")   # new to you
             and r.get("fans", 0) <= max_fans]                          # not a big name
    for r in picks:
        niche = 1 - r.get("star_power", 0)          # 1.0 = tiny artist, 0 = huge
        r["discover_score"] = round(r["personal"] * (0.6 + 0.4 * niche), 3)
    picks.sort(key=lambda r: r["discover_score"], reverse=True)
    return picks[:cfg.get("count", 5)]


# --------------------------------------------------------------------------- main

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--week-ending", type=date.fromisoformat,
                        help="Friday that ends the week (default: today if Friday, else the most recent one)")
    args = parser.parse_args()

    if not RELEASES.exists():
        sys.exit("No data/releases.json yet. Run scripts/analyse.py first.")
    sources = yaml.safe_load(SOURCES.read_text())
    cfg = yaml.safe_load(RANKING.read_text())
    genres = sources["genres"]
    source_weights = {s["name"]: s.get("weight", 1.0) for s in sources["sources"]}

    start, end = newsletter_week(args.week_ending)
    featured = json.loads(FEATURED.read_text()) if FEATURED.exists() else {}
    # Never repeat a release that was in an earlier issue (re-running this
    # week's issue is fine: only OTHER weeks count)
    shown_before = {k for wk, keys in featured.items() if wk != end.isoformat() for k in keys}
    def in_window(r):
        return in_week(r, start, end) or late_friday(r, start, featured)
    candidates = [r for r in json.loads(RELEASES.read_text())
                  if r["genre"] in genres and not is_live(r) and in_window(r)]
    verify_release_dates(candidates)          # real dates from Deezer...
    candidates = [r for r in candidates if in_window(r)]   # ...then check again
    releases = [r for r in candidates if release_key(r) not in shown_before]
    if len(releases) < len(candidates):
        print(f"Skipped {len(candidates) - len(releases)} release(s) already in an earlier issue")
    late = sum(1 for r in releases if late_friday(r, start, featured))
    print(f"Week: Sat {start} to Fri {end}: {len(releases)} releases in your genres"
          + (f" (incl. {late} from last Friday, covered after last issue)" if late else ""))
    releases = drop_album_singles(releases)

    add_star_power(releases)
    from personal import add_personal  # imported here: personal.py imports this file
    use_personal = add_personal(releases, (cfg.get("lastfm_user") or "").strip())
    add_scores(releases, cfg, source_weights, use_personal)
    releases.sort(key=lambda r: r["rank_score"], reverse=True)

    top = releases[:cfg["top_overall"]]
    discover = pick_discoveries(releases[cfg["top_overall"]:], cfg.get("discover", {}))
    rest = [r for r in releases if r not in discover]   # each release appears once
    result = {
        "week_start": start.isoformat(),
        "week_end": end.isoformat(),
        "generated": datetime.now(timezone.utc).isoformat(),
        "top": top,
        "discover": discover,
        "by_genre": {g: [r for r in rest if r["genre"] == g][:cfg["per_genre"]]
                     for g in genres},
    }
    RANKED.write_text(json.dumps(result, indent=2, ensure_ascii=False))

    print(f"\nTop {len(result['top'])} this week")
    print(f"{'#':>2}  {'score':>5}  {'you':>5}  {'pop':>5}  {'pubs':>4}  release")
    for i, r in enumerate(result["top"], 1):
        print(f"{i:>2}  {r['rank_score']:>5.2f}  {r['personal']:>5.2f}  "
              f"{r['popularity']:>5.2f}  {len(r['publications']):>4}  "
              f"{r['artist']} - {r['title']} ({r['type']}, {r['genre']})"
              + (f"  [{r['personal_reason']}]" if r["personal_reason"] else ""))
    print(f"\nDiscover: {len(result['discover'])}")
    for r in result["discover"]:
        print(f"    {r['discover_score']:.2f}  {r['artist']} - {r['title']} ({r['type']})  "
              f"[{r['personal_reason']}; {r.get('fans', 0):,} Deezer fans]")
    for g, rels in result["by_genre"].items():
        print(f"\n{g}: {len(rels)}")
        for r in rels:
            print(f"    {r['rank_score']:.2f}  {r['artist']} - {r['title']} ({r['type']})")
    print("\nSaved to data/ranked.json")


if __name__ == "__main__":
    main()
