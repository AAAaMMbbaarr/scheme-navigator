"use strict";
// All dynamic text is inserted with textContent (never innerHTML): LLM output is untrusted.
//
// State model: the browser keeps only the LAST result (`profile`, `asked`) so it can send it back ONLY when
// the user presses the explicit "Update my profile" button. The main box is always a fresh, stateless request.
// Changing the language starts a NEW interaction: everything is cleared and no API call is made.

const ICON = { ELIGIBLE: "🟢", LIKELY_ELIGIBLE: "🟡", MISSING_INFORMATION: "🔵", NOT_ELIGIBLE: "🔴" };
const ORDER = ["ELIGIBLE", "LIKELY_ELIGIBLE", "MISSING_INFORMATION", "NOT_ELIGIBLE"];

let LANGS = [];            // from /api/languages (single source of truth)
let lang = "en";           // currently selected language code
let profile = null;        // profile of the last successful result
let asked = [];            // follow-up questions currently shown
let lastReadText = "";
let recognizing = null;
let requestSeq = 0;        // ignore responses from superseded requests
const $ = (id) => document.getElementById(id);

const langInfo = () => LANGS.find((l) => l.code === lang) || { code: "en", locale: "en-IN", rtl: false };

// Strings for the selected language; missing keys fall back to English.
function tr() {
  const en = STRINGS.en, l = STRINGS[lang] || {};
  return { ...en, ...l, status: { ...en.status, ...l.status }, fields: { ...en.fields, ...l.fields } };
}

function el(tag, text, cls) {
  const e = document.createElement(tag);
  if (text !== undefined) e.textContent = text;
  if (cls) e.className = cls;
  return e;
}
function list(items) { const ul = el("ul"); items.forEach((i) => ul.append(el("li", i))); return ul; }
const humanize = (v) => (typeof v === "string" ? v.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase()) : v);

// ---------- static text ----------
function applyLang() {
  const t = tr(), info = langInfo();
  document.documentElement.lang = lang;
  document.documentElement.dir = info.rtl ? "rtl" : "ltr";
  $("t-title").textContent = t.title; $("t-sub").textContent = t.sub;
  $("site-disclaimer").textContent = t.siteDisclaimer;   // site-wide, never cleared by a language switch
  $("t-chooseLang").textContent = t.chooseLang;
  $("t-label").textContent = t.label; $("input").placeholder = t.ph;
  $("speak").textContent = recognizing ? t.stop : t.speak; $("go").textContent = t.go;
  $("t-profile").textContent = t.profile; $("t-map").textContent = t.map; $("t-docs").textContent = t.docs;
  $("t-update-title").textContent = asked.length ? t.followup : t.updateTitle;
  $("t-update-hint").textContent = t.updateHint; $("t-answer-label").textContent = t.answerLabel;
  $("answer-input").placeholder = t.updatePh; $("answer-go").textContent = t.updateGo;
  $("answer-speak").textContent = recognizing && recognizing.btn === $("answer-speak") ? t.stop : t.speak;
  const ex = $("examples"); ex.replaceChildren();
  const own = STRINGS[lang] || {};   // examples only where we have real translations
  (own.examples || []).forEach((full, i) => {
    const b = el("button", own.exLabels[i]); b.type = "button";
    b.onclick = () => { $("input").value = full; $("input").focus(); };
    ex.append(b);
  });
  const lg = $("legend"); lg.replaceChildren();
  ORDER.forEach((s) => lg.append(el("li", `${ICON[s]} ${t.status[s]}`)));
  $("lang").value = lang;
  updateReadButton();
}

// ---------- notices ----------
function showNotice(main, detail, isError) {
  $("notice-main").textContent = main || "";
  $("notice-detail").textContent = detail || "";
  $("notice").classList.toggle("hidden", !main);
  $("notice").classList.toggle("error", !!isError);
}
const clearNotice = () => showNotice("");

// Turn the backend's `ai` status into a clean, honest, localized notice.
function noticeForAI(data) {
  const t = tr(), ai = data.ai;
  if (!ai || ai.ok) return clearNotice();
  let main;
  if (data.extraction_source === "offline_english") main = t.aiOfflineReader;
  else if (ai.code === "translation_failure") main = t.aiPartial;
  else main = t.aiFallback;
  showNotice(main, `${t.reason}: ${ai.code}${ai.message ? " — " + ai.message : ""}`, false);
}

