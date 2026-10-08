"""Connectors for sponsors that don't use Greenhouse/Lever/Ashby/SmartRecruiters.

Workday (26 sponsors) exposes a public "cxs" JSON endpoint. It caps pages at 20
results but accepts sortBy=POSTING_START_DATE_DESC, so a recency-windowed pull
can stop as soon as it walks past the window instead of paging the whole board.

Amazon and Microsoft each run their own in-house system, handled natively.
Google has no public job API -- its careers site is server-rendered and the old
careers.google.com/api/v3 endpoint now 404s. Google stays a direct link.
"""

import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept": "application/json",
    "Content-Type": "application/json",
}
TIMEOUT = 45

# Superseded by probe_boards.py, which discovers and verifies tenants into
# boards.json. Kept only as a reference of the originally hand-verified set.
_LEGACY_WORKDAY_TENANTS = [
    ("NVIDIA",                    "Top 25 sponsor", "Semiconductors / AI", "nvidia", "wd5", "NVIDIAExternalCareerSite"),
    ("Adobe",                     "Large sponsor",  "Software",            "adobe", "wd5", "external_experienced"),
    ("Salesforce",                "Large sponsor",  "SaaS",                "salesforce", "wd12", "External_Career_Site"),
    ("PayPal",                    "Large sponsor",  "Fintech",             "paypal", "wd1", "jobs"),
    ("eBay",                      "Large sponsor",  "E-commerce",          "ebay", "wd5", "apply"),
    ("Workday",                   "Large sponsor",  "SaaS",                "workday", "wd5", "Workday"),
    ("Target",                    "Large sponsor",  "Retail / Tech",       "target", "wd5", "targetcareers"),
    ("Comcast",                   "Large sponsor",  "Media / Telecom",     "comcast", "wd5", "Comcast_Careers"),
    ("T-Mobile",                  "Large sponsor",  "Telecom",             "tmobile", "wd1", "External"),
    ("Micron Technology",         "Large sponsor",  "Semiconductors",      "micron", "wd1", "External"),
    ("Applied Materials",         "Large sponsor",  "Semiconductors",      "amat", "wd1", "External"),
    ("Analog Devices",            "Large sponsor",  "Semiconductors",      "analogdevices", "wd1", "External"),
    ("Cadence Design Systems",    "Large sponsor",  "EDA",                 "cadence", "wd1", "External_Careers"),
    ("Broadcom",                  "Large sponsor",  "Semiconductors",      "broadcom", "wd1", "External_Career"),
    ("Capital One",               "Large sponsor",  "Finance",             "capitalone", "wd12", "Capital_One"),
    ("Mastercard",                "Large sponsor",  "Fintech",             "mastercard", "wd1", "CorporateCareers"),
    ("BlackRock",                 "Large sponsor",  "Finance",             "blackrock", "wd1", "BlackRock_Professional"),
    ("Boeing",                    "Large sponsor",  "Aerospace",           "boeing", "wd1", "EXTERNAL_CAREERS"),
    ("Northrop Grumman",          "Large sponsor",  "Aerospace",           "ngc", "wd1", "Northrop_Grumman_External_Site"),
    ("Pfizer",                    "Large sponsor",  "Pharma",              "pfizer", "wd1", "PfizerCareers"),
    ("Merck",                     "Large sponsor",  "Pharma",              "msd", "wd5", "SearchJobs"),
    ("CVS Health",                "Large sponsor",  "Healthcare",          "cvshealth", "wd1", "CVS_Health_Careers"),
    ("CrowdStrike",               "Active sponsor", "Security",            "crowdstrike", "wd5", "crowdstrikecareers"),
    ("Zillow",                    "Active sponsor", "Real Estate Tech",    "zillow", "wd5", "Zillow_Group_External"),
    ("Etsy",                      "Active sponsor", "E-commerce",          "etsy", "wd5", "Etsy_Careers"),
    ("Sony",                      "Active sponsor", "Media / Electronics", "sonyglobal", "wd1", "SonyGlobalCareers"),
]

NATIVE_META = {
    "Amazon":    ("Top 25 sponsor", "Cloud / E-commerce"),
    "Microsoft": ("Top 25 sponsor", "Big Tech"),
    "Google":    ("Top 25 sponsor", "Big Tech"),
    "Apple":     ("Top 25 sponsor", "Big Tech"),
}

# Meta is deliberately absent: metacareers.com renders its job list client-side
# from an internal GraphQL call keyed by a doc_id baked into their JS bundle,
# which rotates on every deploy. That's a private API, not a public feed, so
# Meta stays link-only.

