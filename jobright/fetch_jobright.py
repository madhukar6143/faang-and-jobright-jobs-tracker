"""Standalone JobRight.ai puller. Self-contained -- no tracker dependency.

Pulls your personalized JobRight recommendations, keeps fresh (<=24h) roles that
aren't citizen-only or clearance-gated, and writes:
    jobright_jobs.csv    spreadsheet
    jobright_jobs.html   browsable list with clickable apply links

    python fetch_jobright.py

SESSION_ID is your personal JobRight login cookie. It expires -- when this stops
returning jobs, copy a fresh SESSION_ID from your browser cookies and paste it
below. It's your own account data; keep it private.
"""
import csv
import html
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

HERE = Path(__file__).parent

# ------------------------------------------------------------------- config
SESSION_ID = "YOUR_JOBRIGHT_SESSION_ID"   # <-- refresh when it expires
MAX_AGE_HOURS = 24
MAX_PAGES = 15
PER_PAGE = 10
DROP_CITIZEN_ONLY = True
DROP_CLEARANCE = True

# Light CS-title filter. JobRight already tailors to your profile, so this just
# trims the odd non-engineering rec.
CS_RE = re.compile(r"software|engineer|developer|programmer|\bsde\b|\bswe\b|"
                   r"machine learning|\bml\b|\bai\b|data (scien|engineer|analyst)|"
                   r"backend|back-end|frontend|front-end|full[\s-]?stack|devops|"
                   r"\bsre\b|platform|cloud|security engineer|mobile|ios|android", re.I)

HEADERS = {
    "accept": "application/json, text/plain, */*",
    "origin": "https://jobright.ai",
    "referer": "https://jobright.ai/jobs/recommend?from=homepage",
    "x-client-type": "web",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
}


def pull():
    s = requests.Session()
    s.headers.update(HEADERS)
    s.cookies.update({"SESSION_ID": SESSION_ID})
    out, position = [], 0
    for _ in range(MAX_PAGES):
        refresh = "true" if position == 0 else "false"
        url = ("https://jobright.ai/swan/recommend/list/jobs"
               f"?refresh={refresh}&sortCondition=0&position={position}"
               f"&count={PER_PAGE}&syncRerank=false")
        r = s.get(url, timeout=30)
        if r.status_code != 200:
            print(f"  ! HTTP {r.status_code} at position {position}", file=sys.stderr)
            break
        try:
            d = r.json()
        except ValueError:
            print("  ! non-JSON -- session likely expired", file=sys.stderr)
            break
        if not d.get("success"):
            print(f"  ! error: {d}", file=sys.stderr)
            break
        batch = d.get("result", {}).get("jobList", [])
        if not batch:
            break
        out.extend(batch)
        if len(batch) < PER_PAGE:
            break
        position += PER_PAGE
    return out


def _age_hours(s):
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(s[:19], fmt).replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - dt).total_seconds() / 3600
        except ValueError:
            continue
    return None


def collect():
    items = pull()
    print(f"got {len(items)} recommendations")
    rows = []
    for it in items:
        j = it.get("jobResult", {})
        c = it.get("companyResult", {})
        title = j.get("jobTitle", "")
        if DROP_CITIZEN_ONLY and j.get("isCitizenOnly"):
            continue
        if DROP_CLEARANCE and j.get("isClearanceRequired"):
            continue
        if not CS_RE.search(title):
            continue
        age = _age_hours(j.get("publishTime"))
        if age is not None and age > MAX_AGE_HOURS:
            continue
        rows.append({
            "jobId": j.get("jobId", "") or it.get("jobId", ""),
            "title": title,
            "company": c.get("companyName", ""),
            "location": j.get("jobLocation", ""),
            "seniority": j.get("jobSeniority", ""),
            "h1bSponsor": "Yes" if j.get("isH1bSponsor") else "",
            "match": it.get("rankDesc", ""),
            "score": it.get("displayScore", ""),
            "posted": j.get("publishTimeDesc", ""),
            "age_hours": round(age, 1) if age is not None else "",
            "applyLink": j.get("applyLink", ""),
            "publishTime": j.get("publishTime", ""),
        })
    rows.sort(key=lambda r: (r["age_hours"] if isinstance(r["age_hours"], float) else 1e9))
    return rows


def write_csv(rows):
    out = HERE / "jobright_jobs.csv"
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    return out


