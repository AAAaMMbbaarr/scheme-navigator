"""Deterministic guard for land tenure claims made by the LLM.

"I have 2 acres" / "मेरे पास 2 एकड़ जमीन है" / "আমার ২ একর জমি আছে" say how much land someone has,
NOT who legally owns it. The model must never turn that into `land_ownership = owner`.

Rule: a tenure value (owner / tenant / sharecropper) is accepted only if the model also quotes the exact words
from the user's text that state it (`land_ownership_evidence`) AND code confirms that
  1. the quote really occurs in the user's text,
  2. the quote contains an explicit tenure word for the claimed value (own / मालिक / নামে / rented / बटाई ...),
  3. read in its own sentence, that word really states the user's tenure (see "Context rules"), the sentence
     states no other tenure, and it is not negated ("I don't own ...").
Anything else becomes unknown (None), and the eligibility engine then asks the follow-up question.

Context rules (each one fixes a real false positive):
  * Name phrases ("in my name", "मेरे नाम पर", "আমার নামে", "माझ्या नावावर" ...) need a first-person possessive
    right before the name word AND a land word in the same sentence. "My name is Ramesh and I have 2 acres",
    "मेरा नाम है रमेश", "রমেশ নামে একজন" (someone named Ramesh) and "पिता के नाम पर" (in father's name) are not
    ownership.
  * "owner" / मालिक / মালিক ... must be about the speaker: "I am the owner", "I own", "मैं ... मालिक हूँ",
    "আমি ... মালিক". "the owner's farm", "मालिक के खेत", "মালিকের জমি", "my landlord is the owner" are not.
  * Rent / lease / tenant words need a land or farming word in the same sentence ("I pay house rent" and
    "বাড়ি ভাড়া" are not agricultural tenancy).

Coverage: English, Hindi, Bengali and Marathi have fuller word lists; the other supported languages have the
most common words. A genuine statement in a language/wording we don't recognise is treated as unknown, which is
the safe direction (the user is simply asked). Extend the lists below to widen coverage.
"""
from __future__ import annotations

import logging
import re
import unicodedata

logger = logging.getLogger("scheme_navigator.extract")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", s)).strip().casefold()


def _alt(words) -> str:
    """Regex alternation of normalized literal words, longest first."""
    return "|".join(re.escape(_norm(w)) for w in sorted(set(words), key=len, reverse=True))


# Word edges for Indic/Arabic scripts, where \b is unreliable around vowel signs and viramas.
_START = r"(?:^|(?<=[\s,;:\"'(“‘]))"
_END = r"(?=$|[\s.,;:!?।॥۔؟\"')”’])"

