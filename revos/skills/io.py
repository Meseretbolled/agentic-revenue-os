"""Build and validate an insertion order from deal data and the pack's SAMPLE rate card."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import jinja2

from revos.domain.stages import DealStage
from revos.segments.loader import Pack

SIGNER_FIELDS = ("signer_name", "signer_role")
TERM_FIELDS = ("io_start_date", "io_end_date")


@dataclass(frozen=True)
class IOValidation:
    ok: bool
    problems: tuple[str, ...] = ()
    total: float = 0.0
    line_items: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    needs_manager_approval: bool = False


def _iso(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def _number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def manager_ceiling(fields: Mapping[str, Any]) -> float:
    """The discount a manager has approved, or 0. Only a person can write this field."""
    approval = fields.get("manager_approval")
    if isinstance(approval, dict) and approval.get("approved_by") and _number(approval.get("discount_pct")):
        return float(approval["discount_pct"])
    return 0.0


def validate_io(pack: Pack, fields: Mapping[str, Any], *, today: date) -> IOValidation:
    """Pure check of the deal's IO inputs. Every failure is reported with its reason."""
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
            lines.append({"name": item.name, "unit": item.unit, "quantity": quantity,
                          "unit_price": item.price, "amount": round(item.price * quantity, 2)})

    discount = fields.get("discount_pct", 0)
    needs_manager = False
    if not _number(discount) or discount < 0:
        problems.append("discount_pct must be a number from 0")
        discount = 0
    elif discount > max(pack.policy.max_discount_pct, manager_ceiling(fields)):
        needs_manager = True
        problems.append(f"discount {discount}% is above the maximum of {pack.policy.max_discount_pct}%"
                        " and needs a manager's approval")
    total = round(sum(line["amount"] for line in lines) * (1 - float(discount) / 100), 2)
    return IOValidation(not problems, tuple(problems), total, tuple(lines), needs_manager)


def render_io(pack: Pack, advertiser: str, validation: IOValidation, fields: Mapping[str, Any],
              io_number: str, date_: str) -> str:
    env = jinja2.Environment(undefined=jinja2.StrictUndefined, autoescape=False)
    return env.from_string(pack.templates["io"]).render(
        advertiser_name=advertiser, line_items=list(validation.line_items), total=validation.total,
        rate_card_label=pack.rate_card.label, discount_pct=fields.get("discount_pct", 0) or 0,
        io_number=io_number, date=date_, start_date=fields.get("io_start_date", ""),
        end_date=fields.get("io_end_date", ""), signer_name=fields.get("signer_name", ""),
        signer_role=fields.get("signer_role", ""))
