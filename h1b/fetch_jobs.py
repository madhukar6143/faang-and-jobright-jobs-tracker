"""Pull live job postings from the verified boards and write an Excel workbook.

Run probe_boards.py first (creates boards.json), then:
    python fetch_jobs.py

Output: h1b_jobs_YYYY-MM-DD.xlsx with four sheets --
    Entry-Level Jobs | All CS Jobs | H1B Sponsors | How To Use
"""

import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote_plus

import argparse

import requests
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

import html_report
import sources
import hidden
from companies import COMPANIES

HERE = Path(__file__).parent
BOARDS = HERE / "boards.json"
STATE = HERE / "state.json"
APPLIED = HERE / "applied.json"
FIRST_SEEN = HERE / "first_seen.json"   # dates dateless feeds (Pinpoint)
SEEN_CAP = 400  # only URLs sharing the newest timestamp, so this is generous

# Loaded once per run; url -> ISO timestamp of first sighting.
_FIRST_SEEN = {}
_FIRST_SEEN_NEW = {}
_FIRST_SEEN_LOCK = Lock()


def load_first_seen():
    global _FIRST_SEEN
    if FIRST_SEEN.exists():
        try:
            _FIRST_SEEN = json.loads(FIRST_SEEN.read_text(encoding="utf-8"))
        except Exception:
            _FIRST_SEEN = {}


def first_seen_dt(url):
    """When did we first see this URL? Stamps 'now' the first time."""
    if not url:
        return datetime.now(timezone.utc)
    iso = _FIRST_SEEN.get(url) or _FIRST_SEEN_NEW.get(url)
    if iso:
        try:
            d = datetime.fromisoformat(iso)
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except Exception:
            pass
    now = datetime.now(timezone.utc)
    with _FIRST_SEEN_LOCK:
        _FIRST_SEEN_NEW[url] = now.isoformat()
    return now


def save_first_seen():
    """Merge, drop anything older than 30 days so the file can't grow forever."""
    merged = dict(_FIRST_SEEN)
    merged.update(_FIRST_SEEN_NEW)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    merged = {u: t for u, t in merged.items() if t >= cutoff}
    FIRST_SEEN.write_text(json.dumps(merged, indent=1), encoding="utf-8")
TIMEOUT = 90
HEADERS = {"User-Agent": "h1b-job-tracker/1.0 (personal job search)"}

# ---------------------------------------------------------------- classifiers

# A title must contain one of these anchors to count as a CS role. Bare
# "engineer" is deliberately NOT an anchor -- it sweeps in mechanical,
# manufacturing, flight-test and sourcing roles.
CS_ANCHOR_RE = re.compile(r"""
    software | developer | programmer | \bsde\b | \bswe\b | coding | \bcode\b
  | machine\s*learning | \bml\b | \bmle\b | deep\s*learning | reinforcement\s+learning
  | artificial\s+intelligence | \bllm\b
  | \bai[\s/-]*(engineer|scientist|research|researcher|platform|infra|developer|architect)
  | \b(applied|generative|gen)\s*ai\b | \bai\s*/\s*ml\b
  | \bnlp\b | computer\s+vision | speech\s+recognition
  | data\s+scien | data\s+engineer | data\s+analyst | analytics | data\s+platform
  | business\s+intelligence | \betl\b
  | back[\s-]?end | front[\s-]?end | full[\s-]?stack | web\s+develop | \bweb\s+engineer
  | devops | site\s+reliability | \bsre\b | platform\s+engineer
  | infrastructure\s+engineer | cloud\s+engineer | kubernetes | \bdevex\b
  | systems\s+engineer | network\s+engineer | security\s+engineer | application\s+security
  | \bappsec\b | \binfosec\b | cryptograph
  | \bios\b | android | mobile\s+(engineer|developer) | embedded | firmware
  | \bqa\s+engineer | \bsdet\b | test\s+automation | automation\s+engineer
  | quality\s+assurance | software\s+test
  | applied\s+scientist | research\s+(scientist|engineer)
  | compiler | distributed\s+systems | database | graphics\s+engineer
  | game\s+(engineer|programmer)
  | forward\s+deployed | quantitative\s+developer | quant\s+developer
""", re.X)

# Non-CS disciplines. Full phrases only, so "Manufacturing Software Engineer"
# still survives on its "software" anchor.
CS_EXCLUDE_RE = re.compile(r"""
    mechanical\s+engineer | civil\s+engineer | chemical\s+engineer
  | structural\s+engineer | industrial\s+engineer | materials\s+engineer
  | electrical\s+engineer | propulsion | avionics\s+technician | \bhvac\b
  | facilities | \bsales\b | field\s+engineer | supply\s+chain
  | recruit | \btalent\b | marketing | account\s+executive | customer\s+success
  | technical\s+writer | program\s+manager | product\s+manager | \bhr\b
  | finance\s+associate | accountant | paralegal | counsel
  # non-software "systems"/"solution" engineering
  | (fluid|thermal|electrical|mechanical|motor|power|optical|rf|antenna
    |hydraulic|pneumatic|combustion|welding|solar|battery|packaging)\s
    [\w\s]{0,14}?(systems?\s+)?engineer
  | motor\s+control | power\s+electronics | solar\s+cell | test\s+solution
  | \bprobe\s+test | semiconductor\s+test | wafer
  # sales / comms roles that borrow engineering words
  | solutions?\s+(engineer|architect|consultant) | presales | pre-sales
  | \bpr\s+specialist | public\s+relations | communications\s+(specialist|manager)
  | business\s+development
  # instructional / research / logistics roles that borrow tech words
  | instructional\s+design | content\s+develop | curriculum
  | market\s+research | training\s+analyst | acquisition\s+logistics
  | \btraining\s+(specialist|developer) | learning\s+(designer|developer)
  # customer-facing / pre-sales engineering, and strategy roles
  | customer\s+engineer | customer\s+solutions | high\s+touch\s+support
  | technical\s+account | \bstrategy\s+and\s+operations\b | product\s+strategy
  | partner\s+engineer | field\s+application | implementation\s+consultant
""", re.X)

INTERN_RE = re.compile(r"\b(intern|interns|internship|co-?op)\b|summer\s+20\d\d")

