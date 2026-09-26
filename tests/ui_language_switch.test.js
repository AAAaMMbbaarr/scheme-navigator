// Regression tests: switching language starts a NEW interaction.
//   - the screen is cleared (input, profile, results, checklist, questions, explanation, errors, update box)
//   - the switch itself makes ZERO API calls (no re-explanation, no extraction)
//   - the next request is completely fresh: no previous profile, no questions, no history
//   - the selected language is kept, speech locale follows it
//
// Runs the real frontend/app.js in jsdom against a stub API server that records every request.
// Needs `npm install` (jsdom). Speech APIs are stubbed: this checks our logic, not real audio.
const test = require("node:test");
const assert = require("node:assert");
const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM } = require("jsdom");

const FRONTEND = path.join(__dirname, "..", "frontend");
const BN = "আমি পশ্চিমবঙ্গের একজন কৃষক। আমার ২ একর জমি আছে। আমার আধার এবং ব্যাংক অ্যাকাউন্ট আছে।";
const HI = "मैं पश्चिम बंगाल का किसान हूँ। मेरे पास 2 एकड़ जमीन है।";
const EN = "I am a farmer from West Bengal. I have 2 acres of land.";
const LANGS = [["en", "English", "en-IN"], ["hi", "हिन्दी", "hi-IN"], ["bn", "বাংলা", "bn-IN"], ["mr", "मराठी", "mr-IN"],
  ["te", "తెలుగు", "te-IN"], ["ta", "தமிழ்", "ta-IN"], ["gu", "ગુજરાતી", "gu-IN"], ["ur", "اردو", "ur-IN"],
  ["kn", "ಕನ್ನಡ", "kn-IN"], ["or", "ଓଡ଼ିଆ", "or-IN"], ["ml", "മലയാളം", "ml-IN"]];

// ---- stub backend: serves the real frontend files and records every /api call ----
function startStub() {
  const calls = [];
  const state = { delayMs: 0 };
  const analysis = (body) => {
    const profile = { state: "West Bengal", occupation: "farmer", age: null, gender: null, land_area_acres: 2,
      land_ownership: null, crop: null, annual_family_income: null, has_aadhaar: true, has_bank_account: true,
      is_income_taxpayer: null, is_govt_employee: null };
    const q = ["Is anyone in your family a government employee or pensioner?"];
    return {
      profile, mode: body.mode, language: body.language, extraction_source: "llm",
      ai: { ok: true, code: null, message: null },
      report: { profile, follow_up_fields: ["is_govt_employee"], follow_up_questions: q,
        checklist: [{ document: "Aadhaar", have: true, needed_for: ["PM-KISAN (Pradhan Mantri Kisan Samman Nidhi)"] }],
        disclaimer: "informational only",
        results: [{ scheme_id: "pm_kisan", name: "PM-KISAN (Pradhan Mantri Kisan Samman Nidhi)", status: "LIKELY_ELIGIBLE",
          explanation: "engine text", matched_rules: ["Holds some agricultural land"], failed_rules: [], missing_fields: [],
          manual_checks: ["check"], required_documents: ["Aadhaar"], official_url: "https://pmkisan.gov.in/", last_verified: "2026-09-25" }] },
      localized: { summary: `SUMMARY[${body.language}]`, follow_up_questions: q.map((x) => `Q[${body.language}] ${x}`),
        scheme_explanations: { pm_kisan: `EXPLAIN[${body.language}]` }, localized: true, error_code: null },
    };
  };
  const server = http.createServer((req, res) => {
    let raw = "";
    req.on("data", (c) => (raw += c));
    req.on("end", () => {
      const url = req.url.split("?")[0];
      const send = (code, obj) => { res.writeHead(code, { "Content-Type": "application/json" }); res.end(JSON.stringify(obj)); };
      if (url === "/api/languages") return send(200, LANGS.map(([code, native, locale]) => ({ code, english: code, native, locale, rtl: code === "ur" })));
      if (url.startsWith("/api/")) {
        const body = raw ? JSON.parse(raw) : null;
        calls.push({ url, body });
        const respond = () => {
          if (body && body.text === "FAIL") return send(503, { error: { code: "api_error", message: "AI service is temporarily unavailable.", language: body.language } });
          if (body && body.text === "RATE") return send(503, { error: { code: "rate_limited", message: "The AI service is busy right now (rate limit reached).", language: body.language } });
          if (body && body.text === "MANY") return send(429, { error: { code: "too_many_requests", message: "Too many requests. Please wait 30 seconds and try again." } });
          return send(200, analysis(body));
        };
        return state.delayMs ? setTimeout(respond, state.delayMs) : respond();
      }
      const file = url === "/" ? "index.html" : url.replace("/static/", "");
      const full = path.join(FRONTEND, path.basename(file));
      if (!fs.existsSync(full)) { res.writeHead(404); return res.end(); }
      const types = { ".html": "text/html", ".js": "application/javascript", ".css": "text/css" };
      res.writeHead(200, { "Content-Type": (types[path.extname(full)] || "text/plain") + "; charset=utf-8" });
      res.end(fs.readFileSync(full));
    });
  });
  return new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve({ server, calls, state, base: `http://127.0.0.1:${server.address().port}` })));
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function until(fn, ms = 4000) { const t = Date.now(); while (Date.now() - t < ms) { if (fn()) return true; await sleep(15); } return false; }

