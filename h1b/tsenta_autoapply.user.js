// ==UserScript==
// @name        Tsenta Auto-Apply (H-1B tracker + JobRight, one bridge feed)
// @namespace   h1b-tracker
// @version     3.1-extapply
// @description Reads the ONE bridge feed (H-1B + JobRight merged), judges each job vs your resume, applies via Tsenta (extension-apply with paste-link fallback), marks applied on the bridge + JobRight. Detects unsupported links, logs + skips them. Auto-starts and loops.
// @match       *://*/*
// @grant       GM_xmlhttpRequest
// @grant       GM_setValue
// @grant       GM_getValue
// @grant       GM_deleteValue
// @grant       GM_openInTab
// @connect     127.0.0.1
// @connect     localhost
// @connect     api.groq.com
// @connect     generativelanguage.googleapis.com
// @connect     jobright.ai
// @connect     script.google.com
// @connect     script.googleusercontent.com
// @connect     *
// @run-at      document-idle
// ==/UserScript==

(function () {
  'use strict';

  var VERSION = "3.1-extapply";

  // ===================== CONFIG =====================
  var BRIDGE     = "http://127.0.0.1:8765";   // the ONE server (serves H-1B + JobRight)
  var FEED_URL   = BRIDGE + "/feed.json";
  var APPLIED_URL= BRIDGE + "/applied";

  var PAGE_PATH_HINT = "/dashboard/recommendations";

  var DRY_RUN = false;

  // ---- Auto-run behavior ----
  var AUTO_START = true;
  var AUTO_LOOP  = true;
  var LOOP_COOLDOWN_SEC = 90;

  var MAX_AGE_HOURS = 24;
  var MAX_ADDS_PER_RUN = 10;

  var DELAY_SEC = 30;
  var HUMAN_BREAKS = true;

  var MAX_PER_COMPANY_PER_RUN = 2;
  var DAILY_LIMIT_PER_COMPANY = 3;
  var COMPANY_LIMITS = {};

  function dailyCapFor(co){
    return COMPANY_LIMITS.hasOwnProperty(co) ? COMPANY_LIMITS[co]
                                             : DAILY_LIMIT_PER_COMPANY;
  }

  var ENABLE_WAIT_MS = 8000;
  var SEEN_IDS_CAP = 2000;

  var ALLOW_UNLABELED = true;
  var ALLOW_INTERNSHIPS = true;
  var ALLOW_SENIOR = true;

  var SKIP_US_PERSON_ONLY = true;
  var SKIP_VERDICT_SKIP = true;
  var ALLOW_STRETCH = true;

  var PREFLIGHT_CHECK = true;
  var BLOCK_ON_NO_SPONSORSHIP = false;
  var BLOCK_ON_FUTURE_BAR = false;
  var BLOCK_WHEN_UNREADABLE = true;

  // ---- JobRight routing (source==="jobright" jobs use THIS logic, not the LLM) ----
  // JobRight jobs are decided by JobRight's own match score, like the old script:
  //   score >  SCORE_THRESHOLD  -> apply  (+ mark applied on JobRight)
  //   score <= SCORE_THRESHOLD  -> mark "not interested" on JobRight (don't apply)
  // Senior/management JobRight titles are also marked "not interested".
  var JOBRIGHT_SCORE_THRESHOLD = 60;
  var MARK_JOBRIGHT_APPLIED  = true;                      // POST applied to JobRight
  var MARK_JOBRIGHT_REJECTED = true;                      // POST "not interested" to JobRight
  var REJECT_FEEDBACK_CODE   = 13;                        // 13 = not interested
  var MAX_REJECTS_PER_RUN    = 60;
  var REJECT_GAP_SEC         = [5, 8];                    // gap between reject calls
  var JOBRIGHT_APPLY_URL     = "https://jobright.ai/swan/job/apply";
  var JOBRIGHT_IGNORE_URL    = "https://jobright.ai/swan/job/ignore";
  // Tsenta can't paste some external links ("couldn't detect a supported
  // application system"). Those get skipped and logged to this sheet for manual
  // apply. Leave blank to skip logging. Applies to ALL jobs, not just JobRight.
  var UNSUPPORTED_LOG_WEBHOOK = "";
  var UNSUPPORTED_RE = /couldn't detect a supported application system/i;

  // ---- Extension-apply: open the job in a tab, let the Tsenta browser-extension
  //      detect + scrape it (so the JD is saved), click its "Apply", close the
  //      tab. If the extension doesn't detect it / times out, fall back to the
  //      paste-link flow. USE_EXTENSION_APPLY=false restores paste-only. ----
  // NOTE: this mode relies on the Tsenta extension's "Auto-add jobs" = ON
  // ("Add supported postings the moment you open them") and "Review before
  // submit" = Off. Then merely OPENING a supported job auto-adds + applies it --
  // no Apply button is pressed. We just open the tab, wait, and close.
  var USE_EXTENSION_APPLY  = true;
  var EXT_MARKER           = "tsauto";   // URL-hash tag marking a controller-opened tab
  var EXT_TAB_ACTIVE       = true;       // true = focus each opened tab so you can WATCH it.
                                         // Switch to false (background tabs, no focus stealing)
                                         // once you're happy it works.
  var EXT_CLOSE_TIMER_MS   = 20000;      // <-- HOW LONG to keep the job tab open (detected or not)
                                         // before closing. Gives the extension time to auto-add.
  var EXT_TAB_TIMEOUT_MS   = 35000;      // controller's hard give-up per tab -> paste fallback.
                                         // Keep it > EXT_CLOSE_TIMER_MS + ~10s.
  var EXT_GAP_SEC          = [3, 6];     // short gap between jobs in extension mode (the tab's open
                                         // time already spaces requests out).

  var RESUME = "YOUR_RESUME_SUMMARY_HERE -- a short plain-text summary of your skills, stack and years of experience for the LLM screener.";

  var FIT_CUTOFF = 50;
  var MAX_YOE = 8;
  var USE_LLM_SECOND_OPINION = true;

  var LLM_POOL = [
    { provider: "groq",   key: "YOUR_GROQ_API_KEY", url: "https://api.groq.com/openai/v1/chat/completions", model: "llama-3.1-8b-instant" },
    { provider: "groq",   key: "YOUR_GROQ_API_KEY", url: "https://api.groq.com/openai/v1/chat/completions", model: "llama-3.1-8b-instant" },
    { provider: "groq",   key: "YOUR_GROQ_API_KEY", url: "https://api.groq.com/openai/v1/chat/completions", model: "llama-3.1-8b-instant" },
    { provider: "gemini", key: "YOUR_GEMINI_API_KEY", url: "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions", model: "gemini-3.1-flash-lite" },
    { provider: "gemini", key: "YOUR_GEMINI_API_KEY", url: "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions", model: "gemini-3.1-flash-lite" }
  ];

  // ===================== WORK-AUTH (phrase-based) =====================
  var WA_SUBJECT  = /(?:security|top[\s-]secret|ts\/sci|government|dod|public\s+trust)\s+clearance|\bclearance\b|\bpolygraph\b|u\.?s\.?\s+citizen(?:ship|s)?|u\.?s\.?\s+persons?|\bitar\b|export[\s-]control(?:s|led)?|export\s+administration|permanent\s+resident|green\s+card/gi;
  var WA_REQUIRES = /\b(?:must|required|require[sd]?|shall|mandatory|restricted\s+to|limited\s+to)\b|\bneed\s+to\s+(?:be|have|possess|hold)\b|\bonly\s+u\.?s\.?|\beligibility\s+requirement/i;
  var WA_SOFTENS  = /\b(?:preferred|preferrable|a\s+plus|nice[\s-]to[\s-]have|desirable|desired|beneficial|advantage|bonus|ideally|not\s+required|no\s+clearance)\b|\bwilling(?:ness)?\s+(?:to|and)|\b(?:ability|able|eligible)\s+to\s+obtain|\b(?:customers?|clients?|agencies|partners?|personnel|teams?|colleagues)\s+(?:who|that|with|requiring|holding|hold|need)|\bmay\s+(?:require|involve|need|be\s+subject)|\b(?:standards?|certifications?|frameworks?|regulations?|controls?)\s+(?:like|such\s+as|including|e\.g\.)|\b(?:compliant|compliance|certified|accredited)\s+with|employee\s+polygraph\s+protection|polygraph\s+protection\s+act|know\s+your\s+rights|\beppa\b/i;
  var WA_HARD     = /\bitar\b|export[\s-]control(?:s|led)?|ts\/sci|\bpolygraph\b/i;
  var WA_OVERRIDE = /\b(?:must|required\s+to|need\s+to)\s+(?:be\s+)?(?:able|eligible|willing|capable)\s+to\s+(?:obtain|acquire|secure|get)|\bmust\s+(?:be\s+)?(?:able\s+to\s+)?(?:obtain|maintain)\b/i;
  var WA_WINDOW = 130;

  function workAuthBlock(text){
    if (!text) return null;
    WA_SUBJECT.lastIndex = 0;
    var m;
    while ((m = WA_SUBJECT.exec(text)) !== null){
      var lo = Math.max(0, m.index - WA_WINDOW);
      var win = text.slice(lo, m.index + m[0].length + WA_WINDOW);
      if (WA_OVERRIDE.test(win)) return win.trim().slice(0, 200);
      if (WA_SOFTENS.test(win)) continue;
      if (WA_HARD.test(win) || WA_REQUIRES.test(win)) return win.trim().slice(0, 200);
    }
    return null;
  }

  var MANAGEMENT_RE  = /\b(manager|managing|director|chief|president|partner|lead|leads|principal)\b|\bhead\s+of\b|\bvp\b|vice\s+president/i;
  var SENIOR_IC_RE   = /\b(senior|sr\.?|staff|architect|distinguished|fellow)\b/i;
  var TITLE_BLOCK_RE = /(technician|trader|coordinator|repair|installation|help\s?desk|\bsales\b|driver|geologist|postdoc|recruiter)/i;

  var CARD_TEXT_RE = /add your own link/i;
  var URL_INPUT_SELECTOR = 'input[placeholder^="https://boards.greenhouse.io"]';
  var SUBMIT_TEXT_RE = /apply with optimized resume/i;
  var MODAL_SELECTOR = '[id^="headlessui-dialog-panel"]';

  var LOG = "[TSENTA]";
  function log(){ var a=[].slice.call(arguments); a.unshift(LOG,new Date().toLocaleTimeString()); console.log.apply(console,a); }
  function warn(){ var a=[].slice.call(arguments); a.unshift(LOG,new Date().toLocaleTimeString()); console.warn.apply(console,a); }

  // ============================================================
  //  EXTENSION-APPLY  (controller on tsenta.com  +  worker in each job tab)
  //  Open the job in a background tab, let the Tsenta browser-extension detect +
  //  scrape it (JD gets saved), click its Apply, then close the tab. If the
  //  extension doesn't detect it / times out, fall back to the paste-link flow.
  //  One script, two roles, gated by URL below.
  // ============================================================
  function isTsentaHost(){ return /(^|\.)tsenta\.com$/i.test(location.hostname); }

  // Search open shadow roots too -- the extension panel lives inside one.
  function deepFind(root, test){
    var els = (root || document).querySelectorAll("*");
    for (var i=0;i<els.length;i++){
      var el = els[i];
      if (test(el)) return el;
      if (el.shadowRoot){ var r = deepFind(el.shadowRoot, test); if (r) return r; }
    }
    return null;
  }
  function findTsentaPanel(){
    return deepFind(document, function(el){ return el.classList && el.classList.contains("tsenta-rail"); });
  }
  function findApplyBtn(){
    return deepFind(document, function(el){
      if (el.tagName !== "BUTTON") return false;
      var t = (el.textContent || "").trim();
      return /^apply\b/i.test(t) && !/dashboard/i.test(t);
    });
  }
  function panelSays(re){ var p = findTsentaPanel(); return p ? re.test(p.textContent || "") : false; }

  // WORKER: this tab was opened by the controller. With the extension's
  // "Auto-add jobs" ON, merely opening a supported job auto-adds + applies it --
  // no button to press. So keep the tab open for EXT_CLOSE_TIMER_MS (letting the
  // auto-add finish + so you can watch), noting whether the Tsenta panel ever
  // appeared (= supported), then report via shared GM storage:
  //   detected  -> "added"       (controller marks applied)
  //   already   -> "already"     (was already in Tsenta)
  //   never seen -> "notdetected" (controller falls back to paste-link)
  async function runExtensionWorker(){
    var token = (location.hash.split(EXT_MARKER + "=")[1] || "").split("&")[0];
    function report(status){ try { GM_setValue("tsauto_" + token, JSON.stringify({status:status, at:Date.now()})); } catch(e){} }
    log("worker: opened; watching " + Math.round(EXT_CLOSE_TIMER_MS/1000) + "s for Tsenta auto-add...");
    var deadline = Date.now() + EXT_CLOSE_TIMER_MS;
    var detected = false, already = false;
    while (Date.now() < deadline){
      if (!detected && findTsentaPanel()){ detected = true; log("worker: Tsenta detected -> auto-adding"); }
      if (detected && !already && panelSays(/already in tsenta/i)){ already = true; log("worker: already in tsenta"); }
      await sleep(400);
    }
    log("worker: " + (detected ? "done -> " + (already ? "already" : "added") : "no Tsenta panel -> not supported (will paste)"));
    report(detected ? (already ? "already" : "added") : "notdetected");
  }

  // CONTROLLER helper: open the job in a background tab, wait for the worker's
  // result, close the tab. Resolves "ok" (applied/already) or "unsupported"
  // (not detected / timed out) so run() can fall back to paste-link.
  function extApplyInTab(job){
    return new Promise(function(resolve){
      var token = "t" + Date.now() + "_" + Math.floor(Math.random() * 1e6);
      GM_setValue("tsauto_" + token, "");
      var sep = job.url.indexOf("#") === -1 ? "#" : "&";
      var tab;
      // setParent:true makes THIS (controller) tab the parent, so when the job
      // tab closes the browser returns focus here instead of a random tab.
      try { tab = GM_openInTab(job.url + sep + EXT_MARKER + "=" + token, {active: EXT_TAB_ACTIVE, insert: true, setParent: true}); }
      catch(e){ return resolve("unsupported"); }
      var deadline = Date.now() + EXT_TAB_TIMEOUT_MS;
      (function poll(){
        var raw = GM_getValue("tsauto_" + token, "");
        if (raw){
          var st = "fail"; try { st = JSON.parse(raw).status; } catch(e){}
          try { GM_deleteValue("tsauto_" + token); } catch(e){}
          try { if (tab && tab.close) tab.close(); } catch(e){}
          log("  extApply:", (job.company||"") + " - " + (job.title||""), "->", st);
          return resolve((st === "added" || st === "applied" || st === "already") ? "ok" : "unsupported");
        }
        if (Date.now() > deadline){
          try { if (tab && tab.close) tab.close(); } catch(e){}
          log("  extApply: TIMEOUT ->", job.title);
          return resolve("unsupported");
        }
        setTimeout(poll, 500);
      })();
    });
  }

  // ---- ROLE GATE ----
  if (!isTsentaHost() && location.hash.indexOf(EXT_MARKER) !== -1){
    runExtensionWorker();   // opened job tab: press Apply, report, done
    return;
  }
  if (!isTsentaHost()) return;   // inert on every other page

  log("Loaded", VERSION, "| path:", location.pathname, "| DRY_RUN:", DRY_RUN,
      "| bridge:", BRIDGE, "| extApply:", USE_EXTENSION_APPLY, "| AUTO_START:", AUTO_START);

  function normalizeUrl(url) {
    if (!url) return "";
    try {
      var parsed = new URL(url);
      parsed.searchParams.delete("gh_jid");
      parsed.searchParams.delete("utm_source");
      parsed.searchParams.delete("utm_medium");
      parsed.searchParams.delete("utm_campaign");
      var clean = parsed.origin + parsed.pathname;
      return clean.endsWith("/") ? clean.slice(0, -1) : clean;
    } catch (e) {
      return url.split('?')[0].replace(/\/+$/, "");
    }
  }

  function getSeen(){ try { return new Set(JSON.parse(GM_getValue("h1b_seen","[]"))); } catch(e){ return new Set(); } }
  function saveSeen(set){ var a=Array.from(set); if(a.length>SEEN_IDS_CAP) a=a.slice(a.length-SEEN_IDS_CAP); GM_setValue("h1b_seen",JSON.stringify(a)); }

  window.h1bReset  = function(){ GM_setValue("h1b_seen","[]"); log("Local seen-set cleared."); say("Local seen-set cleared."); };
  window.h1bStop   = function(){ AUTO_LOOP = false; running = false; say("Auto-loop stopped for this tab."); log("Auto-loop stopped by user."); };
  window.h1bStatus = function(){ var s=getSeen(); return { seen:s.size, page:location.pathname, bridge:BRIDGE, dryRun:DRY_RUN, extApply:USE_EXTENSION_APPLY }; };
  window.h1bDiag = function(){
    console.log("%c[TSENTA] ===== DIAGNOSTIC DUMP =====","color:#065f46;font-weight:bold");
    document.querySelectorAll('input,textarea').forEach(function(el,i){
      console.log(i, el.tagName, JSON.stringify({type:el.type,placeholder:el.placeholder,name:el.name,aria:el.getAttribute('aria-label'),cls:el.className}), el.outerHTML.slice(0,220));
    });
    document.querySelectorAll('button,[role=button]').forEach(function(el,i){
      var t=(el.innerText||el.getAttribute('aria-label')||'').trim(); if(t) console.log(i, JSON.stringify(t.slice(0,40)), "disabled="+el.disabled, el.outerHTML.slice(0,150));
    });
    console.log("%c[TSENTA] ===== END DUMP =====","color:#065f46;font-weight:bold");
  };

  function esc(s){ return String(s||"").replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
  function hostOf(u){ try { return new URL(u).hostname.replace(/^www\./,""); } catch(e){ return ""; } }

  function mkBtn(text, bottom, bg){
    var b = document.createElement("button");
    b.textContent = text;
    b.style.cssText = "position:fixed;left:24px;bottom:"+bottom+"px;z-index:999999;background:"+bg+";color:#fff;border:none;padding:12px 16px;border-radius:10px;font-size:13px;font-weight:700;cursor:pointer;box-shadow:0 6px 20px rgba(0,0,0,.3);";
    document.body.appendChild(b); return b;
  }
  var reviewBtn = mkBtn("Review JobRight jobs", 168, "#7c3aed");
  var btn       = mkBtn(DRY_RUN ? "DRY RUN feed" : "Apply from feed", 120, "#065f46");

  var status = document.createElement("div");
  status.style.cssText = "position:fixed;bottom:216px;left:24px;z-index:999999;background:#111827;color:#fff;padding:10px 14px;border-radius:8px;font-size:13px;max-width:420px;display:none;line-height:1.5;";
  document.body.appendChild(status);
  function say(t){ status.style.display="block"; status.innerHTML=t; }

  function sleep(ms){ return new Promise(r=>setTimeout(r,ms)); }
  function randomBetween(a,b){ return Math.floor(Math.random()*(b-a+1))+a; }

  function nextGapSecs(n){
    var s = randomBetween(DELAY_SEC, Math.round(DELAY_SEC * 1.5));
    if (HUMAN_BREAKS && n > 0 && n % randomBetween(4, 6) === 0) s += DELAY_SEC * 2;
    return s;
  }
  async function countdownGap(secs, tail){
    var isBreak = secs > DELAY_SEC * 2;
    for (var left = secs; left > 0; left--){
      say((isBreak ? "&#9749; Longer break " : "Waiting ") + left + "s<br><span style='opacity:.7'>" + tail + "</span>");
      await sleep(1000);
    }
  }

  function gmGet(url){
    return new Promise(function(resolve, reject){
      GM_xmlhttpRequest({ method:"GET", url:url, timeout:15000,
        onload:function(r){
          if (r.status < 200 || r.status >= 300) return reject(new Error("HTTP "+r.status));
          try { resolve(JSON.parse(r.responseText)); } catch(e){ reject(e); }
        },
        onerror:function(){ reject(new Error("network")); },
        ontimeout:function(){ reject(new Error("timeout")); } });
    });
  }

  function markApplied(url, company){
    var cleanUrl = normalizeUrl(url);
    return new Promise(function(resolve){
      GM_xmlhttpRequest({
        method:"POST", url:APPLIED_URL, timeout:10000,
        headers:{ "Content-Type":"application/json" },
        data: JSON.stringify({ url: cleanUrl, company: company || "" }),
        onload:function(r){ log("  marked applied on bridge ->", r.status); resolve(true); },
        onerror:function(){ warn("  could not reach bridge to mark applied"); resolve(false); },
        ontimeout:function(){ warn("  bridge timeout marking applied"); resolve(false); }
      });
    });
  }

  function markSkipped(url, reason){
    return new Promise(function(resolve){
      GM_xmlhttpRequest({
        method:"POST", url:BRIDGE + "/skip", timeout:10000,
        headers:{ "Content-Type":"application/json" },
        data: JSON.stringify({ url: normalizeUrl(url), reason: reason }),
        onload:function(){ resolve(true); }, onerror:function(){ resolve(false); }, ontimeout:function(){ resolve(false); }
      });
    });
  }

  // Tell JobRight you applied (only for source==="jobright" jobs with an id).
  function markJobrightApplied(jobId){
    return new Promise(function(resolve){
      if (!MARK_JOBRIGHT_APPLIED || !jobId) return resolve(false);
      GM_xmlhttpRequest({
        method:"POST", url:JOBRIGHT_APPLY_URL, timeout:15000,
        headers:{ "Content-Type":"application/json", "Accept":"application/json", "x-client-type":"web" },
        data: JSON.stringify({ jobId: jobId, source: 0 }),
        onload:function(r){ var ok=false; try{ ok=JSON.parse(r.responseText).result===true; }catch(e){}
          log("  JobRight marked applied:", jobId, ok?"ok":"(unconfirmed)"); resolve(ok); },
        onerror:function(){ warn("  JobRight mark failed"); resolve(false); },
        ontimeout:function(){ resolve(false); }
      });
    });
  }

  // Tell JobRight you're not interested (removes it from your recommendations).
  function markJobrightIgnored(jobId){
    return new Promise(function(resolve){
      if (!MARK_JOBRIGHT_REJECTED || !jobId) return resolve(false);
      GM_xmlhttpRequest({
        method:"POST", url:JOBRIGHT_IGNORE_URL, timeout:15000,
        headers:{ "Content-Type":"application/json", "Accept":"application/json", "x-client-type":"web" },
        data: JSON.stringify({ jobId: jobId, feedback: REJECT_FEEDBACK_CODE }),
        onload:function(r){ var ok=false; try{ var j=JSON.parse(r.responseText); ok=j.result===true||j.success===true; }catch(e){} resolve(ok); },
        onerror:function(){ resolve(false); }, ontimeout:function(){ resolve(false); }
      });
    });
  }

  // Log an unsupported-link job to your sheet so you can apply it manually.
  // Sends both url and jobLink so either sheet schema fills the link column.
  function logUnsupported(job, reason){
    if (!UNSUPPORTED_LOG_WEBHOOK) return;
    GM_xmlhttpRequest({ method:"POST", url:UNSUPPORTED_LOG_WEBHOOK, timeout:10000,
      headers:{ "Content-Type":"application/json" },
      data: JSON.stringify({
        company: job.company || "", title: job.title || "",
        score: job.jobright_score != null ? job.jobright_score : "",
        status: "unsupported", url: job.url || "", jobLink: job.url || "",
        reason: reason || "Tsenta unsupported link -- apply manually" }),
      onload:function(){}, onerror:function(){} });
  }

  function fetchableUrl(url){
    var m = url.match(/^https:\/\/([^.]+)\.(wd\d+)\.myworkdayjobs\.com\/en-US\/([^\/]+)(\/.*)$/);
    if (m) return "https://"+m[1]+"."+m[2]+".myworkdayjobs.com/wday/cxs/"+m[1]+"/"+m[3]+m[4];
    return url;
  }
  function stripHtml(s){
    return (s||"").replace(/<(script|style)[^>]*>[\s\S]*?<\/\1>/gi," ").replace(/<[^>]+>/g," ")
      .replace(/&nbsp;/gi," ").replace(/&amp;/gi,"&").replace(/&lt;/gi,"<").replace(/&gt;/gi,">")
      .replace(/&quot;/gi,'"').replace(/\s+/g," ").trim();
  }
  function fetchPosting(url){
    return new Promise(function(resolve){
      GM_xmlhttpRequest({ method:"GET", url:fetchableUrl(url), timeout:25000,
        headers:{"Accept":"text/html,application/json,*/*"},
        onload:function(r){ (r.status<200||r.status>=300) ? resolve("") : resolve(stripHtml(r.responseText)); },
        onerror:function(){ resolve(""); }, ontimeout:function(){ resolve(""); } });
    });
  }
  function cachedPosting(url){
    return new Promise(function(resolve){
      GM_xmlhttpRequest({ method:"GET", url: BRIDGE + "/jd?url=" + encodeURIComponent(url), timeout:10000,
        onload:function(r){ if (r.status!==200) return resolve(""); try{ resolve(JSON.parse(r.responseText).text||""); }catch(e){ resolve(""); } },
        onerror:function(){ resolve(""); }, ontimeout:function(){ resolve(""); } });
    });
  }

  var llmIdx = 0;
  function extractJSON(s){
    if (!s) return null;
    try { return JSON.parse(s); } catch(e){}
    var clean = s.replace(/```json/gi,"").replace(/```/g,"").replace(/[\u201C\u201D]/g,'"').trim();
    try { return JSON.parse(clean); } catch(e){}
    var m = clean.match(/\{[\s\S]*\}/);
    if (m){ try { return JSON.parse(m[0]); } catch(e){ try { return JSON.parse(m[0].replace(/,\s*([\}\]])/g,"$1")); } catch(e2){} } }
    return null;
  }
  function parseYears(raw){
    if (typeof raw === "number" && isFinite(raw)) return Math.round(raw);
    if (typeof raw === "string"){ var m = raw.match(/\d+/); if (m) return parseInt(m[0],10); }
    return NaN;
  }
  function askLLM(text, title, attempt, tries){
    attempt = attempt || 0; tries = tries || 0;
    return new Promise(function(resolve){
      var pool = LLM_POOL.filter(function(p){ return p.key && !/REPLACE_ME/.test(p.key); });
      if (!pool.length || attempt >= pool.length) return resolve(null);
      var poolIdx = (llmIdx + attempt) % pool.length;
      var t = pool[poolIdx];
      var maxChars = t.provider === "groq" ? 10000 : 12000;
      var sys = "You are an automated job screener. You MUST respond with ONLY valid JSON and no extra conversational text.";
      var usr = "Screen this job for the user.\n\nRULES:\n"
        + "1. citizenshipOrClearance='required' ONLY if the job EXPLICITLY demands US citizen / US person / security clearance.\n"
        + "2. yearsRequired = MINIMUM years explicitly required, plain integer ('3-5 years'=>3, '6+'=>6). If not stated, null.\n"
        + "3. Ignore visa sponsorship entirely.\n"
        + "4. fit (0-100) by resume alignment with required skills AND domain.\n"
        + "5. Return ONLY:\n"
        + '{"decision":"APPLY or PASS","fit":80,"reason":"...","yearsRequired":3,"citizenshipOrClearance":"required or not stated"}\n\n'
        + "RESUME:\n" + RESUME + "\n\nJOB DESCRIPTION:\n" + text.substring(0, maxChars);
      GM_xmlhttpRequest({
        method:"POST", url:t.url, timeout:25000,
        headers:{"Content-Type":"application/json","Authorization":"Bearer "+t.key},
        data: JSON.stringify({model:t.model, temperature:0, max_tokens:300, response_format:{type:"json_object"},
          messages:[{role:"system",content:sys},{role:"user",content:usr}]}),
        onload:async function(r){
          if (r.status === 429) return resolve(await askLLM(text, title, attempt + 1, tries));
          if (r.status < 200 || r.status >= 300) return resolve(await askLLM(text, title, attempt + 1, 0));
          try{
            var c = JSON.parse(r.responseText).choices[0].message.content;
            var raw = extractJSON(c) || {};
            var n = {}; for (var k in raw) n[k.toLowerCase()] = raw[k];
            llmIdx = poolIdx;
            resolve({
              fit: typeof n.fit === "number" ? n.fit : (parseInt(n.fit,10) || 0),
              years: parseYears(n.yearsrequired),
              reason: String(n.reason||"").trim(),
              decision: String(n.decision||"").toUpperCase().trim(),
              via: t.provider
            });
          }catch(e){ resolve(await askLLM(text, title, attempt + 1, 0)); }
        },
        onerror:async function(){ resolve(await askLLM(text, title, attempt + 1, 0)); },
        ontimeout:async function(){ resolve(await askLLM(text, title, attempt + 1, 0)); }
      });
    });
  }

  async function judge(job){
    if (!PREFLIGHT_CHECK) return {ok:true, decision:"APPLY", fit:null, reason:"checks off"};

    var t = (job.title || "").replace(/[_\-\/()\[\]]+/g, " ");
    if (MANAGEMENT_RE.test(t)) return {ok:false, decision:"PASS", fit:0, reason:"lead/manager/principal title"};
    if (!ALLOW_SENIOR && SENIOR_IC_RE.test(t)) return {ok:false, decision:"PASS", fit:0, reason:"senior title"};
    if (TITLE_BLOCK_RE.test(t)) return {ok:false, decision:"PASS", fit:0, reason:"non-CS title"};

    var text = await cachedPosting(job.url);
    if (text.length < 300) text = await fetchPosting(job.url);
    if (text.length < 300){
      return BLOCK_WHEN_UNREADABLE
        ? {ok:false, decision:"PASS", fit:0, reason:"posting unreadable - nothing to judge"}
        : {ok:true, decision:"APPLY", fit:null, reason:"posting unreadable - NOT verified"};
    }

    var hit = workAuthBlock(text);
    if (hit) return {ok:false, decision:"PASS", fit:0, reason:"work-auth requirement"};

    if (!USE_LLM_SECOND_OPINION) return {ok:true, decision:"APPLY", fit:null, reason:"passed deterministic checks"};

    var v = await askLLM(text, job.title || "");
    if (!v) return {ok:false, decision:"DEFER", fit:null, reason:"all LLM keys unavailable"};

    var reason = v.reason;
    if (!hit && /clearance|citizen|us person/i.test(reason)) reason = "";  // strip hallucinated
    if (!isNaN(v.years) && v.years > MAX_YOE)
      return {ok:false, decision:"PASS", fit:v.fit, reason:"requires " + v.years + "+ yrs"};
    if (v.fit <= FIT_CUTOFF)
      return {ok:false, decision:"PASS", fit:v.fit, reason:"fit " + v.fit + "% below cutoff" + (reason?" - "+reason:"")};
    if (/\b(lacks?|lacking|missing|does not have|doesn't have|no explicit|not aligned|but lacks)\b/i.test(reason))
      return {ok:false, decision:"PASS", fit:v.fit, reason:"reason contradicts score (" + v.fit + "%)"};
    return {ok:true, decision:"APPLY", fit:v.fit, reason: reason || ("fit " + v.fit + "%")};
  }

  function isWorthApplying(job, doneSet, seen){
    if (!job || !job.url) return false;
    var normUrl = normalizeUrl(job.url);
    if (job.applied) return false;
    if (job.skipped) return false;
    if (doneSet.has(normUrl)) return false;
    if (seen.has(normUrl)) return false;

    if (SKIP_US_PERSON_ONLY && job.work_auth_risk) return false;
    if (SKIP_VERDICT_SKIP && job.verdict === "skip") return false;
    if (!ALLOW_STRETCH && job.verdict === "stretch") return false;

    var lvl = job.level || "";
    if (/^Management/i.test(lvl)) return false;
    if (!ALLOW_SENIOR && /^Senior/i.test(lvl)) return false;
    if (!ALLOW_INTERNSHIPS && /^Intern/i.test(lvl)) return false;
    if (!ALLOW_UNLABELED && /^Unlabeled/i.test(lvl)) return false;

    var t = (job.title || "").replace(/[_\-\/()\[\]]+/g, " ");
    if (MANAGEMENT_RE.test(t)) return false;
    if (!ALLOW_SENIOR && SENIOR_IC_RE.test(t)) return false;
    if (TITLE_BLOCK_RE.test(t)) return false;

    if (MAX_AGE_HOURS > 0 && job.age_hours != null && job.age_hours > MAX_AGE_HOURS) return false;
    return true;
  }

  function setReactInputValue(input, value){
    var proto = Object.getPrototypeOf(input);
    var desc = Object.getOwnPropertyDescriptor(proto, "value") || Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value");
    if (desc && desc.set) desc.set.call(input, value); else input.value = value;
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }
  function findByText(selector, re){ return Array.from(document.querySelectorAll(selector)).find(el => re.test((el.innerText||el.textContent||"").trim())); }
  function getModal(){ return document.querySelector(MODAL_SELECTOR); }
  function getUrlInput(){ return document.querySelector(URL_INPUT_SELECTOR); }
  function getSubmitBtn(){ var s=getModal()||document; return Array.from(s.querySelectorAll('button')).find(b => SUBMIT_TEXT_RE.test((b.innerText||"").trim())); }
  function getCloseBtn(){ var s=getModal()||document; return s.querySelector('button[aria-label="Close modal"]'); }
  function isUnsupportedModal(){ var modal = getModal() || document; return UNSUPPORTED_RE.test(modal.innerText || ""); }

  async function openModal(){
    if (getUrlInput()) return true;
    var card = findByText('button', CARD_TEXT_RE);
    if (!card){ warn("openModal: card not found."); return false; }
    card.click();
    for (var i=0;i<20;i++){ if (getUrlInput()) return true; await sleep(150); }
    return false;
  }
  async function closeModal(){
    var c = getCloseBtn();
    if (c){ c.click(); await sleep(500); }
    if (getModal()){ document.dispatchEvent(new KeyboardEvent("keydown",{key:"Escape",keyCode:27,bubbles:true})); await sleep(400); }
  }
  async function waitSubmitEnabled(){
    var waited = 0;
    while (waited < ENABLE_WAIT_MS){ var b = getSubmitBtn(); if (b && !b.disabled) return b; await sleep(300); waited += 300; }
    return null;
  }

  // -> "ok" | "unsupported" | "fail"
  async function applyOne(url){
    if (!await openModal()) return "fail";
    var input = getUrlInput();
    if (!input){ warn("applyOne: no input."); return "fail"; }
    setReactInputValue(input, url);
    await sleep(randomBetween(1000, 1800));
    if (isUnsupportedModal()){ await closeModal(); return "unsupported"; }
    var submit = await waitSubmitEnabled();
    if (!submit){ warn("applyOne: submit never enabled."); await closeModal(); return "fail"; }
    submit.click();
    await sleep(randomBetween(2000, 3500));
    if (isUnsupportedModal()){ await closeModal(); return "unsupported"; }
    await closeModal();
    return "ok";
  }

  // A JobRight job is applyable if its score clears the threshold and its title
  // isn't senior/management/blocked. Everything else JobRight -> "not interested".
  function jrScore(job){ var n = parseFloat(job.jobright_score); return isNaN(n) ? -1 : n; }
  function isSeniorOrBlocked(job){
    var t = (job.title || "").replace(/[_\-\/()\[\]]+/g, " ");
    return MANAGEMENT_RE.test(t) || SENIOR_IC_RE.test(t) || TITLE_BLOCK_RE.test(t);
  }
  function notYetHandled(job, doneSet, seen){
    if (!job || !job.url) return false;
    var n = normalizeUrl(job.url);
    return !job.applied && !job.skipped && !doneSet.has(n) && !seen.has(n);
  }

  async function run(){
    log("run() START. DRY_RUN:", DRY_RUN);
    if (!location.pathname.includes(PAGE_PATH_HINT)){ say("Open the Browse-jobs page first."); return 0; }

    say("Fetching feed from the bridge (H-1B + JobRight)...");
    var feed, rawDone = [];
    try { feed = await gmGet(FEED_URL); }
    catch(e){ say("Can't reach tracker bridge.<br>Double-click <code>D:\\track\\serve_only.bat</code> (or run_check.bat)."); return 0; }
    try { rawDone = await gmGet(BRIDGE + "/applied.json"); } catch(e){ rawDone = []; }

    var jobs = (feed && feed.jobs) || [];
    var doneSet = new Set((rawDone || []).map(u => typeof u === 'string' ? normalizeUrl(u) : normalizeUrl(u.url)));
    var seen = getSeen();

    // ---- Source routing ----
    // H-1B jobs: the LLM-judged path (isWorthApplying + judge()).
    // JobRight jobs: JobRight's own score decides -- apply if score > threshold
    //                and not senior; otherwise mark "not interested" on JobRight.
    var h1bList = [], jrApply = [], jrReject = [];
    jobs.forEach(function(j){
      if (j.source === "jobright"){
        if (!notYetHandled(j, doneSet, seen)) return;
        if (jrScore(j) > JOBRIGHT_SCORE_THRESHOLD && !isSeniorOrBlocked(j)) jrApply.push(j);
        else jrReject.push(j);
      } else if (isWorthApplying(j, doneSet, seen)){
        h1bList.push(j);
      }
    });
    var applyList = jrApply.concat(h1bList);
    applyList.sort(function(a,b){ return (b.posted_epoch||0)-(a.posted_epoch||0); });
    log("feed:", jobs.length, "| H-1B apply:", h1bList.length,
        "| JobRight apply:", jrApply.length, "| JobRight reject:", jrReject.length);

    if (DRY_RUN){
      log("%c[TSENTA] ===== DRY RUN =====","color:#065f46;font-weight:bold");
      applyList.slice(0, MAX_ADDS_PER_RUN).forEach(function(j, i){
        log((i+1)+".", "["+(j.source==="jobright"?"JR "+jrScore(j):"H1B")+"]", j.company, "-", j.title);
      });
      log("JobRight would mark 'not interested':", jrReject.length);
      say("DRY RUN: " + applyList.length + " to apply, " + jrReject.length + " JR rejects. See console.");
      return 0;
    }

    // ---- JobRight reject pass (fast, no Tsenta) ----
    if (MARK_JOBRIGHT_REJECTED && jrReject.length){
      var rcap = Math.min(jrReject.length, MAX_REJECTS_PER_RUN);
      for (var r = 0; r < rcap; r++){
        var rj = jrReject[r];
        seen.add(normalizeUrl(rj.url)); saveSeen(seen);
        say("&#128308; JobRight not-interested " + (r+1) + "/" + rcap + ":<br>" + (rj.company||"") + " - " + (rj.title||""));
        if (rj.jobright_id) await markJobrightIgnored(rj.jobright_id);
        await markSkipped(rj.url, "JobRight below-threshold / senior -> not interested");
        if (r < rcap - 1) await sleep(randomBetween(REJECT_GAP_SEC[0], REJECT_GAP_SEC[1]) * 1000);
      }
    }

    if (!applyList.length){ say("Nothing to apply. Marked " + jrReject.length + " JobRight not-interested."); return 0; }

    // ---- Apply pass (capped) ----
    say("Applying up to " + MAX_ADDS_PER_RUN + "...");
    var applied = 0, blocked = 0, deferred = 0, unsupported = 0, idx = 0;
    var perRun = {}, todayByCo = {};

    while (applied < MAX_ADDS_PER_RUN && idx < applyList.length){
      var job = applyList[idx++];
      var normUrl = normalizeUrl(job.url);
      seen.add(normUrl); saveSeen(seen);

      var co = job.company || "";
      if (MAX_PER_COMPANY_PER_RUN > 0 && (perRun[co] || 0) >= MAX_PER_COMPANY_PER_RUN){ deferred++; continue; }
      if (DAILY_LIMIT_PER_COMPANY > 0 && (todayByCo[co] || 0) >= dailyCapFor(co)){ deferred++; continue; }

      // Decide: JobRight jobs are pre-qualified by score; H-1B jobs go to the LLM.
      if (job.source !== "jobright"){
        say("Judging " + (applied+1) + "/" + MAX_ADDS_PER_RUN + ":<br>" + co + " - " + (job.title||""));
        var pf = await judge(job);
        if (pf.decision === "DEFER"){ deferred++; await sleep(randomBetween(2000,4000)); continue; }
        if (pf.decision !== "APPLY"){
          blocked++; await markSkipped(normUrl, pf.reason);
          say("&#10007; PASS: " + co + " - " + (job.title||"") + "<br><b>" + pf.reason + "</b>");
          await sleep(randomBetween(1200,2500)); continue;
        }
      }

      var tag = job.source === "jobright" ? "[JR " + jrScore(job) + "] " : "";
      say("Applying " + (applied+1) + "/" + MAX_ADDS_PER_RUN + ":<br>" + tag + co + " - " + (job.title||""));

      // Extension mode: open the job so the extension auto-adds it (needs
      // "Auto-add jobs" ON). If the extension doesn't detect it, fall back to
      // pasting the link into Tsenta.
      var res;
      if (USE_EXTENSION_APPLY){
        say("&#8599; Extension auto-add (opening tab): " + co + " - " + (job.title||""));
        res = await extApplyInTab(job);
        if (res !== "ok"){
          say("&#8601; Paste-link fallback: " + co + " - " + (job.title||""));
          res = await applyOne(job.url);
        }
      } else {
        res = await applyOne(job.url);
      }

      if (res === "ok"){
        applied++;
        await markApplied(job.url, co);
        if (job.source === "jobright" && job.jobright_id) await markJobrightApplied(job.jobright_id);
        perRun[co] = (perRun[co] || 0) + 1;
        todayByCo[co] = (todayByCo[co] || 0) + 1;
      } else if (res === "unsupported"){
        unsupported++;
        await markSkipped(normUrl, "Tsenta unsupported link");
        logUnsupported(job, "Tsenta unsupported link -- apply manually");
        say("&#8635; Unsupported (logged): " + co + " - " + (job.title||""));
        await sleep(randomBetween(1200,2500));
      } else {
        warn("Apply FAILED on:", job.title, "-- stopping.");
        await closeModal(); break;
      }

      if (applied < MAX_ADDS_PER_RUN && idx < applyList.length){
        // Extension mode already spends ~EXT_CLOSE_TIMER_MS per job in the tab,
        // so use a short gap; paste mode keeps the human-like longer gap.
        var gapSecs = USE_EXTENSION_APPLY ? randomBetween(EXT_GAP_SEC[0], EXT_GAP_SEC[1]) : nextGapSecs(applied);
        await countdownGap(gapSecs,
          "Applied " + applied + "/" + MAX_ADDS_PER_RUN + (unsupported?" &middot; "+unsupported+" unsupported":""));
      }
    }

    say("Done batch. Applied " + applied
        + (blocked ? " &middot; " + blocked + " passed" : "")
        + (unsupported ? " &middot; " + unsupported + " unsupported" : "")
        + (jrReject.length ? " &middot; " + jrReject.length + " JR not-interested" : ""));
    log("run() DONE. applied:", applied, "passed:", blocked, "unsupported:", unsupported,
        "deferred:", deferred, "jrReject:", jrReject.length);
    return applied;
  }

  // ===================== AUTO-START + LOOP =====================
  var autoStartDone = false;
  var running = false;

  async function runOnce(){
    if (running) return;
    running = true;
    var appliedCount = 0;
    try { appliedCount = await run(); }
    catch(e){ warn("run() error:", e && e.message); }
    finally { running = false; }

    if (AUTO_LOOP && !DRY_RUN && appliedCount > 0 && location.pathname.includes(PAGE_PATH_HINT)){
      var left = LOOP_COOLDOWN_SEC;
      (function tick(){
        if (!AUTO_LOOP){ say("Auto-loop stopped."); return; }
        if (left <= 0){ log("Cooldown done -- reloading for next batch."); location.reload(); return; }
        say("&#9749; Batch break " + left + "s<br><span style='opacity:.7'>Next batch after reload &middot; h1bStop() to cancel</span>");
        left--; setTimeout(tick, 1000);
      })();
    }
  }

  function autoStart(){
    if (!AUTO_START || autoStartDone) return;
    if (!location.pathname.includes(PAGE_PATH_HINT)) return;
    autoStartDone = true;
    setTimeout(function(){ runOnce(); }, randomBetween(2500, 4500));
  }

  // ===================== REVIEW PANEL (JobRight jobs) =====================
  // Manual UI: lists the JobRight jobs that would auto-apply, with checkboxes.
  // Tick = "unsupported / don't apply" (skipped + logged). Untick = apply now.
  var panel;
  function closePanel(){ if (panel){ panel.remove(); panel=null; } }

  async function jobrightApplyables(){
    var feed = await gmGet(FEED_URL);
    var rawDone = []; try { rawDone = await gmGet(BRIDGE + "/applied.json"); } catch(e){}
    var doneSet = new Set((rawDone||[]).map(u => typeof u==='string'?normalizeUrl(u):normalizeUrl(u.url)));
    var seen = getSeen();
    return ((feed && feed.jobs) || []).filter(function(j){
      return j.source === "jobright" && notYetHandled(j, doneSet, seen)
          && jrScore(j) > JOBRIGHT_SCORE_THRESHOLD && !isSeniorOrBlocked(j);
    }).sort(function(a,b){ return jrScore(b) - jrScore(a); });
  }

  async function openReview(){
    if (!location.pathname.includes(PAGE_PATH_HINT)){ say("Open the recommendations page first."); return; }
    say("Loading JobRight jobs from the bridge...");
    var list;
    try { list = await jobrightApplyables(); }
    catch(e){ say("Can't reach bridge.<br>Start the server (serve_only.bat / run_check.bat)."); return; }
    say("");
    if (!list.length){ say("No JobRight jobs above " + JOBRIGHT_SCORE_THRESHOLD + " to review."); return; }

    closePanel();
    panel = document.createElement("div");
    panel.style.cssText = "position:fixed;top:5vh;left:50%;transform:translateX(-50%);width:min(780px,94vw);max-height:88vh;z-index:1000000;background:#0b1220;color:#e6e9ef;border:1px solid #26304a;border-radius:12px;box-shadow:0 20px 60px rgba(0,0,0,.5);display:flex;flex-direction:column;font-size:13px;";
    var head = document.createElement("div");
    head.style.cssText = "padding:14px 16px;border-bottom:1px solid #26304a;";
    head.innerHTML = "<b style='font-size:15px'>Review " + list.length + " JobRight jobs (score &gt; " + JOBRIGHT_SCORE_THRESHOLD + ")</b>"
      + "<div style='opacity:.75;margin-top:4px'>Tick = skip (logged as unsupported). Untick = apply.</div>";
    panel.appendChild(head);
    var tools = document.createElement("div");
    tools.style.cssText = "padding:8px 16px;border-bottom:1px solid #26304a;display:flex;gap:8px;align-items:center;";
    tools.innerHTML = "<button id='r-all'>Tick all</button><button id='r-none'>Untick all</button><span id='r-count' style='margin-left:auto;opacity:.85'></span>";
    Array.from(tools.querySelectorAll("button")).forEach(b=>b.style.cssText="background:#1f2937;color:#e6e9ef;border:1px solid #374151;border-radius:7px;padding:5px 9px;cursor:pointer;font-size:12px;");
    panel.appendChild(tools);
    var listWrap = document.createElement("div");
    listWrap.style.cssText = "overflow:auto;padding:6px 8px;";
    panel.appendChild(listWrap);
    list.forEach(function(j, i){
      var row = document.createElement("label");
      row.style.cssText = "display:grid;grid-template-columns:24px 56px 1fr;gap:8px;align-items:center;padding:7px 8px;border-bottom:1px solid #1a2130;cursor:pointer;";
      row.innerHTML = "<input type='checkbox' data-i='"+i+"' style='width:16px;height:16px'>"
        + "<span style='font-weight:700;color:"+(jrScore(j)>=80?'#34d399':'#93c5fd')+"'>"+jrScore(j).toFixed(0)+"</span>"
        + "<span><b>"+esc(j.company)+"</b> — "+esc(j.title)+"<br><span style='opacity:.6;font-size:11px'>"+esc(hostOf(j.url))+"</span></span>";
      listWrap.appendChild(row);
    });
    var foot = document.createElement("div");
    foot.style.cssText = "padding:12px 16px;border-top:1px solid #26304a;display:flex;gap:10px;align-items:center;flex-wrap:wrap;";
    foot.innerHTML = "<button id='r-apply' style='background:#4338ca;color:#fff;border:none;border-radius:9px;padding:9px 14px;font-weight:700;cursor:pointer'>Apply unchecked</button>"
      + "<button id='r-close' style='background:#374151;color:#fff;border:none;border-radius:9px;padding:9px 14px;cursor:pointer;margin-left:auto'>Close</button>";
    panel.appendChild(foot);
    document.body.appendChild(panel);

    function boxes(){ return Array.from(listWrap.querySelectorAll("input[type=checkbox]")); }
    function refresh(){ var c=boxes().filter(b=>b.checked).length; panel.querySelector("#r-count").textContent = c+" to skip · "+(list.length-c)+" to apply"; }
    boxes().forEach(b=>b.addEventListener("change", refresh)); refresh();
    panel.querySelector("#r-all").onclick  = ()=>{ boxes().forEach(b=>b.checked=true);  refresh(); };
    panel.querySelector("#r-none").onclick = ()=>{ boxes().forEach(b=>b.checked=false); refresh(); };
    panel.querySelector("#r-close").onclick = closePanel;

    panel.querySelector("#r-apply").onclick = async function(){
      var ticked = boxes().filter(b=>b.checked).map(b=>list[+b.dataset.i]);
      var toApply = boxes().filter(b=>!b.checked).map(b=>list[+b.dataset.i]);
      closePanel();
      for (var i=0;i<ticked.length;i++){ await markSkipped(ticked[i].url, "review: marked unsupported"); logUnsupported(ticked[i], "Manually marked unsupported"); }
      var applied=0;
      for (var k=0;k<toApply.length && applied<MAX_ADDS_PER_RUN;k++){
        var job = toApply[k];
        say("Applying " + (applied+1) + "/" + Math.min(toApply.length,MAX_ADDS_PER_RUN) + ":<br>" + (job.company||"") + " - " + (job.title||""));
        var res;
        if (DRY_RUN) res = "ok";
        else if (USE_EXTENSION_APPLY){ res = await extApplyInTab(job); if (res !== "ok") res = await applyOne(job.url); }
        else res = await applyOne(job.url);
        if (res === "ok"){ applied++; await markApplied(job.url, job.company); if (job.jobright_id) await markJobrightApplied(job.jobright_id); }
        else if (res === "unsupported"){ await markSkipped(job.url, "Tsenta unsupported"); logUnsupported(job, "Tsenta unsupported"); }
        if (applied < MAX_ADDS_PER_RUN && k < toApply.length-1 && !DRY_RUN) await countdownGap(nextGapSecs(applied), "Applied "+applied);
      }
      say("Review done. Applied " + applied + " · skipped " + ticked.length + (DRY_RUN?" (DRY RUN)":""));
    };
  }

  btn.onclick = function(){ if(!running){ runOnce(); } };
  reviewBtn.onclick = function(){ openReview(); };
  setTimeout(autoStart, 1200);
  var _ps = history.pushState;
  history.pushState = function(){ _ps.apply(history, arguments); autoStartDone = false; setTimeout(autoStart, randomBetween(1200, 2200)); };

})();