# People-management and lead roles -- excluded entirely, at any seniority.
MANAGEMENT_RE = re.compile(r"""
    \b(manager|managing|director|chief|president|partner)\b
  | \bhead\s+of\b | \bvp\b | vice\s+president
  | \blead\b | \bleads\b            # team lead / tech lead / lead engineer
  | people\s+(manager|lead) | engineering\s+manager
""", re.X | re.I)

# Senior/management titles to drop from the feed entirely. Mirrors the
# userscript's SENIOR_TITLE_RE so the feed only holds roles it would apply to.
SENIOR_TITLE_RE = re.compile(
    r"\b(senior|sr\.?|staff|principal|lead|leads|architect|advisor|manager"
    r"|managing|director|head|vp|vice\s+president|chief|distinguished|fellow"
    r"|president|partner|expert)\b", re.I)

SENIOR_RE = re.compile(r"""
    \b(senior|sr\.?|staff|principal|lead|leads|head|chief|director|manager|mgr
      |vp|vice\s+president|architect|distinguished|fellow|expert|advanced
      |experienced|intermediate|\bii\b|\biii\b)\b
  | \b(engineer|developer|scientist|analyst)s?\s*,?\s*(ii|iii|iv|2|3|4)\b
  | \blevel\s*[2-9]\b | \bl[3-9]\b
  | \d\s*\+\s*(years|yrs|yoe)
  | \(\s*\d+\s*[-–]\s*\d+\s*(years|yrs|yoe)
""", re.X)

# Checked only AFTER entry markers, so "AI Research Scientist, New Grad" still
# reads as entry while a bare "Research Scientist" doesn't.
SOFT_SENIOR_RE = re.compile(r"""
    research\s+scientist | research\s+data\s+scientist
  | applied\s+scientist
  | \badvisor\b | \bconsultant\b | \bmid\b | \bspecialist\s+i{2,}\b
""", re.X)

ENTRY_RE = re.compile(r"""
    \bnew[\s-]?grad | \buniversity\s+grad | \bcollege\s+grad | \bcampus\b
  | \bentry[\s-]?level\b | \bearly[\s-]?career\b | \bjunior\b | \bjr\.?\b
  | \bassociate\b | \bapprentice\b | \btrainee\b | \brotational\b
  | \bgraduate\s+(program|scheme) | \bgrad\s+program
  | \b(engineer|developer|scientist|analyst)\s*,?\s*(i|1)\b
  | \blevel\s*1\b
""", re.X)

# Employers whose engineering roles are overwhelmingly ITAR/EAR-controlled or
# clearance-gated. Those normally require "US Person" status -- citizen or
# permanent resident -- which an H-1B candidate cannot satisfy. Not an absolute
# bar (these firms do sponsor on commercial programs), but the default
# assumption should be "ineligible unless the posting says otherwise".
US_PERSON_EMPLOYERS = {
    "SpaceX", "Anduril Industries", "Northrop Grumman", "Booz Allen Hamilton",
    "Boeing", "Leidos", "SAIC", "CACI", "L3Harris", "General Dynamics",
    "RTX", "Lockheed Martin", "MITRE", "Shield AI", "Saronic",
    "Applied Intuition", "Palantir Technologies",
}

CLEARANCE_RE = re.compile(
    r"clearance | ts/sci | top\s+secret | polygraph | \bsecret\b | \bitar\b"
    r"| us\s+person | \bdod\b | \bnato\b | \bfsp\b | \bci\s+poly", re.X | re.I)


def clearance_flag(company, title):
    """Empty string means no known work-authorization obstacle."""
    if CLEARANCE_RE.search(title or ""):
        return "Clearance required"
    if company in US_PERSON_EMPLOYERS:
        return "Likely US-Person only"
    return ""


ROLE_CATEGORIES = [
    ("ML / AI", ["machine learning", "deep learning", "artificial intelligence",
                 "llm", "nlp", "computer vision", "applied scientist",
                 "research scientist", "research engineer", " ai ", "ai/", "genai",
                 "generative ai", "mlops", "ml engineer", "ml infra"]),
    ("Data", ["data scien", "data engineer", "data analyst", "analytics",
              "business intelligence", "etl", "data platform", "data infra"]),
    ("DevOps / Cloud / SRE", ["devops", "site reliability", "sre", "platform engineer",
                              "infrastructure", "cloud engineer", "kubernetes",
                              "systems engineer", "network engineer", "observability"]),
    ("Security", ["security", "cryptograph", "appsec", "infosec", "threat"]),
    # "ios" needs a boundary or it matches "BIOS"; handled by _cat_re below.
    ("Mobile", [r"\bios\b", "android", "mobile engineer", "mobile developer",
                "react native", "flutter"]),
    ("Embedded / Firmware", ["embedded", "firmware", "hardware engineer", "fpga",
                             "asic", "verification engineer", "silicon"]),
    ("QA / Test", ["qa engineer", "quality engineer", "test engineer", "sdet",
                   "automation engineer"]),
    ("Full Stack", ["full stack", "full-stack", "fullstack"]),
    ("Frontend", ["frontend", "front-end", "front end", "web develop", "ui engineer",
                  "javascript", "react "]),
    ("Backend", ["backend", "back-end", "back end", "server", "api engineer",
                 "distributed systems", "database", "compiler"]),
]

US_STATES = [
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
    "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey",
    "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina",
    "south dakota", "tennessee", "texas", "utah", "vermont", "virginia",
    "washington", "west virginia", "wisconsin", "wyoming",
]
US_CITIES = [
    "san francisco", "new york", "seattle", "austin", "boston", "chicago",
    "los angeles", "san jose", "sunnyvale", "mountain view", "palo alto",
    "menlo park", "cupertino", "santa clara", "redmond", "bellevue", "denver",
    "atlanta", "dallas", "houston", "san diego", "portland", "phoenix",
    "miami", "philadelphia", "washington, d", "washington dc", "arlington",
    "boulder", "raleigh", "durham", "charlotte", "nashville", "pittsburgh",
    "detroit", "minneapolis", "salt lake city", "irvine", "san mateo",
    "hawthorne", "el segundo", "culver city", "brooklyn", "cambridge",
    "jersey city", "plano", "irving", "columbus", "kirkland", "mclean",
    "reston", "herndon", "chandler", "tempe", "st. louis", "kansas city",
    "madison", "ann arbor", "princeton", "stamford", "greenwich",
]
US_TOKENS = ["united states", "usa", "u.s.", " us ", "us-", "-us", "(us)",
             "remote us", "us remote", "remote - us", "americas"]
