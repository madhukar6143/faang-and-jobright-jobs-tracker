// ==UserScript==
// @name       Tsenta H-1B Tracker Feed (Local Bridge + Auto-Apply)
// @namespace  h1b-tracker
// @version    2.0-localfeed
// @description Pulls fresh H-1B-sponsor jobs from the local tracker, applies via Tsenta, marks them applied back into applied.json
// @match      *://*.tsenta.com/*
// @grant      GM_xmlhttpRequest
// @grant      GM_setValue
// @grant      GM_getValue
// @connect    127.0.0.1
// @connect    localhost
// @connect    api.groq.com
// @connect    generativelanguage.googleapis.com
// @connect    *
// @run-at     document-idle
// ==/UserScript==

(function () {
  'use strict';

  var VERSION = "2.0-localfeed";

  // ===================== CONFIG =====================
  // Served by the one server started via D:\track\run.bat (serves H-1B + JobRight).
  var BRIDGE     = "http://127.0.0.1:8765";
  var FEED_URL   = BRIDGE + "/feed.json";
  var APPLIED_URL= BRIDGE + "/applied";

  var PAGE_PATH_HINT = "/dashboard/recommendations";

  // DRY RUN: when true, only log what WOULD be applied to. Nothing is opened,
  // pasted, submitted, or marked. Flip to false only when you've read the list.
  var DRY_RUN = true;

  // Secondary guard -- the tracker already windows by recency. 0 disables.
  var MAX_AGE_HOURS = 24;

  // NOTE: when DRY_RUN is false, each click SUBMITS A REAL APPLICATION.
  // Keep the cap LOW and DELAY_SEC (below) LONG.
  var MAX_ADDS_PER_RUN = 10;

  // ---- Gap between applications ----
  // CHANGE THIS ONE NUMBER to speed up or slow down. Seconds.
  //   30 = default   60 = cautious   10 = fast (higher chance of being flagged)
  // The real wait is randomised between DELAY_SEC and DELAY_SEC x 1.5 so the
  // rhythm isn't machine-perfect, and every 4-6 applications it takes a longer
  // break. A live countdown shows in the status box.
  var DELAY_SEC = 30;
  var HUMAN_BREAKS = true;   // occasional longer pause; false = steady rhythm

  // ---- Per-company limits ----
  // The feed is heavily skewed to a few employers (Google alone is most of it),
  // so without this one batch can fire 5 applications at the same company in
  // ten minutes. Google in particular only considers 3 ACTIVE applications at
  // a time; the rest are wasted. Blocked jobs are DEFERRED, not skipped, so
  // they simply come round on a later run.
  var MAX_PER_COMPANY_PER_RUN = 2;     // spreads a single batch; 0 = no limit
  var DAILY_LIMIT_PER_COMPANY = 3;     // per calendar day; 0 = no limit

  // Per-company overrides, e.g. {"Google": 1}. Google only CONSIDERS 3 ACTIVE
  // applications at a time -- a standing cap, not a daily one -- so 3/day puts
  // you at their ceiling in one sitting. Add "Google": 1 here if you want to
  // pace it. Empty means every company gets DAILY_LIMIT_PER_COMPANY.
  var COMPANY_LIMITS = {};

  function dailyCapFor(co){
    return COMPANY_LIMITS.hasOwnProperty(co) ? COMPANY_LIMITS[co]
                                             : DAILY_LIMIT_PER_COMPANY;
  }

  var ENABLE_WAIT_MS = 8000;
  var SEEN_IDS_CAP = 2000;

  // ---- Level gate ----
  // The tracker already classifies level, so trust its field first.
  // "Senior+" is dropped; set ALLOW_UNLABELED=false to be stricter still.
  var ALLOW_UNLABELED = true;
  var ALLOW_INTERNSHIPS = true;

  // Skip senior/staff/principal/lead/manager/etc. titles AND record them in
  // skipped.json so they're never offered or judged again. Set false to allow
  // senior individual-contributor roles back in.
  var BLOCK_SENIOR_TITLES = true;
  var SENIOR_TITLE_RE = /\b(senior|sr\.?|staff|principal|lead|leads|architect|advisor|manager|managing|director|head|vp|vice\s+president|chief|distinguished|fellow|president|partner|expert)\b/i;

  // ITAR / clearance roles (SpaceX, Booz Allen, Northrop, Boeing, Anduril,
  // Leidos...) normally require US Person status -- citizen or green card --
  // which an H-1B candidate can't satisfy. Skipped by default; set false only
  // if you already hold that status.
  var SKIP_US_PERSON_ONLY = true;

  // If enrich.py has read the posting body, honour what it actually said.
  // "skip"    = 4+ yrs required, or the posting rules out sponsorship
  // "stretch" = 2-3 yrs required
  var SKIP_VERDICT_SKIP = true;
  var ALLOW_STRETCH = true;

  // ---- Preflight: open the real posting and check it BEFORE pasting ----
  // The tracker's flags come from feed.json, which can be hours old and only
  // covers jobs enrich.py reached. This re-reads the live posting for each job
  // about to be applied to, so a clearance line added since the last run still
  // gets caught.
  var PREFLIGHT_CHECK = true;
  // STEM OPT gives 36 months of work authorisation with no employer
  // sponsorship, so a plain "we don't sponsor" is still applicable. Only an
  // explicit "now AND in the future" bar actually rules you out.
  // Both are off: OPT covers 36 months regardless, and a "now and in the
  // future" clause is a stated preference rather than a hard legal bar.
  // Clearance/citizenship below is still blocked -- OPT can't satisfy that.
  var BLOCK_ON_NO_SPONSORSHIP = false;      // plain "no sponsorship"
  var BLOCK_ON_FUTURE_BAR = false;          // "now or in the future"
  // If the body can't be read (JS-only page, 403, bot wall) there is nothing to
  // judge -- no work-auth check, no fit score. Applying blind wastes a slot out
  // of 200+ candidates, so skip it. Set false to apply anyway.
  var BLOCK_WHEN_UNREADABLE = true;

  // Optional LLM second opinion. It can only ever turn APPLY into SKIP --
  // never the reverse -- because these models hallucinate citizenship
  // requirements from EEO boilerplate. The regex above is the real check.
  // ---- Resume match ----
  // The JD is sent to Groq/Gemini together with this resume; the model returns
  // a fit score and an APPLY/PASS call. Deterministic rules below still
  // override it -- the model never gets to approve something they block.
  var RESUME = "YOUR_RESUME_SUMMARY_HERE -- a short plain-text summary of your skills, stack and years of experience for the LLM screener.";

  var FIT_CUTOFF = 50;    // fit <= this -> PASS
  var MAX_YOE = 8;        // years required above this -> PASS

  var USE_LLM_SECOND_OPINION = true;
  // !! These keys have been pasted in chat and should be rotated:
  //    https://console.groq.com/keys  and  https://aistudio.google.com/apikey
  var LLM_POOL = [
    {provider:"groq",   key:"YOUR_GROQ_API_KEY", url:"https://api.groq.com/openai/v1/chat/completions", model:"llama-3.1-8b-instant"},
    {provider:"groq",   key:"YOUR_GROQ_API_KEY", url:"https://api.groq.com/openai/v1/chat/completions", model:"llama-3.1-8b-instant"},
    {provider:"gemini", key:"YOUR_GEMINI_API_KEY", url:"https://generativelanguage.googleapis.com/v1beta/openai/chat/completions", model:"gemini-3.1-flash-lite"}
  ];

  // Keyword matching fails both ways -- it misses "must possess an active Top
  // Secret clearance" and fires on "clearance is a plus" or the EMPLOYEE
  // POLYGRAPH PROTECTION ACT poster. Match the PHRASE: is this a requirement
  // on you? Mirrors work_auth_block() in enrich.py.
  var WA_SUBJECT  = /(?:security|top[\s-]secret|ts\/sci|government|dod|public\s+trust)\s+clearance|\bclearance\b|\bpolygraph\b|u\.?s\.?\s+citizen(?:ship|s)?|u\.?s\.?\s+persons?|\bitar\b|export[\s-]control(?:s|led)?|export\s+administration|permanent\s+resident|green\s+card/gi;
  var WA_REQUIRES = /\b(?:must|required|require[sd]?|shall|mandatory|restricted\s+to|limited\s+to)\b|\bneed\s+to\s+(?:be|have|possess|hold)\b|\bonly\s+u\.?s\.?|\beligibility\s+requirement/i;
  var WA_SOFTENS  = /\b(?:preferred|preferrable|a\s+plus|nice[\s-]to[\s-]have|desirable|desired|beneficial|advantage|bonus|ideally|not\s+required|no\s+clearance)\b|\bwilling(?:ness)?\s+(?:to|and)|\b(?:ability|able|eligible)\s+to\s+obtain|\b(?:customers?|clients?|agencies|partners?|personnel|teams?|colleagues)\s+(?:who|that|with|requiring|holding|hold|need)|\bmay\s+(?:require|involve|need|be\s+subject)|\b(?:standards?|certifications?|frameworks?|regulations?|controls?)\s+(?:like|such\s+as|including|e\.g\.)|\b(?:compliant|compliance|certified|accredited)\s+with|employee\s+polygraph\s+protection|polygraph\s+protection\s+act|know\s+your\s+rights|\beppa\b/i;
  var WA_HARD     = /\bitar\b|export[\s-]control(?:s|led)?|ts\/sci|\bpolygraph\b/i;
  var WA_OVERRIDE = /\b(?:must|required\s+to|need\s+to)\s+(?:be\s+)?(?:able|eligible|willing|capable)\s+to\s+(?:obtain|acquire|secure|get)|\bmust\s+(?:be\s+)?(?:able\s+to\s+)?(?:obtain|maintain)\b/i;
  var WA_WINDOW = 130;

  // -> null when clean, or the offending sentence when it's a real requirement
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

  var NO_SPONSOR_RE = /(?:not|unable|cannot|will\s+not|does\s+not|do\s+not)[\w\s,]{0,40}?sponsor|without\s+[\w\s]{0,34}?sponsorship|no\s+(?:visa\s+)?sponsorship|sponsorship\s+is\s+not\s+(?:available|offered|provided)|not\s+eligible\s+for\s+(?:visa\s+)?sponsorship/i;

  // The bar that OPT can't get past -- they want someone who will never need
  // sponsorship, which you would at the end of the 36 months.
  var FUTURE_BAR_RE = /(?:now|current(?:ly)?)\s*(?:,|or|and|\/)+\s*(?:in\s+the\s+)?future|current\s+or\s+future\s+(?:visa\s+)?sponsorship|future\s+(?:visa\s+)?sponsorship|ongoing\s+(?:visa\s+)?sponsorship/i;
  // Standalone bar -- doesn't mention sponsorship, just demands authorisation
  // that outlasts OPT.
  var PERM_AUTH_RE = /permanent\s+(?:u\.?s\.?\s+)?work\s+authoriz|indefinite\s+work\s+authoriz|unrestricted\s+work\s+authoriz|permanent\s+employment\s+authoriz/i;

  // Belt-and-braces title checks (the tracker's classifier is title-based too,
  // but these catch anything that slips through).
  // Always blocked -- people-management, lead AND principal, matching the
  // /\b(lead|manager|principal)\b/ rule from the auto-judge script.
  var MANAGEMENT_RE   = /\b(manager|managing|director|chief|president|partner|lead|leads|principal)\b|\bhead\s+of\b|\bvp\b|vice\s+president/i;
  // Blocked only when ALLOW_SENIOR is false. Senior/Staff are IC rungs; the
  // years ceiling filters the unrealistic ones.
  var SENIOR_IC_RE    = /\b(senior|sr\.?|staff|architect|distinguished|fellow)\b/i;
  var TITLE_BLOCK_RE  = /(technician|trader|coordinator|repair|installation|help\s?desk|\bsales\b|driver|geologist|postdoc|recruiter)/i;

  // ===================== SELECTORS (unchanged -- your verified DOM) ==========
  var CARD_TEXT_RE = /add your own link/i;
  var URL_INPUT_SELECTOR = 'input[placeholder^="https://boards.greenhouse.io"]';
  var SUBMIT_TEXT_RE = /apply with optimized resume/i;
  var MODAL_SELECTOR = '[id^="headlessui-dialog-panel"]';
  // ==========================================================================

  // ===================== LOGGING =====================
  var LOG = "[H1BFEED]";
  function log(){ var a=[].slice.call(arguments); a.unshift(LOG,new Date().toLocaleTimeString()); console.log.apply(console,a); }
  function warn(){ var a=[].slice.call(arguments); a.unshift(LOG,new Date().toLocaleTimeString()); console.warn.apply(console,a); }
  log("Loaded", VERSION, "| path:", location.pathname, "| DRY_RUN:", DRY_RUN, "| bridge:", BRIDGE);

  // ===================== PERSISTENCE =====================
  // applied.json on disk is the source of truth; this is a local mirror so we
  // don't re-apply if the bridge is briefly unreachable.
  function getSeen(){ try { return new Set(JSON.parse(GM_getValue("h1b_seen","[]"))); } catch(e){ return new Set(); } }
  function saveSeen(set){ var a=Array.from(set); if(a.length>SEEN_IDS_CAP) a=a.slice(a.length-SEEN_IDS_CAP); GM_setValue("h1b_seen",JSON.stringify(a)); }

  window.h1bReset  = function(){ GM_setValue("h1b_seen","[]"); log("Local seen-set cleared."); say("Local seen-set cleared."); };
  window.h1bStatus = function(){
    var s=getSeen(); log("STATUS -- locally seen:", s.size, "| page:", location.pathname);
    return { seen: s.size, page: location.pathname, bridge: BRIDGE, dryRun: DRY_RUN };
  };

  // ===================== DIAGNOSTIC DUMP =====================
  window.h1bDiag = function(){
    console.log("%c[H1BFEED] ===== DIAGNOSTIC DUMP =====","color:#065f46;font-weight:bold");
    console.log("[H1BFEED] INPUTS / TEXTAREAS:");
    document.querySelectorAll('input,textarea').forEach(function(el,i){
      console.log(i, el.tagName, JSON.stringify({type:el.type,placeholder:el.placeholder,name:el.name,aria:el.getAttribute('aria-label'),cls:el.className}), el.outerHTML.slice(0,220));
    });
    console.log("[H1BFEED] BUTTONS:");
    document.querySelectorAll('button,[role=button]').forEach(function(el,i){
      var t=(el.innerText||el.getAttribute('aria-label')||'').trim(); if(t) console.log(i, JSON.stringify(t.slice(0,40)), "disabled="+el.disabled, el.outerHTML.slice(0,150));
    });
    console.log("%c[H1BFEED] ===== END DUMP =====","color:#065f46;font-weight:bold");
  };

  // ===================== UI =====================
  var btn = document.createElement("button");
  btn.textContent = (DRY_RUN ? "DRY RUN H-1B feed" : "Apply from H-1B feed");
  btn.style.cssText = "position:fixed;bottom:120px;left:24px;z-index:999999;background:#065f46;color:#fff;border:none;padding:12px 16px;border-radius:10px;font-size:13px;font-weight:700;cursor:pointer;box-shadow:0 6px 20px rgba(0,0,0,.3);";
  document.body.appendChild(btn);

  // No Refresh button by design: a one-click refetch invites over-clicking,
  // and every click means ~8,000 requests across 238 employer sites. Fetch
  // deliberately from a terminal instead:
  //   python D:\track\fetch_jobs.py && python D:\track\enrich.py
  //
  // No Diag button either -- call h1bDiag() from the console if the selectors
  // stop matching after a Tsenta UI change.

  var status = document.createElement("div");
  status.style.cssText = "position:fixed;bottom:168px;left:24px;z-index:999999;background:#111827;color:#fff;padding:10px 14px;border-radius:8px;font-size:13px;max-width:420px;display:none;line-height:1.5;";
  document.body.appendChild(status);
  function say(t){ status.style.display="block"; status.innerHTML=t; }

  // ===================== HELPERS =====================
  function sleep(ms){ return new Promise(r=>setTimeout(r,ms)); }
  function randomBetween(a,b){ return Math.floor(Math.random()*(b-a+1))+a; }
  // How long to wait before application number `n+1`.
  function nextGapSecs(n){
    var s = randomBetween(DELAY_SEC, Math.round(DELAY_SEC * 1.5));
    if (HUMAN_BREAKS && n > 0 && n % randomBetween(4, 6) === 0) s += DELAY_SEC * 2;
    return s;
  }

  // Waits, ticking down in the status box so it's obvious it hasn't hung.
  async function countdownGap(secs, tail){
    var isBreak = secs > DELAY_SEC * 2;
    for (var left = secs; left > 0; left--){
      say((isBreak ? "&#9749; Longer break " : "Waiting ") + left + "s"
          + "<br><span style='opacity:.7'>" + tail + "</span>");
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
        onerror:function(e){ reject(new Error("network")); },
        ontimeout:function(){ reject(new Error("timeout")); } });
    });
  }

  function markApplied(url, company){
    return new Promise(function(resolve){
      GM_xmlhttpRequest({
        method:"POST", url:APPLIED_URL, timeout:10000,
        headers:{ "Content-Type":"application/json" },
        data: JSON.stringify({ url: url, company: company || "" }),
        onload:function(r){ log("  marked applied on disk ->", r.status); resolve(true); },
        onerror:function(){ warn("  could not reach bridge to mark applied"); resolve(false); },
        ontimeout:function(){ warn("  bridge timeout marking applied"); resolve(false); }
      });
    });
  }

  // ---- Preflight helpers -------------------------------------------------

  // Workday detail pages are JS shells; its cxs endpoint returns the text.
  function fetchableUrl(url){
    var m = url.match(/^https:\/\/([^.]+)\.(wd\d+)\.myworkdayjobs\.com\/en-US\/([^\/]+)(\/.*)$/);
    if (m) return "https://"+m[1]+"."+m[2]+".myworkdayjobs.com/wday/cxs/"+m[1]+"/"+m[3]+m[4];
    return url;
  }

  function stripHtml(s){
    return (s||"")
      .replace(/<(script|style)[^>]*>[\s\S]*?<\/\1>/gi, " ")
      .replace(/<[^>]+>/g, " ")
      .replace(/&nbsp;/gi, " ").replace(/&amp;/gi, "&")
      .replace(/&lt;/gi, "<").replace(/&gt;/gi, ">").replace(/&quot;/gi, '"')
      .replace(/\s+/g, " ").trim();
  }

  function fetchPosting(url){
    return new Promise(function(resolve){
      GM_xmlhttpRequest({
        method:"GET", url:fetchableUrl(url), timeout:25000,
        headers:{"Accept":"text/html,application/json,*/*"},
        onload:function(r){
          if (r.status < 200 || r.status >= 300) return resolve("");
          resolve(stripHtml(r.responseText));
        },
        onerror:function(){ resolve(""); },
        ontimeout:function(){ resolve(""); }
      });
    });
  }

  // Stick to whichever key last worked. Free-tier Groq keys rate-limit fast,
  // and restarting at index 0 every time burns a doomed request per job.
  var llmIdx = 0;

  function extractJSON(s){
    if (!s) return null;
    try { return JSON.parse(s); } catch(e){}
    var clean = s.replace(/```json/gi,"").replace(/```/g,"").replace(/[“”]/g,'"').trim();
    try { return JSON.parse(clean); } catch(e){}
    var m = clean.match(/\{[\s\S]*\}/);
    if (m){
      try { return JSON.parse(m[0]); }
      catch(e){ try { return JSON.parse(m[0].replace(/,\s*([\}\]])/g,"$1")); } catch(e2){} }
    }
    return null;
  }

  function parseYears(raw){
    if (typeof raw === "number" && isFinite(raw)) return Math.round(raw);
    if (typeof raw === "string"){ var m = raw.match(/\d+/); if (m) return parseInt(m[0],10); }
    return NaN;
  }

  // Scores the JD against RESUME and returns the model's raw view.
  function askLLM(text, title, attempt, tries){
    attempt = attempt || 0; tries = tries || 0;
    return new Promise(function(resolve){
      var pool = LLM_POOL.filter(function(p){ return p.key && !/REPLACE_ME/.test(p.key); });
      if (!pool.length || attempt >= pool.length) return resolve(null);
      var poolIdx = (llmIdx + attempt) % pool.length;
      var t = pool[poolIdx];
      var maxChars = t.provider === "groq" ? 10000 : 12000;
      var sys = "You are an automated job screener. You MUST respond with ONLY valid JSON "
        + "and no extra conversational text.";
      var usr = "Screen this job for the user.\n\nRULES:\n"
        + "1. citizenshipOrClearance='required' ONLY if the job EXPLICITLY demands US citizen "
        + "/ US person / security clearance. EEO statements, protected veterans, ADA, "
        + "GDPR/CCPA, and work-authorization questions do NOT count => 'not stated'.\n"
        + "2. yearsRequired = MINIMUM years explicitly required, plain integer "
        + "('3-5 years'=>3, '6+'=>6). If not stated, null. NEVER words.\n"
        + "3. Ignore visa sponsorship entirely.\n"
        + "4. fit (0-100) by resume alignment with required skills AND domain. Be strict: "
        + "a different stack/domain lowers fit a lot.\n"
        + "5. reason: ONE short sentence consistent with the decision. If you mention a "
        + "blocker in the reason, decision MUST be PASS. Never mention citizenship if the "
        + "job does not require it.\n"
        + "6. Return ONLY:\n"
        + '{"decision":"APPLY or PASS","fit":80,"reason":"...","yearsRequired":3,'
        + '"citizenshipOrClearance":"required or not stated"}\n\n'
        + "RESUME:\n" + RESUME + "\n\nJOB DESCRIPTION:\n" + text.substring(0, maxChars);
      GM_xmlhttpRequest({
        method:"POST", url:t.url, timeout:25000,
        headers:{"Content-Type":"application/json","Authorization":"Bearer "+t.key},
        data: JSON.stringify({model:t.model, temperature:0, max_tokens:300,
          response_format:{type:"json_object"},
          messages:[{role:"system",content:sys},{role:"user",content:usr}]}),
        onload:async function(r){
          // Rate limited -- back off once, then fall through to the next key.
          // 429 means this key is spent -- move on rather than waiting.
          // log(), not warn(): console.warn dumps a stack trace per call.
          if (r.status === 429){
            log("  LLM key " + (poolIdx+1) + " (" + t.provider + ") rate-limited, next key");
            // Only advance via `attempt`; moving llmIdx here too would skip the
            // very next key instead of trying it.
            return resolve(await askLLM(text, title, attempt + 1, tries));
          }
          if (r.status < 200 || r.status >= 300){
            log("  LLM key " + (poolIdx+1) + " HTTP " + r.status + " -- next key");
            return resolve(await askLLM(text, title, attempt + 1, 0));
          }
          try{
            var c = JSON.parse(r.responseText).choices[0].message.content;
            var raw = extractJSON(c) || {};
            var n = {}; for (var k in raw) n[k.toLowerCase()] = raw[k];
            llmIdx = poolIdx;                          // this one works, stay on it
            resolve({
              fit: typeof n.fit === "number" ? n.fit : (parseInt(n.fit,10) || 0),
              years: parseYears(n.yearsrequired),
              clearance: String(n.citizenshiporclearance||"").toLowerCase().trim(),
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

  // Prefer the copy enrich.py already stored -- avoids a second fetch.
  function cachedPosting(url){
    return new Promise(function(resolve){
      GM_xmlhttpRequest({
        method:"GET", url: BRIDGE + "/jd?url=" + encodeURIComponent(url), timeout:10000,
        onload:function(r){
          if (r.status !== 200) return resolve("");
          try { resolve(JSON.parse(r.responseText).text || ""); } catch(e){ resolve(""); }
        },
        onerror:function(){ resolve(""); }, ontimeout:function(){ resolve(""); }
      });
    });
  }

  // -> {ok, decision, fit, reason}
  // Deterministic rules decide first and are final. The model only scores fit
  // against the resume; it can never approve something the rules blocked.
  async function judge(job){
    if (!PREFLIGHT_CHECK) return {ok:true, decision:"APPLY", fit:null, reason:"checks off"};

    // 1. title-based block -- no network needed. PASS here means the loop
    //    records it in skipped.json, so it's never judged or offered again.
    var t = (job.title || "").replace(/[_\-\/()\[\]]+/g, " ");
    if (MANAGEMENT_RE.test(t))
      return {ok:false, decision:"PASS", fit:0, reason:"management title"};
    if (BLOCK_SENIOR_TITLES && SENIOR_TITLE_RE.test(t))
      return {ok:false, decision:"PASS", fit:0, reason:"senior-level title (auto-passed)"};
    if (TITLE_BLOCK_RE.test(t))
      return {ok:false, decision:"PASS", fit:0, reason:"non-CS title"};

    // 2. the posting itself -- cached copy first, live fetch as fallback
    var text = await cachedPosting(job.url);
    if (text.length < 300) text = await fetchPosting(job.url);
    if (text.length < 300){
      return BLOCK_WHEN_UNREADABLE
        ? {ok:false, decision:"PASS", fit:0,
           reason:"posting unreadable - nothing to judge (bot wall or JS-only page)"}
        : {ok:true, decision:"APPLY", fit:null, reason:"posting unreadable - NOT verified"};
    }

    // 3. work authorisation -- hard, deterministic
    var hit = workAuthBlock(text);
    if (hit) return {ok:false, decision:"PASS", fit:0,
                     reason:"work-auth requirement: \"" + hit.slice(0,100) + "\""};

    var perm = PERM_AUTH_RE.exec(text);
    if (BLOCK_ON_FUTURE_BAR && perm)
      return {ok:false, decision:"PASS", fit:0,
              reason:"needs permanent work authorization"};
    var s = NO_SPONSOR_RE.exec(text);
    if (s){
      var fb = FUTURE_BAR_RE.exec(text);
      if (BLOCK_ON_FUTURE_BAR && fb)
        return {ok:false, decision:"PASS", fit:0, reason:"needs work auth now AND in future"};
      if (BLOCK_ON_NO_SPONSORSHIP)
        return {ok:false, decision:"PASS", fit:0, reason:"states no sponsorship"};
      // else fine: STEM OPT covers 36 months
    }

    if (!USE_LLM_SECOND_OPINION)
      return {ok:true, decision:"APPLY", fit:null, reason:"passed deterministic checks"};

    // 4. resume fit
    var v = await askLLM(text, job.title || "");
    // Every key rate-limited or erroring is TRANSIENT -- don't apply unjudged,
    // but don't record a skip either or a blip would bury a good job forever.
    // DEFER just moves on; the job stays in the feed for the next run.
    if (!v) return {ok:false, decision:"DEFER", fit:null,
                    reason:"all LLM keys unavailable - deferred, not marked"};

    // Verify the model's claims against the real text before trusting them.
    var jdYears = /\d+\s*\+?\s*(?:-|to|–)?\s*\d*\s*(?:\+\s*)?years?/i.test(text);
    var reason = v.reason;
    if (!hit && /clearance|citizen|us person/i.test(reason)) reason = "";  // hallucinated

    if (!isNaN(v.years) && v.years > MAX_YOE && jdYears)
      return {ok:false, decision:"PASS", fit:v.fit,
              reason:"requires " + v.years + "+ yrs (limit " + MAX_YOE + ")"};
    if (v.fit <= FIT_CUTOFF)
      return {ok:false, decision:"PASS", fit:v.fit,
              reason:"fit " + v.fit + "% below cutoff" + (reason ? " - " + reason : "")};

    // The model sometimes scores 90 while its own reason says you lack the
    // core skill (different keys in the pool score the same job differently).
    // If it names a gap, trust the words over the number.
    if (/\b(lacks?|lacking|missing|does not have|doesn't have|no explicit|not aligned|but lacks)\b/i.test(reason))
      return {ok:false, decision:"PASS", fit:v.fit,
              reason:"reason contradicts score (" + v.fit + "%) - " + reason.slice(0,120)};

    return {ok:true, decision:"APPLY", fit:v.fit,
            reason: reason || ("fit " + v.fit + "%")};
  }

  // Blocked jobs go to skipped.json -- not applied.json -- so they're never
  // offered again while your applied list stays a true record.
  function markSkipped(url, reason){
    return new Promise(function(resolve){
      GM_xmlhttpRequest({
        method:"POST", url:BRIDGE + "/skip", timeout:10000,
        headers:{ "Content-Type":"application/json" },
        data: JSON.stringify({ url: url, reason: reason }),
        onload:function(){ resolve(true); },
        onerror:function(){ warn("  couldn't record skip on disk"); resolve(false); },
        ontimeout:function(){ resolve(false); }
      });
    });
  }

  function isWorthApplying(job, doneSet, seen){
    if (!job || !job.url) return false;
    if (job.applied) return false;
    if (job.skipped) return false;          // blocked at preflight previously
    if (doneSet.has(job.url)) return false;
    if (seen.has(job.url)) return false;

    if (SKIP_US_PERSON_ONLY && job.work_auth_risk) return false;
    if (SKIP_VERDICT_SKIP && job.verdict === "skip") return false;
    if (!ALLOW_STRETCH && job.verdict === "stretch") return false;

    var lvl = job.level || "";
    if (!ALLOW_INTERNSHIPS && /^Intern/i.test(lvl)) return false;
    if (!ALLOW_UNLABELED && /^Unlabeled/i.test(lvl)) return false;

    // Title-based blocks (management/senior/non-CS) are NOT filtered here --
    // they flow through to judge(), which PASSes them so the loop records them
    // in skipped.json. Filtering here would drop them silently and they'd keep
    // reappearing every run.

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

  function findByText(selector, re){
    return Array.from(document.querySelectorAll(selector)).find(el => re.test((el.innerText||el.textContent||"").trim()));
  }
  function getModal(){ return document.querySelector(MODAL_SELECTOR); }
  function getUrlInput(){ return document.querySelector(URL_INPUT_SELECTOR); }
  function getSubmitBtn(){
    var scope = getModal() || document;
    return Array.from(scope.querySelectorAll('button')).find(b => SUBMIT_TEXT_RE.test((b.innerText||"").trim()));
  }
  function getCloseBtn(){
    var scope = getModal() || document;
    return scope.querySelector('button[aria-label="Close modal"]');
  }

  async function openModal(){
    if (getUrlInput()) return true;
    var card = findByText('button', CARD_TEXT_RE);
    if (!card){ warn("openModal: card not found."); return false; }
    card.click();
    for (var i=0;i<20;i++){ if (getUrlInput()) return true; await sleep(150); }
    warn("openModal: input never appeared.");
    return false;
  }

  async function closeModal(){
    var c = getCloseBtn();
    if (c){ c.click(); await sleep(500); }
    if (getModal()){ document.dispatchEvent(new KeyboardEvent("keydown",{key:"Escape",keyCode:27,bubbles:true})); await sleep(400); }
  }

  async function waitSubmitEnabled(){
    var waited = 0;
    while (waited < ENABLE_WAIT_MS){
      var b = getSubmitBtn();
      if (b && !b.disabled) return b;
      await sleep(300); waited += 300;
    }
    return null;
  }

  async function applyOne(url){
    if (!await openModal()) return false;
    var input = getUrlInput();
    if (!input){ warn("applyOne: no input."); return false; }
    log("applyOne: pasting", url);
    setReactInputValue(input, url);
    await sleep(randomBetween(700, 1400));
    var submit = await waitSubmitEnabled();
    if (!submit){ warn("applyOne: submit never enabled after " + ENABLE_WAIT_MS + "ms."); return false; }
    log("applyOne: clicking 'Apply with Optimized Resume'.");
    submit.click();
    await sleep(randomBetween(2000, 3500));
    await closeModal();
    return true;
  }

  // ===================== MAIN =====================
  async function run(){
    log("run() START. DRY_RUN:", DRY_RUN);
    if (!location.pathname.includes(PAGE_PATH_HINT)){
      say("Open the Browse-jobs page first (path should include " + PAGE_PATH_HINT + ").");
      return;
    }

    say("Fetching feed from local tracker...");
    var feed, doneList;
    try {
      feed = await gmGet(FEED_URL);
    } catch(e){
      warn("feed fetch failed:", e.message);
      say("Can't reach the tracker bridge.<br>Double-click <code>D:\\track\\run.bat</code> (or start the server), then retry.");
      return;
    }
    try { doneList = await gmGet(BRIDGE + "/applied.json"); } catch(e){ doneList = []; }

    var jobs = (feed && feed.jobs) || [];
    var doneSet = new Set(doneList || []);
    var seen = getSeen();
    log("feed generated:", feed.generated, "| jobs:", jobs.length,
        "| window:", feed.window_hours + "h", "| already applied:", doneSet.size);

    var candidates = jobs.filter(function(j){ return isWorthApplying(j, doneSet, seen); });
    candidates.sort(function(a,b){ return (b.posted_epoch||0)-(a.posted_epoch||0); });
    log("candidates after filter:", candidates.length);

    if (!candidates.length){
      say("No new eligible listings.<br>Feed has " + jobs.length + " jobs; " + doneSet.size + " already applied.");
      return;
    }

    var toDo = candidates.slice(0, MAX_ADDS_PER_RUN);

    // -------- DRY RUN --------
    if (DRY_RUN){
      log("%c[H1BFEED] ===== DRY RUN: would apply (nothing submitted) =====","color:#065f46;font-weight:bold");
      candidates.forEach(function(j, i){
        log((i+1)+".", "["+(j.age_hours!=null?j.age_hours.toFixed(1)+"h":"?")+"]",
            "["+(j.level||"?")+"]",
            (j.min_yoe != null ? "["+j.min_yoe+"y req] " : "") +
            (j.sponsorship ? "["+j.sponsorship+"] " : "") + "["+(j.category||"?")+"]",
            (j.work_auth_risk ? "[!"+j.work_auth_risk+"] " : "") +
            (j.company||"?")+" -", (j.title||"?"), "|", (j.location||"?"), "|", j.url);
      });
      log("%c[H1BFEED] ===== END DRY RUN -- "+candidates.length+" eligible =====","color:#065f46;font-weight:bold");

      if (PREFLIGHT_CHECK){
        say("DRY RUN: judging the first " + toDo.length + " postings against your resume...");
        log("%c[H1BFEED] --- judging (JD vs resume, via Groq/Gemini) ---","color:#065f46;font-weight:bold");
        var wouldApply = 0, wouldPass = 0, wouldDefer = 0;
        for (var k=0;k<toDo.length;k++){
          var pf = await judge(toDo[k]);
          if (pf.decision === "APPLY") wouldApply++;
          else if (pf.decision === "DEFER") wouldDefer++;
          else wouldPass++;
          log((pf.decision === "APPLY" ? "  APPLY " :
               pf.decision === "DEFER" ? "  DEFER " : "  PASS  "),
              "fit=" + (pf.fit === null ? "n/a" : pf.fit),
              "|", toDo[k].company + " - " + toDo[k].title, "|", pf.reason);
          await sleep(randomBetween(400, 900));
        }
        log("%c[H1BFEED] ===== judged: "+wouldApply+" APPLY, "+wouldPass+" PASS, "
            +wouldDefer+" DEFER =====","color:#065f46;font-weight:bold");
        say("DRY RUN: " + candidates.length + " eligible &middot; judged <b>" + wouldApply
            + " APPLY</b>, <b>" + wouldPass + " PASS</b>"
            + (wouldDefer ? ", <b>" + wouldDefer + " deferred</b>" : "")
            + ".<br>Nothing applied, nothing marked."
            + "<br>Set <code>DRY_RUN=false</code> to go live.");
      } else {
        say("DRY RUN: " + candidates.length + " eligible logged to console.<br>Nothing applied, nothing marked.<br>Set <code>DRY_RUN=false</code> to go live.");
      }
      return;
    }
    // -------------------------

    say("Applying up to " + MAX_ADDS_PER_RUN + " of " + candidates.length + "...");
    var applied = 0, blocked = 0, deferred = 0, idx = 0;

    // Today's per-company tally, rebuilt from applied.json each run. Counting
    // by calendar date means the map empties itself at midnight -- no stored
    // counter to reset, and it survives browser restarts and data clearing.
    var perRun = {}, todayByCo = {};
    if (DAILY_LIMIT_PER_COMPANY > 0){
      try {
        var full = await gmGet(BRIDGE + "/applied/full");
        var today = new Date().toDateString();
        (full || []).forEach(function(e){
          if (!e.company || !e.at) return;
          if (new Date(e.at).toDateString() === today)
            todayByCo[e.company] = (todayByCo[e.company] || 0) + 1;
        });
        var tally = Object.keys(todayByCo).map(function(c){
          return c + "=" + todayByCo[c] + "/" + dailyCapFor(c); });
        log("applied today:", tally.length ? tally.join(", ") : "nothing yet");
      } catch(e){ log("couldn't read applied history - daily cap off this run"); }
    }

    // Walk the whole candidate list: a job blocked at preflight doesn't burn a
    // slot, the next one takes it.
    while (applied < MAX_ADDS_PER_RUN && idx < candidates.length){
      var job = candidates[idx++];

      // Per-company throttle. DEFER, never PASS -- the job is fine, we just
      // don't want five applications at one employer in ten minutes.
      var co = job.company || "";
      if (MAX_PER_COMPANY_PER_RUN > 0 && (perRun[co] || 0) >= MAX_PER_COMPANY_PER_RUN){
        deferred++;
        log("DEFER", co, "|", job.title, "| already", perRun[co], "this run");
        continue;
      }
      var cap = dailyCapFor(co);
      if (DAILY_LIMIT_PER_COMPANY > 0 && (todayByCo[co] || 0) >= cap){
        deferred++;
        // NOT marked -- the job is good, it just waits for tomorrow. Nothing
        // was spent on it either: this check runs before the LLM.
        log("DEFER", co, "|", job.title, "| daily cap", todayByCo[co] + "/" + cap);
        continue;
      }

      if (PREFLIGHT_CHECK){
        say("Judging " + (applied+1) + "/" + MAX_ADDS_PER_RUN + ":<br>"
            + (job.company||"") + " - " + (job.title||""));
        var pf = await judge(job);
        if (pf.decision === "DEFER"){
          // Transient failure -- leave it untouched so a later run can judge it.
          deferred++;
          // log(), not warn(): console.warn dumps a stack trace per line and
          // these are normal outcomes, not faults.
          log("DEFER", job.company, "|", job.title, "|", pf.reason);
          say("&#8635; Deferred: " + (job.company||"") + " - " + (job.title||"")
              + "<br><b>" + pf.reason + "</b>");
          await sleep(randomBetween(2000, 4000));
          continue;
        }
        if (pf.decision !== "APPLY"){
          blocked++;
          seen.add(job.url); saveSeen(seen);
          // PASS is recorded too, so it's never judged or offered again.
          await markSkipped(job.url, pf.reason);
          log("PASS", job.company, "|", job.title, "| fit", pf.fit, "|", pf.reason);
          say("&#10007; PASS: " + (job.company||"") + " - " + (job.title||"")
              + "<br><b>" + pf.reason + "</b>");
          await sleep(randomBetween(1200, 2500));
          continue;
        }
        log("APPLY", job.company, "|", job.title, "| fit", pf.fit, "|", pf.reason);
      }

      log("--- Apply", (applied+1)+"/"+MAX_ADDS_PER_RUN, "|", job.company, "|", job.title);
      say("Applying " + (applied+1) + "/" + MAX_ADDS_PER_RUN + ":<br>" + (job.company||"") + " - " + (job.title||job.url));

      var ok = await applyOne(job.url);
      if (ok){
        applied++;
        seen.add(job.url); saveSeen(seen);
        await markApplied(job.url, job.company);   // <- writes through to applied.json
        perRun[job.company] = (perRun[job.company] || 0) + 1;
        todayByCo[job.company] = (todayByCo[job.company] || 0) + 1;
        log("Apply OK. Marked applied.");
      } else {
        seen.add(job.url); saveSeen(seen);    // don't retry a broken one next run
        warn("Apply FAILED on:", job.title, "-- stopping.");
        say("Apply failed on: " + (job.title||job.url) + "<br>Open the modal, run <code>h1bDiag()</code> in the console, send the dump.");
        await closeModal();
        break;
      }
      if (applied < MAX_ADDS_PER_RUN && idx < candidates.length){
        await countdownGap(nextGapSecs(applied),
                           "Applied " + applied + "/" + MAX_ADDS_PER_RUN
                           + (blocked ? " &middot; " + blocked + " passed" : ""));
      }
    }

    log("run() DONE. Applied", applied, "| passed:", blocked, "| deferred:", deferred);
    say("Done. Applied " + applied
        + (blocked ? " &middot; " + blocked + " passed" : "")
        + (deferred ? " &middot; " + deferred + " deferred (LLM unavailable)" : "")
        + ".<br>Marked in applied.json -- run <code>python report.py</code> to see them flagged in the HTML.");
  }

  btn.onclick = function(){ run(); };

  setInterval(function(){
    log("HEARTBEAT -- onPage:", location.pathname.includes(PAGE_PATH_HINT),
        "| cardPresent:", !!findByText('button', CARD_TEXT_RE), "| modalOpen:", !!getModal());
  }, 30000);

})();
