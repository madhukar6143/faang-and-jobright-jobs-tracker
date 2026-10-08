"""Localhost bridge between the tracker and the Tampermonkey userscript.

A userscript running on tsenta.com can't read files off your disk, so this
serves the feed over HTTP and accepts applied-marks back.

    python serve.py            # http://127.0.0.1:8765

Endpoints
    GET  /feed.json     jobs from the last fetch_jobs.py run
    GET  /applied.json  URLs already marked applied
    POST /applied       {"url": "..."} or {"urls": [...]}  -> merged into applied.json
    GET  /health        sanity check

Binds to 127.0.0.1 only -- nothing is exposed off this machine.
"""

import json
import subprocess
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock, Thread

HERE = Path(__file__).parent      # D:\track  (root)
H1B = HERE / "h1b"                # H-1B tracker code + data
JR = HERE / "jobright"           # JobRight code + data
FEED = H1B / "feed.json"          # the base H-1B feed
ENRICHED = H1B / "enriched.json"
APPLIED = H1B / "applied.json"
SKIPPED = H1B / "skipped.json"    # blocked at preflight -- never re-offered
JD_CACHE = H1B / "jd_cache.json"  # posting text captured by enrich.py

# Reuse the tracker's blocklist helper so the file format stays in one place.
if str(H1B) not in sys.path:
    sys.path.insert(0, str(H1B))
import hidden as hiddenlib


def extra_source_feeds():
    """Auto-discover any additional source. Convention: a source lives in its
    own folder D:\\track\\<name>\\ and writes <name>_feed.json in the shared feed
    schema (see report.py). Drop in a folder + fetcher and it merges here with
    no edits. The base H-1B feed (h1b/feed.json) is loaded separately."""
    jobs = []
    for p in sorted(HERE.glob("*/*_feed.json")):
        try:
            jobs.extend(json.loads(p.read_text(encoding="utf-8")).get("jobs", []))
        except Exception:
            pass
    return jobs
HOST, PORT = "127.0.0.1", 8765

_lock = Lock()


def read_applied_full():
    """Entries are {"url", "at"}. Tolerates the older flat list of URLs."""
    if not APPLIED.exists():
        return []
    try:
        raw = json.loads(APPLIED.read_text(encoding="utf-8"))
    except Exception:
        return []
    if isinstance(raw, dict):
        raw = raw.get("applied", [])
    out = []
    for item in raw or []:
        if isinstance(item, str):
            out.append({"url": item, "at": "", "company": ""})
        elif isinstance(item, dict) and item.get("url"):
            out.append({"url": item["url"], "at": item.get("at", ""),
                        "company": item.get("company", "")})
    return out


def read_applied():
    return [e["url"] for e in read_applied_full()]


def add_applied(urls, company=""):
    """Merge -- never clobber a list built up over previous sessions.
    Company is stored so per-company cooldowns survive across sessions."""
    with _lock:
        current = read_applied_full()
        known = {e["url"] for e in current}
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        added = [u for u in urls if u and u not in known]
        if added:
            current += [{"url": u, "at": now, "company": company} for u in added]
            APPLIED.write_text(json.dumps(current, indent=1), encoding="utf-8")
        return added, len(current)


# ---------------------------------------------------------- JobRight relay
# When a job is marked applied here (HTML checkbox or POST /applied) and it came
# from JobRight, also mark it applied on JobRight itself -- done server-side with
# the fetcher's SESSION_ID, so the browser's cross-origin (CORS) limits, which
# stop the HTML page from calling jobright.ai directly, don't apply.
RELAY_APPLIED_TO_JOBRIGHT = True
JOBRIGHT_APPLY_ENDPOINT = "https://jobright.ai/swan/job/apply"


def jobright_id_map():
    """Raw apply-URL -> jobright_id, read fresh from the JobRight feed(s).
    The HTML checkbox posts the raw feed URL, so keys match exactly."""
    m = {}
    for p in sorted(HERE.glob("*/*_feed.json")):
        try:
            for j in json.loads(p.read_text(encoding="utf-8")).get("jobs", []):
                if j.get("source") == "jobright" and j.get("url") and j.get("jobright_id"):
                    m[j["url"]] = j["jobright_id"]
        except Exception:
            pass
    return m


def _jobright_session():
    """SESSION_ID + request headers from fetch_jobright.py, imported lazily so
    the server has no hard dependency on it."""
    try:
        if str(JR) not in sys.path:
            sys.path.insert(0, str(JR))
        from fetch_jobright import SESSION_ID, HEADERS
        return SESSION_ID, HEADERS
    except Exception:
        return None, None