NON_US_HINTS = [
    "london", "dublin", "bangalore", "bengaluru", "hyderabad", "pune", "chennai",
    "mumbai", "delhi", "noida", "gurgaon", "toronto", "vancouver", "montreal",
    "sydney", "melbourne", "singapore", "tokyo", "seoul", "beijing", "shanghai",
    "shenzhen", "hong kong", "taipei", "berlin", "munich", "paris", "amsterdam",
    "barcelona", "madrid", "lisbon", "warsaw", "krakow", "prague", "zurich",
    "stockholm", "copenhagen", "oslo", "helsinki", "tel aviv", "dubai",
    "sao paulo", "mexico city", "buenos aires", "bogota", "manila", "jakarta",
    "kuala lumpur", "bangkok", "ho chi minh", "hanoi", "cairo", "lagos",
    "united kingdom", "canada", "india", "germany", "france", "netherlands",
    "australia", "ireland", "israel", "japan", "china", "brazil", "poland",
    "spain", "italy", "sweden", "switzerland", "emea", "apac", "latam",
    "reykjavik", "reykjavík", "iceland", "edinburgh", "manchester", "bristol",
    "belfast", "cork", "glasgow", "hamburg", "cologne", "frankfurt", "vienna",
    "brussels", "rotterdam", "milan", "rome", "athens", "bucharest", "budapest",
    "sofia", "tallinn", "vilnius", "riga", "istanbul", "auckland", "wellington",
    "cape town", "johannesburg", "nairobi", "santiago", "lima", "montevideo",
    "napoli", "okinawa", "uruma", "coimbatore", "ludwigshafen", "ho chi minh",
    "tan binh", "hyderabad", "trivandrum", "kochi", "ahmedabad", "kolkata",
    "yokohama", "osaka", "nagoya", "kyoto", "busan", "incheon", "chengdu",
    "wuhan", "xian", "suzhou", "penang", "cebu", "davao", "karachi", "lahore",
    "colombo", "dhaka", "kathmandu", "casablanca", "accra", "kampala",
    "brno", "roznov", "ostrava", "bratislava", "kosice", "cluj", "timisoara",
    "gdansk", "wroclaw", "poznan", "katowice", "lodz", "sofia", "belgrade",
    "zagreb", "ljubljana", "eindhoven", "utrecht", "leuven", "ghent", "graz",
    "stuttgart", "dusseldorf", "dresden", "leipzig", "nuremberg", "bordeaux",
    "toulouse", "lyon", "grenoble", "sophia antipolis", "valencia", "seville",
    "malaga", "turin", "bologna", "naples", "buenos aires", "yokneam",
    "herzliya", "raanana", "petah tikva", "haifa", "beer sheva", "ramat gan",
    "guadalajara", "monterrey", "san jose, costa rica", "portugal", "denmark",
    "norway", "finland", "belgium", "austria", "czech", "romania", "hungary",
    "greece", "turkey", "new zealand", "south africa", "argentina", "chile",
    "colombia", "peru", "mexico", "philippines", "vietnam", "thailand",
    "indonesia", "malaysia", "korea", "taiwan", "uae", "saudi",
]


WS = re.compile(r"\s+")


def _norm(s):
    collapsed = WS.sub(" ", (s or "").lower().strip())
    return " " + collapsed + " "


def is_cs_role(title):
    t = _norm(title)
    if CS_EXCLUDE_RE.search(t):
        return False
    return bool(CS_ANCHOR_RE.search(t))


def level_of(title):
    """Hard seniority wins outright ('Associate Director' is senior). An explicit
    entry marker then beats the softer signals ('Research Scientist, New Grad')."""
    t = _norm(title)
    if INTERN_RE.search(t):
        return "Internship"
    if MANAGEMENT_RE.search(t):
        return "Management"          # own bucket so it can be filtered everywhere
    if SENIOR_RE.search(t):
        return "Senior+"
    if ENTRY_RE.search(t):
        return "Entry / New Grad"
    if SOFT_SENIOR_RE.search(t):
        return "Senior+"
    return "Unlabeled (possible entry)"


_CAT_RE = [(name, re.compile("|".join(k if k.startswith("\\b") else re.escape(k)
                                      for k in keys), re.I))
           for name, keys in ROLE_CATEGORIES]


def category_of(title):
    t = _norm(title)
    for name, rx in _CAT_RE:
        if rx.search(t):
            return name
    return "Software Engineering (general)"


# Workday and Amazon suffix locations with ISO-3 country codes ("Rehovot,ISR").
NON_US_CODE_RE = re.compile(r"""[,\s](
    isr|ind|deu|gbr|can|jpn|chn|kor|twn|sgp|aus|nzl|nld|fra|esp|ita|prt|pol
  | mex|bra|arg|chl|col|per|phl|vnm|tha|idn|mys|are|sau|zaf|egy|nga|ken
  | che|swe|dnk|nor|fin|irl|cze|hun|rou|bgr|tur|ukr|rus|hkg|bel|aut|grc|isl
  | lux|svk|svn|hrv|est|ltu|lva|mar|tun|qat|kwt|pak|bgd|lka|npl
)\b""", re.X)


US_ABBREV_RE = re.compile(r",\s*(al|ak|az|ar|ca|co|ct|de|fl|ga|hi|id|il|in|ia|ks"
                          r"|ky|la|me|md|ma|mi|mn|ms|mo|mt|ne|nv|nh|nj|nm|ny|nc|nd"
                          r"|oh|ok|or|pa|ri|sc|sd|tn|tx|ut|vt|va|wa|wv|wi|wy|dc)\b")


# SmartRecruiters ends locations with a lowercase ISO-2 country code
# ("Ludwigshafen, RP, de"). Case matters: ", IN" is Indiana, ", in" is India.
TRAILING_CC = re.compile(r",\s*([a-z]{2})\s*$")