# ---------------------------------------------------------------- loose vocabulary: does a quote NAME a tenure?
# Latin patterns use word boundaries; Indic/Arabic-script words are substrings.
_LATIN = {
    "owner": r"\bown(s|ed|er|ers|ership)?\b|\blandowner|\b(in|under|on|registered (to|in|under)) my name\b"
             r"|\bin our names?\b|\bregistered (to|under) (me|us)\b",
    "tenant": r"\brent(s|ed|ing)?\b|\bleas(e|es|ed|ing)\b|\btenan(t|ts|cy)\b",
    "sharecropper": r"\bshare[- ]?crop\w*|\bbargad\w*|\bbatai\w*|\bbarga\b",
}
_INDIC = {
    "owner": [
        "मालिक", "मालकिन", "नाम पर", "नाम पे", "नाम से", "नाम में",                   # Hindi
        "মালিক", "নামে", "নামেই",                                                     # Bengali
        "मालक", "मालकीची", "मालकीचे", "मालकीच्या", "नावावर", "नावे", "नावाने",           # Marathi
        "مالک", "نام پر", "نام سے", "نام میں",                                        # Urdu
        "యజమాని", "పేరు మీద", "పేరిట", "పేరున",                                        # Telugu
        "உரிமையாளர்", "பெயரில்",                                                       # Tamil
        "માલિક", "નામે", "નામ પર",                                                     # Gujarati
        "ಮಾಲೀಕ", "ಹೆಸರಿನಲ್ಲಿ", "ಹೆಸರಲ್ಲಿ", "ಹೆಸರಿಗೆ",                                  # Kannada
        "ମାଲିକ", "ନାମରେ",                                                              # Odia
        "ഉടമ", "പേരിൽ",                                                                # Malayalam
    ],
    "tenant": [
        "किराए", "किराये", "किरायेदार", "पट्टे पर",       # not bare "पट्टा": it is also the land title deed
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
_INDIC_N = {k: [_norm(w) for w in ws] for k, ws in _INDIC.items()}

# ---------------------------------------------------------------- context vocabulary
_LAND_RE = re.compile(
    r"\b(land|lands|acres?|bighas?|kathas?|cottahs?|decimals?|hectares?|plots?|fields?|farm|farms|farmland|khet|"
    r"zameen|jameen|jomi)\b"
    "|" + _alt([
        "जमीन", "ज़मीन", "भूमि", "खेत", "एकड", "बीघा", "कट्ठा", "हेक्टेयर",      # Hindi (एकड also covers एकड़)
        "জমি", "খেত", "ক্ষেত", "বিঘা", "একর", "কাঠা", "শতক", "ডেসিমেল",             # Bengali
        "जमिनी", "शेत", "एकर", "गुंठे", "हेक्टर",                                   # Marathi
        "زمین", "کھیت", "ایکڑ", "بیگھ",                                             # Urdu
        "భూమి", "పొలం", "పొలము", "ఎకర",                                            # Telugu
        "நிலம்", "நிலத்", "வயல்", "ஏக்கர்",                                           # Tamil
        "જમીન", "ખેતર", "એકર", "વીઘા",                                              # Gujarati
        "ಜಮೀನು", "ಭೂಮಿ", "ಹೊಲ", "ಎಕರೆ",                                             # Kannada
        "ଜମି", "ଏକର", "କ୍ଷେତ", "ବିଘା",                                              # Odia
        "ഭൂമി", "സ്ഥലം", "ഏക്കർ", "വയൽ", "നിലം",                                     # Malayalam
    ]))
_FARMING_RE = re.compile(
    r"\b(farm\w*|cultivat\w*|crops?|kheti)\b"
    "|" + _alt(["खेती", "চাষ", "शेती", "کاشت", "వ్యవసాయ", "సాగు", "விவசாய", "சாகுபடி", "ખેતી", "ಕೃಷಿ", "ಬೇಸಾಯ",
                "ଚାଷ", "കൃഷി"]))

# Name phrases: first-person possessive + name word + postposition ("मेरे नाम पर", not "पिता के नाम पर").
_NAME_PHRASES = [
    (["मेरे", "हमारे", "अपने"], ["नाम पर", "नाम पे", "नाम से", "नाम में"]),              # Hindi
    (["আমার", "আমাদের", "নিজের", "নিজেদের"], ["নামে"]),                              # Bengali (নামে also covers নামেই)
    (["माझ्या", "आमच्या", "स्वतःच्या"], ["नावावर", "नावे", "नावाने"]),                 # Marathi
    (["میرے", "ہمارے", "اپنے"], ["نام پر", "نام سے", "نام میں"]),                    # Urdu
    (["నా", "మా"], ["పేరు మీద", "పేరిట", "పేరున"]),                                    # Telugu
    (["என்", "எனது", "என்னுடைய", "எங்கள்"], ["பெயரில்"]),                              # Tamil
    (["મારા", "અમારા", "પોતાના"], ["નામે", "નામ પર"]),                                 # Gujarati
    (["ನನ್ನ", "ನಮ್ಮ"], ["ಹೆಸರಿನಲ್ಲಿ", "ಹೆಸರಲ್ಲಿ", "ಹೆಸರಿಗೆ"]),                           # Kannada
    (["ମୋ", "ମୋର", "ଆମ", "ଆମର"], ["ନାମରେ"]),                                          # Odia
    (["എന്റെ", "ഞങ്ങളുടെ"], ["പേരിൽ"]),                                                # Malayalam
]
_NAME_RE = re.compile(
    r"\b(in|under|on)\s+my\s+(own\s+)?name\b|\bin\s+our\s+names?\b|\bregistered\s+(to|in|under)\s+(me|us|my\s+name)\b"
    + "".join(f"|{_START}(?:{_alt(poss)})\\s+(?:{_alt(post)})" for poss, post in _NAME_PHRASES))

# "owner" about the speaker. English: the subject is I/we; possessives ("owner's") never count.
_OWNER_EN_RE = re.compile(
    r"\b(i|we)(\s+(also|still|jointly|now|already|legally|actually|really|personally|do|does|did|not|never)"
    r"|\s+(do|does|did)n['’]?t)*\s+own(s|ed)?\b"
    r"|\bowned\s+by\s+(me|us|myself)\b"
    r"|\b(i|we)\s+(have|hold)\s+(the\s+)?ownership\b"
    r"|\b(i\s+am|i['’]m|we\s+are|we['’]re|i\s+was)(\s+(the|a|an|sole|joint|legal|registered|rightful|real|actual|"
    r"also|still|now|not))*\s+(land\s*)?owners?\b(?!['’])")


def _noun(word: str, *, not_after=(), not_before=(), suffix_ok: bool = True, bad_suffix: str = "") -> str:
    """Owner noun that is not someone else's: not "मेरे मालिक" (my boss), not "मालिक के" (the owner's)."""
    w = re.escape(_norm(word))
    behind = "".join(f"(?<!{re.escape(_norm(p))} )" for p in not_after)
    ahead = f"(?!\\s*(?:{_alt(not_before)}){_END})" if not_before else ""
    tail = "" if suffix_ok else _END
    bad = f"(?!{bad_suffix})" if bad_suffix else ""
    return f"{behind}{w}{bad}{tail}{ahead}"


_HI_FOLLOW = ["के", "की", "का", "ने", "को", "से"]
_OWNER_NOUN_RE = re.compile("|".join([
    _noun("मालिक", not_after=["मेरे", "मेरा", "हमारे", "हमारा"], not_before=_HI_FOLLOW, suffix_ok=False),
    _noun("मालकिन", not_after=["मेरी", "हमारी"], not_before=_HI_FOLLOW, suffix_ok=False),
    _noun("মালিক", not_after=["আমার", "আমাদের"], bad_suffix="ের|কে"),
    _noun("मालक", not_after=["माझा", "माझे", "आमचा", "आमचे"], suffix_ok=False),
    _noun("मालकीच"),                                                         # माझ्या मालकीची जमीन
    _noun("مالک", not_after=["میرے", "میرا", "ہمارے", "ہمارا"], not_before=["کے", "کی", "کا", "نے", "کو", "سے"],
          suffix_ok=False),
    _noun("యజమాని", not_after=["నా", "మా"], not_before=["యొక్క"]),
    _noun("உரிமையாளர்", not_after=["என்", "எனது"]),                          # genitive உரிமையாளரின் never matches
    _noun("માલિક", not_after=["મારા", "મારો", "અમારા"], suffix_ok=False),
    _noun("ಮಾಲೀಕ", not_after=["ನನ್ನ", "ನಮ್ಮ"], bad_suffix=f"ನ{_END}"),
    _noun("ମାଲିକ", not_after=["ମୋ", "ମୋର"], bad_suffix="ର|ଙ୍କ"),
    _noun("ഉടമ", not_after=["എന്റെ"], bad_suffix="യുടെ"),
]))
# First-person subject words (whole tokens); an Indic owner noun only counts in a sentence that has one.
# Urdu میں is left out on purpose: it also means "in".
_FIRST_PERSON = {_norm(w) for w in [
    "मैं", "हम", "हूँ", "हूं", "আমি", "আমরা", "मी", "आम्ही", "माझ्या", "आमच्या", "ہوں", "ہم", "నేను", "మేము",
    "நான்", "நாங்கள்", "હું", "અમે", "ನಾನು", "ನಾವು", "ମୁଁ", "ଆମେ", "ഞാൻ", "ഞങ്ങൾ"]}

# Whole-token negations (token match, so Bengali "নামে" is not mistaken for "না").
_NEG_TOKENS = {"not", "no", "never", "without", "nor", "nahi", "nahin",
               "नहीं", "नही", "बिना", "না", "নয়", "নেই", "नाही", "నో", "లేదు", "இல்லை", "નથી", "નહીં",
               "ಇಲ್ಲ", "ନାହିଁ", "ഇല്ല", "نہیں"}
_STRIP = ".,;:!?।॥۔؟\"'()[]“”‘’"
# Sentence split; a dot between digits ("2.5 acres") is a decimal point, not a full stop.
_SENTENCE_SPLIT = re.compile(r"(?<!\d)\.|\.(?!\d)|[!?।॥۔؟\n]+")


def _loose(kind: str, norm_text: str) -> bool:
    if _LATIN_RE[kind].search(norm_text):
        return True
    return any(w in norm_text for w in _INDIC_N[kind])


def _sentences(norm_text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT.split(norm_text) if s.strip()]


def _kinds_in_sentence(s: str) -> set[str]:
    """Tenure classes that one normalized sentence really states about the speaker (context rules applied)."""
    kinds = set()
    land = bool(_LAND_RE.search(s))
    first_person = any(tok.strip(_STRIP) in _FIRST_PERSON for tok in s.split())
    if _OWNER_EN_RE.search(s) or (land and _NAME_RE.search(s)) or (first_person and _OWNER_NOUN_RE.search(s)):
        kinds.add("owner")
    if (land or _FARMING_RE.search(s)) and _loose("tenant", s):
        kinds.add("tenant")
    if _loose("sharecropper", s):
        kinds.add("sharecropper")
    return kinds


def kinds_in(text: str) -> set[str]:
    """Which tenure classes are explicitly stated in `text`."""
    return set().union(*(_kinds_in_sentence(s) for s in _sentences(_norm(text))))


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
    if {k for k in _LATIN if _loose(k, ev)} != {value}:   # 2. the quote names the tenure, and only the claimed one
        return False
    # 3. read in the sentence(s) the quote covers, the wording is about the user, states only this tenure,
    #    and the sentence stating it is not negated
    pieces = _sentences(ev)
    context = [(s, _kinds_in_sentence(s)) for s in _sentences(text) if any(p in s for p in pieces)]
    if set().union(*(k for _, k in context)) != {value}:
        return False
    return not any(value in k and _negated(s) for s, k in context)


def detect(text: str) -> str | None:
    """Deterministic reading of a statement (used by the offline English fallback).
    Returns a class only if exactly one is explicitly stated and not negated; otherwise None."""
    per_sentence = [(s, _kinds_in_sentence(s)) for s in _sentences(_norm(text))]
    found = set().union(*(k for _, k in per_sentence))
    if len(found) != 1:
        return None
    (kind,) = found
    if any(kind in k and _negated(s) for s, k in per_sentence):
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