def mark_jobright_applied(jobright_id):
    """POST to JobRight's own apply endpoint so the job lands in its Applied tab.
    Same call the userscript makes, but here it rides on the fetcher's cookie."""
    sid, hdrs = _jobright_session()
    if not sid or not jobright_id:
        return False
    try:
        import requests
        s = requests.Session()
        s.headers.update(hdrs or {})
        s.cookies.update({"SESSION_ID": sid})
        r = s.post(JOBRIGHT_APPLY_ENDPOINT,
                   json={"jobId": jobright_id, "source": 0}, timeout=15)
        ok = False
        try:
            ok = r.json().get("result") is True
        except Exception:
            pass
        print(f"  -> JobRight applied {jobright_id}: "
              f"{'ok' if ok else 'unconfirmed'} (HTTP {r.status_code})", flush=True)
        return ok
    except Exception as e:
        print(f"  -> JobRight mark failed for {jobright_id}: "
              f"{type(e).__name__}: {e}", flush=True)
        return False


def read_skipped_full():
    if not SKIPPED.exists():
        return []
    try:
        raw = json.loads(SKIPPED.read_text(encoding="utf-8"))
    except Exception:
        return []
    out = []
    for item in raw or []:
        if isinstance(item, str):
            out.append({"url": item, "at": "", "reason": ""})
        elif isinstance(item, dict) and item.get("url"):
            out.append({"url": item["url"], "at": item.get("at", ""),
                        "reason": item.get("reason", "")})
    return out


def read_skipped():
    return [e["url"] for e in read_skipped_full()]


