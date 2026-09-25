const { JSDOM } = require("jsdom");
const BASE = process.argv[2], MODE = process.argv[3]; // "fake" | "nokey"
const results = [];
const ok = (name, cond, extra) => { results.push([cond ? "PASS" : "FAIL", name, cond ? "" : extra || ""]); };

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function until(fn, ms = 6000) { const t = Date.now(); while (Date.now() - t < ms) { if (fn()) return true; await sleep(40); } return false; }

async function open(stubs) {
  // strip the external Google Fonts <link> tags so jsdom only loads our own same-origin scripts
  const html = (await (await fetch(BASE + "/")).text()).replace(/<link[^>]*fonts[^>]*>/g, "");
  const dom = new JSDOM(html, {
    url: BASE + "/", runScripts: "dangerously", resources: "usable", pretendToBeVisual: true,
    beforeParse(w) { w.fetch = (u, o) => fetch(new URL(u, BASE), o); if (stubs) stubs(w); },
  });
  const d = dom.window.document;
  await until(() => d.getElementById("lang").options.length > 0);
  return { w: dom.window, d, $: (id) => d.getElementById(id) };
}
async function say(ui, text, button = "go", box = "input") {
  ui.$(box).value = text; ui.$(button).click();
  await sleep(60); await until(() => !ui.$(button).disabled);
}
const prof = (ui) => Object.fromEntries([...ui.$("profile").querySelectorAll("div")].map((x) => [x.children[0].textContent, x.children[1].textContent]));
async function setLang(ui, code) {
  ui.$("lang").value = code; ui.$("lang").dispatchEvent(new ui.w.Event("change"));
  await sleep(80); await until(() => ui.$("status").textContent === "");
}

const BN = "আমি পশ্চিমবঙ্গের একজন কৃষক। আমার ২ একর জমি আছে।";
const HI = "मैं बिहार का किसान हूँ और मेरे पास 2 एकड़ जमीन है।";

