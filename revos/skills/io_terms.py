"""Propose the IO's terms from the qualified call fields and the pack's SAMPLE rate card. Pure code.

  package   the largest pack package whose minimum monthly budget the stated budget reaches
  start     the pack's setup lead time from today, rounded up to a Monday, inside the stated timeline
  term      the pack's default term
  quantities PER MONTH; campaign media takes the rest of the monthly budget
  discount  0, unless the pack's own volume rules apply

A proposal is `ok` only when nothing falls outside the pack's limits. Otherwise it says exactly which
term does, and a person decides; nothing here relaxes a limit."""

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from revos.segments.loader import Pack
from revos.skills.io import monthly_budget, validate_io

_WORD_NUMBERS = {w: n for n, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve".split())}
_QUARTER_START = {1: (1, 1), 2: (4, 1), 3: (7, 1), 4: (10, 1)}


@dataclass(frozen=True)
class IOTermsProposal:
    ok: bool
    items: tuple[dict[str, Any], ...] = ()
    start: str = ""
    end: str = ""
    discount_pct: float = 0.0
    package_id: str = ""
    monthly_budget_usd: float | None = None
    deadline: str = ""
    needs_a_person: tuple[str, ...] = ()          # the terms that fall outside the pack's limits
    notes: tuple[str, ...] = field(default_factory=tuple)


def parse_deadline(text: Any, today: date) -> date | None:
    """When the lead wants to be running: 'within six weeks', 'in 3 months', 'by Q1'. None if not stated."""
    if not isinstance(text, str):
        return None
    low = text.lower()
    numbers = r"(\d{1,2}|" + "|".join(_WORD_NUMBERS) + ")"
    if m := re.search(r"\b(?:within|in|inside|under)\s+" + numbers + r"\s+(week|month)s?", low):
        n = int(m.group(1)) if m.group(1).isdigit() else _WORD_NUMBERS[m.group(1)]
        return today + timedelta(days=n * (7 if m.group(2) == "week" else 30))
    if m := re.search(r"\bby\s+q([1-4])\b", low):
        month, day = _QUARTER_START[int(m.group(1))]
        year = today.year if date(today.year, month, day) > today else today.year + 1
        return date(year, month, day) + timedelta(days=89)           # the end of that quarter, roughly
    return None


def _monday_on_or_after(day: date) -> date:
    return day + timedelta(days=(7 - day.weekday()) % 7)


def propose_io_terms(pack: Pack, fields: Mapping[str, Any], today: date) -> IOTermsProposal:
    card, policy = pack.rate_card, pack.policy
    person: list[str] = []
    notes: list[str] = []
    monthly, period = monthly_budget(fields.get("budget"))
    if not card.packages:
        person.append("the pack defines no packages to choose from")
    elif fields.get("budget") and monthly is None:
        person.append("budget: the call states no amount or no period (a month, a quarter or a year), "
                      "so a package cannot be chosen")
    elif not fields.get("budget"):
        person.append("budget: not stated on the call")
    package = None
    if monthly is not None and card.packages:
        eligible = [p for p in card.packages if monthly >= p.min_monthly_budget_usd]
        package = max(eligible, key=lambda p: p.min_monthly_budget_usd) if eligible else None
        if package is None:
            smallest = min(p.min_monthly_budget_usd for p in card.packages)
            person.append(f"budget ${monthly:,.0f} a month is below the smallest package "
                          f"(${smallest:,.0f} a month)")
        else:
            notes.append(f"package {package.id} chosen: ${monthly:,.0f} a month reaches its "
                         f"${package.min_monthly_budget_usd:,.0f} minimum (stated per {period})")

    deadline = parse_deadline(fields.get("timeline"), today)
    start = _monday_on_or_after(today + timedelta(days=policy.io_min_lead_days))
    if not fields.get("timeline"):
        person.append("timeline: not stated on the call")
    elif deadline is None:
        person.append("timeline: no deadline could be read from the call quote")
    elif start > deadline:
        person.append(f"timeline: they want to be running by {deadline.isoformat()}, sooner than our "
                      f"{policy.io_min_lead_days}-day setup allows (earliest start {start.isoformat()})")
    months = policy.io_default_term_months
    end = date(start.year + (start.month - 1 + months) // 12, (start.month - 1 + months) % 12 + 1, start.day)

    items: list[dict[str, Any]] = []
    if package is not None:
        catalogue = {i.id: i for i in card.items}
        for line in package.lines:
            if line.id not in catalogue:
                person.append(f"package {package.id} names {line.id!r}, which is not on the rate card")
                continue
            base = line.quantity
            if line.quantity_from:
                value = fields.get(line.quantity_from)
                if not isinstance(value, int | float) or isinstance(value, bool) or value <= 0:
                    person.append(f"{line.quantity_from} is not known, so the quantity of "
                                  f"{line.id} cannot be set")
                    continue
                base = min(float(value), line.quantity_max) if line.quantity_max else float(value)
            # quantities are PER MONTH: the document multiplies by the months of the term
            items.append({"id": line.id, "quantity": int(base) if float(base).is_integer() else base})
        if package.media_item and monthly is not None:
            # the rest of the monthly budget goes to campaign media, so the monthly subtotal lands between
            # the package minimum and the stated budget
            if package.media_item not in catalogue:
                person.append(f"package {package.id} names {package.media_item!r}, "
                              "which is not on the rate card")
            else:
                fixed = sum(catalogue[i["id"]].price * i["quantity"] for i in items if i["id"] in catalogue)
                media = math.floor((monthly - fixed) / catalogue[package.media_item].price)
                if media <= 0:
                    person.append(f"the {package.name} service lines (${fixed:,.0f} a month) leave nothing "
                                  f"of the ${monthly:,.0f} a month budget for media")
                else:
                    items.append({"id": package.media_item, "quantity": media})
                    notes.append(f"campaign media takes the rest of the monthly budget: {media:,} "
                                 f"after ${fixed:,.0f} of service lines")

    discount = 0.0
    if monthly is not None:
        earned = [d.discount_pct for d in card.volume_discounts if monthly >= d.min_monthly_budget_usd]
        discount = max(earned, default=0.0)
        if discount:
            notes.append(f"volume discount {discount}% from the pack's rules")
    if discount > policy.max_discount_pct:
        person.append(f"the pack's own volume rule gives {discount}%, above its maximum of "
                      f"{policy.max_discount_pct}%: a hard block")

    proposal = IOTermsProposal(
        not person, tuple(items), start.isoformat(), end.isoformat(), discount, package.id if package else "",
        monthly, deadline.isoformat() if deadline else "", tuple(person), tuple(notes))
    if proposal.ok:                                      # one last look at the finished IO terms
        check = validate_io(pack, {**fields, "io_items": list(items), "io_start_date": proposal.start,
                                   "io_end_date": proposal.end, "discount_pct": discount,
                                   "io_package": proposal.package_id},
                            today=today)
        prefixes = ("io_", "item ", "price", "discount", "the term", "no IO", "HARD", "the monthly",
                    "package")
        terms_problems = [p for p in check.problems if p.startswith(prefixes)]
        if terms_problems:
            return IOTermsProposal(
                False, proposal.items, proposal.start, proposal.end, discount, proposal.package_id, monthly,
                proposal.deadline, tuple(terms_problems), proposal.notes)
    return proposal
