"""Feed health check -- verify H-1B and JobRight are fetching correctly.

    python check_feeds.py

Reports, per feed: how fresh the file is, job count, age spread (flags any
job older than 24h), missing url/company/title, duplicate URLs, and the source
breakdown. Exit code is non-zero if anything looks wrong, so you can chain it.
"""
import json
import os
import sys
import collections
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
FEEDS = [
    ("H-1B",     os.path.join(HERE, "faang", "feed.json")),
    ("JobRight", os.path.join(HERE, "jobright", "jobright_feed.json")),
]
MAX_AGE_HOURS = 24
STALE_FILE_MIN = 90        # warn if the feed file itself hasn't been rebuilt

GREEN, RED, YELLOW, DIM, OFF = "\033[92m", "\033[91m", "\033[93m", "\033[2m", "\033[0m"


def ok(msg):   print(f"  {GREEN}OK{OFF}   {msg}")
def bad(msg):  print(f"  {RED}FAIL{OFF} {msg}")
def warn(msg): print(f"  {YELLOW}WARN{OFF} {msg}")


def check(label, path):
    print(f"\n{'='*58}\n{label}   {DIM}{path}{OFF}")
    problems = 0

    if not os.path.exists(path):
        bad("feed file MISSING -- fetcher never ran / wrote elsewhere")
        return 1
    try:
        data = json.load(open(path, encoding="utf-8"))
    except Exception as e:
        bad(f"feed file is CORRUPT JSON: {e}")
        return 1

    file_age = (datetime.now().timestamp() - os.path.getmtime(path)) / 60
    (ok if file_age <= STALE_FILE_MIN else warn)(
        f"file rebuilt {file_age:.0f} min ago"
        + ("" if file_age <= STALE_FILE_MIN else "  <- old; re-run the fetcher or /refresh"))
    if file_age > STALE_FILE_MIN:
        problems += 1

    jobs = data.get("jobs", [])
    if not jobs:
        bad("0 jobs -- fetch returned nothing (expired session? all filtered? network?)")
        return problems + 1
    ok(f"{len(jobs)} jobs")

    # freshness: any job older than the 24h window is a leak
    ages = [j["age_hours"] for j in jobs if j.get("age_hours") is not None]
    if ages:
        stale = [a for a in ages if a > MAX_AGE_HOURS]
        line = f"age spread {min(ages):.1f}h .. {max(ages):.1f}h"
        if stale:
            bad(f"{line}  -- {len(stale)} job(s) OLDER than {MAX_AGE_HOURS}h leaked in")
            problems += 1
        else:
            ok(line + f" (all within {MAX_AGE_HOURS}h)")
    else:
        warn("no age_hours on any job -- can't verify the 24h window")

    # required fields
    for field in ("url", "company", "title"):
        n = sum(1 for j in jobs if not j.get(field))
        (ok if n == 0 else bad)(f"missing {field}: {n}")
        problems += (n > 0)

    # duplicates
    urls = [j.get("url") for j in jobs]
    dups = len(urls) - len(set(urls))
    (ok if dups == 0 else warn)(f"duplicate urls: {dups}")

    # source breakdown -- an empty/one-connector feed can mean a broken connector
    srcs = collections.Counter(j.get("source", "?") for j in jobs)
    ok("sources: " + ", ".join(f"{k} {v}" for k, v in srcs.most_common()))

    return problems


def main():
    if os.name == "nt":
        os.system("")   # enable ANSI colour escapes in the Windows console
    total = sum(check(label, path) for label, path in FEEDS)
    print(f"\n{'='*58}")
    if total == 0:
        print(f"{GREEN}All feeds healthy.{OFF}")
    else:
        print(f"{RED}{total} problem(s) found -- see above.{OFF}")
    sys.exit(1 if total else 0)


if __name__ == "__main__":
    main()
