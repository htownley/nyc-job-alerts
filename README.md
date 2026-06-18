# NYC job alerts

A small personal tool that emails a daily digest of NYC city job postings
matching a set of keywords and agencies. It reads the public
[cityjobs.nyc.gov](https://cityjobs.nyc.gov) postings feed (the SmartRecruiters
`CityOfNewYork` API), de-duplicates against postings it has already sent, and
emails anything new. A GitHub Actions workflow runs it daily.

The feed only contains **currently-open** postings — once a posting closes it
drops out, so closed/filled jobs can't be fetched retroactively. Running on a
schedule is what builds up history over time.

## What it matches

Defaults (edit at the top of `job_alerts.py` or via env vars):

- **Keywords** (case-insensitive substring of the job title or category):
  `tech`, `product`, `mayor`
- **Agencies** (exact): `TECHNOLOGY & INNOVATION` (the Office of Technology &
  Innovation)

> Note: `tech` is a broad substring — it also matches "Technician", "Technical",
> etc. Tighten the `KEYWORDS` list if the digest gets noisy.

Each email groups postings by agency and links to the application page at
`cityjobs.nyc.gov`.

## How state works

`seen_jobs.json` records every posting that has already been matched, so you only
get alerted once per posting. The workflow commits the updated file back to the
repo after each run. On the very first run it seeds this file and sends a short
"tracker is live" confirmation rather than emailing the entire current backlog.

## Setup

1. Push this repo to GitHub (keep it **private**).
2. Create a Gmail [App Password](https://myaccount.google.com/apppasswords)
   (requires 2-Step Verification) for the account you want to send *from*.
3. In the repo, go to **Settings → Secrets and variables → Actions** and add:
   - `SMTP_USERNAME` — the sending Gmail address
   - `SMTP_PASSWORD` — the 16-character app password
   - `RECIPIENT` — where the digest goes (e.g. `hendrick.townley@gmail.com`)
4. Optionally trigger a test run from the **Actions** tab
   (**NYC job alerts → Run workflow**).

The schedule is `0 11 * * *` (11:00 UTC ≈ 7:00 AM Eastern). Edit the cron in
`.github/workflows/daily.yml` to change the time.

## Run locally

```bash
pip install -r requirements.txt
DRY_RUN=1 python job_alerts.py          # prints the digest, sends nothing
```

To send for real locally, set `SMTP_USERNAME`, `SMTP_PASSWORD`, and `RECIPIENT`
in your environment and drop `DRY_RUN`.

## Configuration reference

| Variable        | Default                      | Notes                                   |
|-----------------|------------------------------|-----------------------------------------|
| `KEYWORDS`      | `tech,product,mayor`         | Comma-separated                         |
| `AGENCIES`      | `TECHNOLOGY & INNOVATION`    | `||`-separated (names contain commas)   |
| `RECIPIENT`     | `hendrick.townley@gmail.com` | Digest destination                      |
| `SMTP_HOST`     | `smtp.gmail.com`             |                                         |
| `SMTP_PORT`     | `587`                        |                                         |
| `DRY_RUN`       | unset                        | `1` = print instead of send             |
