"""Pack schema. A segment is data: these models are all the code knows about one.

Every model forbids unknown keys, so a typo in a pack is an error that names the file and the
field, not a silently ignored setting.
"""

import re
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import jinja2
import jinja2.meta
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from revos.domain.actions import KNOWN_ACTION_KEYS
from revos.domain.enums import AutonomyLevel
from revos.domain.stages import DealStage

MAX_NUDGE = 10   # hard ceiling on the LLM score nudge; a pack may lower it, never raise it
ID = r"^[a-z0-9_]+$"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def check_template(text: str, required: tuple[str, ...] = ()) -> str:
    """Parse a Jinja template and make sure it uses the variables it must use."""
    env = jinja2.Environment()
    try:
        tree = env.parse(text)
    except jinja2.TemplateSyntaxError as exc:
        raise ValueError(f"template syntax error on line {exc.lineno}: {exc.message}") from exc
    used = jinja2.meta.find_undeclared_variables(tree)
    missing = [name for name in required if name not in used]
    if missing:
        raise ValueError("template must use " + ", ".join("{{ " + m + " }}" for m in missing))
    return text


# --- pack.yaml ---------------------------------------------------------------------------

class PackMeta(Strict):
    id: str = Field(pattern=ID)
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    description: str = ""
    demo: bool = False          # the pack `make demo` runs, with its fictional lead


# --- icp.yaml: lead fields, scoring rules, weights, tiers --------------------------------

Op = Literal["eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in", "contains_any", "between",
             "truthy"]
_NUMERIC_OPS = {"gt", "gte", "lt", "lte", "between"}
_LIST_OPS = {"in", "not_in", "contains_any"}


class LeadField(Strict):
    type: Literal["int", "float", "bool", "str", "list"]
    description: str = ""


class Condition(Strict):
    field: str = Field(min_length=1)
    op: Op
    value: Any = Field(default=None, validate_default=True)

    @field_validator("value")
    @classmethod
    def _value_fits_op(cls, value: Any, info: ValidationInfo) -> Any:
        op = info.data.get("op")
        if op is None:
            return value
        is_num = isinstance(value, int | float) and not isinstance(value, bool)
        if op in _LIST_OPS and not (isinstance(value, list) and value):
            raise ValueError(f"op '{op}' needs a non-empty list as value")
        if op == "between" and not (
            isinstance(value, list) and len(value) == 2
            and all(isinstance(v, int | float) for v in value) and value[0] <= value[1]
        ):
            raise ValueError("op 'between' needs [low, high] with low <= high")
        if op in {"gt", "gte", "lt", "lte"} and not is_num:
            raise ValueError(f"op '{op}' needs a number as value")
        if op == "truthy" and value is not None:
            raise ValueError("op 'truthy' takes no value")
        if op in {"eq", "ne"} and (value is None or isinstance(value, list | dict)):
            raise ValueError(f"op '{op}' needs a single value")
        return value


def _check_reason(reason: str) -> str:
    try:
        reason.format(value="x")
    except (KeyError, IndexError, ValueError) as exc:
        raise ValueError("reason may only use the placeholder {value}") from exc
    return reason


class ScoringRule(Condition):
    id: str = Field(pattern=ID)
    points: int = Field(gt=0, le=100)
    reason: str = Field(min_length=3)

    _reason = field_validator("reason")(_check_reason)


class DisqualifierRule(Condition):
    id: str = Field(pattern=ID)
    reason: str = Field(min_length=3)

    _reason = field_validator("reason")(_check_reason)


class Weights(Strict):
    fit: float = Field(ge=0, le=1)
    intent: float = Field(ge=0, le=1)
    engagement: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _sum_to_one(self) -> "Weights":
        total = self.fit + self.intent + self.engagement
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"fit + intent + engagement weights must sum to 1.0 (got {total:g})")
        return self


class Tiers(Strict):
    A: int = Field(ge=1, le=100)
    B: int = Field(ge=1, le=100)
    C: int = Field(ge=0, le=100)

    @model_validator(mode="after")
    def _ordered(self) -> "Tiers":
        if not (self.A > self.B > self.C):
            raise ValueError("tier thresholds must satisfy A > B > C")
        return self


