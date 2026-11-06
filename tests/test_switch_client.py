import json
from itertools import islice

import httpx

from chief.switch_client import SwitchClient


def frame(kind, d, seq=None):
    return (f"id: {seq}\n" if seq else "") + f"event: {kind}\ndata: {json.dumps(d)}\n\n"


STATE = frame("connection_state", {"heartbeat_interval_seconds": 3600, "rooms": ["R"]})


def msg(seq, room="R", body="hi"):
    return frame("message", {"type": "message", "room_id": room, "sequence": seq, "payload": {
        "addressed": True, "sender": "@switch-user-1:x", "sender_name": "ravi", "body": body,
        "timestamp": 1700000000000, "thread_id": None}}, seq)


def client(*streams):
    streams = list(streams)
    seen = []

    def handler(req: httpx.Request):
        seen.append(req)
        if req.url.path.endswith("/events"):
            return httpx.Response(200, content=(streams.pop(0) if streams else "").encode(),
                                  headers={"content-type": "text/event-stream"})
        if req.url.path.endswith("/ops/list_rooms"):
            return httpx.Response(200, json={"result": [{"room_id": "R", "name": "#product"}]})
        return httpx.Response(200, json={"result": {"ok": True}})

    c = SwitchClient("http://s", "key", "a1", http=httpx.Client(transport=httpx.MockTransport(handler)),
                     retry_delay=0)
    return c, seen


def test_parses_message_frame():
    c, _ = client(": keepalive\n\n" + STATE + msg(7))
    m = next(c.events())
    c.close()
    assert (m.body, m.sequence, m.room_id, m.room_name, m.sender_name, m.addressed, m.is_agent) == \
        ("hi", 7, "R", "#product", "ravi", True, False)


def test_skips_duplicate_sequence():
    c, seen = client(STATE + msg(7), STATE + msg(7) + msg(8, body="b"))
    got = [m.sequence for m in islice(c.events(), 2)]
    c.close()
    assert got == [7, 8]
    ev = [r for r in seen if r.url.path.endswith("/events")]
    assert ev[1].headers["last-event-id"] == "7"
    assert ev[0].url.params["connection_id"] == ev[1].url.params["connection_id"]


def test_gap_frame_marks_room():
    c, _ = client(STATE + frame("gap", {"from_sequence": 1, "resumed_at": 5, "reason": "x"}) + msg(9))
    next(c.events())
    c.close()
    assert "R" in c.gap_rooms


def test_op_sends_connection_header():
    c, seen = client()
    c.op("post_message", body="x")
    r = seen[-1]
    assert r.headers["x-switch-connection-id"] == c.connection_id
    assert json.loads(r.content) == {"body": "x"}
