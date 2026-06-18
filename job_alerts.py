#!/usr/bin/env python3
"""Daily digest of NYC city job postings matching a set of keywords/agencies.

Queries the public NYC Jobs dataset on NYC Open Data (Socrata), filters for
postings of interest, de-duplicates against previously seen postings, and emails
a digest of anything new. State is kept in ``seen_jobs.json`` so each posting is
only ever alerted once.

Run locally:  DRY_RUN=1 python job_alerts.py
"""

from __future__ import annotations

import json
import os
import smtplib
import sys
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from html import escape
from pathlib import Path
from urllib.parse import urlencode

import requests

# --- Configuration (override any of these with environment variables) --------

# Case-insensitive substrings matched against the posting's title / category.
KEYWORDS = os.environ.get("KEYWORDS", "tech,product,mayor").split(",")

# Agencies matched exactly (NYC Open Data spelling). "TECHNOLOGY & INNOVATION"
# is the Office of Technology & Innovation (OTI).
AGENCIES = os.environ.get("AGENCIES", "TECHNOLOGY & INNOVATION").split("||")

# Fields the keywords are searched in.
KEYWORD_FIELDS = ["business_title", "civil_service_title", "job_category"]

RECIPIENT = os.environ.get("RECIPIENT", "hendrick.townley@gmail.com")

DATASET = "kpav-sd4t"  # "NYC Jobs" on data.cityofnewyork.us
API = f"https://data.cityofnewyork.us/resource/{DATASET}.json"
JOB_URL = "https://cityjobs.nyc.gov/job/{job_id}"

STATE_FILE = Path(__file__).with_name("seen_jobs.json")

KEYWORDS = [k.strip() for k in KEYWORDS if k.strip()]
AGENCIES = [a.strip() for a in AGENCIES if a.strip()]


# --- Fetch -------------------------------------------------------------------

def _like(field: str, needle: str) -> str:
    needle = needle.upper().replace("'", "''")
    return f"upper({field}) like '%{needle}%'"


def build_where() -> str:
    clauses = [f"agency='{a.replace(chr(39), chr(39) * 2)}'" for a in AGENCIES]
    for kw in KEYWORDS:
        for field in KEYWORD_FIELDS:
            clauses.append(_like(field, kw))
        # Also catch keyword in the agency name (e.g. OFFICE OF THE MAYOR).
        clauses.append(_like("agency", kw))
    return " OR ".join(clauses)


def fetch_postings() -> list[dict]:
    params = {
        "$where": build_where(),
        "$order": "posting_date DESC",
        "$limit": "1000",
    }
    resp = requests.get(API, params=urlencode(params), timeout=60)
    resp.raise_for_status()
    return resp.json()


def categorize(row: dict) -> list[tuple[str, str]]:
    """All match reasons for a row as (kind, label) tuples.

    kind is "agency" (an exact agency match, e.g. OTI) or "keyword".
    """
    cats: list[tuple[str, str]] = []
    agency = row.get("agency") or ""
    if agency in AGENCIES:
        cats.append(("agency", clean(agency)))
    haystack = " ".join(
        (row.get(f) or "") for f in (*KEYWORD_FIELDS, "agency")
    ).lower()
    for kw in KEYWORDS:
        if kw.lower() in haystack:
            cats.append(("keyword", kw))
    return cats


def dedupe(rows: list[dict]) -> dict[str, dict]:
    """One entry per job_id (the dataset has a row per position/location)."""
    by_id: dict[str, dict] = {}
    for row in rows:
        jid = row.get("job_id")
        if jid and jid not in by_id:
            by_id[jid] = row
    return by_id


# --- State -------------------------------------------------------------------

def load_seen() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_seen(seen: dict) -> None:
    STATE_FILE.write_text(json.dumps(seen, indent=2, sort_keys=True) + "\n")


# --- Email -------------------------------------------------------------------

def clean(value: str) -> str:
    """Return the source value as-is (trimmed). We never re-case the data: the
    dataset's capitalization is authoritative, so ALL-CAPS stays ALL-CAPS rather
    than us guessing the 'right' casing."""
    return (value or "").strip()


