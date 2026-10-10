"""Build and validate an insertion order from deal data and the pack's SAMPLE rate card."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import jinja2

from revos.domain.stages import DealStage
from revos.segments.loader import Pack


@dataclass(frozen=True)
class IOValidation:
    ok: bool
    problems: tuple[str, ...] = ()
    total: float = 0.0
    line_items: tuple[dict[str, Any], ...] = field(default_factory=tuple)


def validate_io(pack: Pack, fields: Mapping[str, Any]) -> IOValidation:
    """Pure check of the deal's IO inputs: required fields, rate-card items, discount limit."""
    problems: list[str] = []
    required = pack.stages.exit_criteria.get(DealStage.QUALIFIED, [])
    problems += [f"missing deal field: {name}" for name in required if not fields.get(name)]

    catalogue = {item.id: item for item in pack.rate_card.items}
    requested = fields.get("io_items")
    lines: list[dict[str, Any]] = []
    if not isinstance(requested, list) or not requested:
        problems.append("no IO line items requested")
        requested = []
    for entry in requested:
        item = catalogue.get(entry.get("id")) if isinstance(entry, dict) else None
        quantity = entry.get("quantity") if isinstance(entry, dict) else None
        if item is None:
            problems.append(f"item {entry.get('id') if isinstance(entry, dict) else entry!r} "
                            "is not on the rate card")
        elif not isinstance(quantity, int | float) or isinstance(quantity, bool) or quantity <= 0:
            problems.append(f"item {item.id} needs a positive quantity")
        else:
            lines.append({"name": item.name, "unit": item.unit, "quantity": quantity,
                          "unit_price": item.price, "amount": round(item.price * quantity, 2)})
    discount = fields.get("discount_pct", 0)
    if not isinstance(discount, int | float) or isinstance(discount, bool) or discount < 0:
        problems.append("discount_pct must be a number from 0")
        discount = 0
    elif discount > pack.policy.max_discount_pct:
        problems.append(f"discount {discount}% is above the maximum of {pack.policy.max_discount_pct}%")
    total = round(sum(line["amount"] for line in lines) * (1 - float(discount) / 100), 2)
    return IOValidation(not problems, tuple(problems), total, tuple(lines))


def render_io(pack: Pack, advertiser: str, validation: IOValidation, discount_pct: float,
              io_number: str, date: str) -> str:
    env = jinja2.Environment(undefined=jinja2.StrictUndefined, autoescape=False)
    return env.from_string(pack.templates["io"]).render(
        advertiser_name=advertiser, line_items=list(validation.line_items), total=validation.total,
        rate_card_label=pack.rate_card.label, discount_pct=discount_pct, io_number=io_number,
        date=date)
