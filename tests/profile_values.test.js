// Profile VALUES (state, occupation, gender, land ownership) are shown in the selected language.
// This is display-only: the profile keeps canonical English values, which is what the server and rules engine see.
// Needs `npm install` (jsdom).
const test = require("node:test");
const assert = require("node:assert");
const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { JSDOM } = require("jsdom");

const FRONTEND = path.join(__dirname, "..", "frontend");
const ctx = { window: {} };
vm.runInNewContext(fs.readFileSync(path.join(FRONTEND, "values.js"), "utf8"), ctx);
const { VALUE_KEYS, VALUE_LABELS, STATE_ALIASES } = ctx.window;
const CODES = ["en", "hi", "bn", "mr", "te", "ta", "gu", "ur", "kn", "or", "ml"];
const OTHERS = CODES.filter((c) => c !== "en");
const FIELDS = ["occupation", "gender", "land_ownership", "state"];

// ---------------------------------------------------------------- data integrity
test("every non-English language has a complete, aligned, non-empty list for every field", () => {
  for (const c of OTHERS) {
    for (const f of FIELDS) {
      const list = VALUE_LABELS[c][f];
      assert.ok(Array.isArray(list), `${c}.${f} missing`);
      assert.equal(list.length, VALUE_KEYS[f].length, `${c}.${f}: ${list.length} labels for ${VALUE_KEYS[f].length} keys`);
      assert.ok(list.every((x) => typeof x === "string" && x.trim().length > 0), `${c}.${f} has an empty label`);
      assert.equal(new Set(list).size, list.length, `${c}.${f} has duplicate labels (misaligned list?)`);
    }
  }
});

test("labels are in the native script, not English, and English has readable labels for the enums", () => {
  for (const c of OTHERS)
    for (const f of FIELDS)
      VALUE_LABELS[c][f].forEach((label, i) => assert.ok(/[^\x00-\x7F]/.test(label), `${c}.${f}[${i}] '${label}' has no native characters`));
  for (const f of ["occupation", "gender", "land_ownership"]) assert.equal(VALUE_LABELS.en[f].length, VALUE_KEYS[f].length);
});

test("spot checks: the important West Bengal / farmer / owner values are right", () => {
  const at = (c, f, key) => VALUE_LABELS[c][f][VALUE_KEYS[f].indexOf(key)];
  assert.equal(at("bn", "state", "West Bengal"), "পশ্চিমবঙ্গ");
  assert.equal(at("hi", "state", "West Bengal"), "पश्चिम बंगाल");
  assert.equal(at("bn", "occupation", "farmer"), "কৃষক");
  assert.equal(at("hi", "occupation", "farmer"), "किसान");
  assert.equal(at("hi", "state", "Bihar"), "बिहार");
  assert.equal(at("hi", "land_ownership", "owner"), "मालिक");
  assert.equal(at("bn", "land_ownership", "sharecropper"), "বর্গাদার");
  assert.equal(at("ta", "state", "Tamil Nadu"), "தமிழ்நாடு");
  assert.equal(at("ml", "state", "Kerala"), "കേരളം");
});

test("state keys are unique and aliases point at real keys", () => {
  assert.equal(new Set(VALUE_KEYS.state).size, VALUE_KEYS.state.length);
  for (const target of Object.values(STATE_ALIASES)) assert.ok(VALUE_KEYS.state.includes(target), target);
});

