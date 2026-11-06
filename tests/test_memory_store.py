import pytest

from chief.memory.store import MemoryStore

SRC = {"room": "#eng", "date": "2026-11-07", "by": "person"}


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path)


def test_add_then_items_roundtrip(store):
    i = store.add("personal", "asha", "preference", "Likes charts", SRC)
    assert i.startswith("m_") and len(i) == 10
    [it] = store.items("asha")
    assert (it.id, it.text, it.kind, it.status, it.version, it.use_count) == (
        i, "Likes charts", "preference", "active", 1, 0)
    assert it.edited_by_person and it.supersedes is None
    store.touch(i)
    assert store.items("asha")[0].use_count == 1
    store.archive(i)
    assert store.items("asha") == [] and store.items("asha", "archived")[0].id == i


def test_supersede_marks_old_and_links_new(store):
    old = store.add("personal", "ravi", "fact", "Sprint is 2 weeks", SRC)
    new = store.supersede(old, "Sprint is 1 week", "dreamer")
    [o] = store.items("ravi", "superseded")
    [n] = store.items("ravi")
    assert o.id == old and n.id == new
    assert n.supersedes == o.id and n.version == o.version + 1


def test_rejects_301_chars(store):
    store.add("team", "team", "fact", "x" * 300, SRC)
    with pytest.raises(ValueError):
        store.add("team", "team", "fact", "x" * 301, SRC)


def test_unknown_profile_defaults(store):
    p = store.profile("nobody")
    assert (p.name, p.role, p.style, p.focus, p.preapproved, p.card) == (
        "nobody", "unknown", "short and plain", [], [], "")


def test_profile_parses_focus(tmp_path):
    d = tmp_path / "people/meera"
    d.mkdir(parents=True)
    (d / "profile.md").write_text('---\nname: Meera\nrole: dev\nfocus: ["10:00-15:00"]\n---\nCard.\n')
    p = MemoryStore(tmp_path).profile("meera")
    assert p.focus == [("10:00", "15:00")] and p.card == "Card."


def test_index_lists_only_active_grouped_by_owner(store):
    a = store.add("personal", "asha", "fact", "Asha fact", SRC)
    gone = store.add("personal", "asha", "fact", "Old fact", SRC)
    store.archive(gone)
    store.add("personal", "ravi", "fact", "Ravi fact", SRC)
    store.add("team", "team", "definition", "Team def", SRC)
    store.write_index()
    md = (store.root / "MEMORY.md").read_text()
    assert md.index("## asha") < md.index("## ravi") < md.index("## team")
    assert "Old fact" not in md
    assert f"- Asha fact ([fact](people/asha/items/{a}.md), added " in md


def test_search_ranks_overlap(store):
    store.add("team", "team", "fact", "Deploys happen on Thursday", SRC)
    best = store.add("team", "team", "fact",
                     "The refunds data lives in the tickets table, queue Refunds", SRC)
    store.add("team", "team", "fact", "Refunds are reviewed weekly", SRC)
    assert store.search("refunds table", ["team"])[0].id == best
    assert len(store.search("refunds table", ["team"])) == 2


def test_seed_memory_loads():
    from pathlib import Path
    s = MemoryStore(Path(__file__).parent.parent / "memory")
    assert s.profile("ravi").preapproved == ["mochi_read"]
    assert s.profile("meera").focus == [("10:00", "15:00")]
    assert s.profile("asha").style == "one line and a chart"
    assert len(s.items("team")) == 2


def test_proposal_written_to_team_proposals(store):
    i = store.add("personal", "ravi", "fact", "Maybe X", SRC, status="proposed")
    assert (store.root / "team" / "proposals" / f"{i}.md").exists()
    assert not (store.root / "people" / "ravi").exists()
