// Run with:  node --test tests/voice.test.js
// Covers the browser-side helpers in Node (no browser, so real microphone/TTS are NOT exercised).
const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const Voice = require("../frontend/voice.js");

const voices = (...langs) => langs.map((lang) => ({ lang, name: "v-" + lang }));

test("pickVoice prefers the exact locale", () => {
  const r = Voice.pickVoice(voices("en-IN", "bn-BD", "bn-IN"), "bn-IN");
  assert.equal(r.voice.lang, "bn-IN"); assert.equal(r.exact, true);
});

test("pickVoice accepts another region of the same language", () => {
  const r = Voice.pickVoice(voices("en-US", "bn-BD"), "bn-IN");
  assert.equal(r.voice.lang, "bn-BD"); assert.equal(r.exact, false);
});

test("pickVoice never falls back to an unrelated voice (no silent English)", () => {
  assert.equal(Voice.pickVoice(voices("en-US", "en-IN", "hi-IN"), "bn-IN"), null);
  assert.equal(Voice.pickVoice([], "bn-IN"), null);
});

test("pickVoice tolerates underscore locales (Android)", () => {
  assert.equal(Voice.pickVoice(voices("ta_IN"), "ta-IN").voice.lang, "ta_IN");
});

test("speech errors map to messages that tell the user they can type", () => {
  assert.equal(Voice.speechErrorKey("language-not-supported"), "voiceUnsupported");
  assert.equal(Voice.speechErrorKey("not-allowed"), "micDenied");
  assert.equal(Voice.speechErrorKey("no-speech"), "voiceNoResult");
  assert.equal(Voice.speechErrorKey("audio-capture"), "micMissing");
  assert.equal(Voice.speechErrorKey("aborted"), null);
  assert.equal(Voice.speechErrorKey("whatever"), "voiceError");
});

// ---- UI strings ----
const ctx = { window: {} };
vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../frontend/strings.js"), "utf8"), ctx);
const S = ctx.window.STRINGS;
const CODES = ["en", "hi", "bn", "mr", "te", "ta", "gu", "ur", "kn", "or", "ml"];
const CORE = ["title", "chooseLang", "ph", "speak", "stop", "go", "working", "profile", "map", "docs",
  "why", "docsNeeded", "verify", "followup", "aiDown"];

test("every one of the 11 languages has the core UI strings", () => {
  for (const c of CODES) {
    assert.ok(S[c], "missing strings for " + c);
    for (const k of CORE) assert.ok(typeof S[c][k] === "string" && S[c][k].length > 0, `${c}.${k}`);
    for (const s of ["ELIGIBLE", "LIKELY_ELIGIBLE", "MISSING_INFORMATION", "NOT_ELIGIBLE"]) assert.ok(S[c].status[s], `${c}.status.${s}`);
  }
});

test("the language label is exactly 'Choose your language' in English", () => {
  assert.equal(S.en.chooseLang, "Choose your language");
});

test("app.js drives speech recognition from the selected language and never hard-codes a locale", () => {
  const app = fs.readFileSync(path.join(__dirname, "../frontend/app.js"), "utf8");
  assert.match(app, /r\.lang = langInfo\(\)\.locale/);
  assert.doesNotMatch(app, /\.lang\s*=\s*["'][a-z]{2}-[A-Z]{2}["']/);  // no hard-coded recognition/synthesis locale
  assert.match(app, /Voice\.pickVoice/);
});

test("main box is always a fresh request; only the explicit update button attaches the old profile", () => {
  const app = fs.readFileSync(path.join(__dirname, "../frontend/app.js"), "utf8");
  assert.match(app, /\{ text, language: lang, mode: "new" \}/);                 // fresh body has no profile/asked
  assert.match(app, /\{ text, language: lang, mode: "update", profile, asked \}/);
  assert.match(app, /submitFresh = \(\) => submit\(\$\("input"\), \$\("go"\), false\)/);
  assert.match(app, /submitUpdate = \(\) => submit\(\$\("answer-input"\), \$\("answer-go"\), true\)/);
  assert.doesNotMatch(app, /localStorage|sessionStorage/);                           // no browser-side stale state
});

test("no bare 'speechSynthesis &&' check (regression: language switch crashed on browsers without TTS)", () => {
  const app = fs.readFileSync(path.join(__dirname, "../frontend/app.js"), "utf8");
  assert.doesNotMatch(app, /if \(speechSynthesis &&/);
  assert.match(app, /if \("speechSynthesis" in window\) speechSynthesis\.cancel\(\);/);
});