async function openPage(stub, stubs) {
  const html = (await (await fetch(stub.base + "/")).text()).replace(/<link[^>]*fonts[^>]*>/g, "");
  const dom = new JSDOM(html, { url: stub.base + "/", runScripts: "dangerously", resources: "usable", pretendToBeVisual: true,
    beforeParse(w) { w.fetch = (u, o) => fetch(new URL(u, stub.base), o); if (stubs) stubs(w); } });
  const d = dom.window.document, $ = (id) => d.getElementById(id);
  await until(() => $("lang").options.length === 11);
  stub.calls.length = 0;  // ignore /api/languages etc.; count only what the test does
  const ui = { w: dom.window, d, $, close: () => dom.window.close() };
  ui.submit = async (text) => { $("input").value = text; $("go").click(); await sleep(20); await until(() => !$("go").disabled && $("status").textContent === ""); };
  ui.switchTo = async (code) => { $("lang").value = code; $("lang").dispatchEvent(new dom.window.Event("change")); await sleep(60); };
  ui.visible = (id) => !$(id).classList.contains("hidden");
  return ui;
}

// Everything that belongs to "the current interaction" must be empty.
function assertClean(ui, code, why) {
  const { $ } = ui;
  const m = (x) => `${why}: ${x}`;
  assert.equal($("input").value, "", m("main input not cleared"));
  assert.equal($("answer-input").value, "", m("answer input not cleared"));
  assert.equal($("profile").children.length, 0, m("profile not cleared"));
  assert.equal($("cards").children.length, 0, m("results not cleared"));
  assert.equal($("docs").children.length, 0, m("checklist not cleared"));
  assert.equal($("questions").children.length, 0, m("follow-up questions not cleared"));
  assert.equal($("summary").textContent, "", m("AI summary not cleared"));
  assert.equal($("disclaimer").textContent, "", m("disclaimer not cleared"));
  assert.ok(!ui.visible("notice") && $("notice-main").textContent === "" && $("notice-detail").textContent === "", m("error/notice not cleared"));
  assert.equal($("status").textContent, "", m("status not cleared"));
  for (const id of ["profile-sec", "map-sec", "docs-sec", "update-sec"]) assert.ok(!ui.visible(id), m(`${id} still visible`));
  assert.equal($("read-btn"), null, m("read-aloud button not removed"));
  assert.equal($("lang").value, code, m("selected language was lost"));
  assert.equal(ui.d.documentElement.lang, code, m("<html lang> not updated"));
  const page = ui.d.body.cloneNode(true);
  page.querySelector("#examples").remove();               // example chips are static UI, not interaction state
  assert.ok(!/EXPLAIN\[|SUMMARY\[|Q\[|PM-KISAN|West Bengal/.test(page.textContent), m("old results/profile text still on page"));
  assert.ok(!/[ঀ-৿]{3}/.test($("input").value + $("answer-input").value), m("Bengali text remains in an input"));
}

// ---------------------------------------------------------------------------------------------
test("Bengali -> Hindi: clean screen, zero API calls, next request fully independent", async (t) => {
  const stub = await startStub(); const ui = await openPage(stub);
  t.after(() => { ui.close(); stub.server.close(); });

  await ui.switchTo("bn");
  ui.$("input").value = BN;
  assert.equal(ui.$("input").value, BN, "Bengali input is displayed");

  ui.$("go").click(); await sleep(20); await until(() => !ui.$("go").disabled && ui.$("cards").children.length > 0);
  assert.equal(stub.calls.length, 1);
  assert.deepEqual(stub.calls[0].body, { text: BN, language: "bn", mode: "new" });
  assert.ok(ui.$("profile").children.length > 0 && ui.$("cards").children.length > 0, "profile/results exist");
  assert.ok(ui.$("questions").children.length > 0 && ui.visible("update-sec"), "follow-up questions exist");
  assert.match(ui.$("cards").textContent, /EXPLAIN\[bn\]/);
  ui.$("input").value = "some unsent bengali draft " + BN;        // text still sitting in the box
  ui.$("answer-input").value = "আমার জমি আসলে ৫ একর";              // pending 'correct my details' text

  stub.calls.length = 0;
  await ui.switchTo("hi");
  assert.equal(stub.calls.length, 0, "language switch must make ZERO API calls");
  assertClean(ui, "hi", "bn->hi");
  assert.match(ui.$("t-title").textContent, /सरकारी/, "UI is now Hindi");
  assert.equal(ui.$("go").textContent, "मेरी योजनाएँ खोजें");
  assert.match(ui.$("speak").textContent, /बोलिए/);
  assert.equal(ui.$("go").disabled, false);

  // the update box is not usable any more: nothing to update -> no request even if triggered programmatically
  ui.$("answer-input").value = "x"; ui.$("answer-go").click(); await sleep(60);
  assert.equal(stub.calls.length, 0, "no update request possible after a language switch");
  ui.$("answer-input").value = "";

  // next request: Hindi, completely independent of the Bengali interaction
  ui.$("input").value = HI; ui.$("go").click(); await sleep(20); await until(() => ui.$("cards").children.length > 0);
  assert.equal(stub.calls.length, 1);
  assert.deepEqual(stub.calls[0].body, { text: HI, language: "hi", mode: "new" }, "no profile / asked / history in the request");
  assert.ok(!JSON.stringify(stub.calls[0].body).match(/profile|asked|West Bengal|আমি/), "nothing from Bengali leaked");
  assert.match(ui.$("cards").textContent, /EXPLAIN\[hi\]/);
  assert.ok(!/EXPLAIN\[bn\]/.test(ui.d.body.textContent), "no Bengali explanation remains");
});

test("every switch is a reset with zero calls: bn->en, en->hi, hi->bn (and back)", async (t) => {
  const stub = await startStub(); const ui = await openPage(stub);
  t.after(() => { ui.close(); stub.server.close(); });
  const texts = { bn: BN, en: EN, hi: HI };
  let current = "bn"; await ui.switchTo("bn");
  for (const next of ["en", "hi", "bn", "en", "bn"]) {
    await ui.submit(texts[current]);
    assert.ok(ui.$("cards").children.length > 0, `${current}: results shown`);
    ui.$("input").value = "leftover " + texts[current];
    const before = stub.calls.length;
    await ui.switchTo(next);
    assert.equal(stub.calls.length, before, `${current}->${next} made an API call`);
    assertClean(ui, next, `${current}->${next}`);
    // next fresh request carries nothing from the previous interaction
    await ui.submit(texts[next]);
    const last = stub.calls[stub.calls.length - 1];
    assert.deepEqual(last.body, { text: texts[next], language: next, mode: "new" }, `${current}->${next}: request not clean`);
    current = next;
  }
  assert.ok(!stub.calls.some((c) => c.url === "/api/explain"), "the UI must never call /api/explain");
});

test("switching while a request is in flight: late response is ignored, screen stays clean, button usable", async (t) => {
  const stub = await startStub(); stub.state.delayMs = 250;
  const ui = await openPage(stub);
  t.after(() => { ui.close(); stub.server.close(); });
  await ui.switchTo("bn");
  ui.$("input").value = BN; ui.$("go").click(); await sleep(40);
  assert.equal(stub.calls.length, 1);
  await ui.switchTo("hi");                      // response for the Bengali request is still pending
  await sleep(500);                             // ...and now it arrives
  assertClean(ui, "hi", "in-flight bn request");
  assert.equal(ui.$("go").disabled, false, "go button must be usable again");
  stub.state.delayMs = 0;
  await ui.submit(HI);
  assert.deepEqual(stub.calls[stub.calls.length - 1].body, { text: HI, language: "hi", mode: "new" });
});

test("error banner from a failed request is cleared by a language switch", async (t) => {
  const stub = await startStub(); const ui = await openPage(stub);
  t.after(() => { ui.close(); stub.server.close(); });
  await ui.switchTo("bn");
  await ui.submit("FAIL");
  assert.ok(ui.visible("notice") && /api_error/.test(ui.$("notice-detail").textContent), "error shown");
  const before = stub.calls.length;
  await ui.switchTo("hi");
  assert.equal(stub.calls.length, before);
  assertClean(ui, "hi", "after error");
});

test("explicit update still works within one language (stale protection unchanged)", async (t) => {
  const stub = await startStub(); const ui = await openPage(stub);
  t.after(() => { ui.close(); stub.server.close(); });
  await ui.switchTo("bn"); await ui.submit(BN);
  ui.$("answer-input").value = "আমার জমি আসলে ৫ একর"; ui.$("answer-go").click(); await sleep(20);
  await until(() => !ui.$("answer-go").disabled);
  const body = stub.calls[stub.calls.length - 1].body;
  assert.equal(body.mode, "update"); assert.equal(body.profile.state, "West Bengal"); assert.ok(Array.isArray(body.asked));
  await ui.switchTo("hi"); await ui.submit(HI);            // and a fresh request afterwards is clean again
  assert.deepEqual(stub.calls[stub.calls.length - 1].body, { text: HI, language: "hi", mode: "new" });
});

test("language switch updates the speech locale, stops dictation, and drops late speech results", async (t) => {
  const stub = await startStub(); const made = [];
  class SR { constructor() { this.lang = ""; this.stopped = false; made.push(this); }
    start() { setTimeout(() => this.onstart && this.onstart(), 5); } stop() { this.stopped = true; } }
  const ui = await openPage(stub, (w) => { w.SpeechRecognition = SR; });
  t.after(() => { ui.close(); stub.server.close(); });
  await ui.switchTo("bn"); ui.$("speak").click(); await sleep(40);
  assert.equal(made[0].lang, "bn-IN");
  assert.equal(ui.$("speak").getAttribute("aria-pressed"), "true");
  const r = made[0];
  await ui.switchTo("hi");                                   // switch while still listening
  assert.ok(r.stopped, "dictation stopped");
  assert.equal(r.onresult, null, "late results are detached");
  assert.equal(ui.$("speak").getAttribute("aria-pressed"), "false");
  assert.equal(ui.$("input").value, "");
  ui.$("speak").click(); await sleep(40);
  assert.equal(made[1].lang, "hi-IN", "recognition locale follows the new language");
  assert.equal(stub.calls.length, 0);
});

test("speech synthesis locale follows the new language after a reset", async (t) => {
  const stub = await startStub(); const spoken = [];
  const voices = [{ lang: "bn-IN", name: "BnV" }, { lang: "hi-IN", name: "HiV" }];
  const ui = await openPage(stub, (w) => {
    w.SpeechSynthesisUtterance = function (x) { this.text = x; };
    w.speechSynthesis = { getVoices: () => voices, speak: (u) => spoken.push(u), cancel() {}, speaking: false, addEventListener() {} };
  });
  t.after(() => { ui.close(); stub.server.close(); });
  await ui.switchTo("bn"); await ui.submit(BN);
  await ui.switchTo("hi"); await ui.submit(HI);
  ui.$("read-btn").click();
  assert.equal(spoken.length, 1);
  assert.equal(spoken[0].voice.name, "HiV"); assert.equal(spoken[0].lang, "hi-IN");
  assert.ok(!/EXPLAIN\[bn\]/.test(spoken[0].text), "nothing Bengali is read aloud");
});

test("selecting the same language again also resets (dropdown re-pick is a new interaction)", async (t) => {
  const stub = await startStub(); const ui = await openPage(stub);
  t.after(() => { ui.close(); stub.server.close(); });
  await ui.switchTo("hi"); await ui.submit(HI);
  const before = stub.calls.length;
  await ui.switchTo("hi");
  assert.equal(stub.calls.length, before); assertClean(ui, "hi", "hi->hi");
});

test("site-wide disclaimer is always visible, follows the language, and is never cleared by a switch", async (t) => {
  const stub = await startStub(); const ui = await openPage(stub);
  t.after(() => { ui.close(); stub.server.close(); });
  const text = () => ui.$("site-disclaimer").textContent;
  assert.match(text(), /not a government website/);
  await ui.switchTo("hi"); assert.match(text(), /सरकारी वेबसाइट नहीं/);
  await ui.submit(HI); assert.match(text(), /सरकारी वेबसाइट नहीं/);
  await ui.switchTo("bn"); assert.match(text(), /সরকারি ওয়েবসাইট নয়/);
  await ui.switchTo("ta"); assert.match(text(), /not a government website/, "languages without a translation fall back to English, never blank");
  assert.equal(stub.calls.length, 1, "only the one real submit made an API call");
});

test("rate-limit errors show friendly localized messages, not raw codes alone", async (t) => {
  const stub = await startStub(); const ui = await openPage(stub);
  t.after(() => { ui.close(); stub.server.close(); });
  await ui.switchTo("hi");
  await ui.submit("RATE");
  assert.match(ui.$("notice-main").textContent, /व्यस्त/);
  assert.match(ui.$("notice-detail").textContent, /rate_limited/);
  await ui.submit("MANY");
  assert.match(ui.$("notice-main").textContent, /बहुत अधिक अनुरोध/);
  await ui.switchTo("en"); await ui.submit("RATE");
  assert.equal(ui.$("notice-main").textContent, "The AI service is busy right now. Please wait a minute and try again.");
  assert.equal(ui.$("go").disabled, false, "the user can retry");
});
