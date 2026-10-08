"""Rebuild h1b_jobs.html from local files. No network, runs in well under a second.

Use it after applying to jobs so the report reflects applied.json:

    python report.py

fetch_jobs.py and enrich.py both call the same renderer; this just skips
straight to it, reading whatever feed.json / enriched.json / applied.json
currently hold.
"""

import json
import sys
from pathlib import Path

import html_report
import hidden

HERE = Path(__file__).parent
FEED = HERE / "feed.json"
ENRICHED = HERE / "enriched.json"
APPLIED = HERE / "applied.json"
ROOT = HERE.parent                 # D:\track
PAGE = ROOT / "jobs.html"          # global report at D:\track\ root


def extra_source_feeds():
    """Auto-discover extra sources: any D:\\track\\<name>\\<name>_feed.json in the
    shared schema. Add a source by dropping its folder + fetcher -- no edits here.
    The base H-1B feed (h1b/feed.json) is loaded separately."""
    jobs = []
    for p in sorted(ROOT.glob("*/*_feed.json")):
        try:
            jobs.extend(json.loads(p.read_text(encoding="utf-8")).get("jobs", []))
        except Exception:
            pass
    return jobs


def load_applied():
    """Returns {url: applied-at}. Tolerates the older flat list of URLs."""
    if not APPLIED.exists():
        return {}
    try:
        raw = json.loads(APPLIED.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if isinstance(raw, dict):
        raw = raw.get("applied", [])
    out = {}
    for item in raw or []:
        if isinstance(item, str):
            out[item] = ""
        elif isinstance(item, dict) and item.get("url"):
            out[item["url"]] = item.get("at", "")
    return out


def main():
    if not FEED.exists():
        sys.exit("feed.json missing -- run fetch_jobs.py first")
    feed = json.loads(FEED.read_text(encoding="utf-8"))
    jobs = feed.get("jobs", [])
    # Base H-1B tracker jobs -> group "H-1B tracker".
    for j in jobs:
        j["_srcgroup"] = "H-1B tracker"

    # Merge every extra source (dedup by URL). Each plug-in job keeps its own
    # source name as the group, so the report's dropdown lists it automatically.
    have = {j["url"] for j in jobs}
    for j in extra_source_feeds():
        if j.get("url") and j["url"] not in have:
            j["_srcgroup"] = (j.get("source") or "other").title()
            jobs.append(j)
            have.add(j["url"])

    # Honour the blocklist here too, so hiding a company shows up on the page
    # right away -- even for jobs already sitting in feed.json or a JobRight
    # feed -- without waiting for the next fetch_jobs.py run.
    blocked = hidden.load_hidden()
    if blocked:
        jobs = [j for j in jobs
                if str(j.get("company", "")).strip().casefold() not in blocked]

    enriched = {}
    if ENRICHED.exists():
        try:
            enriched = json.loads(ENRICHED.read_text(encoding="utf-8"))
        except Exception:
            pass

    applied = load_applied()

    rows = []
    for j in jobs:
        e = enriched.get(j["url"]) or {}
        rows.append({
            "Company": j["company"], "Job Title": j["title"], "Level": j["level"],
            "Work Auth Risk": j.get("work_auth_risk", ""),
            "Role Category": j["category"], "Location": j["location"],
            "H-1B Sponsor Tier": j["tier"],
            "Age": (f"{int(j['age_hours'])} hrs ago"
                    if j.get("age_hours") is not None else "unknown"),
            "_age": j["age_hours"] if j.get("age_hours") is not None else 1e9,
            "Apply Link": j["url"],
            "Min YOE": j.get("min_yoe", e.get("min_yoe")),
            "Sponsorship": j.get("sponsorship", e.get("sponsorship", "")),
            "Verdict": j.get("verdict", e.get("verdict", "")),
            "Source": j.get("source", ""),
            "SourceGroup": j.get("_srcgroup", "H-1B tracker"),
            "Match": j.get("jobright_match", ""),
        })

    html_report.write_html(PAGE, rows, feed.get("boards", "?"), applied)

    hit = [j for j in jobs if j["url"] in applied]
    print(f"Rebuilt {PAGE.name}: {len(rows)} jobs, {len(applied)} marked applied "
          f"({len(hit)} of them in this feed)")
    if hit:
        print("\nApplied:")
        for j in sorted(hit, key=lambda x: applied.get(x["url"], "")):
            when = applied.get(j["url"]) or "date unknown"
            print(f"   {when:<17} {j['company'][:18]:<18} {j['title'][:50]}")
    orphans = len(applied) - len(hit)
    if orphans:
        print(f"\n   ({orphans} applied URL(s) are from an older feed and "
              f"aren't in the current one)")


if __name__ == "__main__":
    main()