// ---------- voice input ----------
const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
// Dictate into `input` (main box or answer box). Typing always stays available.
function toggleVoice(input, btn, note) {
  const t = tr();
  if (recognizing) { recognizing.stop(); return; }
  if (!SR) { note.textContent = t.noSR; return; }
  const r = new SR();
  r.btn = btn;
  r.lang = langInfo().locale;             // exactly the selected language; never silently switched
  r.interimResults = true; r.continuous = false;
  const base = input.value ? input.value.trim() + " " : "";
  let gotResult = false, failed = false;
  r.onstart = () => { recognizing = r; btn.setAttribute("aria-pressed", "true"); btn.textContent = t.stop; note.textContent = t.listening; };
  r.onresult = (ev) => { gotResult = true; input.value = base + Array.from(ev.results).map((x) => x[0].transcript).join(" "); };
  r.onerror = (ev) => {
    failed = true;
    const key = Voice.speechErrorKey(ev.error);
    note.textContent = key ? tr()[key] : "";
  };
  r.onend = () => {
    recognizing = null; btn.setAttribute("aria-pressed", "false"); btn.textContent = tr().speak;
    if (!failed) note.textContent = gotResult ? "" : tr().voiceNoResult;
  };
  try { r.start(); } catch (e) { note.textContent = tr().voiceError; }
}

// ---------- voice output ----------
function currentVoice() {
  if (!("speechSynthesis" in window)) return { state: "none" };
  const voices = speechSynthesis.getVoices();
  if (!voices.length) return { state: "unknown" };   // list not loaded yet (or browser hides it)
  const hit = Voice.pickVoice(voices, langInfo().locale);
  return hit ? { state: "ok", ...hit } : { state: "none" };
}

function updateReadButton() {
  const rb = $("read-btn");
  if (!rb) return;
  const v = currentVoice(), t = tr();
  rb.textContent = t.readAloud;
  rb.disabled = v.state === "none";
  $("voice-note").textContent = v.state === "none" ? t.noVoice : "";
}
if ("speechSynthesis" in window) speechSynthesis.addEventListener("voiceschanged", updateReadButton);

function readAloud(text) {
  const t = tr(), v = currentVoice();
  if (v.state === "none") { $("voice-note").textContent = t.noVoice; return false; }
  speechSynthesis.cancel();
  const u = new SpeechSynthesisUtterance(text);
  u.lang = v.state === "ok" ? v.voice.lang : langInfo().locale;
  if (v.state === "ok") u.voice = v.voice;
  u.onend = () => { const rb = $("read-btn"); if (rb) rb.textContent = tr().readAloud; };
  u.onerror = (e) => { if (e.error !== "canceled" && e.error !== "interrupted") $("voice-note").textContent = tr().speechFailed; const rb = $("read-btn"); if (rb) rb.textContent = tr().readAloud; };
  speechSynthesis.speak(u);
  return true;
}

// ---------- rendering ----------
// Display label for a profile value in the selected language (display only: the profile keeps canonical English values).
function valueLabel(field, v) {
  const keys = VALUE_KEYS[field];
  if (!keys) return humanize(v);                         // free text (e.g. crop) or numbers: shown as-is
  let canon = String(v).trim().toLowerCase();
  if (field === "state" && STATE_ALIASES[canon]) canon = STATE_ALIASES[canon].toLowerCase();
  const i = keys.findIndex((k) => k.toLowerCase() === canon);
  if (i < 0) return humanize(v);                         // unknown value: never invent a translation
  const own = (VALUE_LABELS[lang] || {})[field], en = (VALUE_LABELS.en || {})[field];
  return (own && own[i]) || (en && en[i]) || humanize(v);
}

function fmt(field, v) {
  const t = tr();
  if (v === true) return "✓ " + t.yes;
  if (v === false) return "✗ " + t.no;
  return valueLabel(field, v);
}

