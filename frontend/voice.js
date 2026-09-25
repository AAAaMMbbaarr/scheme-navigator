"use strict";
// Pure helpers for voice features. No DOM access, so they can be unit-tested with plain Node.
(function (root) {
  function normLocale(l) { return String(l || "").replace("_", "-").toLowerCase(); }

  // Choose a speech-synthesis voice for `locale` (e.g. "bn-IN").
  // Exact locale wins; otherwise any voice for the same language (bn-BD for bn-IN).
  // Never falls back to an unrelated voice: returns null so the UI can say so honestly.
  function pickVoice(voices, locale) {
    const want = normLocale(locale), primary = want.split("-")[0];
    const list = Array.from(voices || []);
    const exact = list.find((v) => normLocale(v.lang) === want);
    if (exact) return { voice: exact, exact: true };
    const same = list.find((v) => normLocale(v.lang).split("-")[0] === primary);
    return same ? { voice: same, exact: false } : null;
  }

  // Map a SpeechRecognition error code to a message key in the UI dictionary.
  function speechErrorKey(error) {
    switch (error) {
      case "language-not-supported": return "voiceUnsupported";
      case "not-allowed": case "service-not-allowed": return "micDenied";
      case "no-speech": return "voiceNoResult";
      case "audio-capture": return "micMissing";
      case "aborted": return null; // user pressed stop
      default: return "voiceError";
    }
  }

  const api = { pickVoice, speechErrorKey, normLocale };
  if (typeof module !== "undefined" && module.exports) module.exports = api; else root.Voice = api;
})(typeof window !== "undefined" ? window : globalThis);
