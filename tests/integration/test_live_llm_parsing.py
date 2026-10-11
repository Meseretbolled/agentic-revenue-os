"""What the real model really returned (a fenced answer, then a self-correction) and the evidence check."""

import json

import pytest

from revos.adapters.claude_llm import CallResult, ClaudeConfig, ClaudeLLM
from revos.adapters.llm_tasks import extract_json_object
from revos.domain.errors import LLMError
from revos.domain.untrusted import UntrustedText

FENCED_THEN_CORRECTED = (
    '```json\n{"personalised_line": "first try", "claims": []}\n```\n\nWait, that was wrong. Correcting:\n\n'
    '{"personalised_line": "second try", "claims": [{"text": "x", "evidence": ["F1"]}]}')


def test_a_fenced_answer_and_a_later_correction_use_the_last_complete_object():
    assert extract_json_object(FENCED_THEN_CORRECTED, "draft_email")["personalised_line"] == "second try"
    assert extract_json_object('Sure!\n```json\n{"a": {"b": 1}}\n```', "t") == {"a": {"b": 1}}
    assert extract_json_object('text {"a": 1} and {broken', "t") == {"a": 1}


@pytest.mark.parametrize("raw", ["no json here", "{broken", "[1, 2]", ""])
def test_no_object_is_an_error(raw):
    with pytest.raises(LLMError):
        extract_json_object(raw, "t")


def llm(conn, answer: dict):
    transport = lambda *_a: CallResult(json.dumps(answer), 0.001, 100, 20)       # noqa: E731
    return ClaudeLLM(conn, ClaudeConfig(max_spend_usd=1.0), transport)


REPLY = UntrustedText.wrap("reply", "m1", "Yes, interested, can we talk Thursday?\n\n> earlier: sounds good")


def test_a_quote_that_is_in_the_reply_is_kept_and_marked_verified(conn):
    got = llm(conn, {"label": "interested", "confidence": 0.9, "rationale": "r",
                     "quote": "Yes, interested, can we talk Thursday?"}).classify_reply(REPLY)
    assert got.label == "interested" and got.quote_verified and got.quote.startswith("Yes, interested")


def test_a_quote_the_model_made_up_is_dropped_and_marked_unverified(conn):
    got = llm(conn, {"label": "interested", "confidence": 0.9, "rationale": "r",
                     "quote": "I would love to buy right now"}).classify_reply(REPLY)
    assert got.label == "interested" and got.quote == "" and not got.quote_verified


def test_tokens_and_cost_are_recorded_in_the_ledger(conn):
    llm(conn, {"label": "interested", "confidence": 0.9, "rationale": "r", "quote": "interested"}
        ).classify_reply(REPLY)
    row = conn.execute("SELECT input_tokens, output_tokens, cost_usd FROM llm_spend").fetchone()
    assert (row[0], row[1], float(row[2])) == (100, 20, 0.001)
