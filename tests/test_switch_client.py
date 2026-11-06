import json
import time
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
    c, seen = client(STATE)
    c.op("post_message", body="x")
    c.close()
    r = [x for x in seen if "/ops/" in x.url.path][-1]
    assert r.headers["x-switch-connection-id"] == c.connection_id
    assert json.loads(r.content) == {"body": "x"}


def test_gap_with_lower_sequence_resumes():
    gap = frame("gap", {"from_sequence": 50, "resumed_at": 3, "reason": "restart"})
    c, _ = client(STATE + msg(50), STATE + gap + msg(3, room="R2"))
    got = list(islice(c.events(), 2))
    c.close()
    assert [m.sequence for m in got] == [50, 3]
    assert {"R", "R2"} <= c.gap_rooms  # covered rooms plus the message's room


def _wait(cond, t=2.0):
    end = time.time() + t
    while time.time() < end and not cond():
        time.sleep(0.01)
    return cond()


def test_heartbeat_fires_at_interval():
    state = frame("connection_state", {"heartbeat_interval_seconds": 0.01, "rooms": ["R"]})
    c, seen = client(state + msg(1))
    next(c.events())

    def beats():
        return [r for r in seen if r.url.path.endswith("/connection/beat")]

    assert _wait(lambda: len(beats()) >= 2)
    c.close()
    assert json.loads(beats()[0].content)["connection_id"] == c.connection_id


def test_beat_404_triggers_reconnect():
    n = [0]

    def slow():
        yield STATE.replace("3600", "0.01").encode()
        time.sleep(0.5)
        yield b": ka\n\n"

    def handler(req):
        if req.url.path.endswith("/events"):
            n[0] += 1
            body = slow() if n[0] == 1 else iter([(STATE + msg(5)).encode()])
            return httpx.Response(200, content=body)
        if req.url.path.endswith("/connection/beat"):
            return httpx.Response(404)
        return httpx.Response(200, json={"result": []})

    c = SwitchClient("http://s", "k", "a1", http=httpx.Client(transport=httpx.MockTransport(handler)),
                     retry_delay=0)
    m = next(c.events())
    c.close()
    assert m.sequence == 5 and n[0] >= 2


def test_op_before_events_opens_stream_first():
    c, seen = client(STATE)
    assert c.op("list_rooms")
    c.close()
    paths = [r.url.path for r in seen]
    assert paths[0].endswith("/events") and paths.index(next(p for p in paths if "/ops/" in p)) > 0


def test_op_409_not_open_retries_after_reconnect():
    streams = [STATE] * 100
    ops = []

    def handler(req):
        if req.url.path.endswith("/events"):
            return httpx.Response(200, content=streams.pop(0).encode() if streams else b"")
        ops.append(req)
        if len(ops) == 1:
            return httpx.Response(409, json={"detail": "connection x is not open; reconnect"})
        return httpx.Response(200, json={"result": ["ok"]})

    c = SwitchClient("http://s", "k", "a1", http=httpx.Client(transport=httpx.MockTransport(handler)),
                     retry_delay=0.05)
    assert c.op("list_rooms") == ["ok"]
    c.close()
    assert len(ops) == 2


def test_start_timeout_reports_status():
    import pytest
    c = SwitchClient("http://s", "k", "a1", retry_delay=0.05,
                     http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401))))
    with pytest.raises(RuntimeError, match="last status 401"):
        c.start(timeout=0.3)
    c.close()


def test_connect_registers_once_and_saves_creds(tmp_path, monkeypatch):
    from chief import switch_client as sc

    calls = []
    monkeypatch.setenv("SWITCH_URL", "http://s")
    monkeypatch.setenv("SWITCH_REGISTRATION_TOKEN", "tok")
    monkeypatch.setattr(sc, "register", lambda *a: calls.append(a) or ("a1", "key1"))
    monkeypatch.setattr(sc.SwitchClient, "start", lambda self: None)
    creds = tmp_path / ".chief.json"
    c = sc.connect("chief", "desc", creds)
    assert (c.agent_id, c.headers["Authorization"]) == ("a1", "Bearer key1")
    assert json.loads(creds.read_text()) == {"agent_id": "a1", "api_key": "key1"}
    assert creds.stat().st_mode & 0o077 == 0
    sc.connect("chief", "desc", creds)
    assert calls == [("http://s", "tok", "chief", "desc")]


def test_assume_role_releases_previous_lease_first():
    c, seen = client(STATE)
    c.assume_role("R", "data-steward")
    ops = [r.url.path.rsplit("/", 1)[1] for r in seen if "/ops/" in r.url.path and "list_rooms" not in r.url.path]
    assert ops == ["connect_to_room", "release_role", "assume_role"]
    assert json.loads(seen[-1].content) == {"role": "data-steward"}
    c.close()


def test_resolve_rooms_exact_then_suffix():
    from types import SimpleNamespace

    from chief.switch_client import resolve_rooms

    rooms = [{"room_id": "1", "name": "Mattermost: Product"}, {"room_id": "2", "name": "chief-ops"},
             {"room_id": "3", "name": "work-product"}, {"room_id": "4", "name": "eng"},
             {"room_id": "5", "name": "#eng"}]
    c = SimpleNamespace(op=lambda name: rooms)
    assert resolve_rooms(c, ["product", "chief-ops", "eng", "leadership"]) == {"product": "1", "chief-ops": "2", "eng": "4"}


def test_knowledge_doc_is_chiefs_own_so_updates_are_allowed():
    from chief.memory.dreamer import KNOWLEDGE_DOC

    docs = [{"id": "D0", "name": "Team knowledge", "mine": False, "content": "(placeholder)"}]  # template-made

    def handler(req: httpx.Request):
        op = req.url.path.rsplit("/", 1)[1]
        args = json.loads(req.content or b"{}")
        if op == "events":
            return httpx.Response(200, content=STATE.encode(), headers={"content-type": "text/event-stream"})
        if op == "connect_to_room":
            return httpx.Response(200, json={"result": {"documents": [{"id": d["id"], "name": d["name"]} for d in docs]}})
        if op == "update_room_document":
            d = next(d for d in docs if d["id"] == args["document_id"])
            if not d["mine"]:
                return httpx.Response(403, json={"error": "only the document's creator can update it"})
            d["content"] = args["content"]
        if op == "create_room_document":
            docs.append({"id": f"D{len(docs)}", "name": args["name"], "mine": True, "content": args["content"]})
            return httpx.Response(200, json={"result": {"document_id": docs[-1]["id"]}})
        return httpx.Response(200, json={"result": {"ok": True}})

    c = SwitchClient("http://s", "k", "a1", http=httpx.Client(transport=httpx.MockTransport(handler)), retry_delay=0)
    assert c.room_doc("R", KNOWLEDGE_DOC, "- a") == "D1"  # first night: published by Chief
    assert c.room_doc("R", KNOWLEDGE_DOC, "- a\n- b") == "D1"  # next night: updated, no 403
    assert [d["content"] for d in docs] == ["(placeholder)", "- a\n- b"]
    c.close()
