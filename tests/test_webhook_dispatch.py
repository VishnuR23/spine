"""Worker-side webhook dispatch: event filtering and signed delivery."""

import hashlib
import hmac
import json
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from spine.core import webhook_dispatch
from spine.core.webhook_dispatch import _should_send_plan_drift, dispatch_plan_drift_event_sync
from spine.db.base import Base
from spine.models.webhook import Webhook

HMAC_KEY = "ab" * 32


@pytest.mark.parametrize(
    "events, expected",
    [
        (None, True),
        ([], True),
        (["decision:*"], True),
        (["spine.plan.drift"], True),
        (["plan.drift"], True),
        (["decision:plan_drift"], True),
        (["decision:blocked"], False),
    ],
)
def test_plan_drift_filter(events, expected):
    assert _should_send_plan_drift(events) is expected


class _RecordingClient:
    """Stands in for httpx.Client; records every POST."""

    posts: list[dict] = []

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, *, content, headers):
        _RecordingClient.posts.append({"url": url, "content": content, "headers": headers})


@pytest.fixture()
def db(monkeypatch):
    _RecordingClient.posts = []
    monkeypatch.setattr(webhook_dispatch.httpx, "Client", _RecordingClient)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(engine, expire_on_commit=False)()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _add_hook(db, org_id, *, events=None, url="https://example.com/hook", active=True):
    db.add(Webhook(org_id=org_id, url=url, hmac_key=HMAC_KEY, name="h", events=events, is_active=active))
    db.commit()


def test_plan_drift_dispatch_signs_and_filters(db):
    org_id = uuid.uuid4()
    _add_hook(db, org_id, url="https://example.com/all")
    _add_hook(db, org_id, url="https://example.com/blocked-only", events=["decision:blocked"])
    _add_hook(db, org_id, url="https://example.com/inactive", active=False)
    _add_hook(db, uuid.uuid4(), url="https://example.com/other-org")

    dispatch_plan_drift_event_sync(db, org_id=org_id, payload={"event": "spine.plan.drift", "drift_score": 0.5})

    assert [p["url"] for p in _RecordingClient.posts] == ["https://example.com/all"]
    post = _RecordingClient.posts[0]
    assert post["headers"]["X-Spine-Event"] == "spine.plan.drift"
    expected = hmac.new(bytes.fromhex(HMAC_KEY), post["content"], hashlib.sha256).hexdigest()
    assert post["headers"]["X-Spine-Signature"] == f"v1={expected}"
    assert json.loads(post["content"])["drift_score"] == 0.5
