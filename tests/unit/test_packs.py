import shutil
from collections.abc import Callable
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from revos.cli.main import app
from revos.domain.actions import KNOWN_ACTION_KEYS
from revos.domain.enums import AutonomyLevel, AutonomyMode, Outcome, Tier
from revos.domain.stages import DealStage
from revos.policy.config import DEFAULT_LEVELS, PolicyConfig
from revos.policy.engine import ActionRequest, decide
from revos.segments import PackError, load_pack


def test_known_action_keys_match_the_policy_defaults() -> None:
    assert set(DEFAULT_LEVELS) == KNOWN_ACTION_KEYS


# --- the real pack ---------------------------------------------------------------------------

def test_real_pack_loads_with_everything_in_it(pack) -> None:
    assert pack.id == "franchise_brands"
    assert set(pack.templates) == {"io", "agreement"}
    assert set(pack.messaging.templates) >= {"first_touch", "follow_up", "case_study_reply"}
    assert pack.stages.exit_criteria[DealStage.QUALIFIED] == [
        "budget", "timeline", "decision_maker", "needs"]
    assert pack.stages.sla_hours[DealStage.REPLIED] > 0


def test_rate_card_is_labelled_as_a_sample(pack) -> None:
    assert "SAMPLE" in pack.rate_card.label
    assert "SAMPLE" in pack.templates["io"] and "{{ rate_card_label }}" in pack.templates["io"]


def test_every_case_study_has_a_source_link_a_label_and_is_verified(pack) -> None:
    for study in pack.case_studies:
        assert study.source_url.startswith("https://cool.co/")
        assert study.label in {"vendor-reported", "public testimonial"}
        assert study.source_verified is True
        assert study.tags


def test_case_study_facts_are_only_those_in_the_brief(pack) -> None:
    by_id = {c.id: c for c in pack.case_studies}
    assert {c.id for c in pack.case_studies} == {
        "global_beauty_brand", "ethos_veterinary_health", "luxury_retail_brand", "pearle_vision"}
    beauty = by_id["global_beauty_brand"]
    assert [(m.label, m.value) for m in beauty.metrics] == [
        ("ROAS", "$15.52 vs $3.80"), ("CPA", "$19.23 vs $87.64")]
    assert beauty.quote_dollar_figures_only
    assert "339" not in str(beauty) and "79%" not in str(beauty)     # the page's wrong percentages
    assert by_id["pearle_vision"].metrics == []
    assert by_id["global_beauty_brand"].tags == ["price", "cpm", "performance"]


def test_all_email_templates_carry_unsubscribe_and_postal_lines(pack) -> None:
    for tpl in pack.messaging.templates.values():
        assert "{{ unsubscribe_line }}" in tpl.body and "{{ postal_address }}" in tpl.body


# --- the pack drives the policy engine ---------------------------------------------------------

def test_policy_config_comes_from_the_pack(pack) -> None:
    config = PolicyConfig.from_pack(pack)
    assert config.levels["first_touch.A"] is AutonomyLevel.AUTO_WITH_VETO
    assert (config.daily_cap, config.domain_cap) == (50, 3)
    assert config.max_discount_pct == 15
    assert config.send_window.days == (0, 1, 2, 3, 4)


def test_pack_autonomy_levels_change_policy_outcomes(pack, pack_copy) -> None:
    request = ActionRequest("first_touch", tier=Tier.B, body="Hello", claim_check_passed=True)
    kwargs = {"mode": AutonomyMode.AUTONOMOUS, "kill_switch": False}
    assert decide(request, PolicyConfig.from_pack(pack), **kwargs).outcome is Outcome.AUTO

    path = pack_copy / "policy.yaml"
    data = yaml.safe_load(path.read_text())
    data["autonomy_levels"]["first_touch.B"] = "needs_approval"
    path.write_text(yaml.safe_dump(data))
    stricter = PolicyConfig.from_pack(load_pack(pack_copy))
    assert decide(request, stricter, **kwargs).outcome is Outcome.NEEDS_APPROVAL