(async () => {
  let ui = await open();
  const opts = [...ui.$("lang").options].map((o) => o.textContent);
  ok("dropdown has 11 languages", opts.length === 11, opts.join(","));
  ok("dropdown labels/order", JSON.stringify(opts) === JSON.stringify(["English", "हिन्दी", "বাংলা", "मराठी", "తెలుగు", "தமிழ்", "ગુજરાતી", "اردو", "ಕನ್ನಡ", "ଓଡ଼ିଆ", "മലയാളം"]), opts.join(","));
  ok("label is 'Choose your language'", ui.$("t-chooseLang").textContent === "Choose your language");

  if (MODE === "fake") {
    await say(ui, "I am a student from Bihar.");
    ok("A shows Student/Bihar", prof(ui)["State"] === "Bihar" && prof(ui)["Occupation"] === "Student", JSON.stringify(prof(ui)));
    await say(ui, "I am a farmer from West Bengal.");
    let p = prof(ui);
    ok("B shows Farmer/West Bengal", p["Occupation"] === "Farmer" && p["State"] === "West Bengal", JSON.stringify(p));
    ok("no stale Bihar/Student in profile", !JSON.stringify(p).match(/Bihar|Student/), JSON.stringify(p));
    await say(ui, "I own 3 acres", "answer-go", "answer-input");
    p = prof(ui);
    ok("explicit update adds land, keeps farmer/WB", p["Land (acres)"] === "3" && p["Occupation"] === "Farmer" && p["State"] === "West Bengal", JSON.stringify(p));
    await say(ui, "I am a student from Bihar.");
    ok("fresh input after an update drops everything old", !prof(ui)["Land (acres)"] && prof(ui)["State"] === "Bihar", JSON.stringify(prof(ui)));

    ui = await open();
    await setLang(ui, "bn");
    ok("Bengali UI title", ui.$("t-title").textContent.includes("সরকারি"));
    await say(ui, BN);
    ok("Bengali extraction -> WB / 2 acres", prof(ui)["রাজ্য"] === "পশ্চিমবঙ্গ" && prof(ui)["জমি (একর)"] === "2", JSON.stringify(prof(ui)));
    ok("Bengali explanations rendered (mock tag)", ui.$("cards").textContent.includes("[Bengali]"));
    ok("scheme names untouched", ui.$("cards").textContent.includes("PM-KISAN"));
    ok("no AI-down notice on success", ui.$("notice").classList.contains("hidden"));
    await setLang(ui, "hi");
    ok("switch to Hindi RESETS the screen (no re-explain, language kept)", ui.$("cards").children.length === 0 && ui.$("profile").children.length === 0 && ui.$("input").value === "" && ui.$("lang").value === "hi");
    await say(ui, HI);
    ok("Hindi extraction -> Bihar / 2 acres", prof(ui)["राज्य"] === "बिहार" && prof(ui)["ज़मीन (एकड़)"] === "2", JSON.stringify(prof(ui)));
    await setLang(ui, "en");
    ok("back to English: clean screen again", ui.$("cards").children.length === 0 && Object.keys(prof(ui)).length === 0 && ui.$("lang").value === "en");
    await setLang(ui, "ur");
    ok("Urdu switches page to RTL", ui.w.document.documentElement.dir === "rtl" && ui.w.document.documentElement.lang === "ur");
    await setLang(ui, "ta");
    ok("Tamil switches back to LTR", ui.w.document.documentElement.dir === "ltr");

    // voice recognition: locale follows the dropdown; errors are honest; typing still works
    const seen = [];
    class SR {
      constructor() { this.lang = ""; seen.push(this); }
      start() { setTimeout(() => { if (this.onstart) this.onstart(); setTimeout(() => { if (this.onerror) this.onerror({ error: "language-not-supported" }); if (this.onend) this.onend(); }, 20); }, 10); }
      stop() {}
    }
    ui = await open((w) => { w.SpeechRecognition = SR; });
    for (const [code, loc] of [["bn", "bn-IN"], ["hi", "hi-IN"], ["en", "en-IN"], ["te", "te-IN"], ["or", "or-IN"], ["ml", "ml-IN"]]) {
      await setLang(ui, code); ui.$("speak").click(); await sleep(120);
      ok(`recognition.lang for ${code} = ${loc}`, seen[seen.length - 1].lang === loc, seen[seen.length - 1].lang);
    }
    await setLang(ui, "bn"); ui.$("speak").click(); await sleep(120);
    ok("language-not-supported message shown in Bengali", ui.$("speech-note").textContent.includes("ভয়েস ইনপুট সমর্থিত নয়"), ui.$("speech-note").textContent);
    await say(ui, BN);
    ok("typing still works after voice failure", prof(ui)["রাজ্য"] === "পশ্চিমবঙ্গ");

    // no SpeechRecognition at all
    ui = await open();
    await setLang(ui, "hi"); ui.$("speak").click(); await sleep(50);
    ok("no SpeechRecognition: honest Hindi message, typing unaffected", ui.$("speech-note").textContent.includes("वॉइस इनपुट उपलब्ध नहीं"), ui.$("speech-note").textContent);

    // text-to-speech voice selection
    const spoken = [];
    const synth = (voices) => (w) => {
      w.SpeechSynthesisUtterance = function (t) { this.text = t; };
      w.speechSynthesis = { getVoices: () => voices, speak: (u) => spoken.push(u), cancel() {}, speaking: false, addEventListener() {} };
    };
    ui = await open(synth([{ lang: "en-US", name: "Eng" }, { lang: "hi-IN", name: "HiV" }]));
    await setLang(ui, "bn"); await say(ui, BN);
    ok("Bengali w/o bn voice: read-aloud disabled + honest message", ui.$("read-btn").disabled === true && ui.$("voice-note").textContent.includes("ভয়েস ইনস্টল করা নেই"), ui.$("voice-note").textContent);
    ui.$("read-btn").click();
    ok("nothing spoken with a wrong-language voice", spoken.length === 0);
    await setLang(ui, "hi");
    ok("Hindi voice exists: button enabled", ui.$("read-btn").disabled === false);
    ui.$("read-btn").click();
    ok("Hindi utterance uses hi-IN voice", spoken.length === 1 && spoken[0].voice.name === "HiV" && spoken[0].lang === "hi-IN", JSON.stringify(spoken[0]));
  } else {
    await setLang(ui, "bn");
    await say(ui, BN);
    ok("Bengali w/o key: language stays Bengali", ui.$("lang").value === "bn");
    ok("Bengali w/o key: clean Bengali 'AI unavailable' message", ui.$("notice-main").textContent.includes("AI পরিষেবা সাময়িকভাবে"), ui.$("notice-main").textContent);
    ok("detail names the real cause, not the language", ui.$("notice-detail").textContent.includes("missing_api_key") && !/unsupported|not supported/i.test(ui.$("notice-detail").textContent), ui.$("notice-detail").textContent);
    ok("old misleading string never shown", !ui.d.body.textContent.includes("শুধু ইংরেজি বোঝা যাবে"));
    await setLang(ui, "en");
    await say(ui, "I am a farmer from West Bengal with 2 acres of land.");
    ok("English w/o key still works (offline reader)", prof(ui)["State"] === "West Bengal");
    ok("English w/o key: says AI unavailable", ui.$("notice-main").textContent.includes("temporarily unavailable"), ui.$("notice-main").textContent);
    await say(ui, "I am a student from Bihar."); await say(ui, "I am a farmer from West Bengal.");
    ok("stale test on real server (offline): Farmer/WB", prof(ui)["Occupation"] === "Farmer" && prof(ui)["State"] === "West Bengal", JSON.stringify(prof(ui)));
    await setLang(ui, "hi");
    ok("language switch w/o key: stays Hindi, screen and banner cleared", ui.$("lang").value === "hi" && ui.$("notice").classList.contains("hidden") && ui.$("cards").children.length === 0, ui.$("notice-main").textContent);
  }
  for (const r of results) console.log(r[0], "-", r[1], r[2] ? "\n     " + r[2] : "");
  const fails = results.filter((r) => r[0] === "FAIL").length;
  console.log(`\n${results.length - fails}/${results.length} passed`);
  process.exit(fails ? 1 : 0);
})().catch((e) => { console.error("DRIVER ERROR", e); process.exit(2); });
