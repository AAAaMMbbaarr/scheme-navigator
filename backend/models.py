"""Data models: citizen profile, scheme definitions, and eligibility results."""
from __future__ import annotations

import re
from datetime import date
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Occupation = Literal[
    "farmer", "agricultural_labourer", "student", "worker", "self_employed",
    "unemployed", "homemaker", "other",
]
LandOwnership = Literal["owner", "tenant", "sharecropper"]
Gender = Literal["female", "male", "other"]


class Profile(BaseModel):
    """Structured citizen profile. Every field is optional: None means 'unknown'."""

    model_config = ConfigDict(extra="forbid")

    state: Optional[str] = Field(default=None, max_length=100)
    occupation: Optional[Occupation] = None
    age: Optional[int] = Field(default=None, ge=0, le=120)
    gender: Optional[Gender] = None
    land_area_acres: Optional[float] = Field(default=None, ge=0, le=100000)
    land_ownership: Optional[LandOwnership] = None
    crop: Optional[str] = Field(default=None, max_length=100)
    annual_family_income: Optional[float] = Field(default=None, ge=0)
    has_aadhaar: Optional[bool] = None
    has_bank_account: Optional[bool] = None
    is_income_taxpayer: Optional[bool] = None
    is_govt_employee: Optional[bool] = None

    @field_validator("state", "crop")
    @classmethod
    def _strip(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        return v or None


PROFILE_FIELDS = list(Profile.model_fields)

# Follow-up question per field, used when a scheme needs a value we don't have.
FIELD_QUESTIONS: dict[str, str] = {
    "state": "Which state do you live in?",
    "occupation": "What is your main occupation (for example farmer, student, worker)?",
    "age": "How old are you?",
    "gender": "What is your gender (female, male, other)?",
    "land_area_acres": "How much agricultural land does your family hold, in acres?",
    "land_ownership": "Do you own the land, or do you cultivate it as a tenant or sharecropper (bargadar)?",
    "crop": "Which crop do you grow?",
    "annual_family_income": "What is your family's approximate yearly income in rupees?",
    "has_aadhaar": "Do you have an Aadhaar card?",
    "has_bank_account": "Do you have a bank account?",
    "is_income_taxpayer": "Does anyone in your family pay income tax?",
    "is_govt_employee": "Is anyone in your family a government employee or pensioner?",
}

FIELD_LABELS: dict[str, str] = {
    "state": "State", "occupation": "Occupation", "age": "Age", "gender": "Gender",
    "land_area_acres": "Land (acres)", "land_ownership": "Land ownership",
    "crop": "Crop", "annual_family_income": "Family income (₹/year)",
    "has_aadhaar": "Aadhaar", "has_bank_account": "Bank account",
    "is_income_taxpayer": "Pays income tax", "is_govt_employee": "Govt employee/pensioner in family",
}

Operator = Literal["equals", "not_equals", "gte", "lte", "gt", "lt", "in", "is_true", "is_false"]
Importance = Literal["required", "verify"]


class Rule(BaseModel):
    """A machine-checkable rule.

    importance="required": if the field is unknown the scheme is MISSING_INFORMATION.
    importance="verify":   if the field is unknown the scheme can be at best LIKELY_ELIGIBLE
                           (used for exclusion criteria that most people can confirm later).
    """

    model_config = ConfigDict(extra="forbid")

    field: str
    operator: Operator
    value: Any = None
    importance: Importance = "required"
    description: str

    @model_validator(mode="after")
    def _check(self) -> "Rule":
        if self.field not in PROFILE_FIELDS:
            raise ValueError(f"rule references unknown profile field '{self.field}'")
        if self.operator == "in" and not isinstance(self.value, list):
            raise ValueError("operator 'in' needs a list value")
        if self.operator in ("gte", "lte", "gt", "lt") and not isinstance(self.value, (int, float)):
            raise ValueError(f"operator '{self.operator}' needs a numeric value")
        if self.operator in ("equals", "not_equals") and self.value is None:
            raise ValueError(f"operator '{self.operator}' needs a value")
        return self


class ManualRule(BaseModel):
    """A condition code cannot safely decide; must be checked by the applicant / official."""

    model_config = ConfigDict(extra="forbid")

    description: str


class Scheme(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    state: str  # "India" or a state name
    description: str
    eligibility_rules: list[Rule]
    manual_verification: list[ManualRule] = []
    required_documents: list[str] = Field(min_length=1)
    official_url: str
    last_verified: date
    verification_notes: str = ""

    @field_validator("official_url")
    @classmethod
    def _https(cls, v: str) -> str:
        if not re.match(r"^https://[^/\s]+\.[^/\s]+", v):
            raise ValueError("official_url must be an https URL")
        return v

    @field_validator("eligibility_rules")
    @classmethod
    def _nonempty(cls, v: list[Rule]) -> list[Rule]:
        if not v:
            raise ValueError("a scheme needs at least one eligibility rule")
        return v


class Status(str, Enum):
    ELIGIBLE = "ELIGIBLE"
    LIKELY_ELIGIBLE = "LIKELY_ELIGIBLE"
    MISSING_INFORMATION = "MISSING_INFORMATION"
    NOT_ELIGIBLE = "NOT_ELIGIBLE"


class SchemeResult(BaseModel):
    scheme_id: str
    name: str
    status: Status
    explanation: str
    matched_rules: list[str]
    failed_rules: list[str]
    missing_fields: list[str]
    manual_checks: list[str]
    required_documents: list[str]
    official_url: str
    last_verified: date


class ChecklistItem(BaseModel):
    document: str
    have: Optional[bool] = None  # True/False when the profile tells us, else None
    needed_for: list[str]


class EligibilityReport(BaseModel):
    profile: Profile
    results: list[SchemeResult]
    follow_up_fields: list[str]
    follow_up_questions: list[str]
    checklist: list[ChecklistItem]
    disclaimer: str
