# FAANG Job Tracker

Everything lives flat in `D:\track`. All scripts resolve paths relative to their
own location, so the folder can be moved anywhere.

## The flow

```
probe_boards.py   (rare)  436 companies -> boards.json (238 with live feeds)
        |
fetch_jobs.py     (~3min) 238 employer feeds -> only jobs newer than state.json
        |                 -> h1b_jobs.xlsx, h1b_jobs.html, feed.json
enrich.py         (~2min) reads each posting body -> years + sponsorship
        |                 -> enriched.json, updates feed.json + h1b_jobs.html
serve.py          (leave running) feed.json -> http://127.0.0.1:8765
        |
Tampermonkey      preflight each posting -> paste -> Apply -> POST back
        |                 -> applied.json / skipped.json
report.py         (instant) rebuild h1b_jobs.html so applies show ticked
```


Tracks live computer-science job postings at companies with a history of H-1B sponsorship,
and dumps them into a filterable Excel workbook.

## Run it

```bash
python fetch_jobs.py
```

Shows **only postings you haven't already been shown**, then reads the shortlist's actual
posting bodies for real experience and sponsorship requirements.

Both write to the **same two files every time** — `h1b_jobs.xlsx` and `h1b_jobs.html`.
Nothing dated, nothing accumulates. If a run finds nothing new it leaves them untouched
rather than blanking them.

## Files

| File | Role |
|---|---|
| `fetch_jobs.py` | main — pulls new jobs, writes the workbook + report |
| `enrich.py` | reads posting bodies for real YOE / sponsorship |
| `probe_boards.py` | discovers which feed each employer uses (run rarely) |
| `sources.py` | Workday / Amazon / Google / Apple / Microsoft connectors |
| `companies.py` | the 436 candidate sponsors |
| `html_report.py` | renders `h1b_jobs.html` |
| `serve.py` | localhost bridge for the userscript |
| `tsenta_h1b_autoapply.user.js` | Tampermonkey auto-apply |
| `boards.json` | probed feed map — regenerate with `probe_boards.py` |
| `state.json` | watermark: last job seen per company |
| `feed.json` | machine-readable output for the bridge |
| `enriched.json` | posting-body findings |
| `applied.json` | what you've applied to (created on first mark) |

| Flag | Effect |
|---|---|
| *(none)* | new since last run; look-back auto-sized to cover the gap |
| `--hours 72` | force a 3-day look-back |
| `--all-levels` | don't restrict the main sheet to entry level |
| `--reset` | forget the watermark, treat everything as new |
| `--no-state` | one-off run; don't read or update the watermark |

```bash
python probe_boards.py
```

Re-verifies which job-board API each company uses and rewrites `boards.json`.
Run every few months; companies migrate between ATS vendors.

## The "new since last run" watermark

`state.json` keeps **one last job per company**:

```json
"Google": {
  "last_posted": "2026-08-03T19:40:42+00:00",
  "last_title":  "Senior Software Engineer, Infrastructure, Google Cloud AI",
  "last_url":    "https://www.google.com/about/careers/...",
  "same_stamp":  ["...url..."]
}
```

A posting is new if it's **posted after that mark**. Each run replaces the mark with the new
newest and discards the old one — nothing accumulates.

`same_stamp` is the one necessary wrinkle. Workday and Amazon report a posting *date*, not a
time, so every job from a given day carries an identical timestamp and `>` alone can't tell
them apart. It holds only the URLs sharing that exact newest stamp, and it resets the moment
a newer one arrives. For the 48-of-73 feeds with real timestamps it holds exactly one URL.

Workday stamps are snapped to the **start of the posting day**. Deriving them from the clock
(`now - 3 days`) would make the same job look newer on every run and defeat the watermark
entirely.

The look-back window auto-sizes to the gap since your last run (minimum 24h, +12h margin,
capped at 30 days), so skipping a few days doesn't silently lose postings.

## Marking jobs as applied

Tick the checkbox next to any row in the HTML report. State lives in `localStorage` keyed by
apply URL, so it survives every regenerated report in that browser — applied rows dim and
strike through, and the **Applied / Not applied** dropdown filters on it.

To carry the list into the spreadsheet or onto another machine, hit **Download applied.json**
and drop the file next to `fetch_jobs.py`. From then on the workbook's `Applied` column is
filled in and the HTML pre-ticks those boxes.

## How it works

`companies.py` holds **436 candidate H-1B sponsors**. `probe_boards.py` tests each against four
free, unauthenticated job-board APIs and keeps whatever responds:

| ATS | Endpoint |
|---|---|
| Greenhouse | `boards-api.greenhouse.io/v1/boards/{token}/jobs` |
| Lever | `api.lever.co/v0/postings/{token}?mode=json` |
| Ashby | `api.ashbyhq.com/posting-api/job-board/{token}` |
| SmartRecruiters | `api.smartrecruiters.com/v1/companies/{token}/postings` |

Then it sweeps **Workday** tenants (`WORKDAY_CANDIDATES` in `companies.py`), trying host ×
site combinations until the public `cxs` endpoint answers — 47 sponsors resolve this way,
including Morgan Stanley, KLA, Marvell, NXP, Amgen, Leidos, Booz Allen, Truist, HP and Zoom.
Workday caps pages at 20 but accepts `sortBy=POSTING_START_DATE_DESC`, so a windowed pull
stops as soon as it passes the window. Postings collapsed to `"3 Locations"` get a detail
lookup to recover the real location list, which the US filter needs.