def us_status(location):
    """Order matters: named foreign places must beat the 2-letter state guess,
    or 'Buenos Aires, AR' reads as Arkansas."""
    raw = (location or "").strip()
    m = TRAILING_CC.search(raw)          # checked case-sensitively, before lowering
    if m:
        return "US" if m.group(1) == "us" else "Non-US"

    loc = _norm(location)
    if not loc.strip():
        return "Unknown"

    has_us_token = bool(re.search(r"[,\s(](usa|u\.s\.|united states)\b", loc))
    if NON_US_CODE_RE.search(loc) and not has_us_token:
        return "Non-US"
    if any(h in loc for h in NON_US_HINTS) and not has_us_token:
        return "Non-US"

    if (any(f" {s} " in loc or f",{s}" in loc or f", {s}" in loc for s in US_STATES)
            or any(c in loc for c in US_CITIES)
            or any(t in loc for t in US_TOKENS)
            or US_ABBREV_RE.search(loc)):
        return "US"
    return "Unknown"


# ---------------------------------------------------------------- fetchers

def _dt(value, unit="s"):
    """Parse a board timestamp into an aware datetime, or None."""
    if not value:
        return None
    try:
        if isinstance(value, str):
            d = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        v = float(value) / (1000 if unit == "ms" else 1)
        return datetime.fromtimestamp(v, tz=timezone.utc)
    except Exception:
        return None


def age_hours(dt):
    if dt is None:
        return None
    return (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0


def fetch_greenhouse(session, token):
    r = session.get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs",
                    params={"content": "false"}, timeout=TIMEOUT, headers=HEADERS)
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        out.append({
            "title": j.get("title", ""),
            "location": (j.get("location") or {}).get("name", ""),
            "url": j.get("absolute_url", ""),
            "posted_dt": _dt(j.get("first_published") or j.get("updated_at")),
        })
    return out


def fetch_lever(session, token):
    r = session.get(f"https://api.lever.co/v0/postings/{token}",
                    params={"mode": "json"}, timeout=TIMEOUT, headers=HEADERS)
    r.raise_for_status()
    out = []
    for j in r.json():
        cats = j.get("categories") or {}
        out.append({
            "title": j.get("text", ""),
            "location": cats.get("location", "") or j.get("workplaceType", ""),
            "url": j.get("hostedUrl", ""),
            "posted_dt": _dt(j.get("createdAt"), "ms"),
        })
    return out


def fetch_ashby(session, token):
    r = session.get(f"https://api.ashbyhq.com/posting-api/job-board/{token}",
                    timeout=TIMEOUT, headers=HEADERS)
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        out.append({
            "title": j.get("title", ""),
            "location": j.get("location", "") or "",
            "url": j.get("jobUrl", "") or j.get("applyUrl", ""),
            "posted_dt": _dt(j.get("publishedAt")),
        })
    return out


def fetch_smartrecruiters(session, token):
    out, offset = [], 0
    while True:
        r = session.get(f"https://api.smartrecruiters.com/v1/companies/{token}/postings",
                        params={"limit": 100, "offset": offset}, timeout=TIMEOUT, headers=HEADERS)
        r.raise_for_status()
        data = r.json()
        items = data.get("content", [])
        for j in items:
            loc = j.get("location") or {}
            city = ", ".join(x for x in [loc.get("city"), loc.get("region"), loc.get("country")] if x)
            out.append({
                "title": j.get("name", ""),
                "location": city,
                "url": (j.get("ref") or "").replace("api.smartrecruiters.com/v1",
                                                    "jobs.smartrecruiters.com") or
                       f"https://jobs.smartrecruiters.com/{token}/{j.get('id','')}",
                "posted_dt": _dt(j.get("releasedDate")),
            })
        offset += len(items)
        if len(items) < 100 or offset >= data.get("totalFound", 0) or offset > 2000:
            break
    return out


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "smartrecruiters": fetch_smartrecruiters,
}


def _fmt_age(hrs):
    if hrs is None:
        return "unknown"
    if hrs < 1:
        return "<1 hr ago"
    if hrs < 24:
        return f"{int(hrs)} hrs ago"
    return f"{int(hrs // 24)}d ago"


def pull(board, hours, last_dt, same_stamp, applied):
    """Fetch one board, keep fresh CS roles, flag which beat the last-job mark."""
    session = requests.Session()
    ats = board["ats"]
    jobs, last_err = None, None

    for attempt in range(3):
        try:
            if ats == "workday":
                jobs = sources.fetch_workday(session, board["tenant"], board["wd"],
                                             board["site"], hours)
            elif ats == "amazon":
                jobs = sources.fetch_amazon(session, hours)
            elif ats == "google":
                jobs = sources.fetch_google(session, hours)
            elif ats == "apple":
                jobs = sources.fetch_apple(session, hours)
            elif ats == "microsoft":
                jobs = sources.fetch_microsoft(session, hours)
            elif ats == "pinpoint":
                jobs = sources.fetch_pinpoint(session, board["token"])
            elif ats == "jibe":
                jobs = sources.fetch_jibe(session, board["token"])
            else:
                jobs = FETCHERS[ats](session, board["token"])
            break
        except Exception as e:
            last_err = e
            time.sleep(2 * (attempt + 1))

    if jobs is None:
        print(f"  ! {board['name']}: {type(last_err).__name__}: "
              f"{str(last_err)[:90]}", file=sys.stderr)
        return board["name"], [], 0

    seen, rows, total_cs = set(), [], 0
    for j in jobs:
        title = j.get("title") or ""
        if not is_cs_role(title):
            continue
        total_cs += 1

        # Dateless feeds (Pinpoint) get a synthetic date from the first-seen
        # cache: unknown until now -> stamped now (appears fresh once), and
        # ages out of the window on later runs.
        if j.get("posted_dt") is None and ats == "pinpoint":
            j["posted_dt"] = first_seen_dt(j.get("url") or "")

        hrs = age_hours(j.get("posted_dt"))
        # Workday/Amazon report day-level granularity only; their connectors
        # already window server-side, so an unknown age there is still fresh.
        if hrs is None:
            if ats not in ("workday", "amazon", "microsoft"):
                continue
        elif hrs > hours:
            continue

        key = (title.lower().strip(), (j.get("location") or "").lower().strip())
        if key in seen:
            continue
        seen.add(key)

        dt = j.get("posted_dt")
        url = j.get("url") or ""
        rows.append({
            "_is_new": bool(url) and is_new(dt, url, last_dt, same_stamp),
            "_posted_dt": dt,
            "Applied": "Yes" if url in applied else "",
            "Company": board["name"],
            "H-1B Sponsor Tier": board["tier"],
            "Industry": board["industry"],
            "Job Title": title,
            "Level": level_of(title),
            "Work Auth Risk": clearance_flag(board["name"], title),
            "Role Category": category_of(title),
            "Location": j.get("location") or "",
            "US?": us_status(j.get("location") or ""),
            "Posted": dt.strftime("%Y-%m-%d %H:%M") if dt else "",
            "Age": _fmt_age(hrs),
            "Apply Link": j.get("url") or "",
            "Source": ats,
            "_age": hrs if hrs is not None else 1e9,
        })
    return board["name"], rows, total_cs


