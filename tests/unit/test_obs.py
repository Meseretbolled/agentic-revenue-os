import json

import pytest

from revos import obs

FAKE_SECRETS = {
    "anthropic": "sk-ant-api03-FAKEFAKEFAKEFAKE1234",
    "hubspot": "pat-na1-12345678-abcd-ef01-2345-6789abcdef01",
    "slack": "xoxb-1234567890-FAKESLACKTOKEN",
    "tavily": "tvly-dev-FAKETAVILYKEY123",
    "bearer": "FAKEBEARERVALUE0123456789",
    "app_password": "wxyz qrst abcd mnop",
    "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJmYWtlIn0.FAKESIGNATURE12345",
}


@pytest.fixture(autouse=True)
def clean():
    obs.clear_secrets()
    yield
    obs.clear_secrets()
    obs.disable_logging()


@pytest.fixture
def logfile(tmp_path):
    return obs.configure_logging(tmp_path / "revos.jsonl")


def lines(path):
    return [json.loads(x) for x in path.read_text().splitlines()]


def test_anything_that_looks_like_a_key_or_token_is_removed_from_text() -> None:
    text = (f"called with Authorization: Bearer {FAKE_SECRETS['bearer']} and key {FAKE_SECRETS['anthropic']} "
            f"then {FAKE_SECRETS['hubspot']} / {FAKE_SECRETS['slack']} / {FAKE_SECRETS['tavily']} "
            f"/ {FAKE_SECRETS['jwt']} password=hunter2hunter2 api_key: 'abcdef123456' token=zzzz9999")
    out = obs.redact(text)
    for value in (*FAKE_SECRETS.values(), "hunter2hunter2", "abcdef123456", "zzzz9999"):
        assert value not in out
    assert out.count(obs.REDACTED) >= 7 and "called with Authorization" in out


def test_an_authorization_header_in_a_dict_or_json_is_removed() -> None:
    row = {"headers": {"Authorization": f"Bearer {FAKE_SECRETS['bearer']}", "Accept": "json"},
           "HUBSPOT_TOKEN": FAKE_SECRETS["hubspot"], "nested": [{"api_key": "plainvalue99"}]}
    shown = json.dumps(obs.redact_value(row))
    assert FAKE_SECRETS["bearer"] not in shown and FAKE_SECRETS["hubspot"] not in shown
    assert "plainvalue99" not in shown and "Accept" in shown


def test_the_exact_value_of_a_secret_in_the_environment_is_removed_even_without_a_known_shape() -> None:
    env = {"GMAIL_APP_PASSWORD": FAKE_SECRETS["app_password"], "SENDER_NAME": "Jo Rivers",
           "SOME_API_KEY": "plain-looking-secret", "REVOS_NO_DOTENV": "1"}
    assert obs.register_secrets(env) >= 3
    out = obs.redact(f"login failed for {FAKE_SECRETS['app_password']} and wxyzqrstabcdmnop, "
                     f"also plain-looking-secret, from Jo Rivers")
    assert "wxyz" not in out and "mnop" not in out and "plain-looking-secret" not in out
    assert "Jo Rivers" in out                          # ordinary values are left alone


def test_ids_and_idempotency_keys_are_not_mistaken_for_secrets() -> None:
    out = obs.redact('idempotency_key=send:abc123:first_touch revos_key="k-1" message_id=<a@b>')
    assert "send:abc123:first_touch" in out and "<a@b>" in out


def test_a_log_line_has_the_agreed_fields_and_no_secret(logfile) -> None:
    obs.register_secrets({"GMAIL_APP_PASSWORD": FAKE_SECRETS["app_password"]})
    with obs.correlation("c0ffee123456"):
        obs.log("outbox", f"sent with {FAKE_SECRETS['anthropic']} and {FAKE_SECRETS['app_password']}",
                deal_id="d-1", headers={"Authorization": f"Bearer {FAKE_SECRETS['bearer']}"},
                note=f"token={FAKE_SECRETS['tavily']}", provider_id="<m@x>")
    (row,) = lines(logfile)
    assert {"ts", "level", "component", "deal_id", "correlation_id", "message"} <= set(row)
    assert row["component"] == "outbox" and row["deal_id"] == "d-1" and row["correlation_id"] == "c0ffee123456"
    raw = logfile.read_text()
    for value in FAKE_SECRETS.values():
        assert value not in raw
    assert "wxyzqrstabcdmnop" not in raw and "<m@x>" in raw


def test_nothing_is_written_until_logging_is_configured(tmp_path) -> None:
    obs.log("x", "silent")
    assert not list(tmp_path.iterdir())


def test_the_correlation_id_is_scoped_and_logs_can_be_filtered_by_deal_and_id(logfile) -> None:
    assert obs.current_correlation() is None
    with obs.correlation("aaa"):
        obs.log("a", "one", deal_id="D1")
        with obs.correlation("bbb"):
            obs.log("a", "two", deal_id="D2")
        assert obs.current_correlation() == "aaa"
    assert obs.current_correlation() is None
    assert [r["message"] for r in obs.read_log(logfile, deal_id="D1")] == ["one"]
    assert [r["message"] for r in obs.read_log(logfile, correlation_id="bbb")] == ["two"]
    assert len(obs.read_log(logfile)) == 2 and len(obs.read_log(logfile, limit=1)) == 1


def test_the_correlation_id_is_set_on_the_database_session_and_cleared(conn) -> None:
    with obs.correlation("sess123", conn):
        assert conn.execute("SELECT current_setting('revos.correlation_id', true)").fetchone()[0] == "sess123"
        conn.execute("INSERT INTO audit_log (actor, event) VALUES ('t', 'x')")
    assert conn.execute("SELECT correlation_id FROM audit_log").fetchone()[0] == "sess123"
    conn.execute("INSERT INTO audit_log (actor, event) VALUES ('t', 'y')")
    assert conn.execute("SELECT correlation_id FROM audit_log WHERE event = 'y'").fetchone()[0] is None


def test_non_bearer_authorization_headers_are_removed_too() -> None:
    out = obs.redact("Authorization: Basic dXNlcjpwYXNzd29yZA== and authorization=Token abcdef0123456789")
    assert "dXNlcjpwYXNzd29yZA" not in out and "abcdef0123456789" not in out