def fmt_salary(row: dict) -> str:
    lo, hi = row.get("salary_range_from"), row.get("salary_range_to")
    freq = (row.get("salary_frequency") or "").lower()
    # Annual figures read best without cents; hourly/daily need them.
    if "hour" in freq:
        decimals, suffix = 2, "/hr"
    elif "day" in freq:
        decimals, suffix = 2, "/day"
    else:
        decimals, suffix = 0, ""
    try:
        lo_s = f"${float(lo):,.{decimals}f}"
        hi_s = f"${float(hi):,.{decimals}f}"
    except (TypeError, ValueError):
        return ""
    span = lo_s if lo_s == hi_s else f"{lo_s} – {hi_s}"
    return span + suffix


def fmt_date(value: str) -> str:
    """'2026-06-17T00:00:00.000' -> 'Wed Jun 17, 2026'."""
    try:
        return datetime.fromisoformat(value).strftime("%a %b %-d, %Y")
    except (TypeError, ValueError):
        return (value or "")[:10]


def group_by_match(jobs: list[dict]) -> list[tuple[tuple[str, str], list[dict]]]:
    """Assign each posting to one match category, ordered least -> most noisy.

    A posting can match several categories; it's filed under its most specific
    one (agency match beats keyword; rarer keyword beats common one). Groups are
    then ordered so the most relevant/least noisy lead and broad terms (e.g.
    "tech") fall to the bottom.
    """
    counts: dict[tuple[str, str], int] = {}
    cats_for: dict[str, list[tuple[str, str]]] = {}
    for row in jobs:
        cats = categorize(row)
        cats_for[row["job_id"]] = cats
        for cat in cats:
            counts[cat] = counts.get(cat, 0) + 1

    # Lower key == more specific: agencies before keywords, rarer before common.
    def specificity(cat: tuple[str, str]) -> tuple:
        kind, label = cat
        return (0 if kind == "agency" else 1, counts[cat], label)

    groups: dict[tuple[str, str], list[dict]] = {}
    for row in jobs:
        cats = cats_for[row["job_id"]]
        primary = min(cats, key=specificity) if cats else ("keyword", "other")
        groups.setdefault(primary, []).append(row)

    # Least noisy first: agency groups, then keyword groups by ascending size.
    return sorted(groups.items(),
                  key=lambda kv: (kv[0][0] != "agency", len(kv[1]), kv[0][1]))


def header_for(cat: tuple[str, str], count: int) -> str:
    kind, label = cat
    base = label if kind == "agency" else f"Keyword: “{label}”"
    return f"{base} · {count} match{'es' if count != 1 else ''}"


def org_line(row: dict) -> str:
    agency = clean(row.get("agency", ""))
    office = clean(row.get("division_work_unit", ""))
    return f"{agency} — {office}" if office and office != agency else agency


def render(jobs: list[dict], intro: str) -> tuple[str, str]:
    text_lines = [intro]
    html_parts = [
        f"<p style='margin:0 0 4px;font-size:16px;color:#222'>{escape(intro)}</p>"
    ]

    n = 0
    for cat, rows in group_by_match(jobs):
        header = header_for(cat, len(rows))
        text_lines += ["", header, "─" * min(len(header), 52)]
        html_parts.append(
            f"<div style='margin:30px 0 10px;padding-bottom:5px;font-size:13px;"
            f"font-weight:700;color:#111;border-bottom:1px solid #e3e3e3'>"
            f"{escape(header)}</div>"
        )
        for row in rows:
            n += 1
            title = row.get("business_title", "Untitled")
            url = JOB_URL.format(job_id=row["job_id"])
            # Indented detail lines, each kept short for easy scanning.
            details = [
                org_line(row),
                " · ".join(p for p in [
                    fmt_salary(row), f"Posted {fmt_date(row.get('posting_date'))}"
                ] if p),
                clean(row.get("work_location", "")),
            ]
            details = [d for d in details if d]

            text_lines.append(f"{n}.  {title}")
            text_lines += [f"       {d}" for d in details]
            text_lines.append(f"       → View posting: {url}")
            text_lines.append("")

            detail_html = "<br>".join(escape(d) for d in details)
            html_parts.append(
                f"<div style='margin:0 0 20px'>"
                f"<div style='font-size:16px;color:#111'>"
                f"<b>{n}.&nbsp; {escape(title)}</b></div>"
                f"<div style='margin:3px 0 0 26px;font-size:14px;color:#444;"
                f"line-height:1.55'>{detail_html}<br>"
                f"<a href='{escape(url)}' style='color:#0b5cad;"
                f"text-decoration:none'>→ View posting</a></div>"
                f"</div>"
            )

    html = (
        "<div style='font-family:-apple-system,BlinkMacSystemFont,Segoe UI,"
        "Roboto,Helvetica,Arial,sans-serif;max-width:600px;margin:0 auto;"
        "padding:8px 4px'>" + "".join(html_parts) +
        "<hr style='border:none;border-top:1px solid #eee;margin:24px 0 12px'>"
        "<p style='color:#aaa;font-size:11px;line-height:1.4'>Source: NYC Open "
        "Data — NYC Jobs. Edit keywords/agencies in job_alerts.py.</p></div>"
    )
    return "\n".join(text_lines), html


