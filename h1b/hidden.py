"""Company blocklist shared by fetch_jobs.py and report.py.

A hidden company is neither fetched nor shown: fetch_jobs.py drops its board
before pulling, and report.py drops its jobs before rendering the HTML -- so
hiding one takes effect on the page immediately, without a re-fetch.

The list lives in hidden_companies.json next to this file, a plain JSON array
of company names, e.g. ["Google", "Meta"]. Matching is case-insensitive and
exact on the company name, so "google" hides "Google" but not "Google Cloud".
Edit it by hand, or use:  python fetch_jobs.py --hide "Google"
"""
import json
from pathlib import Path

HIDDEN_FILE = Path(__file__).parent / "hidden_companies.json"


def load_hidden():
    """Return the blocklist as a set of casefolded company names."""
    if not HIDDEN_FILE.exists():
        return set()
    try:
        raw = json.loads(HIDDEN_FILE.read_text(encoding="utf-8"))
    except Exception:
        return set()
    return {str(n).strip().casefold() for n in raw if str(n).strip()}


def is_hidden(name, hidden=None):
    """True if `name` is on the blocklist (case-insensitive exact match)."""
    if hidden is None:
        hidden = load_hidden()
    return bool(name) and name.strip().casefold() in hidden


def load_hidden_names():
    """The blocklist as originally written, for display (order preserved)."""
    if not HIDDEN_FILE.exists():
        return []
    try:
        raw = json.loads(HIDDEN_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []
    return [str(n).strip() for n in raw if str(n).strip()]


def _save(names):
    # De-dupe case-insensitively while preserving first-seen spelling/order.
    seen, out = set(), []
    for n in names:
        key = n.casefold()
        if key not in seen:
            seen.add(key)
            out.append(n)
    HIDDEN_FILE.write_text(json.dumps(out, indent=1, ensure_ascii=False),
                           encoding="utf-8")
    return out


def hide(name):
    """Add a company to the blocklist. Returns (names, already_present)."""
    name = name.strip()
    names = load_hidden_names()
    already = any(n.casefold() == name.casefold() for n in names)
    if not already:
        names.append(name)
        _save(names)
    return names, already


def unhide(name):
    """Remove a company from the blocklist. Returns (names, was_present)."""
    name = name.strip()
    names = load_hidden_names()
    kept = [n for n in names if n.casefold() != name.casefold()]
    was_present = len(kept) != len(names)
    if was_present:
        _save(kept)
    return kept, was_present
