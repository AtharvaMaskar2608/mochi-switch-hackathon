import json
import subprocess
from datetime import date
from types import SimpleNamespace

import pytest

from chief.memory import dreamer
from chief.memory.store import MemoryStore

SRC = {"room": "#eng", "date": "2026-11-07", "by": "dreamer"}
THREADS = [{"root": {"sender_name": "asha", "body": "hi"}, "replies": []}]


class Fake:
    """Fake Anthropic client: replies with queued JSON, records prompts."""
    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.calls.append(kw)
        r = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(r))])


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path)


def test_extract_caps_at_8():
    items = [{"owner": "asha", "kind": "fact", "text": f"fact {n}", "tierHint": "personal"} for n in range(10)]
    out = dreamer.extract(THREADS, ["asha"], Fake(items))
    assert len(out) == 8 and out[0] == {"owner": "asha", "kind": "fact", "text": "fact 0", "tier_hint": "personal"}


def test_dedup_update_supersedes(store):
    old = store.add("personal", "asha", "fact", "Owns Billing", SRC)
    c = {"owner": "asha", "kind": "fact", "text": "Owns Billing and Refunds", "tier_hint": "personal"}
    fake = Fake({"action": "update", "targetId": "m1", "text": "Owns Billing and Refunds"})
    action, target, text = dreamer.dedup(c, store.items("asha"), fake)
    assert (action, target) == ("update", old)
    new = store.supersede(target, text, "dreamer")
    assert store.items("asha")[0].id == new and store.items("asha", "superseded")[0].id == old
    # unknown target fails closed
    assert dreamer.dedup(c, store.items("asha"), Fake({"action": "update", "targetId": "m9", "text": "x"}))[0] == "skip"


def test_person_edited_item_untouched(store):
    mine = store.add("personal", "asha", "fact", "Owns Billing", {**SRC, "by": "person"})
    other = store.add("personal", "asha", "fact", "Owns billing queue", SRC)
    fake = Fake([{"op": "retire", "ids": ["m1"]}])
    dreamer.dream_personal("asha", store, [], fake)
    assert "Owns Billing" not in fake.calls[0]["messages"][0]["content"]  # never shown to the model
    assert [i.id for i in store.items("asha")] == [mine]
    assert store.items("asha", "archived")[0].id == other


def _three_people(store, text="refunds data lives in the orders table"):
    for p in ("asha", "ravi", "meera"):
        store.add("personal", p, "definition", text, SRC)


def test_team_dream_writes_proposals_only(store):
    _three_people(store)
    out = dreamer.dream_team(store, Fake([{"kind": "definition", "text": "Refund figures come from orders.", "reason": "shared"}]))
    assert out == ["Refund figures come from orders."]
    assert store.items("team") == []
    assert len(list((store.root / "team/proposals").glob("m_*.md"))) == 1


def test_quote_guard_drops_12_word_copy(store):
    lesson = "the refunds data lives in the orders table and the finance team owns every refund report"
    _three_people(store, lesson)
    copy = "Note: " + " ".join(lesson.split()[:12]) + " always."
    assert dreamer.quotes(copy, [lesson]) and not dreamer.quotes("Refund data sits in orders.", [lesson])
    out = dreamer.dream_team(store, Fake([{"kind": "fact", "text": copy, "reason": "x"}]))
    assert out == [] and not (store.root / "team/proposals").exists()


def test_unused_90_days_archived_without_model(store):
    i = store.add("personal", "asha", "fact", "Old thing", SRC)
    store._update(i, last_used_at="2026-06-01T00:00:00")
    fake = Fake([])
    dreamer.dream_personal("asha", store, [], fake, today=date(2026, 11, 7))
    assert fake.calls == [] and store.items("asha", "archived")[0].id == i


def test_run_nightly_end_to_end(tmp_path):
    repo = tmp_path / "repo"
    store = MemoryStore(repo / "memory")
    (repo / "memory/people/asha").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)

    class Sw:
        docs = []

        def context(self, room, limit):
            return {"threads": THREADS}

        def room_doc(self, room, name, content):
            self.docs.append((room, name))

    fake = Fake([{"owner": "asha", "kind": "fact", "text": "Owns Billing", "tierHint": "personal"}])
    sw = Sw()
    note = dreamer.run_nightly(sw, store, ["#eng"], fake, today=date(2026, 11, 7))
    assert "1 new" in note and sw.docs == [("#eng", "Team knowledge")]
    assert (repo / "memory/MEMORY.md").exists()
    log = subprocess.run(["git", "log", "--format=%s"], cwd=repo, capture_output=True, text=True).stdout
    assert log.strip() == "memory: dream 2026-11-07"
    dreamer._git_commit(store.root, "2026-11-08")  # no changes: no second commit
    n = subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=repo, capture_output=True, text=True).stdout
    assert n.strip() == "1"
