"""One live, spend-capped model drafting call, checked by the same code as production.

    make db-up && .venv/bin/python scripts/live_draft.py

It drafts the personal paragraph of a first email for the fictional hot seed lead from its sourced
(fictional demo) facts, the pack's tone rules and a cited lesson, with your ANTHROPIC_API_KEY (read
from .env by the app's loader; never printed). Then it runs the claim checker (code, then a model
verifier) and prints the draft, the result and what the call cost. Nothing is sent anywhere and no
deal is created. It stops at LIVE_DRAFT_MAX_USD (default 0.25)."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    from revos.env import load_env_file
    load_env_file()
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        print("ANTHROPIC_API_KEY is not set in .env: no live call was made. Add it and run this again.")
        return 2

    from revos.adapters.claude_llm import ClaudeConfig, ClaudeLLM
    from revos.agents.common import check_inputs, evidence_from_fields
    from revos.agents.types import Identity
    from revos.db import connect, migrate
    from revos.domain.errors import LLMError, SpendCapExceeded
    from revos.ports.llm import DraftRequest
    from revos.seed import load_profiles
    from revos.segments import load_all
    from revos.skills.drafting import draft_with_checks

    packs = load_all(ROOT / "packs")
    pack = next(p for p in packs.values() if p.meta.demo)
    profile = load_profiles(pack.id)["hot"]
    fields = {}
    for n, note in enumerate(profile["research_notes"], 1):
        fields[f"fact:DEMO{n}"], fields[f"fact_source:DEMO{n}"] = note["text"], note["source"]
    evidence = evidence_from_fields(fields)
    identity = Identity()
    template = pack.messaging.templates["first_touch"]
    context = {"company": profile["company"], "first_name": profile["contact"].split()[0],
               "sender_name": identity.sender_name, "unsubscribe_line": identity.unsubscribe_line,
               "postal_address": identity.postal_address, "pillar": pack.messaging.pillars[0],
               "case_study": None, "angle": pack.messaging.pillars[0].headline}
    request = DraftRequest(
        template.subject, template.body, context, evidence, purpose="first_touch",
        tone=tuple(pack.messaging.tone),
        lessons=({"id": 0, "text": "Buyers care about keeping local campaigns on brand."},))
    approved, names = check_inputs(pack, template, identity, profile["company"], profile["contact"], None, ())

    cap = float(os.environ.get("LIVE_DRAFT_MAX_USD", "0.25"))
    config = ClaudeConfig(model_drafting=ClaudeConfig.from_env().model_drafting,
                          model_classify=ClaudeConfig.from_env().model_classify, max_spend_usd=cap)
    with connect() as conn:
        migrate(conn)
        llm = ClaudeLLM(conn, config)
        print(f"drafting model: {config.model_drafting}  (classification would use {config.model_classify})")
        print(f"spend cap for this run: ${cap:.2f}\n")
        try:
            outcome = draft_with_checks(llm, request, approved, names, llm)
        except (LLMError, SpendCapExceeded) as exc:
            print(f"The call failed ({type(exc).__name__}): {exc}")
            return 1
        print(f"status: {outcome.status} after {outcome.attempts} attempt(s)")
        if outcome.draft is None:
            print("issues:", [(i.code, i.message) for i in outcome.issues])
            return 1
        print("\n--- draft body (the model wrote the first paragraph; code wrote the rest) ---")
        print(outcome.draft.body)
        print("\n--- claims ---")
        for claim in outcome.draft.claims:
            print(f"  cites {list(claim.evidence)}: {claim.text}")
        print("\n--- claim-check result ---")
        if outcome.status == "passed":
            print("PASSED: no undeclared fact, every claim restates its cited evidence, the model verifier "
                  "found each claim supported, no instruction-like wording, footer present.")
        else:
            print("NOT PASSED:", [(i.code, i.message) for i in outcome.issues],
                  "(in production this falls back to the pack's own wording and writes an audit row)")
        print(f"\nspent so far (all runs): ${llm.spent_usd():.4f} of ${cap:.2f}")
    return 0 if outcome.status == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