# --- invalid packs fail loudly, naming file and field -----------------------------------------

def edit(pack_dir, filename: str, change: Callable[[dict[str, Any]], None]) -> None:
    path = pack_dir / filename
    data = yaml.safe_load(path.read_text())
    change(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def drop_levels(d): del d["autonomy_levels"]["send_io"]
def add_level(d): d["autonomy_levels"]["wire_transfer"] = "auto"
def break_weights(d): d["weights"]["fit"] = 0.9
def break_tiers(d): d["tiers"] = {"A": 30, "B": 40, "C": 20}
def big_nudge(d): d["nudge_limit"] = 11
def typo_field(d): d["fit_rules"][0]["field"] = "locaton_count"
def bad_op(d): d["fit_rules"][0]["op"] = "roughly"
def numeric_on_text(d): d["fit_rules"][0]["field"] = "industry"
def dup_rule(d): d["intent_rules"][0]["id"] = d["fit_rules"][0]["id"]
def bad_reason(d): d["fit_rules"][0]["reason"] = "has {nope} placeholder"
def extra_key(d): d["wieghts"] = {}
def no_source(d): del d["case_studies"][0]["source_url"]
def bad_label(d): d["case_studies"][1]["label"] = "independently audited"
def percent_in_dollar_only(d): d["case_studies"][0]["metrics"][0]["value"] = "339% increase"
def unlabelled_rate_card(d): d["label"] = "Rate card"
def unknown_stage(d): d["exit_criteria"]["limbo"] = ["x"]
def bad_sla(d): d["sla_hours"]["new"] = 0
def bad_zone(d): d["default_timezone"] = "Mars/Olympus"
def bad_window(d): d["send_window"]["end_hour"] = 8
def no_unsubscribe(d): d["templates"]["first_touch"]["body"] = "Hi {{ postal_address }}"
def jinja_error(d): d["templates"]["follow_up"]["body"] = "{% if %} {{ unsubscribe_line }}{{ postal_address }}"
def missing_template(d): del d["templates"]["case_study_reply"]
def wrong_id(d): d["id"] = "something_else"
def bad_regex(d): d["extractors"][0]["pattern"] = "(unclosed"
def two_groups(d): d["extractors"][0]["pattern"] = r"(\d+)\s+(\w+)"
def unknown_lead_field(d): d["extractors"][0]["field"] = "not_a_lead_field"
def relative_page(d): d["pages"] = ["news"]
def empty_keyword_map(d): d["extractors"][1]["map"] = {}
def no_meeting_offer(d): del d["templates"]["meeting_offer"]


INVALID = [
    ("policy.yaml", drop_levels, "autonomy_levels", "send_io"),
    ("policy.yaml", add_level, "autonomy_levels", "wire_transfer"),
    ("policy.yaml", bad_zone, "default_timezone", "Mars/Olympus"),
    ("policy.yaml", bad_window, "send_window", "end_hour"),
    ("icp.yaml", break_weights, "weights", "sum to 1.0"),
    ("icp.yaml", break_tiers, "tiers", "A > B > C"),
    ("icp.yaml", big_nudge, "nudge_limit", "less than or equal to 10"),
    ("icp.yaml", typo_field, "fit_rules[0].field", "locaton_count"),
    ("icp.yaml", bad_op, "fit_rules.0.op", "roughly"),
    ("icp.yaml", numeric_on_text, "fit_rules[0].op", "numeric"),
    ("icp.yaml", dup_rule, "intent_rules[0].id", "duplicate"),
    ("icp.yaml", bad_reason, "fit_rules.0.reason", "{value}"),
    ("icp.yaml", extra_key, "wieghts", "Extra inputs"),
    ("case_studies.yaml", no_source, "case_studies.0.source_url", "required"),
    ("case_studies.yaml", bad_label, "case_studies.1.label", "vendor-reported"),
    ("case_studies.yaml", percent_in_dollar_only, "case_studies.0", "dollar figures only"),
    ("rate_card.yaml", unlabelled_rate_card, "label", "SAMPLE"),
    ("stages.yaml", unknown_stage, "exit_criteria", "limbo"),
    ("stages.yaml", bad_sla, "sla_hours", "positive"),
    ("messaging.yaml", no_unsubscribe, "templates.first_touch.body", "unsubscribe_line"),
    ("messaging.yaml", jinja_error, "templates.follow_up.body", "syntax error"),
    ("messaging.yaml", missing_template, "templates", "case_study_reply"),
    ("pack.yaml", wrong_id, "id", "must match the folder name"),
    ("enrichment.yaml", bad_regex, "extractors.0", "valid regular expression"),
    ("enrichment.yaml", two_groups, "extractors.0", "exactly one capture group"),
    ("enrichment.yaml", unknown_lead_field, "extractors.0.field", "not declared in icp.yaml"),
    ("enrichment.yaml", relative_page, "pages", "starting with '/'"),
    ("enrichment.yaml", empty_keyword_map, "extractors.1", "non-empty map"),
    ("messaging.yaml", no_meeting_offer, "templates", "meeting_offer"),
]


@pytest.mark.parametrize("filename,change,field,fragment", INVALID,
                         ids=[f"{i[0]}:{i[1].__name__}" for i in INVALID])
def test_invalid_pack_names_the_file_and_the_field(pack_copy, filename, change, field, fragment):
    edit(pack_copy, filename, change)
    with pytest.raises(PackError) as err:
        load_pack(pack_copy)
    text = str(err.value)
    assert f"franchise_brands/{filename}" in text
    assert field in text.replace("fit_rules.0", "fit_rules[0]") or field in text
    assert fragment in text


def test_missing_file_is_named(pack_copy) -> None:
    (pack_copy / "rate_card.yaml").unlink()
    with pytest.raises(PackError, match=r"rate_card\.yaml: file is missing"):
        load_pack(pack_copy)


def test_missing_template_file_is_named(pack_copy) -> None:
    (pack_copy / "templates" / "io.md.j2").unlink()
    with pytest.raises(PackError, match=r"templates/io\.md\.j2: file is missing"):
        load_pack(pack_copy)


def test_io_template_must_show_the_sample_label(pack_copy) -> None:
    (pack_copy / "templates" / "io.md.j2").write_text("{{ advertiser_name }} {{ line_items }} {{ total }}")
    with pytest.raises(PackError, match="rate_card_label"):
        load_pack(pack_copy)


def test_broken_yaml_is_reported_with_the_file(pack_copy) -> None:
    (pack_copy / "stages.yaml").write_text("exit_criteria: [unclosed")
    with pytest.raises(PackError, match=r"stages\.yaml: not valid YAML"):
        load_pack(pack_copy)


def test_missing_folder_is_reported(tmp_path) -> None:
    with pytest.raises(PackError, match="pack folder not found"):
        load_pack(tmp_path / "nope")


def test_all_problems_in_a_file_are_reported_together(pack_copy) -> None:
    def many(d):
        d["weights"]["fit"] = 0.9
        d["nudge_limit"] = 50
    edit(pack_copy, "icp.yaml", many)
    with pytest.raises(PackError) as err:
        load_pack(pack_copy)
    assert len(err.value.problems) == 2


def test_cli_validates_packs(tmp_path, pack_copy) -> None:
    runner = CliRunner()
    ok = runner.invoke(app, ["packs", str(pack_copy.parent)])
    assert ok.exit_code == 0 and "ok  franchise_brands" in ok.output
    edit(pack_copy, "icp.yaml", break_weights)
    bad = runner.invoke(app, ["packs", str(pack_copy.parent)])
    assert bad.exit_code == 1 and "icp.yaml: weights" in bad.output
    shutil.rmtree(pack_copy)
