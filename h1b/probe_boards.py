"""Resolve which public job-board API (if any) each company uses.

Probes the free, unauthenticated job-board endpoints:
  Greenhouse       https://boards-api.greenhouse.io/v1/boards/{token}/jobs
  Lever            https://api.lever.co/v0/postings/{token}?mode=json
  Ashby            https://api.ashbyhq.com/posting-api/job-board/{token}
  SmartRecruiters  https://api.smartrecruiters.com/v1/companies/{token}/postings

Writes boards.json -- the verified company -> board mapping used by fetch_jobs.py.
Re-run occasionally; companies do migrate between ATS vendors.
"""

import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

from companies import COMPANIES, WORKDAY_CANDIDATES

HERE = Path(__file__).parent
OUT = HERE / "boards.json"
TIMEOUT = 12
HEADERS = {"User-Agent": "h1b-job-tracker/1.0 (personal job search)"}


def slug_variants(name, extra):
    """Generate plausible board tokens for a company name."""
    base = name.lower()
    base = base.replace("&", "and")
    cleaned = re.sub(r"[^a-z0-9]+", "", base)
    hyphen = re.sub(r"[^a-z0-9]+", "-", base).strip("-")
    # drop common corporate suffixes
    stripped = re.sub(r"(inc|llc|corp|corporation|company|group|technologies|labs)$", "", cleaned)
    out = [cleaned, hyphen, stripped] + list(extra)
    seen, uniq = set(), []
    for s in out:
        if s and s not in seen:
            seen.add(s)
            uniq.append(s)
    return uniq


def try_greenhouse(session, token):
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
    r = session.get(url, params={"content": "false"}, timeout=TIMEOUT, headers=HEADERS)
    if r.status_code != 200:
        return None
    jobs = r.json().get("jobs")
    return len(jobs) if isinstance(jobs, list) and jobs else None


def try_lever(session, token):
    url = f"https://api.lever.co/v0/postings/{token}"
    r = session.get(url, params={"mode": "json"}, timeout=TIMEOUT, headers=HEADERS)
    if r.status_code != 200:
        return None
    jobs = r.json()
    return len(jobs) if isinstance(jobs, list) and jobs else None


def try_ashby(session, token):
    url = f"https://api.ashbyhq.com/posting-api/job-board/{token}"
    r = session.get(url, timeout=TIMEOUT, headers=HEADERS)
    if r.status_code != 200:
        return None
    jobs = r.json().get("jobs")
    return len(jobs) if isinstance(jobs, list) and jobs else None


def try_smartrecruiters(session, token):
    url = f"https://api.smartrecruiters.com/v1/companies/{token}/postings"
    r = session.get(url, params={"limit": 10}, timeout=TIMEOUT, headers=HEADERS)
    if r.status_code != 200:
        return None
    total = r.json().get("totalFound")
    return total if total else None


def try_pinpoint(session, token):
    url = f"https://{token}.pinpointhq.com/postings.json"
    r = session.get(url, timeout=TIMEOUT, headers=HEADERS)
    if r.status_code != 200:
        return None
    data = r.json().get("data")
    return len(data) if isinstance(data, list) and data else None


PROBES = [
    ("greenhouse", try_greenhouse),
    ("lever", try_lever),
    ("ashby", try_ashby),
    ("smartrecruiters", try_smartrecruiters),
    ("pinpoint", try_pinpoint),
]


def resolve(company):
    name, tier, industry, extra = company
    session = requests.Session()
    for token in slug_variants(name, extra):
        for ats, fn in PROBES:
            try:
                count = fn(session, token)
            except Exception:
                continue
            if count:
                return {
                    "name": name, "tier": tier, "industry": industry,
                    "ats": ats, "token": token, "open_roles": count,
                }
    return {"name": name, "tier": tier, "industry": industry, "ats": None, "token": None, "open_roles": 0}


WD_HOSTS = ["wd1", "wd5", "wd3", "wd12", "wd101", "wd103"]
WD_SITES = ["External", "Careers", "External_Career_Site", "ExternalCareerSite",
            "external", "careers", "Search", "External_Careers", "jobs"]


def resolve_workday(cand):
    """Try host/site combinations until the cxs endpoint answers with jobs."""
    name, tier, industry, tenant, host_hint, site_hint = cand
    session = requests.Session()

    hosts = [host_hint] + [h for h in WD_HOSTS if h != host_hint]
    sites = [site_hint] + [s for s in WD_SITES if s != site_hint]
    sites += [f"{tenant.capitalize()}_Careers", f"{tenant}careers"]

    # The hinted pair is nearly always right; probe it before the cartesian sweep.
    combos = [(host_hint, site_hint)]
    combos += [(h, s) for h in hosts for s in sites if (h, s) != (host_hint, site_hint)]

    for host, site in combos:
        url = f"https://{tenant}.{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
        try:
            r = session.post(url, json={"appliedFacets": {}, "limit": 1, "offset": 0,
                                        "searchText": ""},
                             headers={**HEADERS, "Content-Type": "application/json"},
                             timeout=TIMEOUT)
            if r.status_code != 200:
                continue
            total = r.json().get("total", 0)
            if total:
                return {"name": name, "tier": tier, "industry": industry,
                        "ats": "workday", "tenant": tenant, "wd": host,
                        "site": site, "open_roles": total}
        except Exception:
            continue
    return None


def main():
    results = []
    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = {pool.submit(resolve, c): c[0] for c in COMPANIES}
        for i, fut in enumerate(as_completed(futures), 1):
            res = fut.result()
            results.append(res)
            mark = f"{res['ats']}:{res['token']} ({res['open_roles']})" if res["ats"] else "-- none"
            print(f"[ATS {i:>3}/{len(COMPANIES)}] {res['name']:<30} {mark}", flush=True)

    # Workday second: it only fills gaps the ATS pass couldn't resolve.
    already = {r["name"] for r in results if r["ats"]}
    todo = [c for c in WORKDAY_CANDIDATES if c[0] not in already]
    print(f"\nProbing {len(todo)} Workday candidates...\n", flush=True)

    wd_hits = 0
    with ThreadPoolExecutor(max_workers=14) as pool:
        futures = {pool.submit(resolve_workday, c): c[0] for c in todo}
        for i, fut in enumerate(as_completed(futures), 1):
            res = fut.result()
            if res:
                wd_hits += 1
                results = [r for r in results if r["name"] != res["name"]]
                results.append(res)
                print(f"[WD  {i:>3}/{len(todo)}] {res['name']:<30} "
                      f"{res['tenant']}/{res['wd']}/{res['site']} ({res['open_roles']})",
                      flush=True)

    results.sort(key=lambda r: (r["ats"] is None, r["name"].lower()))
    OUT.write_text(json.dumps(results, indent=2), encoding="utf-8")

    hit = sum(1 for r in results if r["ats"])
    print(f"\nResolved {hit}/{len(results)} companies to a public employer feed "
          f"({wd_hits} via Workday) -> {OUT}", file=sys.stderr)


if __name__ == "__main__":
    main()