class ScoringConfig(Strict):
    lead_fields: dict[str, LeadField] = Field(min_length=1)
    weights: Weights
    tiers: Tiers
    nudge_limit: int = Field(default=MAX_NUDGE, ge=0, le=MAX_NUDGE)
    fit_rules: list[ScoringRule] = Field(min_length=1)
    intent_rules: list[ScoringRule] = Field(min_length=1)
    engagement_rules: list[ScoringRule] = Field(min_length=1)
    disqualifiers: list[DisqualifierRule] = []

    @model_validator(mode="after")
    def _cross_checks(self) -> "ScoringConfig":
        seen: set[str] = set()
        groups = {"fit_rules": self.fit_rules, "intent_rules": self.intent_rules,
                  "engagement_rules": self.engagement_rules,
                  "disqualifiers": self.disqualifiers}
        for group, rules in groups.items():
            for i, rule in enumerate(rules):
                where = f"{group}[{i}]"
                if rule.id in seen:
                    raise ValueError(f"{where}.id: duplicate rule id '{rule.id}'")
                seen.add(rule.id)
                spec = self.lead_fields.get(rule.field)
                if spec is None:
                    raise ValueError(
                        f"{where}.field: '{rule.field}' is not declared in lead_fields")
                if rule.op in _NUMERIC_OPS and spec.type not in {"int", "float"}:
                    raise ValueError(f"{where}.op: '{rule.op}' needs a numeric field, "
                                     f"'{rule.field}' is {spec.type}")
                if rule.op == "contains_any" and spec.type != "list":
                    raise ValueError(f"{where}.op: 'contains_any' needs a list field, "
                                     f"'{rule.field}' is {spec.type}")
        return self


# --- enrichment.yaml ---------------------------------------------------------------------

class Extractor(Strict):
    """Reads one lead field out of page text. Rules, not a model: reproducible and quotable."""

    field: str = Field(min_length=1)
    kind: Literal["number", "keyword", "flag"]
    pattern: str | None = None                       # number: one capture group
    map: dict[str, list[str]] | None = None          # keyword: value -> words that imply it
    any: list[str] | None = None                     # flag: true if any of these words appear

    @model_validator(mode="after")
    def _shape(self) -> "Extractor":
        if self.kind == "number":
            try:
                compiled = re.compile(self.pattern or "")
            except re.error as exc:
                raise ValueError(f"pattern is not a valid regular expression: {exc}") from exc
            if not self.pattern or compiled.groups != 1:
                raise ValueError("a number extractor needs a pattern with exactly one capture group")
        elif self.kind == "keyword" and not self.map:
            raise ValueError("a keyword extractor needs a non-empty map")
        elif self.kind == "flag" and not self.any:
            raise ValueError("a flag extractor needs a non-empty 'any' list")
        return self


class WebResearch(Strict):
    """How a real web search is run for a lead. Defaults suit every pack; a pack may override them."""

    queries: list[str] = Field(default_factory=lambda: ["{company} {domain}"], min_length=1)
    max_searches: int = Field(default=1, ge=1, le=3)       # search calls per lead
    max_page_fetches: int = Field(default=2, ge=0, le=5)   # page extractions per lead


class EnrichmentConfig(Strict):
    pages: list[str] = Field(min_length=1)           # paths on the company's own site
    extractors: list[Extractor] = Field(min_length=1)
    web: WebResearch = Field(default_factory=WebResearch)

    @field_validator("pages")
    @classmethod
    def _paths(cls, value: list[str]) -> list[str]:
        bad = [p for p in value if not p.startswith("/")]
        if bad:
            raise ValueError("pages must be paths starting with '/': " + ", ".join(bad))
        return value


# --- stages.yaml -------------------------------------------------------------------------

class StagesConfig(Strict):
    exit_criteria: dict[DealStage, list[str]]       # stage entered -> fields that must exist
    sla_hours: dict[DealStage, int]                 # max hours a deal may sit in a stage
    qualification_fields: list[str] = Field(min_length=1)

    @field_validator("sla_hours")
    @classmethod
    def _positive(cls, value: dict[DealStage, int]) -> dict[DealStage, int]:
        bad = [str(stage) for stage, hours in value.items() if hours <= 0]
        if bad:
            raise ValueError("SLA hours must be positive: " + ", ".join(bad))
        return value


# --- policy.yaml -------------------------------------------------------------------------

