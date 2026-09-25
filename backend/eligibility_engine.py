"""Deterministic eligibility engine. No LLM is involved anywhere in this module."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .models import (
    FIELD_QUESTIONS, ChecklistItem, EligibilityReport, Profile, Rule, Scheme,
    SchemeResult, Status,
)

SCHEMES_DIR = Path(__file__).parent / "schemes"

DISCLAIMER = (
    "This is informational guidance only, not an official eligibility decision. "
    "Scheme rules change; always verify the latest requirements on the official "
    "government source before applying."
)

# Documents whose possession we can read straight from the profile.
DOCUMENT_PROFILE_FIELDS = {
    "aadhaar": "has_aadhaar",
    "bank passbook / account details": "has_bank_account",
}


def load_schemes(directory: Path = SCHEMES_DIR) -> list[Scheme]:
    """Load and strictly validate every scheme file. Raises on any invalid file."""
    schemes: list[Scheme] = []
    for path in sorted(directory.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        try:
            scheme = Scheme.model_validate(data)
        except Exception as exc:  # re-raise with the file name for easy debugging
            raise ValueError(f"Invalid scheme file {path.name}: {exc}") from exc
        if scheme.id != path.stem:
            raise ValueError(f"Scheme id '{scheme.id}' must match file name '{path.stem}'")
        schemes.append(scheme)
    ids = [s.id for s in schemes]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate scheme ids")
    return schemes


def _norm(v: Any) -> Any:
    return v.strip().casefold() if isinstance(v, str) else v


def check_rule(rule: Rule, actual: Any) -> bool:
    """Evaluate one rule against a known (non-None) value."""
    expected = rule.value
    op = rule.operator
    if op == "equals":
        return _norm(actual) == _norm(expected)
    if op == "not_equals":
        return _norm(actual) != _norm(expected)
    if op == "in":
        return _norm(actual) in [_norm(x) for x in expected]
    if op == "is_true":
        return actual is True
    if op == "is_false":
        return actual is False
    if not isinstance(actual, (int, float)) or isinstance(actual, bool):
        return False
    return {"gte": actual >= expected, "lte": actual <= expected,
            "gt": actual > expected, "lt": actual < expected}[op]


def evaluate_scheme(scheme: Scheme, profile: Profile) -> SchemeResult:
    matched: list[str] = []
    failed: list[str] = []
    missing_required: list[str] = []
    missing_verify: list[str] = []

    for rule in scheme.eligibility_rules:
        actual = getattr(profile, rule.field)
        if actual is None:
            (missing_required if rule.importance == "required" else missing_verify).append(rule.field)
        elif check_rule(rule, actual):
            matched.append(rule.description)
        else:
            failed.append(rule.description)

    manual = [m.description for m in scheme.manual_verification]

    if failed:
        status = Status.NOT_ELIGIBLE
        explanation = "Based on what you told us, you do not meet: " + "; ".join(failed) + "."
    elif missing_required:
        status = Status.MISSING_INFORMATION
        explanation = "We need a bit more information to check this scheme."
    elif missing_verify or manual:
        status = Status.LIKELY_ELIGIBLE
        explanation = "You appear to meet the main conditions, but a few points must be confirmed."
    else:
        status = Status.ELIGIBLE
        explanation = "You meet all the conditions we can check."

    missing_fields = list(dict.fromkeys(missing_required + missing_verify))
    return SchemeResult(
        scheme_id=scheme.id, name=scheme.name, status=status, explanation=explanation,
        matched_rules=matched, failed_rules=failed, missing_fields=missing_fields,
        manual_checks=manual, required_documents=scheme.required_documents,
        official_url=scheme.official_url, last_verified=scheme.last_verified,
    )


def build_checklist(results: list[SchemeResult], profile: Profile) -> list[ChecklistItem]:
    """Merge documents across all still-possible schemes, listing each document once."""
    merged: dict[str, ChecklistItem] = {}
    for r in results:
        if r.status == Status.NOT_ELIGIBLE:
            continue
        for doc in r.required_documents:
            key = doc.strip().casefold()
            if key not in merged:
                field = DOCUMENT_PROFILE_FIELDS.get(key)
                merged[key] = ChecklistItem(
                    document=doc.strip(),
                    have=getattr(profile, field) if field else None,
                    needed_for=[],
                )
            merged[key].needed_for.append(r.name)
    return sorted(merged.values(), key=lambda i: (-len(i.needed_for), i.document))


def follow_up_fields(results: list[SchemeResult], profile: Profile) -> list[str]:
    """Fields worth asking about: only those blocking a MISSING_INFORMATION scheme.

    Asked in order of how many schemes they unblock.
    """
    counts: dict[str, int] = {}
    for r in results:
        if r.status != Status.MISSING_INFORMATION:
            continue
        for f in r.missing_fields:
            counts[f] = counts.get(f, 0) + 1
    return sorted(counts, key=lambda f: -counts[f])


def evaluate(profile: Profile, schemes: list[Scheme] | None = None) -> EligibilityReport:
    schemes = schemes if schemes is not None else load_schemes()
    results = [evaluate_scheme(s, profile) for s in schemes]
    order = {Status.ELIGIBLE: 0, Status.LIKELY_ELIGIBLE: 1,
             Status.MISSING_INFORMATION: 2, Status.NOT_ELIGIBLE: 3}
    results.sort(key=lambda r: order[r.status])
    fields = follow_up_fields(results, profile)
    return EligibilityReport(
        profile=profile, results=results,
        follow_up_fields=fields,
        follow_up_questions=[FIELD_QUESTIONS[f] for f in fields],
        checklist=build_checklist(results, profile),
        disclaimer=DISCLAIMER,
    )
