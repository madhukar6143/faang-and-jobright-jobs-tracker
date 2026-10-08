"""Render the pulled jobs as a self-contained, filterable HTML page.

No external assets -- data is embedded as JSON, so the file works offline and
can be emailed or opened straight from disk.
"""

import html
import json
from datetime import datetime

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>H-1B Sponsor Jobs &mdash; {generated}</title>
<style>
  :root {{
    --bg:#f6f7f9; --card:#fff; --fg:#16181d; --muted:#666e7a; --line:#e2e5ea;
    --accent:#1f4e79; --chip:#eef2f7; --new:#0a7c42; --newbg:#e6f5ec;
    --intern:#8a5a00; --internbg:#fdf3e0; --unk:#4a5361; --unkbg:#eef0f3;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg:#12141a; --card:#1a1d25; --fg:#e8eaee; --muted:#98a1ae; --line:#2a2f3a;
      --accent:#7fb3e0; --chip:#232833; --new:#5fd39b; --newbg:#12301f;
      --intern:#e0b25f; --internbg:#2e2415; --unk:#aab2be; --unkbg:#232833;
    }}
  }}
  * {{ box-sizing:border-box; }}
  body {{
    margin:0; padding:24px; background:var(--bg); color:var(--fg);
    font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  }}
  .wrap {{ max-width:1400px; margin:0 auto; }}
  h1 {{ font-size:22px; margin:0 0 4px; }}
  .sub {{ color:var(--muted); font-size:13px; margin-bottom:18px; }}
  .stats {{ display:flex; flex-wrap:wrap; gap:10px; margin-bottom:18px; }}
  .stat {{
    background:var(--card); border:1px solid var(--line); border-radius:10px;
    padding:10px 14px; min-width:110px;
  }}
  .stat b {{ display:block; font-size:20px; }}
  .stat span {{ color:var(--muted); font-size:12px; }}
  .controls {{
    display:flex; flex-wrap:wrap; gap:8px; margin-bottom:14px;
    background:var(--card); border:1px solid var(--line);
    border-radius:10px; padding:12px;
  }}
  input,select {{
    font:inherit; padding:7px 10px; border:1px solid var(--line);
    border-radius:7px; background:var(--bg); color:var(--fg);
  }}
  input {{ flex:1; min-width:220px; }}
  .tablewrap {{
    overflow-x:auto; background:var(--card);
    border:1px solid var(--line); border-radius:10px;
  }}
  table {{ border-collapse:collapse; width:100%; font-size:14px; }}
  th {{
    text-align:left; padding:10px 12px; background:var(--chip);
    border-bottom:1px solid var(--line); position:sticky; top:0;
    cursor:pointer; white-space:nowrap; font-size:12px;
    text-transform:uppercase; letter-spacing:.04em; color:var(--muted);
  }}
  th:hover {{ color:var(--fg); }}
  td {{ padding:10px 12px; border-bottom:1px solid var(--line); vertical-align:top; }}
  tr:last-child td {{ border-bottom:0; }}
  a {{ color:var(--accent); text-decoration:none; font-weight:600; }}
  a:hover {{ text-decoration:underline; }}
  .co {{ font-weight:600; white-space:nowrap; }}
  .lvl {{
    display:inline-block; padding:2px 8px; border-radius:20px;
    font-size:11px; font-weight:700; white-space:nowrap;
  }}
  .l-new {{ background:var(--newbg); color:var(--new); }}
  .l-int {{ background:var(--internbg); color:var(--intern); }}
  .l-unk {{ background:var(--unkbg); color:var(--unk); }}
  .muted {{ color:var(--muted); font-size:13px; }}
  .age {{ white-space:nowrap; color:var(--muted); font-size:13px; }}
  .empty {{ padding:40px; text-align:center; color:var(--muted); }}
  .note {{
    margin-top:16px; padding:12px 14px; background:var(--card);
    border:1px solid var(--line); border-left:3px solid var(--accent);
    border-radius:8px; font-size:13px; color:var(--muted);
  }}
  .btn {{
    font:inherit; padding:7px 12px; border:1px solid var(--line);
    border-radius:7px; background:var(--chip); color:var(--fg); cursor:pointer;
  }}
  .btn:hover {{ border-color:var(--accent); color:var(--accent); }}
  th.ck, td.ck {{ width:34px; text-align:center; padding-left:10px; padding-right:4px; }}
  td.ck input {{ width:16px; height:16px; cursor:pointer; accent-color:var(--new); }}
  tr.done td {{ opacity:.45; }}
  tr.done td a {{ text-decoration:line-through; }}
  tr.done td.ck {{ opacity:1; }}
  .risk {{
    margin-top:4px; font-size:11px; font-weight:600; color:var(--intern);
    background:var(--internbg); border-radius:5px; padding:2px 6px;
    display:inline-block; white-space:nowrap;
  }}
  .yoe {{
    margin-top:4px; font-size:11px; font-weight:700; border-radius:5px;
    padding:2px 6px; display:inline-block; white-space:nowrap;
  }}
  .jdbtn {{
    margin-left:8px; font:inherit; font-size:11px; font-weight:600;
    padding:1px 7px; border:1px solid var(--line); border-radius:5px;
    background:var(--chip); color:var(--muted); cursor:pointer;
  }}
  .jdbtn:hover {{ color:var(--accent); border-color:var(--accent); }}
  .hidebtn {{
    margin-top:4px; font:inherit; font-size:11px; font-weight:600;
    padding:1px 7px; border:1px solid var(--line); border-radius:5px;
    background:transparent; color:var(--muted); cursor:pointer; display:block;
  }}
  .hidebtn:hover {{ color:#a11; border-color:#a11; }}
  @media (prefers-color-scheme: dark) {{ .hidebtn:hover {{ color:#f88; border-color:#f88; }} }}
  .hpanel {{
    width:100%; margin-top:8px; padding:12px 14px; background:var(--card);
    border:1px solid var(--line); border-radius:10px; font-size:13px;
  }}
  .hpanel h4 {{ margin:0 0 8px; font-size:12px; text-transform:uppercase;
    letter-spacing:.04em; color:var(--muted); }}
  .htag {{
    display:inline-flex; align-items:center; gap:6px; margin:0 6px 6px 0;
    padding:3px 6px 3px 10px; background:var(--chip); border:1px solid var(--line);
    border-radius:20px; font-weight:600;
  }}
  .htag button {{
    border:0; background:transparent; color:var(--muted); cursor:pointer;
    font-size:15px; line-height:1; padding:0 2px;
  }}
  .htag button:hover {{ color:#a11; }}
  .jdrow td {{ background:var(--chip); }}
  .jd {{
    max-height:340px; overflow-y:auto; white-space:pre-wrap; font-size:12.5px;
    line-height:1.55; color:var(--fg); padding:4px 2px;
  }}
  .jr {{
    font-size:10px; font-weight:700; margin-top:3px; display:inline-block;
    padding:1px 6px; border-radius:4px; background:#efe6ff; color:#6b3fa0;
  }}
  @media (prefers-color-scheme: dark) {{ .jr {{ background:#2a1f3d; color:#c9a9ff; }} }}
  .when {{ font-size:11px; color:var(--new); font-weight:600; margin-top:2px; }}
  .sync {{ font-size:12px; }}
  .sync.ok {{ color:var(--new); font-weight:600; }}
  .sync.off {{ color:var(--muted); }}
  .yoe.good {{ background:var(--newbg); color:var(--new); }}
  .yoe.mid  {{ background:var(--internbg); color:var(--intern); }}
  .yoe.bad  {{ background:#fde2e2; color:#a11; }}
  @media (prefers-color-scheme: dark) {{ .yoe.bad {{ background:#3a1616; color:#f88; }} }}
</style>
</head>
<body>
<div class="wrap">
  <h1>H-1B Sponsor Jobs</h1>
  <div class="sub">Generated {generated} &middot; pulled directly from employer career feeds
   &middot; {boards} employer boards &middot; <span id="sync" class="sync">checking tracker&hellip;</span></div>

  <div class="stats">
    <div class="stat"><b>{n_total}</b><span>total postings</span></div>
    <div class="stat"><b>{n_h1b}</b><span>H-1B tracker</span></div>
    <div class="stat"><b>{n_ext}</b><span>other sources</span></div>
    <div class="stat"><b>{n_entry}</b><span>entry level</span></div>
    <div class="stat"><b>{n_intern}</b><span>internships</span></div>
    <div class="stat"><b>{n_co}</b><span>companies</span></div>
    <div class="stat"><b id="nApplied">0</b><span>marked applied</span></div>
  </div>

  <div class="controls">
    <input id="q" placeholder="Search title, company or location&hellip;" autocomplete="off">
    <select id="src"><option value="">All sources</option>{opt_src}</select>
    <select id="lvl"><option value="">All levels</option>{opt_lvl}</select>
    <select id="cat"><option value="">All roles</option>{opt_cat}</select>
    <select id="co"><option value="">All companies</option>{opt_co}</select>
    <select id="tier"><option value="">All sponsor tiers</option>{opt_tier}</select>
    <select id="app">
      <option value="">Applied &amp; not</option>
      <option value="no">Not applied only</option>
      <option value="yes">Applied only</option>
    </select>
    <select id="risk">
      <option value="">All work-auth</option>
      <option value="clear">Hide US-Person/cleared</option>
      <option value="only">Only US-Person/cleared</option>
    </select>
    <select id="vd">
      <option value="">Any requirement</option>
      <option value="ok">Verified 0-1 yrs</option>
      <option value="okstretch">0-3 yrs</option>
      <option value="unchecked">Body not read yet</option>
    </select>
    <button id="save" class="btn">Download applied.json</button>
    <button id="hbtn" class="btn">Hidden (0)</button>
  </div>

  <div id="hpanel" class="hpanel" style="display:none">
    <h4>Hidden companies &mdash; not shown here, and skipped by the next fetch</h4>
    <select id="hpick" style="margin-bottom:10px"><option value="">Hide a company&hellip;</option></select>
    <div id="hlist"></div>
    <button id="savehidden" class="btn" style="margin-top:8px">Download hidden_companies.json</button>
  </div>

  <div class="tablewrap">
    <table id="t">
      <thead><tr>
        <th class="ck" title="Mark as applied">&#10003;</th>
        <th data-k="company">Company</th>
        <th data-k="title">Role</th>
        <th data-k="level">Level</th>
        <th data-k="category">Category</th>
        <th data-k="location">Location</th>
        <th data-k="age">Posted</th>
      </tr></thead>
      <tbody id="b"></tbody>
    </table>
    <div class="empty" id="none" style="display:none">No roles match those filters.</div>
  </div>

  <div class="note">
    <b>Tick the box</b> when you apply. It's saved in this browser and survives every regenerated
    report. Hit <i>Download applied.json</i> and drop the file next to <code>fetch_jobs.py</code>
    to carry the list into the spreadsheet and across machines.<br><br>
    <b>Level</b> is inferred from the job title. <i>Unlabeled</i> means no seniority marker was
    present &mdash; often open to 0&ndash;2 years, but confirm in the posting.
    Past H-1B sponsorship does not guarantee sponsorship for any specific role: always check the
    posting's work-authorization line.
  </div>
</div>

<script>
const DATA = {data};
const SEEDED = {applied};
const SEEDED_AT = {applied_at};
const $ = id => document.getElementById(id);
let sortKey = "age", sortAsc = true;

/* Applied set persists in localStorage, keyed by apply URL, so it survives
   every regenerated report. SEEDED comes from applied.json on disk. */
const KEY = "h1b_applied_v1";
const BRIDGE = "http://127.0.0.1:8765";
let applied = new Set(SEEDED);
let appliedAt = Object.assign({{}}, SEEDED_AT);
try {{
  const saved = JSON.parse(localStorage.getItem(KEY) || "[]");
  saved.forEach(u => applied.add(u));
}} catch (e) {{}}

function persist() {{
  try {{ localStorage.setItem(KEY, JSON.stringify([...applied])); }} catch (e) {{}}
  $("nApplied").textContent = applied.size;
}}

/* Hidden companies: a whole company you never want to see or fetch again.
   Persists in localStorage (works offline) and, when the bridge is up, is
   mirrored into hidden_companies.json so fetch_jobs.py stops pulling it too. */
const HKEY = "h1b_hidden_v1";
let hiddenCos = new Set();
try {{
  JSON.parse(localStorage.getItem(HKEY) || "[]").forEach(c => hiddenCos.add(c));
}} catch (e) {{}}

const isHidden = co => {{
  const k = (co || "").toLowerCase();
  for (const h of hiddenCos) if (h.toLowerCase() === k) return true;
  return false;
}};

function persistHidden() {{
  try {{ localStorage.setItem(HKEY, JSON.stringify([...hiddenCos])); }} catch (e) {{}}
  renderHiddenPanel();
}}

function renderHiddenPanel() {{
  const names = [...hiddenCos].sort((a, b) => a.toLowerCase().localeCompare(b.toLowerCase()));
  $("hbtn").textContent = `Hidden (${{names.length}})`;
  $("hlist").innerHTML = names.length
    ? names.map(c => `<span class="htag">${{esc(c)}}<button data-c="${{esc(c)}}" title="Unhide">&times;</button></span>`).join("")
    : `<span class="muted">Nothing hidden yet. Use the <b>hide</b> button on any row, or the dropdown above.</span>`;
  $("hlist").querySelectorAll("button[data-c]").forEach(b =>
    b.onclick = () => unhideCo(b.dataset.c));

  // Dropdown of every company still showing, so you can hide one without
  // hunting for its row. Already-hidden companies drop off the list.
  const all = [...new Set(DATA.map(r => r.company))]
    .filter(c => !isHidden(c))
    .sort((a, b) => a.toLowerCase().localeCompare(b.toLowerCase()));
  $("hpick").innerHTML = `<option value="">Hide a company&hellip;</option>`
    + all.map(c => `<option value="${{esc(c)}}">${{esc(c)}}</option>`).join("");
}}

function pushHidden(co, on) {{
  fetch(BRIDGE + "/hidden", {{
    method: "POST", headers: {{"Content-Type": "application/json"}},
    body: JSON.stringify({{company: co, on: on}})
  }}).catch(() => {{}});
}}

function hideCo(co, skipConfirm) {{
  if (!skipConfirm) {{
    const n = DATA.filter(r => r.company.toLowerCase() === co.toLowerCase()).length;
    if (!confirm(`Hide ${{co}}? This removes its ${{n}} posting(s) here and stops the `
               + `next fetch from pulling ${{co}}. Undo it from "Hidden".`)) return;
  }}
  if (!isHidden(co)) hiddenCos.add(co);
  persistHidden();
  pushHidden(co, true);
  render();
}}

function unhideCo(co) {{
  for (const h of [...hiddenCos]) if (h.toLowerCase() === co.toLowerCase()) hiddenCos.delete(h);
  persistHidden();
  pushHidden(co, false);
  render();
}}

$("hbtn").onclick = () => {{
  const p = $("hpanel");
  p.style.display = p.style.display === "none" ? "" : "none";
}};

$("hpick").onchange = () => {{
  const c = $("hpick").value;
  if (c) hideCo(c, true);          // explicit pick -- no confirm needed
  $("hpick").value = "";
}};

$("savehidden").onclick = () => {{
  const blob = new Blob([JSON.stringify([...hiddenCos], null, 1)],
                        {{type: "application/json"}});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "hidden_companies.json";
  a.click();
  URL.revokeObjectURL(a.href);
}};

/* Jobs applied through Tampermonkey land in applied.json, which this page
   can't see once it's already open. Pull them from the bridge if it's up. */
async function syncFromBridge() {{
  try {{
    const r = await fetch(BRIDGE + "/applied/full", {{cache: "no-store"}});
    if (!r.ok) return;
    const entries = await r.json();
    let added = 0;
    entries.forEach(e => {{
      if (e.url) {{
        if (!applied.has(e.url)) added++;
        applied.add(e.url);
        if (e.at) appliedAt[e.url] = e.at.slice(0, 16).replace("T", " ");
      }}
    }});
    $("sync").textContent = "synced with tracker";
    $("sync").className = "sync ok";
    // Fold in whatever hidden_companies.json holds on disk, so a company hidden
    // from another browser or by hand also disappears here.
    try {{
      const hr = await fetch(BRIDGE + "/hidden", {{cache: "no-store"}});
      if (hr.ok) {{
        let changed = false;
        (await hr.json()).forEach(c => {{ if (!isHidden(c)) {{ hiddenCos.add(c); changed = true; }} }});
        if (changed) persistHidden();
      }}
    }} catch (e) {{}}
    if (added) {{ persist(); render(); }} else {{ persist(); render(); }}
  }} catch (e) {{
    $("sync").textContent = "bridge offline - this browser only";
    $("sync").className = "sync off";
  }}
}}

function pushToBridge(url, on) {{
  if (!on) return;                       // bridge only records, never un-marks
  fetch(BRIDGE + "/applied", {{
    method: "POST", headers: {{"Content-Type": "application/json"}},
    body: JSON.stringify({{url: url}})
  }}).catch(() => {{}});
}}

function toggle(url, on) {{
  on ? applied.add(url) : applied.delete(url);
  if (on) appliedAt[url] = new Date().toISOString().slice(0, 16).replace("T", " ");
  persist();
  pushToBridge(url, on);
  render();
}}

$("save").onclick = () => {{
  const blob = new Blob([JSON.stringify([...applied], null, 1)],
                        {{type: "application/json"}});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "applied.json";
  a.click();
  URL.revokeObjectURL(a.href);
}};

const cls = l => l.startsWith("Entry") ? "l-new" : l.startsWith("Intern") ? "l-int" : "l-unk";
const esc = s => (s||"").replace(/[&<>"]/g, c => ({{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}}[c]));

function render() {{
  const q = $("q").value.toLowerCase().trim();
  const f = {{ level:$("lvl").value, category:$("cat").value, company:$("co").value, tier:$("tier").value }};

  const wantApplied = $("app").value;
  const wantRisk = $("risk").value;
  const wantVd = $("vd").value;
  const wantSrc = $("src").value;
  const vdOk = r => wantVd === "ok"        ? r.verdict === "ok"
              : wantVd === "okstretch"     ? (r.verdict === "ok" || r.verdict === "stretch")
              : wantVd === "unchecked"     ? !r.verdict
              : true;
  const srcOk = r => !wantSrc || r.srcgroup === wantSrc;

  let rows = DATA.filter(r =>
    !isHidden(r.company) &&
    vdOk(r) && srcOk(r) &&
    (!wantRisk || (wantRisk === "only" ? !!r.risk : !r.risk)) &&
    (!f.level    || r.level    === f.level) &&
    (!f.category || r.category === f.category) &&
    (!f.company  || r.company  === f.company) &&
    (!f.tier     || r.tier     === f.tier) &&
    (!wantApplied || (wantApplied === "yes") === applied.has(r.url)) &&
    (!q || (r.title+" "+r.company+" "+r.location).toLowerCase().includes(q)));

  rows.sort((a,b) => {{
    let x = sortKey === "age" ? a.hours : a[sortKey],
        y = sortKey === "age" ? b.hours : b[sortKey];
    if (typeof x === "string") {{ x = x.toLowerCase(); y = y.toLowerCase(); }}
    return (x < y ? -1 : x > y ? 1 : 0) * (sortAsc ? 1 : -1);
  }});

  $("b").innerHTML = rows.map(r => {{
    const done = applied.has(r.url);
    return `<tr class="${{done ? "done" : ""}}">
    <td class="ck"><input type="checkbox" data-u="${{esc(r.url)}}" ${{done ? "checked" : ""}}></td>
    <td class="co">${{esc(r.company)}}<div class="muted">${{esc(r.tier)}}</div>
        ${{r.srcgroup && r.srcgroup !== "H-1B tracker" ? `<div class="jr">${{esc(r.srcgroup)}}${{r.match ? " &middot; " + esc(r.match) : ""}}</div>` : ""}}
        ${{done && appliedAt[r.url] ? `<div class="when">applied ${{esc(appliedAt[r.url])}}</div>` : ""}}
        <button class="hidebtn" data-co="${{esc(r.company)}}" title="Hide this company everywhere">hide</button></td>
    <td><a href="${{esc(r.url)}}" target="_blank" rel="noopener">${{esc(r.title)}}</a>
        <button class="jdbtn" data-u="${{esc(r.url)}}">description</button></td>
    <td><span class="lvl ${{cls(r.level)}}">${{esc(r.level.replace(" (possible entry)",""))}}</span>
        ${{r.risk ? `<div class="risk" title="ITAR / clearance roles normally require US citizen or green-card status">&#9888; ${{esc(r.risk)}}</div>` : ""}}
        ${{r.yoe != null ? `<div class="yoe ${{r.verdict === "skip" ? "bad" : r.verdict === "stretch" ? "mid" : "good"}}">${{r.yoe}}+ yrs required</div>` : ""}}
        ${{r.sponsor && r.sponsor !== "not stated" ? `<div class="risk">${{esc(r.sponsor)}}</div>` : ""}}</td>
    <td class="muted">${{esc(r.category)}}</td>
    <td class="muted">${{esc(r.location)}}</td>
    <td class="age">${{esc(r.age)}}</td></tr>`;
  }}).join("");

  $("b").querySelectorAll("input[type=checkbox]").forEach(cb => {{
    cb.onchange = () => toggle(cb.dataset.u, cb.checked);
  }});

  $("b").querySelectorAll(".hidebtn").forEach(btn => {{
    btn.onclick = () => hideCo(btn.dataset.co);
  }});

  $("b").querySelectorAll(".jdbtn").forEach(b => {{
    b.onclick = async () => {{
      const row = b.closest("tr");
      const open = row.nextElementSibling && row.nextElementSibling.classList.contains("jdrow");
      if (open) {{ row.nextElementSibling.remove(); b.textContent = "description"; return; }}
      const tr = document.createElement("tr");
      tr.className = "jdrow";
      tr.innerHTML = `<td colspan="7"><div class="jd">loading&hellip;</div></td>`;
      row.after(tr);
      b.textContent = "hide";
      try {{
        const res = await fetch(BRIDGE + "/jd?url=" + encodeURIComponent(b.dataset.u),
                                {{cache: "no-store"}});
        const d = await res.json();
        tr.querySelector(".jd").textContent = d.text
          ? d.text
          : (d.error || "no description stored for this posting");
      }} catch (e) {{
        tr.querySelector(".jd").innerHTML =
          "Bridge not running. Start it, or read jd_cache.json directly.";
      }}
    }};
  }});

  $("none").style.display = rows.length ? "none" : "block";
}}

document.querySelectorAll("th").forEach(th => th.onclick = () => {{
  const k = th.dataset.k;
  sortAsc = k === sortKey ? !sortAsc : true;
  sortKey = k;
  render();
}});
["q","lvl","cat","co","tier","app","risk","vd","src"].forEach(id => {{
  $(id).addEventListener(id === "q" ? "input" : "change", render);
}});
persist();
renderHiddenPanel();
render();
syncFromBridge();
setInterval(syncFromBridge, 30000);   // pick up Tampermonkey applies live
</script>
</body>
</html>
"""


def _options(rows, key):
    vals = sorted({r[key] for r in rows if r.get(key)})
    return "".join(f'<option value="{html.escape(v)}">{html.escape(v)}</option>' for v in vals)


def write_html(path, rows, boards_scanned, applied=()):
    """rows: the internal dicts from fetch_jobs.pull()."""
    data = [{
        "company": r["Company"],
        "title": r["Job Title"],
        "level": r["Level"],
        "risk": r.get("Work Auth Risk", ""),
        "yoe": r.get("Min YOE"),
        "sponsor": r.get("Sponsorship", ""),
        "verdict": r.get("Verdict", ""),
        "category": r["Role Category"],
        "location": r["Location"] or "—",
        "tier": r["H-1B Sponsor Tier"],
        "age": r["Age"],
        "hours": round(r["_age"], 2) if r["_age"] < 1e9 else 999999,
        "url": r["Apply Link"],
        "source": r.get("Source", ""),
        "srcgroup": r.get("SourceGroup", "H-1B tracker"),
        "match": r.get("Match", ""),
    } for r in rows]

    entry = [d for d in data if not d["level"].startswith("Senior")]
    page = PAGE.format(
        generated=datetime.now().strftime("%Y-%m-%d %H:%M"),
        boards=boards_scanned,
        n_total=len(data),
        n_h1b=sum(1 for d in data if d.get("srcgroup") == "H-1B tracker"),
        n_ext=sum(1 for d in data if d.get("srcgroup", "H-1B tracker") != "H-1B tracker"),
        n_entry=len(entry),
        n_grad=sum(1 for d in data if d["level"].startswith("Entry")),
        n_intern=sum(1 for d in data if d["level"].startswith("Intern")),
        n_co=len({d["company"] for d in data}),
        n_risk=sum(1 for d in data if d["risk"]),
        opt_src=_options(data, "srcgroup"),
        opt_lvl=_options(data, "level"),
        opt_cat=_options(data, "category"),
        opt_co=_options(data, "company"),
        opt_tier=_options(data, "tier"),
        data=json.dumps(data, ensure_ascii=False),
        applied=json.dumps(sorted(applied)),
        applied_at=json.dumps(applied if isinstance(applied, dict) else {}),
    )
    path.write_text(page, encoding="utf-8")
    return path