// ---------------------------------------------------------------- UI (real app.js against a stub API)
function startStub(profileFor) {
  const calls = [];
  const langs = CODES.map((code) => ({ code, english: code, native: code, locale: `${code}-IN`, rtl: code === "ur" }));
  const server = http.createServer((req, res) => {
    let raw = ""; req.on("data", (c) => (raw += c));
    req.on("end", () => {
      const url = req.url.split("?")[0], send = (o) => { res.writeHead(200, { "Content-Type": "application/json" }); res.end(JSON.stringify(o)); };
      if (url === "/api/languages") return send(langs);
      if (url === "/api/analyze") {
        const body = JSON.parse(raw); calls.push(body);
        const profile = { state: null, occupation: null, age: null, gender: null, land_area_acres: null, land_ownership: null, crop: null,
          annual_family_income: null, has_aadhaar: null, has_bank_account: null, is_income_taxpayer: null, is_govt_employee: null, ...profileFor(body.text) };
        return send({ profile, mode: body.mode, language: body.language, extraction_source: "claude", ai: { ok: true },
          report: { profile, results: [], follow_up_fields: [], follow_up_questions: [], checklist: [], disclaimer: "d" },
          localized: { summary: "", follow_up_questions: [], scheme_explanations: {}, localized: true, error_code: null } });
      }
      const file = url === "/" ? "index.html" : url.replace("/static/", ""), full = path.join(FRONTEND, path.basename(file));
      if (!fs.existsSync(full)) { res.writeHead(404); return res.end(); }
      const types = { ".html": "text/html", ".js": "application/javascript", ".css": "text/css" };
      res.writeHead(200, { "Content-Type": (types[path.extname(full)] || "text/plain") + "; charset=utf-8" }); res.end(fs.readFileSync(full));
    });
  });
  return new Promise((r) => server.listen(0, "127.0.0.1", () => r({ server, calls, base: `http://127.0.0.1:${server.address().port}` })));
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function until(fn, ms = 4000) { const t = Date.now(); while (Date.now() - t < ms) { if (fn()) return true; await sleep(15); } return false; }

async function open(stub) {
  const html = (await (await fetch(stub.base + "/")).text()).replace(/<link[^>]*fonts[^>]*>/g, "");
  const dom = new JSDOM(html, { url: stub.base + "/", runScripts: "dangerously", resources: "usable", pretendToBeVisual: true,
    beforeParse(w) { w.fetch = (u, o) => fetch(new URL(u, stub.base), o); } });
  const $ = (id) => dom.window.document.getElementById(id);
  await until(() => $("lang").options.length === CODES.length);
  const values = () => Object.fromEntries([...$("profile").querySelectorAll("div")].map((d) => [d.children[0].textContent, d.children[1].textContent]));
  return {
    $, values, close: () => dom.window.close(),
    async say(lang, text) {
      $("lang").value = lang; $("lang").dispatchEvent(new dom.window.Event("change")); await sleep(40);
      $("input").value = text; $("go").click(); await sleep(20);
      await until(() => !$("go").disabled && $("status").textContent === "" && $("profile").children.length > 0);
    },
  };
}
const FULL = { state: "West Bengal", occupation: "farmer", gender: "female", land_ownership: "tenant", land_area_acres: 2, has_aadhaar: true, crop: "rice" };

test("Bengali, Hindi and English show the profile in the selected language", async (t) => {
  const stub = await startStub(() => FULL), ui = await open(stub);
  t.after(() => { ui.close(); stub.server.close(); });
  await ui.say("bn", "x");
  const bn = Object.values(ui.values());
  for (const v of ["পশ্চিমবঙ্গ", "কৃষক", "মহিলা", "ভাড়াটিয়া"]) assert.ok(bn.includes(v), `bn missing ${v}: ${bn}`);
  assert.ok(!bn.includes("West Bengal") && !bn.includes("Farmer"), "English values must not remain in Bengali mode");
  await ui.say("hi", "x");
  const hi = Object.values(ui.values());
  for (const v of ["पश्चिम बंगाल", "किसान", "महिला", "किरायेदार"]) assert.ok(hi.includes(v), `hi missing ${v}: ${hi}`);
  await ui.say("en", "x");
  const en = Object.values(ui.values());
  for (const v of ["West Bengal", "Farmer", "Female", "Tenant"]) assert.ok(en.includes(v), `en missing ${v}: ${en}`);
});

test("all 11 languages render translated values (none left in English except English itself)", async (t) => {
  const stub = await startStub(() => FULL), ui = await open(stub);
  t.after(() => { ui.close(); stub.server.close(); });
  for (const c of CODES) {
    await ui.say(c, "x");
    const vals = Object.values(ui.values());
    const shown = [VALUE_LABELS[c].state?.[VALUE_KEYS.state.indexOf("West Bengal")] || "West Bengal", VALUE_LABELS[c].occupation[0], VALUE_LABELS[c].gender[0], VALUE_LABELS[c].land_ownership[1]];
    for (const v of shown) assert.ok(vals.includes(v), `${c}: expected '${v}' in ${JSON.stringify(vals)}`);
    if (c !== "en") assert.ok(!vals.includes("West Bengal") && !vals.includes("Farmer"), `${c}: English value leaked`);
  }
});

test("unknown values are never invented; free text and numbers are untouched; booleans still translate", async (t) => {
  const stub = await startStub((text) => text === "odd" ? { state: "Atlantis", occupation: "astronaut", crop: "rice" } : text === "orissa" ? { state: "Orissa" } : FULL);
  const ui = await open(stub);
  t.after(() => { ui.close(); stub.server.close(); });
  await ui.say("hi", "odd");
  const odd = Object.values(ui.values());
  assert.ok(odd.includes("Atlantis") && odd.includes("Astronaut"), "unknown state/occupation shown as-is: " + odd);
  assert.ok(odd.includes("Rice"), "free-text crop unchanged");
  await ui.say("hi", "orissa");
  assert.ok(Object.values(ui.values()).includes("ओडिशा"), "alias 'Orissa' maps to Odisha");
  await ui.say("hi", "full");
  const full = ui.values();
  assert.equal(full["ज़मीन (एकड़)"], "2");
  assert.equal(full["आधार"], "✓ हाँ");
});

test("display translation never changes the data sent to the server (canonical English profile on update)", async (t) => {
  const stub = await startStub(() => FULL), ui = await open(stub);
  t.after(() => { ui.close(); stub.server.close(); });
  await ui.say("bn", "x");
  ui.$("answer-input").value = "আমার জমি আসলে ৫ একর"; ui.$("answer-go").click(); await sleep(20); await until(() => !ui.$("answer-go").disabled);
  const upd = stub.calls[stub.calls.length - 1];
  assert.equal(upd.mode, "update");
  assert.equal(upd.profile.state, "West Bengal"); assert.equal(upd.profile.occupation, "farmer");
  assert.equal(upd.profile.land_ownership, "tenant"); assert.equal(upd.profile.gender, "female");
});