`sources.py` adds four in-house systems:

- **Amazon** via `amazon.jobs/en/search.json?sort=recent`.
- **Google** — no JSON API; its careers page embeds the result set in an
  `AF_initDataCallback` block, which plain HTTP can read. No browser needed.
- **Apple** — same idea: `jobs.apple.com/en-us/search` ships results inline in
  `window.__staticRouterHydrationData`.
- **Microsoft** via the `gcsservices.careers.microsoft.com` backend. ⚠️ Unverified — that
  host was unreachable from the sandbox this was built in (TLS hostname mismatch, i.e. proxy
  interception). The request shape is right and it should work on a normal connection; it
  fails soft either way.

**Meta is deliberately excluded.** `metacareers.com` renders its job list client-side from an
internal GraphQL call keyed by a `doc_id` baked into their JS bundle, which rotates on every
deploy. That's a private API rather than a public feed, so Meta stays link-only.

**238 of 436 sponsors** now have a live employer feed — including four of the big five.

Every source is the employer's own careers backend — the same endpoint their careers page
calls. Greenhouse/Lever/Ashby/SmartRecruiters are the ATS vendors companies *pay* to host
their listings, not job aggregators. Nothing here reads LinkedIn, Indeed or Glassdoor, and a
company with no verifiable feed is dropped rather than filled in from elsewhere.

**Google is not trackable.** Its careers site is server-rendered and the old
`careers.google.com/api/v3` endpoint 404s. Google stays a direct link, as do Meta, Apple,
and the Indian IT majors.

## Workbook sheets

- **New Entry-Level** — new US roles at entry / new-grad / internship level
- **New All CS** — every new US CS role, all seniority levels
- **H1B Sponsors** — all 436 sponsors, with a live-feed flag and direct links
- **How To Use** — caveats, in the file itself

Rows sort freshest first; the `Age` column shows how long ago each was posted.

## HTML report

The `.html` twin of each workbook is a single self-contained file — data embedded as JSON,
no CDN, no network calls, works offline. Search box, four dropdown filters, click any column
header to sort, and it follows your system light/dark theme. Job titles link straight to the
employer's application page.

## Tampermonkey auto-apply (Tsenta)

A userscript can't read files off your disk, so `serve.py` bridges the gap.

```bash
python serve.py
```

Serves on `127.0.0.1:8765` only — nothing is exposed off the machine.

| Endpoint | Purpose |
|---|---|
| `GET /feed.json` | jobs from the last `fetch_jobs.py` run, with live `applied` flags |
| `GET /applied.json` | URLs already marked applied |
| `POST /applied` | `{"url": "..."}` → merged into `applied.json` |
| `GET /health` | sanity check |

Then install `tsenta_h1b_autoapply.user.js` in Tampermonkey. Workflow:

1. `python fetch_jobs.py` — writes `feed.json`
2. `python serve.py` — leave running
3. Open the Tsenta browse-jobs page, click **DRY RUN H-1B feed**
4. Read the console list. If it looks right, set `DRY_RUN = false` and re-click

The script drops `Senior+` roles, applies your title blocklist, skips anything already
in `applied.json`, and after each successful submit POSTs the URL back — so the next
`fetch_jobs.py` run shows it flagged and the HTML report pre-ticks its box.

**`DRY_RUN = true` is the default and every click while it's on submits nothing.** With it
off, each iteration files a real application, so the caps matter: `MAX_ADDS_PER_RUN = 10`
with a randomised 15–25 s gap. Raise those only if you actually want that volume going out
under your name — a bad batch is not retractable.

## Limits worth knowing

- **Sponsor tiers are approximate bands**, not exact approval counts. Verify any employer at the
  [USCIS H-1B Employer Data Hub](https://www.uscis.gov/tools/reports-and-studies/h-1b-employer-data-hub).
- **Past sponsorship ≠ sponsorship for this role.** Always check the posting's work-authorization
  line and ask the recruiter.
- **Level is inferred from the job title only.** `Unlabeled (possible entry)` means the title had
  no seniority marker — often 0–2 yrs, but confirm in the posting.
- **Workday and Amazon report posting date, not time.** For those, a 24h window means "today or
  yesterday", so a few entries may be slightly older than 24 hours. The URL watermark still
  guarantees you never see one twice.
- **Google's date field can mean "re-published."** Unlike Greenhouse's `first_published`, it
  appears to move when a posting is edited, so an older role may surface once as new.
- **198 candidates still have no public feed** — Meta, the Indian IT majors, and many banks
  run closed or private-API career sites. They're in the sponsors sheet with direct links,
  but their jobs aren't auto-pulled.
- **The sponsor list is candidates, not gospel.** `companies.py` is a hand-built list of
  employers known to file H-1Bs; it isn't derived from USCIS data programmatically. Treat the
  tier column as a rough band and verify anyone you're serious about.

## Adding a company

Add a row to `COMPANIES` in `companies.py`, then re-run `probe_boards.py` and `fetch_jobs.py`.
Wrong slug guesses are harmless — the prober tests several variants and discards misses.