def write_html(rows):
    e = html.escape
    tr = "".join(
        f"<tr><td>{e(r['posted'])}</td><td><b>{e(r['title'])}</b></td>"
        f"<td>{e(r['company'])}</td><td>{e(r['location'])}</td>"
        f"<td>{e(r['seniority'])}</td><td class='{'y' if r['h1bSponsor'] else ''}'>"
        f"{e(r['h1bSponsor'])}</td><td>{e(r['match'])}</td>"
        f"<td><a href='{e(r['applyLink'])}' target='_blank'>Apply</a></td></tr>"
        for r in rows)
    page = f"""<!doctype html><meta charset=utf-8><title>JobRight ({len(rows)})</title>
<style>
 body{{font:14px -apple-system,Segoe UI,Roboto,sans-serif;margin:24px;color:#16181d}}
 h1{{font-size:20px}} .s{{color:#666;font-size:13px;margin-bottom:16px}}
 table{{border-collapse:collapse;width:100%}} th,td{{padding:7px 10px;border-bottom:1px solid #e2e5ea;text-align:left;font-size:13px}}
 th{{background:#f2f4f7;position:sticky;top:0}} a{{color:#1f4e79;font-weight:600}}
 td.y{{color:#0a7c42;font-weight:700}}
 @media(prefers-color-scheme:dark){{body{{background:#12141a;color:#e8eaee}}
  th{{background:#232833}} td,th{{border-color:#2a2f3a}} a{{color:#7fb3e0}}}}
</style>
<h1>JobRight recommendations</h1>
<div class=s>{len(rows)} CS roles, last {MAX_AGE_HOURS}h, citizen-only & clearance removed &middot;
 generated {datetime.now().strftime('%Y-%m-%d %H:%M')}</div>
<table><thead><tr><th>Posted</th><th>Role</th><th>Company</th><th>Location</th>
<th>Seniority</th><th>H1B</th><th>Match</th><th>Apply</th></tr></thead>
<tbody>{tr}</tbody></table>"""
    out = HERE / "jobright_jobs.html"
    out.write_text(page, encoding="utf-8")
    return out


def _epoch(s):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return int(datetime.strptime(s[:19], fmt)
                       .replace(tzinfo=timezone.utc).timestamp())
        except (ValueError, TypeError):
            continue
    return 0


def _level(seniority, title):
    s = (seniority or "").lower()
    t = title.lower()
    if "intern" in t or "co-op" in t or "co op" in t:
        return "Internship"
    if any(w in t for w in ("junior", "entry", "new grad", "associate ", "early career")):
        return "Entry / New Grad"
    if "entry" in s or "new grad" in s:
        return "Entry / New Grad"
    return "Unlabeled (possible entry)"


def _category(title):
    t = title.lower()
    for name, keys in [
        ("ML / AI", ("machine learning", "ml ", " ai", "artificial intelligence", "nlp", "deep learning")),
        ("Data", ("data engineer", "data scien", "data analyst", "analytics", "business intelligence")),
        ("DevOps / Cloud / SRE", ("devops", "sre", "site reliability", "platform", "cloud", "infrastructure")),
        ("Security", ("security", "appsec")),
        ("Frontend", ("frontend", "front-end", "front end", "react", "ui engineer")),
        ("Backend", ("backend", "back-end", "back end", "api")),
        ("Full Stack", ("full stack", "full-stack", "fullstack")),
        ("Mobile", ("ios", "android", "mobile")),
        ("QA / Test", ("qa", "sdet", "test engineer")),
    ]:
        if any(k in t for k in keys):
            return name
    return "Software Engineering (general)"


def write_feed_json(rows):
    """Machine-readable feed the H-1B server/report merges. Same schema as the
    tracker's feed jobs, tagged source='jobright'."""
    jobs = []
    for r in rows:
        jobs.append({
            "url": r["applyLink"],
            "title": r["title"],
            "company": r["company"] or "JobRight",
            "level": _level(r["seniority"], r["title"]),
            "work_auth_risk": "",   # JobRight already dropped citizen/clearance
            "category": _category(r["title"]),
            "location": r["location"],
            "tier": "H-1B sponsor" if r["h1bSponsor"] else "Active sponsor",
            "source": "jobright",
            "posted_epoch": _epoch(r.get("publishTime", "")),
            "age_hours": r["age_hours"] if isinstance(r["age_hours"], float) else None,
            "applied": False,
            "jobright_match": r.get("match", ""),
            "jobright_id": r.get("jobId", ""),      # to mark applied/ignored on JobRight
            "jobright_score": r.get("score", ""),   # 0-100, drives the JobRight apply/reject decision
        })
    out = HERE / "jobright_feed.json"
    out.write_text(json.dumps({"generated": datetime.now(timezone.utc).isoformat(),
                               "count": len(jobs), "jobs": jobs},
                              indent=1, ensure_ascii=False), encoding="utf-8")
    return out


def main():
    print("Pulling JobRight recommendations...")
    rows = collect()
    if not rows:
        print("No jobs collected -- refresh SESSION_ID in this file.")
        return
    csv_path = write_csv(rows)
    html_path = write_html(rows)
    feed_path = write_feed_json(rows)
    h1b = sum(1 for r in rows if r["h1bSponsor"])
    print(f"\n{len(rows)} CS roles (<= {MAX_AGE_HOURS}h) | {h1b} flagged H1B sponsor")
    print(f"Saved -> {csv_path}")
    print(f"Saved -> {html_path}")
    print(f"Saved -> {feed_path}   (merged by the H-1B server)")


if __name__ == "__main__":
    main()