# ---------------------------------------------------------------- sponsor sheet

DIRECT_CAREERS = {
    "Amazon": "https://www.amazon.jobs/en/search?base_query=software+engineer&loc_query=United+States",
    "Google": "https://www.google.com/about/careers/applications/jobs/results?q=software%20engineer&target_level=EARLY",
    "Microsoft": "https://jobs.careers.microsoft.com/global/en/search?q=software%20engineer&lc=United%20States",
    "Meta": "https://www.metacareers.com/jobs?q=software%20engineer",
    "Apple": "https://jobs.apple.com/en-us/search?search=software%20engineer&location=united-states-USA",
    "NVIDIA": "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite",
    "Intel": "https://jobs.intel.com/en/search-jobs",
    "Qualcomm": "https://careers.qualcomm.com/careers",
    "Salesforce": "https://careers.salesforce.com/en/jobs/",
    "Oracle": "https://careers.oracle.com/jobs/",
    "Adobe": "https://careers.adobe.com/us/en/search-results",
    "Cisco": "https://jobs.cisco.com/jobs/SearchJobs",
    "IBM": "https://www.ibm.com/careers/search",
    "Infosys": "https://career.infosys.com/jobsearch",
    "Wipro": "https://careers.wipro.com/careers-home/jobs",
    "Cognizant": "https://careers.cognizant.com/global-en/jobs/",
    "Accenture": "https://www.accenture.com/us-en/careers/jobsearch",
    "Deloitte": "https://apply.deloitte.com/careers/SearchJobs",
    "JPMorgan Chase": "https://careers.jpmorgan.com/us/en/students/programs",
    "Goldman Sachs": "https://www.goldmansachs.com/careers/students/programs",
    "Walmart": "https://careers.walmart.com/technology",
    "Tesla": "https://www.tesla.com/careers/search/",
    "Bloomberg": "https://careers.bloomberg.com/job/search",
    "Capital One": "https://www.capitalonecareers.com/search-jobs",
    "PayPal": "https://careers.pypl.com/opportunities",
    "Intuit": "https://jobs.intuit.com/search-jobs",
    "Atlassian": "https://www.atlassian.com/company/careers/all-jobs",
    "DoorDash": "https://careers.doordash.com/",
    "Shopify": "https://www.shopify.com/careers/search",
    "Snap": "https://careers.snap.com/jobs",
    "CrowdStrike": "https://crowdstrike.wd5.myworkdayjobs.com/crowdstrikecareers",
    "Citadel": "https://www.citadel.com/careers/open-opportunities/",
    "Two Sigma": "https://careers.twosigma.com/careers/SearchJobs",
    "Hudson River Trading": "https://www.hudsonrivertrading.com/careers/",
    "Optiver": "https://optiver.com/working-at-optiver/career-opportunities/",
    "Zoom": "https://careers.zoom.us/jobs",
}


def linkedin_search(company):
    q = quote_plus(f"{company} software engineer new grad")
    return (f"https://www.linkedin.com/jobs/search/?keywords={q}"
            f"&location=United%20States&f_E=1%2C2")


# ---------------------------------------------------------------- excel

HDR_FILL = PatternFill("solid", fgColor="1F4E79")
HDR_FONT = Font(bold=True, color="FFFFFF", size=11)
LINK_FONT = Font(color="0563C1", underline="single")


def write_sheet(ws, headers, rows, link_col=None, widths=None):
    ws.append(headers)
    for c in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=c)
        cell.fill, cell.font = HDR_FILL, HDR_FONT
        cell.alignment = Alignment(vertical="center")
    ws.row_dimensions[1].height = 22

    for r in rows:
        ws.append([r.get(h, "") for h in headers])

    if link_col and link_col in headers:
        idx = headers.index(link_col) + 1
        for row in range(2, ws.max_row + 1):
            cell = ws.cell(row=row, column=idx)
            if isinstance(cell.value, str) and cell.value.startswith("http"):
                cell.hyperlink = cell.value
                cell.font = LINK_FONT
                cell.value = "Apply / View"

    for i, h in enumerate(headers, 1):
        w = (widths or {}).get(h)
        if not w:
            longest = max([len(str(h))] + [len(str(r.get(h, ""))) for r in rows[:400]] or [10])
            w = min(max(longest + 2, 12), 55)
        ws.column_dimensions[get_column_letter(i)].width = w

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{ws.max_row}"


def load_state():
    """Per-company watermark: the single most recent posting already reported."""
    if STATE.exists():
        try:
            s = json.loads(STATE.read_text(encoding="utf-8"))
            if isinstance(s, dict) and "companies" in s:
                return s
        except Exception:
            print("  ! state.json unreadable, starting fresh", file=sys.stderr)
    return {"last_run": None, "companies": {}}


def mark_of(entry):
    """(last_posted datetime, set of URLs sharing that exact stamp)."""
    if not entry:
        return None, set()
    raw = entry.get("last_posted")
    dt = None
    if raw:
        try:
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except Exception:
            dt = None
    return dt, set(entry.get("same_stamp") or [])


def is_new(posted_dt, url, last_dt, same_stamp):
    """Newer than the company's last job -- or same instant but a different
    posting, which happens on feeds that only report a date."""
    if last_dt is None:
        return True
    if posted_dt is None:
        return False
    if posted_dt > last_dt:
        return True
    if posted_dt == last_dt:
        return url not in same_stamp
    return False


