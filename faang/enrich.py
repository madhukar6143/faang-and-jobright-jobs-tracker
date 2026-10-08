"""Fetch the actual posting body for a shortlist and read what the title can't tell you.

Level in the main tracker is inferred from job TITLES, so most rows land in
"Unlabeled (possible entry)" -- a guess. This opens each posting and pulls the
two facts that decide whether it's worth applying:

  * minimum years of experience actually demanded
  * whether the employer says it will NOT sponsor, or requires citizenship

    python enrich.py              # the eligible shortlist from feed.json
    python enrich.py --all        # every US row in the feed
    python enrich.py --limit 20

Writes enriched.json keyed by apply URL. serve.py merges it into /feed.json, so
the userscript and the HTML report both pick it up.
"""

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import unescape
from pathlib import Path

import requests

HERE = Path(__file__).parent
FEED = HERE / "feed.json"
OUT = HERE / "enriched.json"
JD_CACHE = HERE / "jd_cache.json"   # posting text, so re-analysis needs no refetch
JD_MAX_CHARS = 24000
JD_MIN_CHARS = 300

# Bot walls and JS-shell pages must never be cached -- cached junk is never
# retried, so the posting would stay unreadable forever.
JD_JUNK = re.compile(r"javascript is disabled|enable javascript|not a robot"
                     r"|access denied|are you a human|checking your browser"
                     r"|request blocked|captcha", re.I)
TIMEOUT = 40
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none", "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

# ---------------------------------------------------------------- extraction

_TAG = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
_ANY_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def to_text(html):
    s = _TAG.sub(" ", html or "")
    s = _ANY_TAG.sub(" ", s)
    return _WS.sub(" ", unescape(s)).strip()


def workday_body(session, url):
    """Workday detail pages are JS; the cxs endpoint returns the description."""
    m = re.match(r"https://([^.]+)\.(wd\d+)\.myworkdayjobs\.com/en-US/([^/]+)(/.*)$", url)
    if not m:
        return ""
    tenant, wd, site, path = m.groups()
    api = f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{path}"
    r = session.get(api, headers={**UA, "Accept": "application/json"}, timeout=TIMEOUT)
    r.raise_for_status()
    info = r.json().get("jobPostingInfo") or {}
    return to_text(info.get("jobDescription") or "")


