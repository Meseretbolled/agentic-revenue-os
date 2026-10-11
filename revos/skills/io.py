"""Build and validate an insertion order from deal data and the pack's SAMPLE rate card."""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import jinja2

from revos.domain.stages import DealStage
from revos.segments.loader import Pack

SIGNER_FIELDS = ("signer_name", "signer_role", "signer_email")
TERM_FIELDS = ("io_start_date", "io_end_date")


@dataclass(frozen=True)
class IOValidation:
    ok: bool
    problems: tuple[str, ...] = ()
    total: float = 0.0
    line_items: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    hard_block: bool = False          # a discount over the pack maximum: never sends, nobody can override
    budget_usd: float | None = None
    term_months: float | None = None
    payment_terms: str = ""
    monthly_subtotal: float = 0.0     # the lines, per month (quantities are per month), before any discount
    billed_months: float = 0.0        # the number of months the term is billed for
    term_subtotal: float = 0.0        # monthly subtotal x months, before the discount
    discount_pct: float = 0.0         # `total` is term_subtotal less this
    package_id: str = ""
    monthly_budget_usd: float | None = None


def _iso(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def _number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


_UNITS = {w: n for n, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
    "seventeen eighteen nineteen".split())}
_TENS = {w: 10 * n for n, w in enumerate(
    "zero ten twenty thirty forty fifty sixty seventy eighty ninety".split())}
_SCALE = {"thousand": 1_000, "million": 1_000_000}
_DIGITS = re.compile(r"\$?\s?(\d[\d,]*(?:\.\d+)?)\s?(k|m|thousand|million)?\b", re.IGNORECASE)


def parse_budget_usd(text: Any) -> float | None:
    """The dollar amount a budget sentence states ("about $40,000 a month", "two hundred thousand
    dollars"), or None if it does not state one. The caller must have the quote that backs the text."""
    if not isinstance(text, str) or not text.strip():
        return None
    if match := _DIGITS.search(text):
        number = float(match.group(1).replace(",", ""))
        scale = (match.group(2) or "").lower()
        return number * {"k": 1_000, "thousand": 1_000, "m": 1_000_000, "million": 1_000_000}.get(scale, 1)
    total = current = 0
    seen = False
    for word in re.findall(r"[a-z]+", text.lower()):
        if word in _UNITS:
            current += _UNITS[word]
            seen = True
        elif word in _TENS:
            current += _TENS[word]
            seen = True
        elif word == "hundred" and seen:
            current = max(current, 1) * 100
        elif word in _SCALE and seen:
            total += max(current, 1) * _SCALE[word]
            current = 0
        elif seen and word not in {"and", "dollars", "dollar", "usd"}:
            break
    amount = total + current
    return float(amount) if seen and amount > 0 else None


def monthly_budget(text: Any) -> tuple[float | None, str]:
    """(dollars per month, the period it was stated for). The period must be stated: never guessed."""
    amount = parse_budget_usd(text)
    if amount is None or not isinstance(text, str):
        return None, ""
    low = text.lower()
    if re.search(r"\b(a|per|each|every)\s+month\b|\bmonthly\b|/\s?mo(nth)?\b", low):
        return amount, "month"
    if re.search(r"\bquarter\b", low):
        return amount / 3, "quarter"
    if re.search(r"\b(a|per|each|every|first|this|next)\s+year\b|\bannual(ly)?\b|\byearly\b", low):
        return amount / 12, "year"
    return None, ""


def billed_months(start: date, end: date) -> float:
    """The months a term is billed for: whole when the dates are whole calendar months apart (or within
    a few days of it), otherwise to one decimal."""
    raw = (end - start).days / 30.4375
    return float(round(raw)) if abs(raw - round(raw)) <= 0.2 else round(raw, 1)