function renderProfile(p) {
  const dl = $("profile"); dl.replaceChildren();
  Object.entries(p).filter(([, v]) => v !== null).forEach(([k, v]) => {
    const d = el("div"); d.append(el("dt", tr().fields[k] || k), el("dd", fmt(k, v))); dl.append(d);
  });
  $("profile-sec").classList.toggle("hidden", dl.children.length === 0);
}

function renderCards(report, loc) {
  const t = tr(), box = $("cards"); box.replaceChildren();
  report.results.forEach((r) => {
    const c = el("article", undefined, `scheme ${r.status}`);
    c.append(el("h3", r.name));  // official scheme name, never translated
    const line = el("div"); line.append(el("span", `${ICON[r.status]} ${t.status[r.status]}`, "badge"),
      el("span", `${t.verified}: ${r.last_verified}`, "stamp")); c.append(line);
    c.append(el("p", loc.scheme_explanations[r.scheme_id] || r.explanation));

    const why = el("details"); why.append(el("summary", t.why));
    if (r.matched_rules.length) { why.append(el("strong", t.matched), list(r.matched_rules)); }
    if (r.failed_rules.length) { why.append(el("strong", t.notMet), list(r.failed_rules)); }
    if (r.missing_fields.length) { why.append(el("strong", t.missing), list(r.missing_fields.map((f) => t.fields[f] || f))); }
    if (r.manual_checks.length) { why.append(el("strong", t.confirm), list(r.manual_checks)); }
    c.append(why);

    const docs = el("details"); docs.append(el("summary", t.docsNeeded), list(r.required_documents)); c.append(docs);
    const a = el("a", t.verify, "verify"); a.href = r.official_url; a.target = "_blank"; a.rel = "noopener noreferrer";
    c.append(a); box.append(c);
  });
}

function renderDocs(items) {
  const ul = $("docs"); ul.replaceChildren();
  items.forEach((d) => {
    const li = el("li"), mark = d.have === true ? "✓" : d.have === false ? "✗" : "○";
    li.append(el("span", `${mark} ${d.document}`), el("span", `${tr().forSchemes}: ${d.needed_for.join(", ")}`, "for"));
    ul.append(li);
  });
  $("docs-sec").classList.toggle("hidden", items.length === 0);
}

function render(data) {
  const { report, localized: loc } = data;
  profile = data.profile;
  asked = loc.follow_up_questions;
  renderProfile(profile); renderCards(report, loc); renderDocs(report.checklist);
  $("map-sec").classList.remove("hidden");
  const sm = $("summary"); sm.textContent = loc.summary; sm.classList.toggle("hidden", !loc.summary);
  const q = $("questions"); q.replaceChildren(); asked.forEach((x) => q.append(el("li", x)));
  // The ONLY way to build on an earlier profile: this separate box, whose button sends an explicit update.
  $("t-update-title").textContent = asked.length ? tr().followup : tr().updateTitle;
  $("update-sec").classList.remove("hidden");
  const d = $("disclaimer"); d.textContent = report.disclaimer; d.classList.remove("hidden");
  noticeForAI(data);

  lastReadText = [loc.summary, ...report.results.filter((r) => r.status !== "NOT_ELIGIBLE")
    .map((r) => `${r.name}. ${tr().status[r.status]}. ${loc.scheme_explanations[r.scheme_id] || r.explanation}`),
    ...asked].filter(Boolean).join(" ");
  let rb = $("read-btn");
  if (!rb) {
    rb = el("button", "", "big secondary"); rb.id = "read-btn"; rb.type = "button";
    $("map-sec").insertBefore(rb, $("voice-note"));
    rb.onclick = () => {
      if ("speechSynthesis" in window && speechSynthesis.speaking) { speechSynthesis.cancel(); rb.textContent = tr().readAloud; return; }
      if (readAloud(lastReadText)) rb.textContent = tr().stopReading;
    };
  }
  if (!("speechSynthesis" in window)) rb.classList.add("hidden");
  updateReadButton();
}

// ---------- API ----------
async function post(path, body) {
  const res = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  let data = null;
  try { data = await res.json(); } catch (e) { /* non-JSON error page */ }
  return { ok: res.ok, status: res.status, data };
}

