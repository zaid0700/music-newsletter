"""
Step 6: write and send the newsletter.

  1. Write:  Claude (Sonnet) writes a short intro and a 2-3 sentence blurb for each
             release, using only the headlines and summaries the feeds provided.
             If Claude is unavailable, each blurb falls back to the main headline,
             so the newsletter still goes out.
  2. Covers: album covers from Deezer for every release (see media.py).
  3. Build:  fills templates/newsletter.html (email) and a plain-text version.
             A copy is saved in newsletters/ as an archive.
  4. Send:   emails it through Gmail.

Run it with:        python scripts/newsletter.py
Without sending:    python scripts/newsletter.py --no-send   (just saves the HTML)

Needs these environment variables (GitHub secrets):
  ANTHROPIC_API_KEY, GMAIL_ADDRESS, GMAIL_APP_PASSWORD, NEWSLETTER_TO
"""

import argparse
import json
import os
import smtplib
import sys
from datetime import date
from email.message import EmailMessage
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from media import add_media

ROOT = Path(__file__).resolve().parent.parent
RANKED = ROOT / "data" / "ranked.json"
ITEMS = ROOT / "data" / "items.json"
TEMPLATES = ROOT / "templates"
ARCHIVE = ROOT / "newsletters"

MODEL = "claude-sonnet-5-5"

SOURCE_LABELS = {  # how each feed is shown in the "Read more" links
    "Pitchfork (album reviews)": "Pitchfork",
    "Pitchfork (Best New Albums)": "Pitchfork",
    "Pitchfork (track reviews)": "Pitchfork",
    "Stereogum": "Stereogum",
    "The Needle Drop (YouTube)": "The Needle Drop",
    "The Needle Drop (website)": "The Needle Drop",
    "AllMusic (new releases)": "AllMusic",
}

WRITING_INSTRUCTIONS = """You write a weekly new-music newsletter for one reader who wants a quick, \
reliable summary of the week's major releases.

You get this week's ranked releases. For each one you get the headlines and summaries that music \
publications published about it.

Write:
- intro: 2-3 sentences on the shape of the week (what stood out, any theme). Plain and specific.
- a blurb for every release: 2-3 sentences saying what it is and why it's notable this week \
(who covered it, review scores, the context the headlines give).

Rules:
- Use ONLY the facts in the material provided. Don't add facts about artists, sound, tracklists, \
collaborators or history that the material doesn't state. If the material is thin, keep the blurb \
short and factual rather than padding it.
- Don't describe how the music sounds unless a summary says so.
- No hype words ("banger", "masterpiece", "iconic"), no exclamation marks, no emoji.
- Refer to publications by name (Pitchfork, Stereogum, The Needle Drop)."""


# --------------------------------------------------------------------------- data

def load_week() -> dict:
    if not RANKED.exists():
        sys.exit("No data/ranked.json yet. Run scripts/rank.py first.")
    week = json.loads(RANKED.read_text())

    summaries = {}
    if ITEMS.exists():
        summaries = {i["link"]: i.get("summary", "") for i in json.loads(ITEMS.read_text())}

    # Every release appears once, with a stable key, even if it's in the top list
    # and its genre list.
    releases = {}
    for rel in week["top"] + [r for rels in week["by_genre"].values() for r in rels]:
        key = f"{rel['artist']} | {rel['title']}"
        if key in releases:
            continue
        rel["key"] = key
        rel["links"] = links_for(rel)
        rel["material"] = [
            {"source": SOURCE_LABELS.get(m["source"], m["source"].split(" (")[0]),
             "type": m["mention"],
             "headline": m["headline"],
             "summary": useful_summary(summaries.get(m["link"], ""))}
            for m in rel["mentions"][:6]
        ]
        releases[key] = rel
    week["releases"] = releases
    return week


def useful_summary(text: str) -> str:
    # Stereogum's feed summary is just "The post X appeared first on Stereogum."
    if not text or "appeared first on" in text:
        return ""
    return text[:500]


def links_for(rel: dict) -> list[dict]:
    """One 'read more' link per publication, preferring reviews."""
    chosen = {}
    for m in sorted(rel["mentions"], key=lambda m: m["mention"] != "review"):
        label = SOURCE_LABELS.get(m["source"], m["source"].split(" (")[0])
        if label not in chosen:
            kind = "review" if m["mention"] == "review" else ""
            if "YouTube" in m["source"]:
                kind = "video review" if m["mention"] == "review" else "video"
            chosen[label] = {"label": label, "kind": kind, "url": m["link"],
                             "pick": "Best New" in m["source"]}
        elif "Best New" in m["source"]:
            chosen[label]["pick"] = True
    return list(chosen.values())


# --------------------------------------------------------------------------- 1. write