def send_email(subject: str, text: str, html: str) -> None:
    user = os.environ["SMTP_USERNAME"].strip()
    # Gmail app passwords are displayed as "xxxx xxxx xxxx xxxx" but contain no
    # spaces; strip any whitespace (incl. non-breaking spaces) pasted in.
    password = "".join(os.environ["SMTP_PASSWORD"].split())
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "587"))

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = RECIPIENT
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    with smtplib.SMTP(host, port) as server:
        server.starttls()
        server.login(user, password)
        server.send_message(msg)


# --- Main --------------------------------------------------------------------

def main() -> int:
    dry_run = os.environ.get("DRY_RUN") == "1"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    rows = fetch_postings()
    current = dedupe(rows)
    print(f"Fetched {len(rows)} rows → {len(current)} unique postings.")

    # Catch-up mode: email every currently-open match posted in the last N days
    # without touching the daily de-dup state. Triggered manually.
    since_days = os.environ.get("SINCE_DAYS")
    if since_days and int(since_days) > 0:
        days = int(since_days)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        recent = sorted(
            (r for r in current.values() if (r.get("posting_date") or "")[:10] >= cutoff),
            key=lambda r: r.get("posting_date", ""), reverse=True,
        )
        intro = (
            f"Catch-up snapshot: {len(recent)} currently-open posting(s) matching "
            f"your filters were posted in the last {days} days (as of {today})."
        )
        subject = f"NYC jobs — last {days} days ({len(recent)} open matches)"
        text, html = render(recent, intro)
        if dry_run:
            print(f"\n--- DRY RUN (catch-up): would send to {RECIPIENT} ---\n{text}")
        else:
            send_email(subject, text, html)
            print(f"Sent catch-up '{subject}' to {RECIPIENT}.")
        return 0

    seen = load_seen()
    first_run = not seen

    new_ids = [jid for jid in current if jid not in seen]
    new_jobs = [current[jid] for jid in new_ids]

    if first_run:
        # Don't blast the entire backlog the first time — seed state and send a
        # short confirmation. Sample a few from each match category so the
        # grouping is visible.
        sample = []
        for _cat, rows_in_cat in group_by_match(list(current.values())):
            sample.extend(rows_in_cat[:3])
        intro = (
            f"NYC job tracker is live. There are {len(current)} open postings "
            f"matching your filters right now; from tomorrow you'll only get "
            f"newly posted ones. A sample of what's currently open:"
        )
        subject = f"NYC job tracker is live ({len(current)} open matches)"
        text, html = render(sample, intro)
    elif new_jobs:
        intro = f"{len(new_jobs)} new NYC posting(s) matched your filters today."
        subject = f"NYC jobs: {len(new_jobs)} new match(es) — {today}"
        text, html = render(new_jobs, intro)
    else:
        print("No new postings today; no email sent.")
        # Still refresh state timestamps below.
        text = html = subject = None

    if subject is not None:
        if dry_run:
            print(f"\n--- DRY RUN: would send to {RECIPIENT} ---")
            print(f"Subject: {subject}\n")
            print(text)
        else:
            send_email(subject, text, html)
            print(f"Sent '{subject}' to {RECIPIENT}.")

    # Update state: record every currently-matching id with first-seen date.
    for jid in current:
        seen.setdefault(jid, today)
    save_seen(seen)
    print(f"State now tracks {len(seen)} postings.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