def save_state(state, window_rows):
    """Keep only the newest posting per company (plus any posting sharing its
    exact timestamp, so date-only feeds don't re-list the same batch).
    Everything older is dropped -- state.json stays tiny."""
    for company, rows in window_rows.items():
        dated = [r for r in rows if r.get("_posted_dt")]
        if not dated:
            continue
        entry = state["companies"].setdefault(company, {})
        prev_dt, prev_same = mark_of(entry)

        newest = max(r["_posted_dt"] for r in dated)
        if prev_dt and prev_dt > newest:
            continue                      # nothing newer arrived; keep the old mark
        if prev_dt and prev_dt == newest:
            same = prev_same | {r["Apply Link"] for r in dated
                                if r["_posted_dt"] == newest and r["Apply Link"]}
        else:
            same = {r["Apply Link"] for r in dated
                    if r["_posted_dt"] == newest and r["Apply Link"]}

        top = next((r for r in dated if r["_posted_dt"] == newest), None)
        entry["last_posted"] = newest.isoformat()
        entry["last_title"] = top["Job Title"] if top else ""
        entry["last_url"] = top["Apply Link"] if top else ""
        entry["same_stamp"] = sorted(same)[:SEEN_CAP]

    state["last_run"] = datetime.now(timezone.utc).isoformat()
    STATE.write_text(json.dumps(state, indent=1), encoding="utf-8")


def auto_window(state, floor=24, cap=720):
    """Fetch far enough back to cover everything since the last run."""
    last = state.get("last_run")
    if not last:
        return floor
    try:
        gap = (datetime.now(timezone.utc)
               - datetime.fromisoformat(last)).total_seconds() / 3600.0
    except Exception:
        return floor
    return int(max(floor, min(cap, gap + 12)))  # +12h safety margin


def load_boards():
    """Combine the probed ATS boards with the Workday + native connectors."""
    if not BOARDS.exists():
        sys.exit("boards.json missing -- run: python probe_boards.py")
    # boards.json already carries the probed Workday tenants.
    boards = [b for b in json.loads(BOARDS.read_text(encoding="utf-8")) if b.get("ats")]

    # Google excluded on request -- it published far more roles than anyone
    # else and dominated the feed. Add ("Google", "google") back here to
    # re-enable it.
    for name, key in (("Amazon", "amazon"),
                      ("Apple", "apple"), ("Microsoft", "microsoft")):
        tier, industry = sources.NATIVE_META[name]
        boards.append({"name": name, "tier": tier, "industry": industry, "ats": key})

    # Drop blocklisted companies before anything else touches them: hidden
    # boards are never fetched and never reach the feed, workbook or report.
    blocked = hidden.load_hidden()
    if blocked:
        boards = [b for b in boards if b["name"].strip().casefold() not in blocked]

    # A company resolved by more than one connector would double-count.
    uniq, seen = [], set()
    for b in boards:
        if b["name"] in seen:
            continue
        seen.add(b["name"])
        uniq.append(b)
    return uniq


