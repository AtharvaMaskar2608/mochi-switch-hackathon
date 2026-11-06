from types import SimpleNamespace as Item

from chief.approvals import Approvals
from chief.commands import Command, forget_target, parse


def test_parse_each_command():
    assert parse("!memory") == Command("memory", "")
    assert parse("!quiet") == Command("quiet_on", "")
    assert parse("!quiet off") == Command("quiet_off", "")
    assert parse("!tick morning") == Command("tick", "morning")
    assert parse("remember I prefer short briefs") == Command("remember", "I prefer short briefs")
    assert parse("chief remember standup is at 10") == Command("remember", "standup is at 10")
    assert parse("forget the standup time") == Command("forget", "the standup time")
    assert parse("approve a7k2") == Command("approve", "A7K2")
    assert parse("deny A7K2") == Command("deny", "A7K2")


def test_plain_text_is_not_command():
    for t in ["hello team", "I remember that", "what should we forget?", "approve", "!unknown", ""]:
        assert parse(t) is None


def test_only_owner_can_approve():
    a = Approvals()
    code = a.request("merge", "meera", {"pr": 12})
    assert len(code) == 4 and not set(code) & set("01OI")
    assert a.resolve(code, "dev", True) is None
    assert a.resolve(code, "meera", True) == {"pr": 12}


def test_code_single_use():
    a = Approvals()
    code = a.request("merge", "meera", {"pr": 12})
    assert a.resolve(code, "meera", False) == {"pr": 12}
    assert a.resolve(code, "meera", True) is None


def test_forget_matches_item_by_overlap():
    class Store:
        def search(self, query, owners, k=5):
            return [Item(id="a", text="Likes dark mode"), Item(id="b", text="Standup is at 10am daily")]

    assert forget_target(Store(), "meera", "standup 10am") == "b"
    assert forget_target(Store(), "meera", "zzz") is None


def test_forget_ignores_stopword_only_overlap():
    class Store:
        def search(self, query, owners, k=5):
            assert owners == ["meera", "team"]
            return [Item(id="a", text="The standup is at 10am")]

    assert forget_target(Store(), "meera", "forget the is at") is None
    assert forget_target(Store(), "meera", "the standup") is None  # one real word is not enough
