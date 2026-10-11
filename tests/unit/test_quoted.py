import pytest

from revos.adapters.fake_llm import FakeLLM
from revos.domain.quoted import new_text, quote_around
from revos.domain.untrusted import UntrustedText

HISTORY = """Yes, interested, can we talk Thursday?

On Sun, Oct 11, 2026 at 11:51 AM Jo Rivers <jo@gmail.com>
wrote:

> Hi Maya, our pricing is too expensive to ignore. Budget cost price discount.
> Would a short call be useful?
"""


def test_only_the_new_text_is_kept() -> None:
    assert new_text(HISTORY) == "Yes, interested, can we talk Thursday?"
    assert new_text("ok\n> quoted\nmore") == "ok\nmore"
    assert new_text("Thanks!\r\n\r\n-----Original Message-----\r\nFrom: x") == "Thanks!"
    assert new_text("plain") == "plain"


def test_a_reply_that_quotes_our_pricing_words_is_not_a_price_objection() -> None:
    reply = UntrustedText.wrap("reply", "<m@x>", HISTORY)
    got = FakeLLM().classify_reply(reply)
    assert got.label == "interested" and got.quote == "Yes, interested, can we talk Thursday?"


def test_the_quote_is_the_sentence_with_the_evidence_and_is_trimmed() -> None:
    assert quote_around("Hello. We are INTERESTED in this! Bye.", "interested") == "We are INTERESTED in this!"
    assert quote_around("nothing here", "zzz") == ""
    assert len(quote_around("x " * 300 + "interested", "interested")) <= 200


@pytest.mark.parametrize("text", ["no hit at all"])
def test_no_match_gives_no_quote(text) -> None:
    assert FakeLLM().classify_reply(UntrustedText.wrap("reply", "<m@x>", text)).quote == ""