def add_skipped(url, reason=""):
    """Record a job blocked at preflight so it's never offered again."""
    with _lock:
        current = read_skipped_full()
        if any(e["url"] == url for e in current):
            return False, len(current)
        current.append({"url": url, "reason": reason[:200],
                        "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        SKIPPED.write_text(json.dumps(current, indent=1), encoding="utf-8")
        return True, len(current)


# ---------------------------------------------------------------- refresh
# Runs the same three scripts you'd type by hand, so the whole loop can be
# driven from the Tampermonkey button. Only these fixed commands are ever
# run -- nothing from the request is passed to a shell.
REFRESH = {"running": False, "stage": "", "started": "", "finished": "",
           "ok": None, "log": []}

# (folder, script + args). Each runs in its own folder.
STEPS = [
    (H1B, "fetch_jobs", ["fetch_jobs.py"]),
    (H1B, "enrich",     ["enrich.py", "--yoe", "5", "--max-yoe", "8"]),
    (JR,  "jobright",   ["fetch_jobright.py"]),
    (H1B, "report",     ["report.py"]),
]


def _run_refresh():
    REFRESH.update(running=True, stage="starting", ok=None, log=[],
                   started=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   finished="")
    ok = True
    try:
        for folder, name, argv in STEPS:
            REFRESH["stage"] = name
            print(f"  [refresh] {name} ...", flush=True)
            p = subprocess.run([sys.executable, str(folder / argv[0])] + argv[1:],
                               cwd=str(folder), capture_output=True, text=True,
                               timeout=3600)
            tail = [ln for ln in (p.stdout or "").strip().splitlines()[-6:]]
            REFRESH["log"].append({"step": name, "code": p.returncode,
                                   "tail": tail,
                                   "err": (p.stderr or "").strip()[-300:]})
            print(f"  [refresh] {name} -> exit {p.returncode}", flush=True)
            if p.returncode != 0:
                ok = False
                break
    except Exception as e:
        ok = False
        REFRESH["log"].append({"step": REFRESH["stage"], "code": -1,
                               "tail": [], "err": f"{type(e).__name__}: {e}"})
    finally:
        REFRESH.update(running=False, stage="done", ok=ok,
                       finished=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        print(f"  [refresh] finished ok={ok}", flush=True)


def start_refresh():
    if REFRESH["running"]:
        return False
    Thread(target=_run_refresh, daemon=True).start()
    return True


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._send({"ok": True})

    def _send_html(self, body, code=200):
        raw = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _index(self):
        """Bare root -- a human landing page, so visiting the URL isn't a dead end."""
        feed_ok = FEED.exists()
        n = gen = "-"
        if feed_ok:
            try:
                d = json.loads(FEED.read_text(encoding="utf-8"))
                n, gen = d.get("count", "?"), (d.get("generated") or "")[:16].replace("T", " ")
            except Exception:
                pass
        enr = 0
        if ENRICHED.exists():
            try:
                enr = sum(1 for v in json.loads(
                    ENRICHED.read_text(encoding="utf-8")).values() if v.get("ok"))
            except Exception:
                pass
        rows = "".join(
            f"<tr><td><a href='{p}'>{p}</a></td><td>{desc}</td></tr>"
            for p, desc in [
                ("/jobs", "the combined report -- H-1B + JobRight jobs, with badges"),
                ("/feed.json", "jobs from the last run (what the userscript reads)"),
                ("/applied.json", "URLs you've marked applied"),
                ("/hidden.json", "companies hidden from the report + fetcher"),
                ("/health", "machine-readable status"),
            ])
        state = ("<b style='color:#0a7c42'>ready</b>" if feed_ok else
                 "<b style='color:#a11'>no feed.json &mdash; run fetch_jobs.py</b>")
        return f"""<!doctype html><meta charset=utf-8>
<title>H-1B tracker bridge</title>
<style>
 body{{font:15px/1.6 -apple-system,Segoe UI,Roboto,sans-serif;max-width:640px;
      margin:60px auto;padding:0 20px;color:#16181d}}
 h1{{font-size:20px;margin:0 0 4px}} .s{{color:#666;font-size:13px;margin-bottom:22px}}
 table{{border-collapse:collapse;width:100%;margin-bottom:22px}}
 td{{padding:8px 10px;border-bottom:1px solid #e2e5ea}}
 td:first-child{{font-family:ui-monospace,Consolas,monospace;white-space:nowrap}}
 a{{color:#1f4e79}} code{{background:#f2f4f7;padding:2px 5px;border-radius:4px}}
 @media(prefers-color-scheme:dark){{body{{background:#12141a;color:#e8eaee}}
  td{{border-color:#2a2f3a}} a{{color:#7fb3e0}} code{{background:#232833}}}}
</style>
<h1>H-1B tracker bridge</h1>
<div class=s>Status: {state} &middot; {n} jobs &middot; {enr} with posting bodies read
 &middot; generated {gen}</div>
<table>{rows}</table>
<p>This server only feeds the Tampermonkey script. Leave it running, then click
<b>DRY RUN H-1B feed</b> on the Tsenta browse page.</p>
<p>Refresh the data with <code>python fetch_jobs.py &amp;&amp; python enrich.py</code>
 &mdash; no need to restart this server.</p>"""

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path == "/status":
            return self._send_html(self._index())
        if path in ("/", "/jobs", "/report", "/jobs.html"):
            # The combined H-1B + JobRight report, served same-origin so its
            # live applied-sync works cleanly.
            page = HERE / "jobs.html"
            if not page.exists():
                return self._send_html(self._index())   # fall back to status
            return self._send_html(page.read_text(encoding="utf-8"))
        if path in ("/feed.json", "/feed"):
            if not FEED.exists():
                return self._send({"error": "feed.json missing -- run fetch_jobs.py"}, 404)
            data = json.loads(FEED.read_text(encoding="utf-8"))
            # One server, every source: merge any */*_feed.json (dedup by URL).
            have = {j["url"] for j in data.get("jobs", [])}
            for j in extra_source_feeds():
                if j.get("url") and j["url"] not in have:
                    data["jobs"].append(j)
                    have.add(j["url"])
            data["count"] = len(data["jobs"])
            done = set(read_applied())
            enriched = {}
            if ENRICHED.exists():
                try:
                    enriched = json.loads(ENRICHED.read_text(encoding="utf-8"))
                except Exception:
                    enriched = {}
            blocked = set(read_skipped())
            for j in data.get("jobs", []):
                j["applied"] = j["url"] in done
                j["skipped"] = j["url"] in blocked
                e = enriched.get(j["url"])
                if e and e.get("ok"):
                    j["min_yoe"] = e.get("min_yoe")
                    j["sponsorship"] = e.get("sponsorship")
                    j["verdict"] = e.get("verdict")
            return self._send(data)
        if path in ("/hidden.json", "/hidden"):
            return self._send(hiddenlib.load_hidden_names())
        if path in ("/applied.json", "/applied"):
            return self._send(read_applied())
        if path == "/applied/full":       # URLs + when they were applied
            return self._send(read_applied_full())
        if path in ("/skipped.json", "/skipped"):
            return self._send(read_skipped())
        if path == "/skipped/full":       # URLs + why they were blocked
            return self._send(read_skipped_full())
        if path in ("/refresh/status", "/refresh"):
            return self._send(REFRESH)
        if path == "/jd":
            # /jd?url=... -> the posting text enrich.py captured
            from urllib.parse import parse_qs, urlparse, unquote
            q = parse_qs(urlparse(self.path).query)
            url = unquote((q.get("url") or [""])[0])
            if not url:
                return self._send({"error": "no url given"}, 400)
            if not JD_CACHE.exists():
                return self._send({"error": "jd_cache.json missing -- run enrich.py"}, 404)
            try:
                jd = json.loads(JD_CACHE.read_text(encoding="utf-8"))
            except Exception as e:
                return self._send({"error": f"cache unreadable: {e}"}, 500)
            hit = jd.get(url)
            if not hit:
                return self._send({"error": "not in cache -- posting was "
                                            "unreadable or not yet enriched"}, 404)
            return self._send(hit)
        if path == "/health":
            return self._send({"ok": True, "feed": FEED.exists(),
                               "applied": len(read_applied()),
                               "time": datetime.now(timezone.utc).isoformat()})
        self._send({"error": "not found"}, 404)

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")
        if path == "/refresh":
            started = start_refresh()
            return self._send({"ok": True, "started": started,
                               "already_running": not started})
        if path not in ("/applied", "/skip", "/hidden"):
            return self._send({"error": "not found"}, 404)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            return self._send({"error": f"bad json: {e}"}, 400)

        if path == "/hidden":
            company = (payload.get("company") or "").strip()
            if not company:
                return self._send({"error": "no company given"}, 400)
            on = payload.get("on", True)
            with _lock:
                if on:
                    names, already = hiddenlib.hide(company)
                    print(f"  {'= already hidden' if already else 'x hidden'}: {company}")
                else:
                    names, was = hiddenlib.unhide(company)
                    print(f"  {'+ unhidden' if was else '= not on list'}: {company}")
            return self._send({"ok": True, "hidden": names})

        if path == "/skip":
            url = payload.get("url")
            if not url:
                return self._send({"error": "no url given"}, 400)
            added, total = add_skipped(url, payload.get("reason", ""))
            if added:
                print(f"  x skipped ({total} total): {payload.get('reason','')[:60]}"
                      f"\n      {url[:90]}")
            return self._send({"ok": True, "added": added, "total": total})

        urls = payload.get("urls") or ([payload["url"]] if payload.get("url") else [])
        if not urls:
            return self._send({"error": "no url(s) given"}, 400)
        added, total = add_applied(urls, payload.get("company", ""))
        print(f"  + marked applied ({total} total): "
              + ", ".join(u[:78] for u in added) if added
              else f"  = already known ({total} total)")

        # If any newly-applied URL came from JobRight, mark it applied on
        # JobRight too -- in a background thread so the HTTP reply isn't blocked.
        jr_marked = 0
        if added and RELAY_APPLIED_TO_JOBRIGHT:
            idmap = jobright_id_map()
            jr_ids = [idmap[u] for u in added if u in idmap]
            jr_marked = len(jr_ids)
            if jr_ids:
                Thread(target=lambda ids=jr_ids: [mark_jobright_applied(i) for i in ids],
                       daemon=True).start()

        return self._send({"ok": True, "added": added, "total": total,
                           "jobright_marked": jr_marked})

    def log_message(self, *a):
        pass  # keep the console readable; we print what matters ourselves


class Server(ThreadingHTTPServer):
    # Windows lets a second process bind the same port with SO_REUSEADDR, so an
    # old instance keeps serving stale code while the new one looks fine.
    # Fail loudly instead.
    allow_reuse_address = False
    daemon_threads = True


def main():
    refresh_now = "--refresh" in sys.argv
    try:
        srv = Server((HOST, PORT), Handler)
    except OSError as e:
        sys.exit(f"Port {PORT} is already in use ({e.__class__.__name__}).\n"
                 f"Another serve.py is still running -- stop it first:\n"
                 f'  powershell "Get-CimInstance Win32_Process -Filter \\"Name like \'%python%\'\\" '
                 f"| Where-Object {{ $_.CommandLine -like '*serve.py*' }} | Stop-Process -Force\"")
    print(f"H-1B tracker bridge -> http://{HOST}:{PORT}")
    print(f"  GET  /feed.json     {'ok' if FEED.exists() else 'MISSING - run fetch_jobs.py'}")
    print(f"  GET  /applied.json  {len(read_applied())} marked")
    print(f"  POST /applied       {{'url': '...'}}")
    if refresh_now:
        # Kick off in the background so the bridge answers immediately -- the
        # browser can poll /refresh/status while the fetch runs.
        print("\nFetching new jobs now (~5 min). The bridge is already usable;")
        print("watch progress here or in the browser status box.")
        start_refresh()
    print("\nCtrl+C to stop.\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
