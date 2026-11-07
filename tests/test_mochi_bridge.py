import json
import re
import time

from fastapi.testclient import TestClient

from mochi_bridge.app import create_app, sign

SECRET = "s3cret"
CARD = {"headline": "Refunds up", "body": "2x", "keyNumbers": [{"label": "Rate", "value": "9%"}],
        "artifact": {"url": "http://m/a/1"}}


class Fake:
    def __init__(self):
        self.posts = []

    def post(self, room_id, body, thread_id=None):
        self.posts.append((room_id, body))


def setup():
    f = Fake()
    return f, TestClient(create_app(f, SECRET, {"C0SWITCH01": "room-product", "C0SWITCH02": "room-lead"}))


def send(c, path, payload, ts=None, sig=None):
    raw = json.dumps(payload)
    ts = int(time.time()) if ts is None else ts
    h = {"x-mochi-timestamp": str(ts), "x-mochi-signature": sig or sign(SECRET, ts, raw)}
    return c.post(path, content=raw, headers=h)


def test_valid_signature_posts_card_to_product():
    f, c = setup()
    r = send(c, "/internal/deliver", {"channelId": "C0SWITCH01", "card": CARD})
    assert r.status_code == 200 and r.json()["status"] == "sent"
    assert f.posts == [("room-product", "🔔 Refunds up\n2x\nRate: 9%\nhttp://m/a/1")]


def test_bad_signature_401():
    f, c = setup()
    assert send(c, "/internal/deliver", {"channelId": "C0SWITCH01", "card": CARD}, sig="v0=bad").status_code == 401
    assert c.post("/internal/deliver", json={}).status_code == 401
    assert f.posts == []


def test_stale_timestamp_401():
    f, c = setup()
    assert send(c, "/internal/deliver", {"channelId": "C0SWITCH01", "card": CARD}, ts=int(time.time()) - 400).status_code == 401
    assert f.posts == []


def test_channels_ids_match_mochi_regex():
    _, c = setup()
    chans = send(c, "/internal/slack-channels", {}).json()["channels"]
    assert [x["name"] for x in chans] == ["product", "leadership"]
    assert all(re.fullmatch(r"[CG][A-Z0-9]{6,20}", x["id"]) for x in chans)


def test_card_text_never_carries_a_live_at():
    f, c = setup()
    card = {"headline": "@chief look", "body": "ping @asha", "artifact": {"url": "http://m/@a"},
            "keyNumbers": [{"label": "@x", "value": "a@b", "change": "@up"}]}
    send(c, "/internal/deliver", {"channelId": "C0SWITCH01", "card": card})
    (_, body), = f.posts
    assert "@" not in body and body.count("＠") == 6