function showApiFailure(r) {
  const t = tr(), err = r.data && r.data.error;
  // friendly, localized text for the two "slow down" cases; everything else uses the generic AI-unavailable text
  const friendly = { rate_limited: t.aiBusy, too_many_requests: t.tooMany }[err && err.code] || t.aiDown;
  if (err) showNotice(friendly, `${t.reason}: ${err.code}${err.message ? " — " + err.message : ""}`, true);
  else showNotice(t.err, r.status ? `HTTP ${r.status}` : "", true);
}

// Main box = ALWAYS a fresh request; answer box = explicit update of the profile on screen.
const submitFresh = () => submit($("input"), $("go"), false);
const submitUpdate = () => submit($("answer-input"), $("answer-go"), true);

async function submit(inputEl, button, update) {
  const text = inputEl.value.trim();
  if (!text) { inputEl.focus(); return; }
  if (update && profile === null) return;      // nothing to update yet
  const seq = ++requestSeq, go = button;
  go.disabled = true; $("status").textContent = tr().working; clearNotice();
  try {
    // Fresh: ONLY the text and the selected language. The previous profile/questions are attached
    // exclusively by the explicit update button.
    const body = update ? { text, language: lang, mode: "update", profile, asked }
                        : { text, language: lang, mode: "new" };
    const r = await post("/api/analyze", body);
    if (seq !== requestSeq) return;
    if (!r.ok) { showApiFailure(r); return; }   // previous results stay on screen; language stays selected
    render(r.data);
    inputEl.value = ""; if (!update) $("answer-input").value = "";
  } catch (e) {
    if (seq === requestSeq) showNotice(tr().err, String(e.message || e), true);
  } finally {
    if (seq === requestSeq) { go.disabled = false; $("status").textContent = ""; }
  }
}

// Throw away everything belonging to the current interaction. The selected language is NOT touched.
function resetInteraction() {
  requestSeq++;                                   // any in-flight response is now ignored
  profile = null; asked = []; lastReadText = "";
  if (recognizing) {                              // stop dictation without letting late results write anywhere
    const r = recognizing; recognizing = null;
    r.onresult = r.onerror = r.onend = null;
    try { r.stop(); } catch (e) { /* already stopped */ }
  }
  if ("speechSynthesis" in window) speechSynthesis.cancel();
  $("input").value = ""; $("answer-input").value = "";
  for (const id of ["profile", "cards", "docs", "questions"]) $(id).replaceChildren();
  for (const id of ["summary", "disclaimer"]) $(id).textContent = "";
  for (const id of ["profile-sec", "map-sec", "docs-sec", "update-sec", "summary", "disclaimer"]) $(id).classList.add("hidden");
  for (const id of ["status", "speech-note", "answer-note", "voice-note"]) $(id).textContent = "";
  clearNotice();
  const rb = $("read-btn"); if (rb) rb.remove();
  for (const id of ["speak", "answer-speak"]) $(id).setAttribute("aria-pressed", "false");
  for (const id of ["go", "answer-go"]) $(id).disabled = false;
}

// Language switch = a NEW interaction. It changes language/UI/speech settings and clears the screen.
// It makes NO API call, never re-explains the old profile, and nothing old is carried into the next request.
function onLanguageChange(newLang) {
  lang = newLang;
  resetInteraction();
  applyLang();
}

async function init() {
  try {
    LANGS = await (await fetch("/api/languages")).json();
  } catch (e) { LANGS = [{ code: "en", english: "English", native: "English", locale: "en-IN", rtl: false }]; }
  const sel = $("lang"); sel.replaceChildren();
  LANGS.forEach((l) => { const o = el("option", l.native); o.value = l.code; o.lang = l.code; sel.append(o); });
  sel.onchange = () => onLanguageChange(sel.value);
  $("go").onclick = submitFresh;
  $("answer-go").onclick = submitUpdate;
  $("speak").onclick = () => toggleVoice($("input"), $("speak"), $("speech-note"));
  $("answer-speak").onclick = () => toggleVoice($("answer-input"), $("answer-speak"), $("answer-note"));
  $("input").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) submitFresh(); });
  $("answer-input").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) submitUpdate(); });
  applyLang();
}
init();