def main():
    ap = argparse.ArgumentParser(description="Pull new CS jobs at H-1B sponsors.")
    ap.add_argument("--hours", type=int, default=None,
                    help="how far back to look (default: auto -- covers the gap "
                         "since your last run, minimum 24h)")
    ap.add_argument("--all-levels", action="store_true",
                    help="don't restrict the main sheet to entry level")
    ap.add_argument("--reset", action="store_true",
                    help="forget the watermark and treat everything as new")
    ap.add_argument("--no-state", action="store_true",
                    help="ignore and don't update the watermark (one-off run)")
    ap.add_argument("--hide", metavar="COMPANY", action="append",
                    help="hide a company: never fetch or show its jobs "
                         "(repeatable). Manages hidden_companies.json, then exits.")
    ap.add_argument("--unhide", metavar="COMPANY", action="append",
                    help="remove a company from the blocklist (repeatable). Exits.")
    ap.add_argument("--list-hidden", action="store_true",
                    help="print the current blocklist and exit")
    args = ap.parse_args()

    # Blocklist management is a standalone action -- do it and exit, no fetch.
    if args.hide or args.unhide or args.list_hidden:
        for name in args.hide or []:
            _, already = hidden.hide(name)
            print(f"  {'already hidden' if already else 'hidden'}: {name}")
        for name in args.unhide or []:
            _, was = hidden.unhide(name)
            print(f"  {'unhidden' if was else 'not on list'}: {name}")
        names = hidden.load_hidden_names()
        print(f"\nHidden companies ({len(names)}):")
        for n in sorted(names, key=str.casefold):
            print(f"   {n}")
        if not names:
            print("   (none)")
        return

    state = {"last_run": None, "companies": {}} if args.reset else load_state()
    hours = args.hours if args.hours is not None else auto_window(state)

    applied = set()
    if APPLIED.exists():
        try:
            raw = json.loads(APPLIED.read_text(encoding="utf-8"))
            items = raw if isinstance(raw, list) else raw.get("applied", [])
            applied = set(it["url"] if isinstance(it, dict) else it for it in items)
        except Exception:
            print("  ! applied.json unreadable, ignoring", file=sys.stderr)

    boards = load_boards()
    last = state.get("last_run")
    if last and not args.reset:
        print(f"Last run: {last[:16].replace('T', ' ')} UTC")
    print(f"Scanning {len(boards)} boards, looking back {hours}h "
          f"for postings newer than the watermark...\n")

    load_first_seen()
    all_rows, scanned = [], 0
    window_rows = {}
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {}
        for b in boards:
            last_dt, same = (None, set()) if args.reset else \
                mark_of(state["companies"].get(b["name"]))
            futures[pool.submit(pull, b, hours, last_dt, same, applied)] = b["name"]
        for i, fut in enumerate(as_completed(futures), 1):
            name, rows, total_cs = fut.result()
            window_rows.setdefault(name, []).extend(rows)
            scanned += total_cs
            new_rows = [r for r in rows if r["_is_new"]]
            all_rows.extend(new_rows)
            if new_rows:
                print(f"[{i:>3}/{len(boards)}] {name:<28} {len(new_rows):>3} new", flush=True)

    if not args.no_state:
        save_state(state, window_rows)
    save_first_seen()

    us_rows = [r for r in all_rows if r["US?"] in ("US", "Unknown")]
    # Drop ITAR/clearance/US-Person roles outright -- they can't be applied to
    # on OPT, so they were only cluttering the list. Set DROP_CLEARANCE=False to
    # keep them (flagged with a badge) instead.
    DROP_CLEARANCE = True
    if DROP_CLEARANCE:
        us_rows = [r for r in us_rows if not r["Work Auth Risk"]]
    # Drop senior/management titles outright -- lead, principal, manager,
    # senior, director, president, VP and the like. Keeps the list to entry/mid
    # roles you'd actually apply to. Set DROP_SENIOR=False to keep them.
    DROP_SENIOR = True
    if DROP_SENIOR:
        us_rows = [r for r in us_rows if not SENIOR_TITLE_RE.search(
            re.sub(r"[_\-/()\[\]]+", " ", r["Job Title"]))]
    entry_levels = ("Entry / New Grad", "Internship", "Unlabeled (possible entry)")
    entry_rows = us_rows if args.all_levels else [r for r in us_rows
                                                  if r["Level"] in entry_levels]

    level_rank = {"Entry / New Grad": 0, "Internship": 1, "Unlabeled (possible entry)": 2,
                  "Senior+": 3}
    # Freshest first -- that's the whole point of the window.
    entry_rows.sort(key=lambda r: (r["_age"], level_rank.get(r["Level"], 9), r["Company"]))
    us_rows.sort(key=lambda r: (r["_age"], r["Company"], r["Job Title"]))

    job_headers = ["Applied", "Company", "Job Title", "Level", "Work Auth Risk",
                   "Role Category", "Location", "US?", "H-1B Sponsor Tier",
                   "Industry", "Posted", "Age", "Apply Link"]
    widths = {"Applied": 9, "Company": 24, "Job Title": 50, "Level": 25,
              "Work Auth Risk": 21, "Role Category": 26, "Location": 34,
              "US?": 9, "H-1B Sponsor Tier": 17, "Industry": 22, "Posted": 17,
              "Age": 11, "Apply Link": 14}

    wb = Workbook()

    ws = wb.active
    ws.title = "New Entry-Level"
    write_sheet(ws, job_headers, entry_rows, "Apply Link", widths)

    write_sheet(wb.create_sheet("New All CS"), job_headers, us_rows,
                "Apply Link", widths)

    board_by_name = {b["name"]: b for b in boards}
    live_counts = {}
    for r in all_rows:
        live_counts[r["Company"]] = live_counts.get(r["Company"], 0) + 1

    def board_link(name, b):
        if DIRECT_CAREERS.get(name):
            return DIRECT_CAREERS[name]
        if not b:
            return linkedin_search(name)
        tok, ats = b.get("token"), b["ats"]
        if ats == "greenhouse":
            return f"https://boards.greenhouse.io/{tok}"
        if ats == "lever":
            return f"https://jobs.lever.co/{tok}"
        if ats == "ashby":
            return f"https://jobs.ashbyhq.com/{tok}"
        if ats == "smartrecruiters":
            return f"https://jobs.smartrecruiters.com/{tok}"
        if ats == "workday":
            return f"https://{b['tenant']}.{b['wd']}.myworkdayjobs.com/en-US/{b['site']}"
        return linkedin_search(name)

    blocked = hidden.load_hidden()
    known = {c[0] for c in COMPANIES}
    roster = [(n, t, i) for n, t, i, _ in COMPANIES
              if n.strip().casefold() not in blocked]
    roster += [(b["name"], b["tier"], b["industry"])
               for b in boards if b["name"] not in known]

    sponsor_rows = []
    for name, tier, industry in roster:
        b = board_by_name.get(name)
        sponsor_rows.append({
            "Company": name,
            "H-1B Sponsor Tier": tier,
            "Industry": industry,
            "Live Feed": f"Yes -- {b['ats']}" if b else "No -- search manually",
            "New This Run": live_counts.get(name, 0),
            "Careers / Search Link": board_link(name, b),
        })
    tier_rank = {"Top 25 sponsor": 0, "Large sponsor": 1, "Active sponsor": 2}
    sponsor_rows.sort(key=lambda r: (tier_rank.get(r["H-1B Sponsor Tier"], 9),
                                     -r["New This Run"], r["Company"]))
    write_sheet(wb.create_sheet("H1B Sponsors"),
                ["Company", "H-1B Sponsor Tier", "Industry", "Live Feed",
                 "New This Run", "Careers / Search Link"],
                sponsor_rows, "Careers / Search Link",
                {"Company": 30, "H-1B Sponsor Tier": 17, "Industry": 24,
                 "Live Feed": 22, "New This Run": 14, "Careers / Search Link": 22})

    live = sum(1 for b in boards if b["ats"])
    notes = wb.create_sheet("How To Use")
    for line in [
        ("H-1B Job Tracker", True),
        (f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} -- "
         f"ONLY postings not seen on a previous run.", False),
        (f"Scanned {live} live boards over a {hours}h look-back; {scanned} CS roles "
         f"were open, {len(all_rows)} of them new.", False),
        ("", False),
        ("HOW THE 'NEW' FILTER WORKS", True),
        ("state.json stores, per company, every posting URL already reported. Each run only shows", False),
        ("postings absent from that list, so nothing is ever listed twice -- and if you skip a few", False),
        ("days, the look-back widens automatically to cover the gap. Delete state.json (or pass", False),
        ("--reset) to start over.", False),
        ("", False),
        ("SHEETS", True),
        ("New Entry-Level - new US roles at entry / new grad / internship level.", False),
        ("New All CS      - every new US computer-science role, all seniority levels.", False),
        ("H1B Sponsors    - full sponsor list, including those with no public feed.", False),
        ("Rows are sorted freshest first. The Age column shows how long ago each was posted.", False),
        ("", False),
        ("LEVEL COLUMN", True),
        ("Entry / New Grad           - title explicitly says new grad, junior, entry, associate, level 1.", False),
        ("Internship                 - intern or co-op.", False),
        ("Unlabeled (possible entry) - a CS role with NO seniority marker in the title. Often open to", False),
        ("                             0-2 yrs, but verify the years-of-experience line in the posting.", False),
        ("", False),
        ("IMPORTANT CAVEATS", True),
        ("1. Sponsor tiers are approximate bands from published USCIS rankings, not exact counts.", False),
        ("   Verify any employer at: https://www.uscis.gov/tools/reports-and-studies/h-1b-employer-data-hub", False),
        ("2. Past H-1B sponsorship does NOT guarantee sponsorship for a specific role. Always check the", False),
        ("   posting's work-authorization line, and ask the recruiter directly.", False),
        ("3. Level is inferred from the job TITLE only. Confirm requirements in the posting itself.", False),
        ("4. Workday and Amazon report posting DATE, not time. For those, a 24h window means 'today or", False),
        ("   yesterday', so a few entries may be slightly older than 24 hours.", False),
        ("5. Google has no JSON jobs API. Its roles are read from google.com/about/careers, which embeds", False),
        ("   the result set in the page itself. Google's date field can reflect a re-publish, so an edited", False),
        ("   older role may surface once as new. Meta, Apple, and the Indian IT majors stay link-only.", False),
        ("", False),
        ("REFRESH", True),
        ("   python fetch_jobs.py                only what's new since the last run", False),
        ("   python fetch_jobs.py --hours 72     force a 3-day look-back", False),
        ("   python fetch_jobs.py --all-levels   don't restrict to entry level", False),
        ("   python fetch_jobs.py --reset        forget the watermark, treat all as new", False),
        ("   python probe_boards.py              re-verify board tokens (every few months)", False),
    ]:
        notes.append([line[0]])
        if line[1]:
            notes.cell(row=notes.max_row, column=1).font = Font(bold=True, size=12)
    notes.column_dimensions["A"].width = 110

    # Nothing new? Leave the existing report alone. Overwriting fixed filenames
    # with an empty run would destroy a perfectly good list.
    if not us_rows:
        print(f"\n{'='*62}")
        print(f"Boards scanned  : {len(boards)}  (look-back {hours}h)")
        print(f"NEW since last run: 0 -- existing report left untouched.")
        print("\nTry a wider window:  python fetch_jobs.py --hours 72")
        return

    # Fixed filenames -- each run overwrites in place, no dated clutter.
    out = HERE / "h1b_jobs.xlsx"
    wb.save(out)
    # The page has its own level filter, so give it everything US-relevant.
    page = html_report.write_html(HERE.parent / "jobs.html",
                                  us_rows, len(boards), applied)

    # Machine-readable feed for serve.py / the Tampermonkey bridge.
    feed = [{
        "url": r["Apply Link"],
        "title": r["Job Title"],
        "company": r["Company"],
        "level": r["Level"],
        "work_auth_risk": r["Work Auth Risk"],
        "category": r["Role Category"],
        "location": r["Location"],
        "tier": r["H-1B Sponsor Tier"],
        "source": r["Source"],
        "posted_epoch": int(r["_posted_dt"].timestamp()) if r.get("_posted_dt") else 0,
        "age_hours": round(r["_age"], 2) if r["_age"] < 1e9 else None,
        "applied": bool(r["Applied"]),
    } for r in us_rows if r["Apply Link"]]
    # Carry forward anything from previous runs you haven't acted on yet.
    # Without this the feed is replaced by each run's small new batch, and jobs
    # you never applied to silently vanish -- state.json already counts them as
    # seen, so they'd never be offered again.
    FEED_KEEP_DAYS = 1   # strict last-24h: unapplied jobs drop once past 24h
    feed_path = HERE / "feed.json"
    carried = 0
    if feed_path.exists():
        try:
            prev = json.loads(feed_path.read_text(encoding="utf-8")).get("jobs", [])
        except Exception:
            prev = []
        skipped = set()
        sk = HERE / "skipped.json"
        if sk.exists():
            try:
                raw = json.loads(sk.read_text(encoding="utf-8"))
                skipped = {x if isinstance(x, str) else x.get("url") for x in raw}
            except Exception:
                pass
        fresh_urls = {j["url"] for j in feed}
        cutoff = datetime.now(timezone.utc).timestamp() - FEED_KEEP_DAYS * 86400
        now_ts = datetime.now(timezone.utc).timestamp()
        for j in prev:
            u = j.get("url")
            if not u or u in fresh_urls or u in applied or u in skipped:
                continue
            if (j.get("posted_epoch") or 0) < cutoff:
                continue
            j["age_hours"] = (round((now_ts - j["posted_epoch"]) / 3600, 2)
                              if j.get("posted_epoch") else None)
            feed.append(j)
            carried += 1

    feed.sort(key=lambda x: -(x["posted_epoch"] or 0))
    (HERE / "feed.json").write_text(
        json.dumps({"generated": datetime.now(timezone.utc).isoformat(),
                    "window_hours": hours, "boards": len(boards),
                    "count": len(feed), "new_this_run": len(feed) - carried,
                    "carried_over": carried, "jobs": feed},
                   indent=1, ensure_ascii=False), encoding="utf-8")

    explicit = sum(1 for r in entry_rows if r["Level"] == "Entry / New Grad")
    interns = sum(1 for r in entry_rows if r["Level"] == "Internship")
    print(f"\n{'='*62}")
    print(f"Boards scanned         : {len(boards)}  (look-back {hours}h)")
    print(f"Open CS roles seen     : {scanned}")
    print(f"NEW since last run     : {len(all_rows)}")
    print(f"  US / unspecified     : {len(us_rows)}")
    print(f"  entry-level bucket   : {len(entry_rows)}")
    print(f"    explicit new grad  : {explicit}")
    print(f"    internships        : {interns}")
    print(f"    unlabeled          : {len(entry_rows) - explicit - interns}")
    if applied:
        print(f"  already applied      : {sum(1 for r in us_rows if r['Applied'])}")
    if not args.no_state:
        kb = STATE.stat().st_size / 1024 if STATE.exists() else 0
        print(f"\nWatermark: last job remembered for {len(state['companies'])} "
              f"companies ({kb:.0f} KB).")
    print(f"\nSaved -> {out}")
    print(f"Saved -> {page}   <- open this in a browser")


if __name__ == "__main__":
    main()
