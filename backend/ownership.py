"""Deterministic guard for land tenure claims made by the LLM.

"I have 2 acres" / "मेरे पास 2 एकड़ जमीन है" / "আমার ২ একর জমি আছে" say how much land someone has,
NOT who legally owns it. The model must never turn that into `land_ownership = owner`.

Rule: a tenure value (owner / tenant / sharecropper) is accepted only if the model also quotes the exact words
from the user's text that state it (`land_ownership_evidence`) AND code confirms that
  1. the quote really occurs in the user's text,
  2. the quote contains an explicit tenure word for the claimed value (own / मालिक / নামে / rented / बटाई ...),
  3. the sentence is not negated ("I don't own ...").
Anything else becomes unknown (None), and the eligibility engine then asks the follow-up question.

Coverage: English, Hindi, Bengali and Marathi have fuller word lists; the other supported languages have the
most common words. A genuine statement in a language/wording we don't recognise is treated as unknown, which is
the safe direction (the user is simply asked). Extend the lists below to widen coverage.
"""
from __future__ import annotations

import logging
import re
import unicodedata

logger = logging.getLogger("scheme_navigator.extract")

# Explicit tenure vocabulary. Latin patterns use word boundaries; Indic/Arabic-script words are substrings.
_LATIN = {
    "owner": r"\bown(s|ed|er|ers|ership)?\b|\blandowner|\b(in|under|on|registered (to|in)) my name\b|\bmy name\b",
    "tenant": r"\brent(s|ed|ing)?\b|\bleas(e|es|ed|ing)\b|\btenan(t|ts|cy)\b",
    "sharecropper": r"\bshare[- ]?crop\w*|\bbargad\w*|\bbatai\w*|\bbarga\b",
}
_INDIC = {
    "owner": [
        "मालिक", "मालकिन", "मलिक", "नाम पर", "नाम से", "नाम में", "नाम है",              # Hindi
        "মালিক", "নামে", "নামেই",                                                     # Bengali
        "मालक", "मालकीची", "मालकीचे", "नावावर", "नावे आहे",                           # Marathi
        "مالک", "نام پر", "نام سے", "نام میں",                                        # Urdu
        "యజమాని", "పేరు మీద", "పేరిట",                                                # Telugu
        "உரிமையாளர்", "பெயரில்",                                                       # Tamil
        "માલિક", "નામે", "નામ પર",                                                     # Gujarati
        "ಮಾಲೀಕ", "ಹೆಸರಿನಲ್ಲಿ", "ಹೆಸರಲ್ಲಿ",                                            # Kannada
        "ମାଲିକ", "ନାମରେ",                                                              # Odia
        "ഉടമ", "പേരിൽ",                                                                # Malayalam
    ],
    "tenant": [
        "किराए", "किराये", "किरायेदार", "पट्टे", "पट्टा",
        "ভাড়া", "ভাড়ায়", "লিজ", "ইজারা",
        "भाड्याने", "भाडेकरू", "पट्ट्याने", "कुळ",
        "کرائے", "پٹے",
        "కౌలు", "అద్దె",
        "குத்தகை", "வாடகை",
        "ભાડે", "ભાડા",
        "ಗುತ್ತಿಗೆ", "ಬಾಡಿಗೆ",
        "ଭଡ଼ା", "ଭଡାରେ",
        "പാട്ടത്തിന്", "വാടക",
    ],
    "sharecropper": [
        "बटाई", "बटाईदार", "बरगा",
        "বর্গা", "বর্গাদার",
        "बटाईने",
        "بٹائی",
    ],
}
_LATIN_RE = {k: re.compile(v, re.IGNORECASE) for k, v in _LATIN.items()}

# Whole-token negations (token match, so Bengali "নামে" is not mistaken for "না").
_NEG_TOKENS = {"not", "no", "never", "without", "nor", "nahi", "nahin",
               "नहीं", "नही", "बिना", "না", "নয়", "নেই", "नाही", "నో", "లేదు", "இல்லை", "નથી", "નહીં",
               "ಇಲ್ಲ", "ନାହିଁ", "ഇല്ല", "نہیں"}
_STRIP = ".,;:!?।॥\"'()[]“”‘’"


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", s)).strip().casefold()


def _has_word(kind: str, norm_text: str) -> bool:
    if _LATIN_RE[kind].search(norm_text):
        return True
    return any(_norm(w) in norm_text for w in _INDIC[kind])


def kinds_in(text: str) -> set[str]:
    """Which tenure classes are explicitly worded in `text`."""
    n = _norm(text)
    return {k for k in _LATIN if _has_word(k, n)}


def _negated(sentence: str) -> bool:
    for tok in sentence.split():
        t = tok.strip(_STRIP)
        if t in _NEG_TOKENS or t.endswith(("n't", "n’t")):
            return True
    return False


def supported(value: str, evidence: object, user_text: str) -> bool:
    """True only if `evidence` proves `value` (see module docstring)."""
    if not isinstance(evidence, str) or not evidence.strip():
        return False
    ev, text = _norm(evidence), _norm(user_text)
    if len(ev) < 3 or ev not in text:                     # 1. quoted words must really be in the user's text
        return False
    if kinds_in(evidence) != {value}:                     # 2. explicit tenure word, and only for the claimed value
        return False
    for sentence in re.split(r"[.!?।॥\n]+", text):        # 3. not negated in its own sentence
        if ev in sentence and _negated(sentence):
            return False
    return True


def detect(text: str) -> str | None:
    """Deterministic reading of a statement (used by the offline English fallback).
    Returns a class only if exactly one is explicitly worded and not negated; otherwise None."""
    found = kinds_in(text)
    if len(found) != 1:
        return None
    (kind,) = found
    n = _norm(text)
    for sentence in re.split(r"[.!?।॥\n]+", n):
        if _has_word(kind, sentence) and _negated(sentence):
            return None
    return kind


def guard(claimed: str | None, evidence: object, user_text: str) -> str | None:
    """Return `claimed` if proven, else None (logged, without the user's text)."""
    if claimed is None:
        return None
    if supported(claimed, evidence, user_text):
        return claimed
    logger.info("dropped unsupported land_ownership=%r (no explicit tenure wording in the user's text)", claimed)
    return None