def generic_body(session, url):
    r = session.get(url, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    return to_text(r.text)


def fetch_body(session, job):
    url = job["url"]
    try:
        if "myworkdayjobs.com" in url:
            body = workday_body(session, url)
            if body:
                return body
        return generic_body(session, url)
    except Exception as e:
        return f"__ERROR__ {type(e).__name__}"


# ---------------------------------------------------------------- signals

# "5+ years", "5-7 years", "minimum of 3 years", "at least 2 years"
YOE_RE = re.compile(
    r"(\d{1,2})\s*(?:\+|plus)?\s*(?:-|–|to)?\s*(?:\d{1,2})?\s*\+?\s*year", re.I)

NO_SPONSOR_RE = re.compile(r"""
    (?:not|unable|cannot|will\s+not|does\s+not|do\s+not)\s
      [\w\s,]{0,40}?(?:sponsor|sponsorship)
  | without\s+[\w\s]{0,34}?sponsorship
  | no\s+(?:visa\s+)?sponsorship
  | sponsorship\s+is\s+not\s+(?:available|offered|provided)
  | not\s+eligible\s+for\s+(?:visa\s+)?sponsorship
""", re.X | re.I)

WILL_SPONSOR_RE = re.compile(
    r"(?:will|do|can|does)\s+(?:consider\s+)?sponsor|sponsorship\s+(?:is\s+)?available"
    r"|visa\s+sponsorship\s+(?:is\s+)?(?:offered|provided|available)", re.I)

# The distinction that matters on STEM OPT (36 months of work authorisation
# with no employer sponsorship at all):
#
#   "we do not provide sponsorship"            -> fine, OPT covers 3 years
#   "authorised to work now AND in the future
#    without sponsorship"                      -> rules you out, you'd need
#                                                 H-1B at the end of OPT
FUTURE_BAR_RE = re.compile(r"""
    (?:now|current(?:ly)?)\s*(?:,|or|and|/)+\s*(?:in\s+the\s+)?future
  | current\s+or\s+future\s+(?:visa\s+)?sponsorship
  | future\s+(?:visa\s+)?sponsorship
  | ongoing\s+(?:visa\s+)?sponsorship
""", re.X | re.I)

# A standalone bar -- these postings never mention sponsorship at all, they
# just demand authorisation that outlasts OPT.
PERM_AUTH_RE = re.compile(
    r"permanent\s+(?:u\.?s\.?\s+)?work\s+authoriz|indefinite\s+work\s+authoriz"
    r"|unrestricted\s+work\s+authoriz|permanent\s+employment\s+authoriz", re.I)

# Keyword matching fails both ways here: it misses "must possess an active Top
# Secret clearance" and fires on "clearance is a plus". What matters is whether
# the mention is a REQUIREMENT ON YOU, so match the phrase, not the word.

# 1. the thing being talked about
_SUBJECT = re.compile(r"""
    (?:security|top[\s-]secret|ts/sci|government|dod|public\s+trust)\s+clearance
  | \bclearance\b | \bpolygraph\b
  | u\.?s\.?\s+citizen(?:ship|s)? | u\.?s\.?\s+persons?
  | \bitar\b | export[\s-]control(?:s|led)? | export\s+administration
  | permanent\s+resident | green\s+card
""", re.X | re.I)

# 2. phrasing that makes it binding
_REQUIRES = re.compile(r"""
    \b(?:must|required|require[sd]?|shall|mandatory|restricted\s+to|limited\s+to)\b
  | \bneed\s+to\s+(?:be|have|possess|hold)\b
  | \bonly\s+u\.?s\.? | \beligibility\s+requirement
""", re.X | re.I)

# 3. phrasing that makes it optional -- or somebody else's requirement
_SOFTENS = re.compile(r"""
    \b(?:preferred|preferrable|a\s+plus|nice[\s-]to[\s-]have|desirable|desired
      |beneficial|advantage|bonus|ideally|not\s+required|no\s+clearance)\b
  | \bwilling(?:ness)?\s+(?:to|and) | \b(?:ability|able|eligible)\s+to\s+obtain
  | \b(?:customers?|clients?|agencies|partners?|personnel|teams?|colleagues)\s+
      (?:who|that|with|requiring|holding|hold|need)
  # conditional boilerplate -- "may require access to export-controlled tech"
  # appears in a large share of ordinary US tech postings
  | \bmay\s+(?:require|involve|need|be\s+subject)
  # the subject is the PRODUCT's compliance, not the applicant
  | \b(?:standards?|certifications?|frameworks?|regulations?|controls?)\s+
      (?:like|such\s+as|including|e\.g\.)
  | \b(?:compliant|compliance|certified|accredited)\s+with
  # US labour-law posters that mention polygraphs to PROTECT employees
  | employee\s+polygraph\s+protection | polygraph\s+protection\s+act
  | know\s+your\s+rights | \beppa\b
""", re.X | re.I)

# Legal restrictions that are essentially never optional.
_HARD = re.compile(r"\bitar\b|export[\s-]control(?:s|led)?|ts/sci|\bpolygraph\b", re.I)

# "must be able to obtain" beats the softener: being *eligible* for a clearance
# still requires citizenship, so it rules you out just as firmly.
_OVERRIDE = re.compile(r"""
    \b(?:must|required\s+to|need\s+to)\s+(?:be\s+)?
      (?:able|eligible|willing|capable)\s+to\s+(?:obtain|acquire|secure|get)
  | \bmust\s+(?:be\s+)?(?:able\s+to\s+)?(?:obtain|maintain)\b
""", re.X | re.I)

# A character window straddles sentence boundaries, so "Required skills: Java"
# pairs with a citizenship mention two sentences later. Scope to the SENTENCE:
# the demand and the subject have to be in the same breath.
_SENTENCE = re.compile(r"[.;!?\n•·|]+")
_LONG_SEGMENT = 300      # stripped bullet lists can run together
_TIGHT = 70

# Periods inside abbreviations would split "must be a U.S. citizen" in half and
# hide the requirement. Flatten them before sentence splitting.
_ABBREV = [
    (re.compile(r"\bU\.\s?S\.\s?A\.", re.I), "USA "),
    (re.compile(r"\bU\.\s?S\.", re.I), "US "),
    (re.compile(r"\be\.\s?g\.", re.I), "eg "),
    (re.compile(r"\bi\.\s?e\.", re.I), "ie "),
    (re.compile(r"\b(Inc|Ltd|Co|Corp|Approx|No|vs)\.", re.I), r"\1 "),
]


def _flatten_abbrev(text):
    for rx, rep in _ABBREV:
        text = rx.sub(rep, text or "")
    return text


def _segments(text):
    for seg in _SENTENCE.split(_flatten_abbrev(text)):
        seg = seg.strip()
        if not seg:
            continue
        if len(seg) <= _LONG_SEGMENT:
            yield seg
            continue
        # Bullets collapsed into one run: fall back to a tight window so a
        # skills list at one end can't reach a citizenship note at the other.
        for m in _SUBJECT.finditer(seg):
            yield seg[max(0, m.start() - _TIGHT): m.end() + _TIGHT]


def work_auth_block(text):
    """-> (blocked: bool, evidence: str). Judges each mention in its own sentence."""
    for seg in _segments(text):
        if not _SUBJECT.search(seg):
            continue
        if _OVERRIDE.search(seg):          # binding even though it says "obtain"
            return True, seg.strip()[:220]
        if _SOFTENS.search(seg):
            continue                       # optional, or someone else's problem
        if _HARD.search(seg) or _REQUIRES.search(seg):
            return True, seg.strip()[:220]
    return False, ""




def min_yoe(text):
    """Lowest year-count mentioned near an experience requirement."""
    best = None
    for m in YOE_RE.finditer(text):
        window = text[max(0, m.start() - 90): m.end() + 90].lower()
        if "experience" not in window:
            continue
        if any(w in window for w in ("years of age", "year of age", "last year",
                                     "years ago", "per year", "years old",
                                     "over the years", "this year")):
            continue
        try:
            n = int(m.group(1))
        except ValueError:
            continue
        if 0 <= n <= 20:
            best = n if best is None else min(best, n)
    return best


def analyse(job, text, my_yoe=5, max_yoe=8):
    """my_yoe  = years you actually have -> anything at or under is a clean fit
       max_yoe = the most you're willing to stretch to before it's not worth it"""
    if text.startswith("__ERROR__"):
        return {"ok": False, "note": text.replace("__ERROR__ ", "fetch failed: ")}

    yoe = min_yoe(text)
    no_sp = bool(NO_SPONSOR_RE.search(text))
    will_sp = bool(WILL_SPONSOR_RE.search(text))
    cit, cit_evidence = work_auth_block(text)

    # "now or in the future" is only meaningful next to a sponsorship clause;
    # the permanent-authorisation phrasing stands on its own.
    future_bar = bool(PERM_AUTH_RE.search(text)) or (
        no_sp and bool(FUTURE_BAR_RE.search(text)))

    if cit:
        sponsor = "US person / clearance"
    elif future_bar:
        sponsor = "no future sponsorship"
    elif no_sp:
        sponsor = "no sponsorship (OPT ok)"
    elif will_sp:
        sponsor = "will sponsor"
    else:
        sponsor = "not stated"

    # Only citizenship/clearance is a hard stop -- OPT can't satisfy it.
    # Sponsorship wording is kept as a LABEL, not a filter: 36 months of STEM
    # OPT covers the job either way, and a "now and in the future" clause is
    # the employer's stated preference, not a legal barrier to being hired.
    verdict = "ok"
    if sponsor == "US person / clearance":
        verdict = "skip"
    elif yoe is not None and yoe > max_yoe:
        verdict = "skip"
    elif yoe is not None and yoe > my_yoe:
        verdict = "stretch"

    # Evidence must be the sentence that actually triggered the block, not the
    # top of the page -- otherwise there's no way to check the call.
    snip = cit_evidence
    if not snip:
        m = NO_SPONSOR_RE.search(text) or PERM_AUTH_RE.search(text)
        if m:
            snip = text[max(0, m.start() - 90): m.end() + 90].strip()

    return {"ok": True, "min_yoe": yoe, "sponsorship": sponsor,
            "verdict": verdict, "evidence": snip[:240], "chars": len(text)}


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Read posting bodies for the shortlist.")
    ap.add_argument("--all", action="store_true",
                    help="include ITAR/clearance employers too (normally skipped)")
    ap.add_argument("--entry-only", action="store_true",
                    help="only entry-level rows; by default senior roles are included")
    ap.add_argument("--yoe", type=int, default=5,
                    help="years of experience you have (default: 5)")
    ap.add_argument("--max-yoe", type=int, default=8,
                    help="highest requirement still worth a shot (default: 8)")
    ap.add_argument("--limit", type=int, default=0, help="cap how many to fetch")
    ap.add_argument("--recheck", action="store_true",
                    help="re-analyse from jd_cache.json without refetching "
                         "anything (instant -- use after changing the rules)")
    args = ap.parse_args()

    if not FEED.exists():
        sys.exit("feed.json missing -- run fetch_jobs.py first")
    feed = json.loads(FEED.read_text(encoding="utf-8"))
    jobs = feed.get("jobs", [])

    # Work-auth risk is still excluded by default -- that's a hard blocker.
    # Seniority is NOT: senior roles are read too unless --entry-only.
    if not args.all:
        jobs = [j for j in jobs if not j.get("work_auth_risk")]
    # Management/lead roles are never in scope -- don't spend fetches on them.
    jobs = [j for j in jobs if j["level"] != "Management"]
    if args.entry_only:
        jobs = [j for j in jobs if not j["level"].startswith("Senior")]
    if args.limit:
        jobs = jobs[:args.limit]

    if not jobs:
        print("Nothing to enrich -- feed.json has no eligible rows.")
        print("Existing enriched.json and h1b_jobs.html left untouched.")
        return

    # Posting text is cached, so changing the rules doesn't mean refetching.
    jd = {}
    if JD_CACHE.exists():
        try:
            jd = json.loads(JD_CACHE.read_text(encoding="utf-8"))
        except Exception:
            jd = {}

    if args.recheck:
        cached = [j for j in jobs if j["url"] in jd]
        print(f"Re-analysing {len(cached)} cached postings (no network)...\n")
        results = {}
        for j in cached:
            results[j["url"]] = analyse(j, jd[j["url"]]["text"], args.yoe, args.max_yoe)
        jobs = cached
    else:
        print(f"Reading {len(jobs)} posting bodies "
              f"({sum(1 for j in jobs if j['url'] in jd)} already cached)...\n")
        results = {}

        def work(job):
            s = requests.Session()
            body = fetch_body(s, job)
            return job, body, analyse(job, body, args.yoe, args.max_yoe)

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(work, j) for j in jobs]
            for i, fut in enumerate(as_completed(futures), 1):
                job, body, res = fut.result()
                results[job["url"]] = res
                cacheable = (body and not body.startswith("__ERROR__")
                             and len(body) >= JD_MIN_CHARS
                             and not JD_JUNK.search(body[:600]))
                if cacheable:
                    jd[job["url"]] = {
                        "company": job.get("company", ""),
                        "title": job.get("title", ""),
                        "location": job.get("location", ""),
                        "fetched": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "chars": len(body),
                        "text": body[:JD_MAX_CHARS],
                    }
                if res["ok"]:
                    y = f"{res['min_yoe']}y" if res["min_yoe"] is not None else "?y"
                    tag = {"ok": "OK    ", "stretch": "STRETCH", "skip": "SKIP  "}[res["verdict"]]
                    print(f"[{i:>3}/{len(jobs)}] {tag} {y:<4} {res['sponsorship']:<22} "
                          f"{job['company'][:16]:<16} {job['title'][:44]}", flush=True)
                else:
                    print(f"[{i:>3}/{len(jobs)}] ---    {res['note'][:40]:<40} "
                          f"{job['company'][:16]:<16} {job['title'][:40]}", flush=True)

        JD_CACHE.write_text(json.dumps(jd, indent=1, ensure_ascii=False),
                            encoding="utf-8")
        print(f"\njd_cache.json: {len(jd)} postings stored "
              f"({JD_CACHE.stat().st_size // 1024} KB)")

    # Merge, never replace: enriched.json is a cache keyed by URL. Overwriting
    # it with just this run's results throws away vetting work on jobs still
    # sitting unapplied in the feed.
    merged = {}
    if OUT.exists():
        try:
            merged = json.loads(OUT.read_text(encoding="utf-8"))
        except Exception:
            merged = {}
    before = len(merged)
    merged.update(results)
    OUT.write_text(json.dumps(merged, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"\nenriched.json: {before} known + {len(results)} read "
          f"-> {len(merged)} total")

    # Merge back into feed.json and rebuild the HTML, so one enrich run updates
    # everything without a 3-minute re-fetch of all 238 boards.
    # Use the merged cache, so jobs carried over from earlier runs keep the
    # verdict we already paid to work out.
    for j in feed.get("jobs", []):
        e = merged.get(j["url"])
        if e and e.get("ok"):
            j["min_yoe"] = e.get("min_yoe")
            j["sponsorship"] = e.get("sponsorship")
            j["verdict"] = e.get("verdict")
    FEED.write_text(json.dumps(feed, indent=1, ensure_ascii=False), encoding="utf-8")

    try:
        import html_report
        applied = set()
        ap = HERE / "applied.json"
        if ap.exists():
            raw = json.loads(ap.read_text(encoding="utf-8"))
            items = raw if isinstance(raw, list) else raw.get("applied", [])
            applied = set(it["url"] if isinstance(it, dict) else it for it in items)
        rows = [{
            "Company": j["company"], "Job Title": j["title"], "Level": j["level"],
            "Work Auth Risk": j.get("work_auth_risk", ""),
            "Role Category": j["category"], "Location": j["location"],
            "H-1B Sponsor Tier": j["tier"],
            "Age": (f"{int(j['age_hours'])} hrs ago" if j.get("age_hours") is not None
                    else "unknown"),
            "_age": j.get("age_hours") if j.get("age_hours") is not None else 1e9,
            "Apply Link": j["url"],
            "Min YOE": j.get("min_yoe"), "Sponsorship": j.get("sponsorship", ""),
            "Verdict": j.get("verdict", ""),
        } for j in feed.get("jobs", [])]
        # Same file fetch_jobs.py writes -- updated in place, not duplicated.
        page = html_report.write_html(HERE / "h1b_jobs.html", rows,
                                      feed.get("count", len(rows)), applied)
        print(f"Updated -> {page}   (now shows verified requirements)")
    except Exception as e:
        print(f"  (HTML rebuild skipped: {type(e).__name__}: {e})", file=sys.stderr)

    ok = [u for u, r in results.items() if r.get("verdict") == "ok"]
    stretch = [u for u, r in results.items() if r.get("verdict") == "stretch"]
    skip = [u for u, r in results.items() if r.get("verdict") == "skip"]
    failed = [u for u, r in results.items() if not r.get("ok")]
    optok = [u for u, r in results.items()
             if r.get("sponsorship") == "no sponsorship (OPT ok)"]
    bar = [u for u, r in results.items()
           if r.get("sponsorship") == "no future sponsorship"]

    print(f"\n{'='*64}")
    print(f"  good fit (<={args.yoe} yrs, no blocker)      : {len(ok)}")
    print(f"     incl. no-sponsorship, OPT covers it: {len(optok)}")
    print(f"  stretch ({args.yoe+1}-{args.max_yoe} yrs)                     : {len(stretch)}")
    print(f"  skip ({args.max_yoe}+ yrs, or work-auth blocker) : {len(skip)}")
    print(f"     'now AND future' sponsorship bar  : {len(bar)}")
    print(f"  body unreadable                      : {len(failed)}")
    print(f"\nSaved -> {OUT}")


if __name__ == "__main__":
    main()
