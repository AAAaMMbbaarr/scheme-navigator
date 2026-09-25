"""Supported languages: single source of truth (the frontend loads /api/languages)."""
from __future__ import annotations

from typing import NamedTuple


class Language(NamedTuple):
    code: str      # short code used in API requests
    english: str   # name used in prompts to Claude
    native: str    # name shown in the dropdown
    locale: str    # BCP-47 tag for Web Speech recognition and synthesis
    rtl: bool = False


LANGUAGES: list[Language] = [
    Language("en", "English", "English", "en-IN"),
    Language("hi", "Hindi", "हिन्दी", "hi-IN"),
    Language("bn", "Bengali", "বাংলা", "bn-IN"),
    Language("mr", "Marathi", "मराठी", "mr-IN"),
    Language("te", "Telugu", "తెలుగు", "te-IN"),
    Language("ta", "Tamil", "தமிழ்", "ta-IN"),
    Language("gu", "Gujarati", "ગુજરાતી", "gu-IN"),
    Language("ur", "Urdu", "اردو", "ur-IN", rtl=True),
    Language("kn", "Kannada", "ಕನ್ನಡ", "kn-IN"),
    Language("or", "Odia", "ଓଡ଼ିଆ", "or-IN"),
    Language("ml", "Malayalam", "മലയാളം", "ml-IN"),
]
BY_CODE = {lang.code: lang for lang in LANGUAGES}


def get_language(code: str) -> Language:
    """Raises KeyError for a code that is genuinely not supported."""
    return BY_CODE[code]
