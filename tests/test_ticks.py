from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from chief.ladder import Ladder
from chief.memory.store import MemoryStore
from chief.ticks import IST, due, on_tick, tick_loop

FRI = datetime(2026, 11, 6, tzinfo=IST)  # a Friday


def at(base, h, m=0):
    return base.replace(hour=h, minute=m)


def test_friday_digest_due_only_friday_after_16_ist():
    assert "friday-digest" not in due(at(FRI, 15, 59), {})
    assert "friday-digest" in due(at(FRI, 16), {})
    assert "friday-digest" not in due(at(FRI + timedelta(days=1), 17), {})  # Saturday
    assert "friday-digest" in due(datetime(2026, 11, 6, 11, 0, tzinfo=timezone.utc), {})  # 16:30 IST


def test_not_due_twice_same_period():
    t = at(FRI, 16, 5)
    assert "friday-digest" not in due(t, {"friday-digest": at(FRI, 16, 1)})
    assert "friday-digest" in due(t, {"friday-digest": at(FRI, 16) - timedelta(days=7)})


def test_tick_loop_posts_once_per_kind():
    posts, clock, n = [], [at(FRI, 18, 1)], [0]
    client = SimpleNamespace(post=lambda room, body: posts.append((room, body)))

    def sleep(_):
        n[0] += 1
        clock[0] += timedelta(seconds=30)

    tick_loop(client, "ops", now=lambda: clock[0], sleep=sleep, stop=lambda: n[0] >= 3)
    bodies = sorted(b for _, b in posts)
    assert bodies == ["[tick] dream", "[tick] evening","[tick] friday-digest", "[tick] morning"]


def test_on_tick_friday_opens_shipped_digest():
    [(kind, a)] = on_tick("friday-digest", SimpleNamespace(org_admin="asha"))
    assert kind == "open_work_room" and a["template"] == "shipped-digest" and a["lead_role"] == "release-scribe"


def test_on_tick_dream_and_morning():
    ctx = SimpleNamespace(room_ids={"chief-ops": "R1"})
    [(kind, a)] = on_tick("dream", ctx)
    assert kind == "dream" and a["room_id"] == "R1" and len(a["rooms"]) == 4
    assert on_tick("morning", ctx)[0][1]["room_id"] == "R1"
    assert on_tick("bogus", ctx) == []


def test_on_tick_evening_releases_holds_even_in_focus(tmp_path):
    store = MemoryStore(tmp_path)
    (tmp_path / "people" / "meera").mkdir(parents=True, exist_ok=True)
    ladder = Ladder()
    ladder.hold("meera", {"text": "ravi: ping", "room_id": "R9", "thread_id": None})
    writer = SimpleNamespace(batched_brief=lambda items, p: "; ".join(items))
    ctx = SimpleNamespace(store=store, ladder=ladder, writer=writer)
    [(kind, a)] = on_tick("evening", ctx)
    assert kind == "target" and a["room_id"] == "R9" and a["names"] == ["meera"] and a["body"] == "ravi: ping"
    assert on_tick("evening", ctx) == []


def test_tick_loop_survives_post_error(capsys):
    posts, n = [], [0]

    def post(room, body):
        if n[0] == 0:
            raise ConnectionError("down")
        posts.append(body)

    def sleep(_):
        n[0] += 1

    tick_loop(SimpleNamespace(post=post), "ops", now=lambda: at(FRI, 9, 1), sleep=sleep, stop=lambda: n[0] >= 2)
    assert "[tick] morning" in posts  # failed once, retried next round
    assert "tick morning: ConnectionError" in capsys.readouterr().err


def test_tick_loop_fires_after_post_and_survives_fire_error(capsys):
    fired, n = [], [0]

    def on_fire(kind):
        fired.append(kind)
        raise RuntimeError("writer down")

    def sleep(_):
        n[0] += 1

    tick_loop(SimpleNamespace(post=lambda room, body: None), "ops", now=lambda: at(FRI, 9, 1), sleep=sleep,
              stop=lambda: n[0] >= 2, on_fire=on_fire)
    assert sorted(fired) == ["dream", "morning"]  # once each: a failed fire is not retried
    assert "writer down" in capsys.readouterr().err