class SendWindowConfig(Strict):
    start_hour: int = Field(ge=0, le=23)
    end_hour: int = Field(ge=1, le=24)
    days: list[int] = Field(min_length=1)       # Monday = 0

    @model_validator(mode="after")
    def _valid(self) -> "SendWindowConfig":
        if self.end_hour <= self.start_hour:
            raise ValueError("end_hour must be later than start_hour")
        if any(d < 0 or d > 6 for d in self.days) or len(set(self.days)) != len(self.days):
            raise ValueError("days must be unique numbers from 0 (Monday) to 6 (Sunday)")
        return self


class PolicyPack(Strict):
    autonomy_levels: dict[str, AutonomyLevel]
    max_discount_pct: float = Field(ge=0, le=100)
    daily_cap: int = Field(gt=0)
    domain_cap: int = Field(gt=0)
    veto_window_seconds: int = Field(ge=0)
    send_window: SendWindowConfig
    default_timezone: str = "UTC"
    # The IO's own limits (SAMPLE numbers: ours, not COOL's). A budget outside the bounds, a term longer
    # than the maximum, or a discount over max_discount_pct all stop the IO.
    io_budget_min_usd: float = Field(default=5_000, ge=0)
    io_budget_max_usd: float = Field(default=2_000_000, gt=0)
    io_max_term_months: int = Field(default=24, gt=0)
    io_payment_terms: str = "Net 30 (SAMPLE terms)"
    io_min_lead_days: int = Field(default=14, ge=0)       # setup time before an IO can start
    io_default_term_months: int = Field(default=3, gt=0)  # the term when nothing else is stated

    @field_validator("autonomy_levels")
    @classmethod
    def _complete_allowlist(cls, value: dict[str, AutonomyLevel]) -> dict[str, AutonomyLevel]:
        unknown = sorted(set(value) - KNOWN_ACTION_KEYS)
        if unknown:
            raise ValueError("unknown action(s): " + ", ".join(unknown))
        missing = sorted(KNOWN_ACTION_KEYS - set(value))
        if missing:
            raise ValueError("missing action(s), give each a level: " + ", ".join(missing))
        return value

    @field_validator("default_timezone")
    @classmethod
    def _known_zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown time zone '{value}'") from exc
        return value


# --- messaging.yaml ----------------------------------------------------------------------

class Pillar(Strict):
    id: str = Field(pattern=ID)
    headline: str = Field(min_length=1)
    body: str = Field(min_length=1)


class EmailTemplate(Strict):
    subject: str = Field(min_length=1)
    body: str = Field(min_length=1)

    @field_validator("subject")
    @classmethod
    def _subject_ok(cls, value: str) -> str:
        return check_template(value)

    # Every email carries an unsubscribe line and a postal address (CLAUDE.md safety rules).
    @field_validator("body")
    @classmethod
    def _body_ok(cls, value: str) -> str:
        return check_template(value, ("unsubscribe_line", "postal_address"))


class LinkedInConfig(Strict):
    """How this segment's LinkedIn DRAFTS read. Drafts only: a person copies and sends them; nothing in the
    system can send to LinkedIn. The note is a connection request (at most 300 characters, LinkedIn's limit);
    the message follows once connected and carries the one ask."""

    tone: list[str] = Field(min_length=1)
    note_max_chars: int = Field(ge=60, le=300)
    message_max_chars: int = Field(ge=120, le=1900)
    use_case_study: bool = False                  # whether the follow-up message cites a library case study
    note_template: str = Field(min_length=1)
    message_template: str = Field(min_length=1)
    note_fallback: str = Field(min_length=1)      # the pack's wording if a model-written line fails a check
    message_fallback: str = Field(min_length=1)

    @model_validator(mode="after")
    def _templates(self) -> "LinkedInConfig":
        for name in ("note_template", "message_template"):
            check_template(getattr(self, name), ("personalised_line",))
        return self