def validate_io(pack: Pack, fields: Mapping[str, Any], *, today: date,
                signer_allowed: Callable[[str], bool] | None = None) -> IOValidation:
    """Pure check of the deal's IO inputs. Every failure is reported with its reason.

    `signer_allowed` is the recipient allowlist (demo mode). The signer's address must be the lead
    record's `signer_email`; nothing a call or a payload says can supply it."""
    problems: list[str] = []
    required = [*pack.stages.exit_criteria.get(DealStage.QUALIFIED, []), *SIGNER_FIELDS, *TERM_FIELDS]
    problems += [f"missing deal field: {name}" for name in required if not fields.get(name)]

    start, end = _iso(fields.get("io_start_date")), _iso(fields.get("io_end_date"))
    for name, parsed in (("io_start_date", start), ("io_end_date", end)):
        if fields.get(name) and parsed is None:
            problems.append(f"{name} is not a valid date (use YYYY-MM-DD)")
    if start and start < today:
        problems.append("io_start_date is in the past")
    if start and end and end <= start:
        problems.append("io_end_date must be after io_start_date")
    months = billed_months(start, end) if start and end and end > start else None
    if months is not None and months > pack.policy.io_max_term_months:
        problems.append(f"the term of {months} months is longer than the maximum of "
                        f"{pack.policy.io_max_term_months} months")

    email = str(fields.get("signer_email") or "").strip()
    if email and "@" not in email:
        problems.append("signer_email is not an email address")
    elif email and signer_allowed is not None and not signer_allowed(email):
        problems.append("signer_email is not on the recipient allowlist")

    budget = parse_budget_usd(fields.get("budget"))
    if fields.get("budget") and budget is None:
        problems.append("budget: no dollar amount could be read from the call quote")
    elif budget is not None and not pack.policy.io_budget_min_usd <= budget <= pack.policy.io_budget_max_usd:
        problems.append(f"budget ${budget:,.0f} is outside the pack's bounds "
                        f"(${pack.policy.io_budget_min_usd:,.0f} to ${pack.policy.io_budget_max_usd:,.0f})")

    catalogue = {item.id: item for item in pack.rate_card.items}
    requested = fields.get("io_items")
    lines: list[dict[str, Any]] = []
    if not isinstance(requested, list) or not requested:
        problems.append("no IO line items requested")
        requested = []
    for entry in requested:
        entry = entry if isinstance(entry, dict) else {}
        item, quantity = catalogue.get(entry.get("id")), entry.get("quantity")
        if item is None:
            problems.append(f"item {entry.get('id')!r} is not on the rate card")
        elif not _number(quantity) or quantity <= 0:
            problems.append(f"item {item.id} needs a positive quantity")
        elif "unit_price" in entry and entry["unit_price"] != item.price:
            problems.append(f"price for {item.id} ({entry['unit_price']}) does not match the rate "
                            f"card ({item.price})")
        else:
            # quantities are PER MONTH; the amount is the line's monthly amount
            lines.append({"id": item.id, "name": item.name, "unit": item.unit, "quantity": quantity,
                          "unit_price": item.price, "amount": round(item.price * quantity, 2)})

    discount = fields.get("discount_pct", 0)
    hard_block = False
    if not _number(discount) or discount < 0:
        problems.append("discount_pct must be a number from 0")
        discount = 0
    elif discount > pack.policy.max_discount_pct:
        hard_block = True
        problems.append(f"HARD BLOCK: discount {discount}% is above the maximum of "
                        f"{pack.policy.max_discount_pct}%; it is never sent and cannot be overridden")
    monthly = round(sum(line["amount"] for line in lines), 2)
    term_subtotal = round(monthly * months, 2) if months else 0.0
    total = round(term_subtotal * (1 - float(discount) / 100), 2)

    # The totals must agree with the package and with what the lead said they would spend.
    budget_month, _period = monthly_budget(fields.get("budget"))
    package_id = str(fields.get("io_package") or "")
    package = next((p for p in pack.rate_card.packages if p.id == package_id), None)
    if package_id and package is None:
        problems.append(f"package {package_id!r} is not in the pack")
    if lines and months:
        net_monthly = total / months
        if package is not None and net_monthly + 0.005 < package.min_monthly_budget_usd:
            problems.append(
                f"the monthly subtotal ${net_monthly:,.2f} (term total ${total:,.2f} over {months:g} months) "
                f"is below the {package.name} minimum of ${package.min_monthly_budget_usd:,.2f} a month")
        if budget_month is not None and net_monthly > budget_month + 0.005:
            problems.append(
                f"the monthly subtotal ${net_monthly:,.2f} (term total ${total:,.2f} over {months:g} months) "
                f"is above the stated budget of ${budget_month:,.2f} a month")
    return IOValidation(not problems, tuple(problems), total, tuple(lines), hard_block, budget, months,
                        pack.policy.io_payment_terms, monthly, months or 0.0, term_subtotal,
                        float(discount), package_id, budget_month)


def money(value: float) -> str:
    """$1,234.50, or $1,234 when whole."""
    return f"${value:,.0f}" if float(value).is_integer() else f"${value:,.2f}"


def render_io(pack: Pack, advertiser: str, validation: IOValidation, fields: Mapping[str, Any],
              io_number: str, date_: str) -> str:
    env = jinja2.Environment(undefined=jinja2.StrictUndefined, autoescape=False)
    shown = [{**line, "quantity": f"{line['quantity']:,g}", "unit_price": money(line["unit_price"]),
              "amount": money(line["amount"])} for line in validation.line_items]
    months = validation.billed_months
    return env.from_string(pack.templates["io"]).render(
        advertiser_name=advertiser, line_items=shown, total=money(validation.total),
        rate_card_label=pack.rate_card.label, discount_pct=fields.get("discount_pct", 0) or 0,
        io_number=io_number, date=date_, start_date=fields.get("io_start_date", ""),
        end_date=fields.get("io_end_date", ""), signer_name=fields.get("signer_name", ""),
        signer_role=fields.get("signer_role", ""), signer_email=fields.get("signer_email", ""),
        budget_text=(f"{money(validation.monthly_budget_usd)} a month" if validation.monthly_budget_usd
                     else "not stated"),
        package_name=next((p.name for p in pack.rate_card.packages if p.id == validation.package_id), ""),
        monthly_subtotal=money(validation.monthly_subtotal), months=f"{months:g}",
        term_subtotal=money(validation.term_subtotal), payment_terms=validation.payment_terms)