UA_HTML = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def _now():
    return datetime.now(timezone.utc)


# -------------------------------------------------------------------- Pinpoint
# {tenant}.pinpointhq.com/postings.json -> {"data": [ {title, location, path...} ]}
# No posting date in the payload, so posted_dt is None; fetch_jobs dates these
# via its first-seen cache.

def fetch_pinpoint(session, tenant):
    url = f"https://{tenant}.pinpointhq.com/postings.json"
    r = session.get(url, headers={"User-Agent": UA["User-Agent"],
                                  "Accept": "application/json"}, timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json().get("data", []):
        loc = j.get("location") or {}
        out.append({
            "title": j.get("title", ""),
            "location": (loc.get("name") if isinstance(loc, dict) else loc) or "",
            "url": f"https://{tenant}.pinpointhq.com" + (j.get("path") or ""),
            "posted_dt": None,
        })
    return out


# ------------------------------------------------------------------------ Jibe
# Jibe-hosted career sites (e.g. careers.medpace.com) expose /api/jobs as clean
# paginated JSON. token = the careers hostname, e.g. "careers.medpace.com".

def fetch_jibe(session, host, page_cap=25, per_page=50):
    out = []
    for page in range(1, page_cap + 1):
        r = session.get(f"https://{host}/api/jobs",
                        params={"page": page, "limit": per_page},
                        headers={"User-Agent": UA["User-Agent"],
                                 "Accept": "application/json",
                                 "Referer": f"https://{host}/"}, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        items = data.get("jobs", [])
        if not items:
            break
        for it in items:
            j = it.get("data", it)
            raw = j.get("create_date") or j.get("posted_date") or ""
            dt = None
            if raw:
                try:
                    dt = datetime.fromisoformat(raw.replace("+0000", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                except Exception:
                    dt = None
            out.append({
                "title": j.get("title", ""),
                "location": j.get("full_location") or j.get("city") or "",
                "url": j.get("apply_url") or j.get("url") or "",
                "posted_dt": dt,
            })
        if len(items) < per_page or len(out) >= (data.get("totalCount") or 0):
            break
    return out


# --------------------------------------------------------------------- Workday

_REL = re.compile(r"posted\s+(?:(today)|(yesterday)|(\d+)\s*\+?\s*day)", re.I)


def workday_age_hours(posted_on):
    """Workday reports relative strings: 'Posted Today', 'Posted 30+ Days Ago'."""
    if not posted_on:
        return None
    m = _REL.search(posted_on)
    if not m:
        return None
    if m.group(1):
        return 0.0
    if m.group(2):
        return 24.0
    return float(m.group(3)) * 24.0


_MULTI_LOC = re.compile(r"^\s*\d+\s+locations?\s*$", re.I)


def _resolve_workday_location(session, cxs_base, path):
    """Workday collapses multi-site postings to '3 Locations', which defeats a
    US filter. The job-detail endpoint returns the real list."""
    try:
        r = session.get(cxs_base + path, headers=UA, timeout=TIMEOUT)
        r.raise_for_status()
        info = r.json().get("jobPostingInfo") or {}
        locs = [info.get("location")] + list(info.get("additionalLocations") or [])
        return "; ".join(x for x in locs if x)
    except Exception:
        return ""


def fetch_workday(session, tenant, wd, site, max_age_hours, page_cap=40):
    """Page newest-first, stop once we walk past the window."""
    url = f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
    cxs_base = f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"
    base = f"https://{tenant}.{wd}.myworkdayjobs.com/en-US/{site}"
    out, offset, stale_pages = [], 0, 0

    for _ in range(page_cap):
        r = session.post(url, json={
            "appliedFacets": {}, "limit": 20, "offset": offset,
            "searchText": "", "sortBy": "POSTING_START_DATE_DESC",
        }, headers=UA, timeout=TIMEOUT)
        r.raise_for_status()
        postings = r.json().get("jobPostings", [])
        if not postings:
            break

        fresh_here = 0
        for j in postings:
            age = workday_age_hours(j.get("postedOn"))
            if age is None or age > max_age_hours:
                continue
            fresh_here += 1
            # Snap to the start of the posting day. Workday only reports
            # "Posted Today"/"N Days Ago", so deriving the stamp from the
            # clock would make the same job look newer on every run and
            # defeat the watermark. Day boundaries are stable.
            day = (_now() - timedelta(hours=age)).replace(
                hour=0, minute=0, second=0, microsecond=0)
            out.append({
                "title": j.get("title", ""),
                "location": j.get("locationsText", "") or "",
                "url": base + (j.get("externalPath") or ""),
                "posted_dt": day,
                "_path": j.get("externalPath") or "",
            })

        # Sorted newest-first, so two barren pages means we're past the window.
        stale_pages = 0 if fresh_here else stale_pages + 1
        if stale_pages >= 2:
            break
        offset += len(postings)

    # Only the collapsed ones need a detail call -- cheap inside a short window.
    vague = [j for j in out if _MULTI_LOC.match(j["location"])]
    if vague:
        with ThreadPoolExecutor(max_workers=4) as pool:
            resolved = pool.map(
                lambda j: _resolve_workday_location(session, cxs_base, j["_path"]), vague)
            for j, loc in zip(vague, resolved):
                if loc:
                    j["location"] = loc

    for j in out:
        j.pop("_path", None)
    return out


# ---------------------------------------------------------------------- Amazon

def _parse_amazon_date(s):
    if not s:
        return None
    s = re.sub(r"\s+", " ", s).strip()
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def fetch_amazon(session, max_age_hours, page_cap=25):
    """amazon.jobs search.json, sorted most-recent-first."""
    cutoff = _now() - timedelta(hours=max_age_hours)
    out, offset = [], 0

    for _ in range(page_cap):
        r = session.get("https://www.amazon.jobs/en/search.json", params={
            "sort": "recent", "result_limit": 100, "offset": offset,
        }, headers={"User-Agent": UA["User-Agent"]}, timeout=TIMEOUT)
        r.raise_for_status()
        jobs = r.json().get("jobs", [])
        if not jobs:
            break

        past_window = False
        for j in jobs:
            dt = _parse_amazon_date(j.get("posted_date"))
            # Date-only granularity: keep the whole day the cutoff falls on.
            if dt and dt.date() < cutoff.date():
                past_window = True
                continue
            out.append({
                "title": j.get("title", ""),
                "location": j.get("normalized_location") or j.get("location") or "",
                "url": "https://www.amazon.jobs" + (j.get("job_path") or ""),
                "posted_dt": dt,
            })
        if past_window:
            break
        offset += len(jobs)

    return out


# ---------------------------------------------------------------------- Google

GOOGLE_URL = "https://www.google.com/about/careers/applications/jobs/results/"
_AF_BLOCK = re.compile(r"AF_initDataCallback\((\{.*?\})\);</script>", re.S)
_AF_DATA = re.compile(r"data:(\[.*\])\s*,\s*sideChannel", re.S)


def _google_slug(title):
    s = re.sub(r"[^a-z0-9]+", "-", (title or "").lower())
    return s.strip("-")


def _google_payload(html_text):
    """Google's careers page is a JS shell, but it ships the result set inline in
    an AF_initDataCallback block, so plain HTTP is enough -- no browser needed."""
    for block in _AF_BLOCK.findall(html_text):
        m = _AF_DATA.search(block)
        if not m:
            continue
        try:
            data = json.loads(m.group(1))
        except Exception:
            continue
        if isinstance(data, list) and data and isinstance(data[0], list) and data[0]:
            first = data[0][0]
            if isinstance(first, list) and len(first) > 9 and isinstance(first[1], str):
                return data[0]
    return []


def fetch_google(session, max_age_hours, page_cap=15):
    """Google has no public jobs API; this reads its own careers result page,
    which embeds the records as JSON. Sorted newest-first, so we stop early."""
    cutoff = _now() - timedelta(hours=max_age_hours)
    out = []

    for page in range(1, page_cap + 1):
        r = session.get(GOOGLE_URL,
                        params={"location": "United States", "sort_by": "date",
                                "page": page},
                        headers=UA_HTML, timeout=TIMEOUT)
        r.raise_for_status()
        jobs = _google_payload(r.text)
        if not jobs:
            break

        past_window = False
        for rec in jobs:
            try:
                job_id, title = rec[0], rec[1]
            except Exception:
                continue

            dt = None
            stamp = rec[13] if len(rec) > 13 else None
            if isinstance(stamp, list) and stamp and isinstance(stamp[0], (int, float)):
                dt = datetime.fromtimestamp(stamp[0], tz=timezone.utc)
            if dt and dt < cutoff:
                past_window = True
                continue

            locs = []
            if len(rec) > 9 and isinstance(rec[9], list):
                for L in rec[9]:
                    if isinstance(L, list) and L and isinstance(L[0], str):
                        locs.append(L[0])

            out.append({
                "title": title,
                "location": "; ".join(locs[:4]),
                "url": f"{GOOGLE_URL}{job_id}-{_google_slug(title)}",
                "posted_dt": dt,
            })

        if past_window:
            break

    return out


# ----------------------------------------------------------------------- Apple

APPLE_SEARCH = "https://jobs.apple.com/en-us/search"
APPLE_DETAIL = "https://jobs.apple.com/en-us/details"
_APPLE_HYDRATE_STR = re.compile(
    r"window\.__staticRouterHydrationData\s*=\s*JSON\.parse\((\"(?:[^\"\\]|\\.)*\")\)", re.S)
_APPLE_HYDRATE_OBJ = re.compile(
    r"window\.__staticRouterHydrationData\s*=\s*(\{.*?\})\s*;?\s*</script>", re.S)


def _apple_payload(html_text):
    """Apple's search page is a React shell that ships results inline in
    window.__staticRouterHydrationData."""
    data = None
    m = _APPLE_HYDRATE_STR.search(html_text)
    if m:
        try:
            data = json.loads(json.loads(m.group(1)))
        except Exception:
            data = None
    if data is None:
        m = _APPLE_HYDRATE_OBJ.search(html_text)
        if m:
            try:
                data = json.loads(m.group(1))
            except Exception:
                return []
    if not isinstance(data, dict):
        return []
    search = ((data.get("loaderData") or {}).get("search") or {})
    results = search.get("searchResults")
    return results if isinstance(results, list) else []


def _apple_location(rec):
    out = []
    for L in rec.get("locations") or []:
        if not isinstance(L, dict):
            continue
        city, state = L.get("city") or "", L.get("stateProvince") or ""
        if city:
            out.append(f"{city}, {state}".strip().rstrip(","))
        elif L.get("name"):
            out.append(L["name"])
    return "; ".join(dict.fromkeys(out))[:160]


def fetch_apple(session, max_age_hours, page_cap=15):
    """jobs.apple.com, US roles sorted newest-first."""
    cutoff = _now() - timedelta(hours=max_age_hours)
    out = []

    for page in range(1, page_cap + 1):
        r = session.get(APPLE_SEARCH,
                        params={"sort": "newest", "location": "united-states-USA",
                                "page": page},
                        headers=UA_HTML, timeout=TIMEOUT)
        r.raise_for_status()
        results = _apple_payload(r.text)
        if not results:
            break

        past_window = False
        for rec in results:
            if not isinstance(rec, dict):
                continue
            dt = None
            raw = rec.get("postDateInGMT") or ""
            if raw:
                try:
                    # trim sub-second precision Python can't parse
                    cleaned = re.sub(r"\.(\d{6})\d*Z?$", r".\1+00:00", raw.replace("Z", "+00:00"))
                    dt = datetime.fromisoformat(cleaned)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                except Exception:
                    dt = None
            if dt and dt < cutoff:
                past_window = True
                continue

            pid = rec.get("positionId") or ""
            slug = rec.get("transformedPostingTitle") or ""
            out.append({
                "title": rec.get("postingTitle") or "",
                "location": _apple_location(rec),
                "url": f"{APPLE_DETAIL}/{pid}/{slug}" if pid else APPLE_SEARCH,
                "posted_dt": dt,
            })

        if past_window:
            break

    return out


# ------------------------------------------------------------------- Microsoft

def fetch_microsoft(session, max_age_hours, page_cap=25):
    """jobs.careers.microsoft.com backend, sorted most-recent-first.

    NOTE: this host was unreachable from the sandboxed network used to build
    this script (TLS hostname mismatch -> proxy interception). The request shape
    is correct and should work on an ordinary connection; it fails soft if not.
    """
    cutoff = _now() - timedelta(hours=max_age_hours)
    out = []

    for page in range(1, page_cap + 1):
        r = session.get(
            "https://gcsservices.careers.microsoft.com/search/api/v1/search",
            params={"q": "", "l": "en_us", "pg": page, "pgSz": 20,
                    "o": "Recent", "flt": "true"},
            headers=UA, timeout=TIMEOUT)
        r.raise_for_status()
        result = (r.json().get("operationResult") or {}).get("result") or {}
        jobs = result.get("jobs") or []
        if not jobs:
            break

        past_window = False
        for j in jobs:
            raw = j.get("postingDate") or j.get("postedDate") or ""
            dt = None
            if raw:
                try:
                    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                except ValueError:
                    dt = None
            if dt and dt < cutoff:
                past_window = True
                continue
            props = j.get("properties") or {}
            locs = props.get("locations") or []
            out.append({
                "title": j.get("title", ""),
                "location": ", ".join(locs[:3]) if locs else (props.get("primaryLocation") or ""),
                "url": f"https://jobs.careers.microsoft.com/global/en/job/{j.get('jobId', '')}",
                "posted_dt": dt,
            })
        if past_window:
            break

    return out