def write_copy(week: dict) -> dict:
    """Ask Claude for the intro and blurbs. Returns {"intro": str, "blurbs": {key: str}}."""
    releases = week["releases"]
    fallback = {
        "intro": "",
        "blurbs": {k: r["mentions"][0]["headline"] for k, r in releases.items()},
    }
    if not releases or not os.environ.get("ANTHROPIC_API_KEY"):
        return fallback

    material = [
        {"key": k, "artist": r["artist"], "title": r["title"], "type": r["type"],
         "genre": r["genre"], "release_date": r["release_date"],
         "average_review_score": r["avg_score"], "coverage": r["material"]}
        for k, r in releases.items()
    ]
    schema = {
        "type": "object",
        "properties": {
            "intro": {"type": "string"},
            "blurbs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"key": {"type": "string"}, "blurb": {"type": "string"}},
                    "required": ["key", "blurb"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["intro", "blurbs"],
        "additionalProperties": False,
    }
    try:
        import anthropic

        response = anthropic.Anthropic().messages.create(
            model=MODEL,
            max_tokens=16000,
            system=WRITING_INSTRUCTIONS,
            messages=[{"role": "user", "content":
                       f"Week: {week['week_start']} to {week['week_end']}\n\n"
                       f"Releases (ranked, best first):\n{json.dumps(material, indent=1, ensure_ascii=False)}"}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
        if response.stop_reason in ("max_tokens", "refusal"):
            raise RuntimeError(f"stopped early ({response.stop_reason})")
        data = json.loads(next(b.text for b in response.content if b.type == "text"))
    except Exception as err:  # noqa: BLE001 - never let writing stop the newsletter
        print(f"Writing: Claude unavailable ({err}); using headlines instead")
        return fallback

    blurbs = dict(fallback["blurbs"])
    blurbs.update({b["key"]: b["blurb"] for b in data["blurbs"] if b["key"] in releases})
    print(f"Writing: intro and {len(data['blurbs'])} blurbs written by Claude")
    return {"intro": data["intro"], "blurbs": blurbs}


# --------------------------------------------------------------------------- 2. build

def issue_date(week: dict) -> date:
    # The week ends on the Friday the newsletter goes out.
    return date.fromisoformat(week["week_end"])


def nice_range(start: str, end: str) -> str:
    s, e = date.fromisoformat(start), date.fromisoformat(end)
    if s.month == e.month:
        return f"{s.day}–{e.day} {e:%b %Y}"
    return f"{s.day} {s:%b} – {e.day} {e:%b %Y}"


def build(week: dict, copy: dict) -> tuple[str, str, str]:
    releases = week["releases"]
    for key, rel in releases.items():
        rel["blurb"] = copy["blurbs"].get(key, "")

    top = [releases[f"{r['artist']} | {r['title']}"] for r in week["top"]]
    top_keys = {r["key"] for r in top}
    genres = {
        g: [releases[f"{r['artist']} | {r['title']}"] for r in rels
            if f"{r['artist']} | {r['title']}" not in top_keys]
        for g, rels in week["by_genre"].items()
    }
    in_top = {g: sum(1 for r in top if r["genre"] == g) for g in week["by_genre"]}

    # The issue is dated by the Friday it goes out.
    issue = issue_date(week)
    issue_title = f"Friday {issue.day} {issue:%B %Y}"
    dates = nice_range(week["week_start"], week["week_end"])  # used in the footer
    names = ", ".join(r["artist"] for r in top[:3])
    subject = f"Weekly Releases, Friday {issue.day} {issue:%b}" + (f": {names}" if names else "")

    env = Environment(loader=FileSystemLoader(TEMPLATES),
                      autoescape=select_autoescape(["html"]),
                      trim_blocks=True, lstrip_blocks=True)
    env.filters["day"] = lambda d: f"{date.fromisoformat(d):%a} {date.fromisoformat(d).day} " \
                                   f"{date.fromisoformat(d):%b}" if d else ""
    env.filters["kind"] = lambda t: {"ep": "EP", "lp": "LP"}.get(t, (t or "").capitalize())
    context = dict(issue_title=issue_title, dates=dates, intro=copy["intro"], top=top,
                   genres=genres, in_top=in_top, empty=not releases,
                   personal=any(r.get("personal_reason") for r in releases.values()))
    html = env.get_template("newsletter.html").render(**context)
    text = env.get_template("newsletter.txt").render(**context)
    return subject, html, text


# --------------------------------------------------------------------------- 3. send

def send(subject: str, html: str, text: str) -> None:
    missing = [v for v in ("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "NEWSLETTER_TO")
               if not os.environ.get(v)]
    if missing:
        sys.exit(f"Can't send: missing {', '.join(missing)} (add them as GitHub secrets)")

    sender = os.environ["GMAIL_ADDRESS"].strip()
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"Weekly Releases <{sender}>"
    msg["To"] = os.environ["NEWSLETTER_TO"].strip()
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    password = os.environ["GMAIL_APP_PASSWORD"].replace(" ", "")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as smtp:
        smtp.login(sender, password)
        smtp.send_message(msg)
    print("Sent.")
  
def already_sent(week: dict) -> bool:
    from rank import FEATURED
    featured = json.loads(FEATURED.read_text()) if FEATURED.exists() else {}
    return week["week_end"] in featured
  
def remember_featured(week: dict) -> None:
    """Record which releases this issue contained, so next week's issue can add
    last-Friday releases that were covered too late for this one (see rank.py)."""
    from rank import FEATURED, release_key
    featured = json.loads(FEATURED.read_text()) if FEATURED.exists() else {}
    featured[week["week_end"]] = sorted({release_key(r) for r in week["releases"].values()})
    for old in sorted(featured)[:-10]:   # keep the last 10 issues
        del featured[old]
    FEATURED.write_text(json.dumps(featured, indent=1, ensure_ascii=False))


# --------------------------------------------------------------------------- main

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-send", action="store_true", help="save the HTML but don't email it")
    args = parser.parse_args()

    week = load_week()
    copy = write_copy(week)
    add_media(list(week["releases"].values()))
    subject, html, text = build(week, copy)

    ARCHIVE.mkdir(exist_ok=True)
    out = ARCHIVE / f"{issue_date(week)}.html"
    out.write_text(html)
    print(f"Subject: {subject}\nSaved: {out.relative_to(ROOT)}")

    if args.no_send:
        print("Not sent (--no-send).")
    elif already_sent(week) and os.environ.get("GITHUB_EVENT_NAME") == "schedule":
        print(f"Not sent: the issue for the week ending {week['week_end']} was already emailed.")
    else:
        send(subject, html, text)
        remember_featured(week)


if __name__ == "__main__":
    main()
