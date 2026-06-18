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
from datetime import datetime, timezone
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


def matched_terms(row: dict) -> list[str]:
    """Human-readable list of why a row matched (for the email)."""
    hits = []
    agency = (row.get("agency") or "")
    if agency in AGENCIES:
        hits.append(f"agency: {agency.title()}")
    haystack = " ".join(
        (row.get(f) or "") for f in (*KEYWORD_FIELDS, "agency")
    ).lower()
    for kw in KEYWORDS:
        if kw.lower() in haystack:
            hits.append(f'"{kw}"')
    return hits


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

def fmt_salary(row: dict) -> str:
    lo, hi = row.get("salary_range_from"), row.get("salary_range_to")
    freq = (row.get("salary_frequency") or "").lower()
    try:
        lo_s = f"${float(lo):,.0f}"
        hi_s = f"${float(hi):,.0f}"
    except (TypeError, ValueError):
        return ""
    span = lo_s if lo_s == hi_s else f"{lo_s} – {hi_s}"
    return f"{span}/{freq}" if freq else span


def render(jobs: list[dict], intro: str) -> tuple[str, str]:
    # Group by agency for readability.
    groups: dict[str, list[dict]] = {}
    for row in jobs:
        groups.setdefault(row.get("agency", "—").title(), []).append(row)

    text_lines = [intro, ""]
    html_parts = [f"<p>{escape(intro)}</p>"]

    for agency in sorted(groups):
        text_lines.append(f"== {agency} ==")
        html_parts.append(f"<h3 style='margin:18px 0 6px'>{escape(agency)}</h3>")
        for row in groups[agency]:
            title = row.get("business_title", "Untitled")
            url = JOB_URL.format(job_id=row["job_id"])
            salary = fmt_salary(row)
            ptype = row.get("posting_type", "")
            posted = (row.get("posting_date") or "")[:10]
            why = ", ".join(matched_terms(row))
            meta = " · ".join(p for p in [ptype, salary, f"posted {posted}"] if p)

            text_lines.append(f"  • {title}")
            text_lines.append(f"    {meta}")
            text_lines.append(f"    match: {why}")
            text_lines.append(f"    {url}")
            html_parts.append(
                f"<div style='margin:0 0 12px'>"
                f"<a href='{escape(url)}' style='font-weight:600;font-size:15px'>"
                f"{escape(title)}</a><br>"
                f"<span style='color:#555;font-size:13px'>{escape(meta)}</span><br>"
                f"<span style='color:#888;font-size:12px'>match: {escape(why)}</span>"
                f"</div>"
            )
        text_lines.append("")

    html = (
        "<div style='font-family:-apple-system,Segoe UI,Roboto,sans-serif;"
        "max-width:640px'>" + "".join(html_parts) +
        "<hr style='border:none;border-top:1px solid #eee;margin:20px 0'>"
        "<p style='color:#aaa;font-size:11px'>Source: NYC Open Data — NYC Jobs. "
        "You can edit keywords/agencies in job_alerts.py.</p></div>"
    )
    return "\n".join(text_lines), html


def send_email(subject: str, text: str, html: str) -> None:
    user = os.environ["SMTP_USERNAME"]
    password = os.environ["SMTP_PASSWORD"]
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

    seen = load_seen()
    first_run = not seen

    new_ids = [jid for jid in current if jid not in seen]
    new_jobs = [current[jid] for jid in new_ids]

    if first_run:
        # Don't blast the entire backlog the first time — seed state and send a
        # short confirmation so you know it's wired up.
        sample = list(current.values())[:10]
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
