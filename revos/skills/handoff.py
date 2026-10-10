"""The closed-won message for Customer Success: what was sold, to whom, and what is still open."""

from collections.abc import Mapping, Sequence
from typing import Any

from revos.skills.brief import open_objections


def build_handoff(*, company: str, contact: str | None, tier: str | None, total: int | None,
                  fields: Mapping[str, Any], terms: Mapping[str, Any], rate_card_label: str,
                  envelope_id: str, lesson_ids: Sequence[int]) -> str:
    signer = (f"{fields['signer_name']} ({fields['signer_role']})"
              if fields.get("signer_name") else "not recorded")
    decision = (f"{fields['decision_maker_name']} ({fields['decision_maker_role']})"
                if fields.get("decision_maker_name") else fields.get("decision_maker") or "not recorded")
    score = f"tier {tier} ({total}/100)" if tier else "not scored"
    items = "; ".join(f"{line['quantity']} x {line['name']} at {line['unit_price']} {line['unit']}"
                      for line in terms["line_items"])
    discount = fields.get("discount_pct", 0) or 0
    open_items = [*open_objections(fields),
                  f"agree the kick-off for the start date {fields.get('io_start_date')}",
                  "replace the sample terms with the real agreement: this IO used a sample rate card"]
    lines = [
        f"Closed won: {company}",
        f"Summary: {score}. Contact {contact or 'unknown'}. Decision maker {decision}.",
        f"Signer: {signer}.",
        f"Terms: {items}. Total {terms['total']} after a {discount}% discount, "
        f"{fields.get('io_start_date')} to {fields.get('io_end_date')}. {rate_card_label}",
        f"Signature: envelope {envelope_id}, TEST MODE, so it is not legally binding.",
        "Open items: " + "; ".join(open_items) + ".",
        "Lessons used on this deal: " + (", ".join(f"L{i}" for i in lesson_ids) or "none") + ".",
    ]
    return "\n".join(lines)
