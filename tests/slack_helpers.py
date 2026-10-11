"""A full Slack setup for tests: the mock Slack, the notifier, the signed-request client."""

import hashlib
import hmac
import json
import random
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import psycopg
from fastapi.testclient import TestClient

from revos.adapters.mock.slack import MockSlack
from revos.api.main import create_app
from revos.runtime.worker import Worker
from revos.seed import load_profiles
from revos.slack.interactions import SlackInteractions
from revos.slack.notifier import SlackNotifier
from tests.conftest import PACK_ID
from tests.pipeline_helpers import make_rig

SECRET = "test-signing-secret"
CHANNEL = "C0TEST"


@dataclass
class SlackRig:
    rig: object
    slack: MockSlack
    notifier: SlackNotifier
    client: TestClient
    conn: psycopg.Connection
    clock: object
    _n: int = 0

    # --- driving the system ----------------------------------------------------------------------------
    def drain(self, seconds: float = 0) -> None:
        if seconds:
            self.clock.advance(seconds)
        worker = Worker(self.rig.orch.handlers(), self.clock, rng=random.Random(1))
        while worker.run_once(self.conn):
            pass

    def hot_first_touch(self):
        """The hot lead's first email: tier A, so auto_with_veto. Returns the action id."""
        result = self.rig.orch.handle_lead_created(self.rig.deal_id)
        return result.action_id

    def post_for(self, action_id):
        channel, ts = self.notifier.message_for(action_id)
        return self.slack.message(ts)

    # --- a signed button press -------------------------------------------------------------------------
    def press(self, verb: str, action_id, user: str = "U1", *, trigger: str | None = None,
              extra: dict | None = None, secret: str = SECRET, timestamp: int | None = None,
              signature: str | None = None, headers: bool = True):
        self._n += 1
        payload = {"type": "block_actions", "user": {"id": user, "username": user.lower()},
                   "trigger_id": trigger or f"trig-{self._n}", "channel": {"id": CHANNEL},
                   "actions": [{"action_id": f"revos_{verb}", "value": str(action_id)}], **(extra or {})}
        raw = urlencode({"payload": json.dumps(payload)}).encode()
        ts = str(int(time.time()) if timestamp is None else timestamp)
        sig = signature or sign(raw, ts, secret)
        hdrs = {"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig,
                "Content-Type": "application/x-www-form-urlencoded"} if headers else {}
        return self.client.post("/slack/interactions", content=raw, headers=hdrs), raw


def sign(raw: bytes, timestamp: str, secret: str = SECRET) -> str:
    return "v0=" + hmac.new(secret.encode(), b"v0:" + timestamp.encode() + b":" + raw, hashlib.sha256).hexdigest()


def slack_rig(conn, ro_conn, pack, clock, test_db_url, *, allowed=(), **kw) -> SlackRig:
    profile = load_profiles(PACK_ID)["hot"]
    slack = MockSlack()
    notifier = SlackNotifier(conn, slack, CHANNEL, clock, {pack.id: pack})
    rig = make_rig(conn, ro_conn, pack, clock, notifier=notifier, domain=profile["domain"],
                   company=profile["company"], name=profile["contact"],
                   fields=json.dumps(profile["fields"]), **kw)
    interactions = SlackInteractions(lambda: psycopg.connect(test_db_url), clock, slack, allowed)
    client = TestClient(create_app(interactions, SECRET))
    return SlackRig(rig, slack, notifier, client, conn, clock)