class Messaging(Strict):
    source_note: str = Field(min_length=1)
    tone: list[str] = Field(min_length=1)
    pillars: list[Pillar] = Field(min_length=1)
    templates: dict[str, EmailTemplate]
    linkedin: LinkedInConfig | None = None          # absent: this segment has no LinkedIn drafts yet
    # Short pieces of wording the code fills in (a reply acknowledgement, how the slot list is
    # introduced). They are approved text for the claim checker, like the templates.
    snippets: dict[str, str] = Field(default_factory=dict)

    @field_validator("snippets")
    @classmethod
    def _required_snippets(cls, value: dict[str, str]) -> dict[str, str]:
        missing = sorted({"reply_ack_interested", "reply_ack_default", "slots_intro_local",
                          "slots_intro_utc", "first_touch_fallback", "follow_up_fallback"} - set(value))
        if missing:
            raise ValueError("missing snippet(s): " + ", ".join(missing))
        return value

    @field_validator("templates")
    @classmethod
    def _required_templates(cls, value: dict[str, EmailTemplate]) -> dict[str, EmailTemplate]:
        missing = sorted({"first_touch", "follow_up", "case_study_reply", "meeting_offer",
                            "meeting_confirmation"} - set(value))
        if missing:
            raise ValueError("missing template(s): " + ", ".join(missing))
        return value


# --- case_studies.yaml -------------------------------------------------------------------

class Metric(Strict):
    label: str = Field(min_length=1)
    value: str = Field(min_length=1)


class CaseStudy(Strict):
    id: str = Field(pattern=ID)
    customer: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    tags: list[str] = []
    metrics: list[Metric] = []
    source_url: str = Field(pattern=r"^https?://\S+$")
    source_verified: bool                      # False: page listed in the brief, not yet matched
    label: Literal["public testimonial", "vendor-reported"]     # how every use of it is marked
    quote_dollar_figures_only: bool = False
    use_for: list[str] = []                    # situations to use it in (routing, not a claim)

    @model_validator(mode="after")
    def _dollar_figures_only(self) -> "CaseStudy":
        if self.quote_dollar_figures_only:
            bad = [m.label for m in self.metrics if "$" not in m.value or "%" in m.value]
            if not self.metrics or bad:
                raise ValueError("quote_dollar_figures_only: metrics must be dollar figures only"
                                 + (f" (check: {', '.join(bad)})" if bad else ""))
        return self

    @property
    def metric_text(self) -> str:
        return "; ".join(f"{m.label}: {m.value}" for m in self.metrics)


class CaseStudyLibrary(Strict):
    case_studies: list[CaseStudy] = Field(min_length=1)

    @field_validator("case_studies")
    @classmethod
    def _unique(cls, value: list[CaseStudy]) -> list[CaseStudy]:
        ids = [c.id for c in value]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError("duplicate case study id(s): " + ", ".join(dupes))
        return value


# --- rate_card.yaml ----------------------------------------------------------------------

class RateItem(Strict):
    id: str = Field(pattern=ID)
    name: str = Field(min_length=1)
    unit: str = Field(min_length=1)
    price: float = Field(gt=0)


class PackageLine(Strict):
    id: str = Field(pattern=ID)                       # a rate card item
    quantity: float = Field(gt=0, default=1)          # per month (or 1 for a percentage fee)
    quantity_from: str | None = None                  # a lead field (location_count) that sets the quantity
    quantity_max: float | None = Field(default=None, gt=0)


class Package(Strict):
    id: str = Field(pattern=ID)
    name: str = Field(min_length=1)
    min_monthly_budget_usd: float = Field(ge=0)       # chosen when the stated budget reaches this; the IO's
    # monthly subtotal must reach it too
    lines: list[PackageLine] = Field(min_length=1)
    media_item: str | None = None                     # the rate card item that takes the rest of the monthly
    # budget (campaign media); its quantity is whatever the fixed lines leave


class VolumeDiscount(Strict):
    min_monthly_budget_usd: float = Field(ge=0)
    discount_pct: float = Field(ge=0, le=100)


class RateCard(Strict):
    label: str
    currency: str = Field(min_length=3, max_length=3)
    items: list[RateItem] = Field(min_length=1)
    # SAMPLE packages the IO planner chooses from by budget, and the only discounts it may apply.
    packages: list[Package] = Field(default_factory=list)
    volume_discounts: list[VolumeDiscount] = Field(default_factory=list)

    # Honesty rule: COOL publishes no rate card, so ours must say it is a sample.
    @field_validator("label")
    @classmethod
    def _says_sample(cls, value: str) -> str:
        if "sample" not in value.lower():
            raise ValueError("the rate card label must say it is a SAMPLE")
        return value
