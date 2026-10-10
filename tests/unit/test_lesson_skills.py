import pytest

from revos.adapters.llm_tasks import draft_prompt
from revos.domain.drafting import Evidence
from revos.ports.llm import DraftRequest
from revos.skills.angle import choose_pillar
from revos.skills.brief import build_brief, open_objections
from revos.skills.people import Person, parse_person_role


@pytest.mark.parametrize("quote,person", [
    ("Dana Park, our CMO, is the decision maker.", Person("Dana Park", "CMO")),
    ("Dana Park is our Head of Marketing and the decision maker.", Person("Dana Park", "Head of Marketing")),
    ("It is Maria De Luca, the VP Growth, who signs.", Person("Maria De Luca", "VP Growth")),
    ("Our CMO is the decision maker.", None),
    ("The decision maker is whoever has budget.", None),
    ("", None),
])
def test_name_and_role_are_read_from_the_quote_or_not_at_all(quote, person) -> None:
    got = parse_person_role(quote)
    assert got == person
    if got:
        assert got.name in quote and got.role in quote                 # nothing outside the quote


def lesson(i, tags, text, confidence=0.5):
    return {"id": i, "tags": tags, "text": text, "confidence": confidence}


def test_the_pillar_follows_the_lessons_that_point_at_it(pack) -> None:
    first, second = pack.messaging.pillars[0], pack.messaging.pillars[1]
    pillar, ids = choose_pillar(pack.messaging.pillars, [lesson(7, ["attribution"],
                                                               "buyers want results and attribution in one place")])
    assert pillar.id == second.id and ids == [7]
    pillar, ids = choose_pillar(pack.messaging.pillars, [lesson(3, ["compliance"],
                                                               "owners worry about brand compliance")])
    assert pillar.id == first.id and ids == [3]


def test_no_overlap_means_the_default_pillar_and_no_lesson_is_credited(pack) -> None:
    pillar, ids = choose_pillar(pack.messaging.pillars, [lesson(1, ["weather"], "it rained")])
    assert pillar.id == pack.messaging.pillars[0].id and ids == []
    assert choose_pillar(pack.messaging.pillars, [])[1] == []


def test_only_lessons_that_pointed_at_the_chosen_pillar_are_cited(pack) -> None:
    lessons = [lesson(1, ["attribution", "results"], "results and attribution matter", 0.9),
               lesson(2, ["compliance"], "brand compliance worries owners", 0.4)]
    pillar, ids = choose_pillar(pack.messaging.pillars, lessons)
    assert pillar.id == pack.messaging.pillars[1].id and ids == [1]


def test_confidence_weights_the_choice(pack) -> None:
    lessons = [lesson(1, ["compliance"], "brand compliance", 0.2),
               lesson(2, ["attribution"], "results attribution", 0.9)]
    assert choose_pillar(pack.messaging.pillars, lessons)[1] == [2]


def test_the_draft_prompt_gives_the_angle_as_an_angle_only() -> None:
    request = DraftRequest("s", "b", {"first_name": "Sam", "company": "Acme", "angle": "See which campaigns work"},
                           Evidence())
    text = draft_prompt(request)
    assert "Lead with this angle, as an angle only (never as a new fact): See which campaigns work" in text
    assert "angle" not in draft_prompt(DraftRequest("s", "b", {"first_name": "S", "company": "A"}, Evidence()))


def test_the_brief_summarises_lists_open_objections_and_cites_lessons() -> None:
    fields = {"budget": "about fifty thousand", "timeline": "launch by Q1", "needs": "local campaigns",
              "objections": "owners worry about compliance", "open_objections": ["price_objection"],
              "decision_maker_name": "Dana Park", "decision_maker_role": "CMO"}
    text = build_brief(company="Acme", contact="Sam Lee", stage="replied", tier="B", total=61,
                       fields=fields, evidence=Evidence(),
                       lessons=[{"id": 4, "text": "Lead with compliance", "confidence": 0.62}])
    for expected in ("Call brief: Acme", "stage replied, tier B (61/100)",
                     "decision maker Dana Park (CMO)", "budget: about fifty thousand",
                     "raised on the call: owners worry about compliance",
                     "price objection in a reply", "L4 (confidence 0.62): Lead with compliance"):
        assert expected in text, expected


def test_the_brief_says_so_when_nothing_is_known() -> None:
    text = build_brief(company="Acme", contact=None, stage="new", tier=None, total=None, fields={},
                       evidence=Evidence(), lessons=[])
    assert "not scored" in text and "nothing yet" in text and "none recorded" in text
    assert "Lessons used: none." in text and open_objections({}) == []
