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


def in_week(rel: dict, start: date, end: date) -> bool:
    if rel["release_date"]:
        return start <= date.fromisoformat(rel["release_date"]) <= end
    # No known date: keep albums/EPs that were reviewed during the week.
    return rel["type"] != "single" and any(
        m["mention"] == "review" and start <= uae_date(m["published"]) <= end
        for m in rel["mentions"]
    )


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
    releases = [r for r in json.loads(RELEASES.read_text())
                if r["genre"] in genres and in_week(r, start, end)]
    print(f"Week: Sat {start} to Fri {end}: {len(releases)} releases in your genres")

    add_star_power(releases)
    from personal import add_personal  # imported here: personal.py imports this file
    use_personal = add_personal(releases, (cfg.get("lastfm_user") or "").strip())
    add_scores(releases, cfg, source_weights, use_personal)
    releases.sort(key=lambda r: r["rank_score"], reverse=True)

    result = {
        "week_start": start.isoformat(),
        "week_end": end.isoformat(),
        "generated": datetime.now(timezone.utc).isoformat(),
        "top": releases[:cfg["top_overall"]],
        "by_genre": {g: [r for r in releases if r["genre"] == g][:cfg["per_genre"]]
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
    for g, rels in result["by_genre"].items():
        print(f"\n{g}: {len(rels)}")
        for r in rels:
            print(f"    {r['rank_score']:.2f}  {r['artist']} - {r['title']} ({r['type']})")
    print("\nSaved to data/ranked.json")


if __name__ == "__main__":
    main()
