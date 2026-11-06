import json
from pathlib import Path
from types import SimpleNamespace as NS

import yaml

from chief import factory
from chief.factory import TeamSpec

SPEC = TeamSpec("growth", [{"name": "kiran", "role": "dev"}, {"name": "zoya", "role": "marketer"}])
TEMPLATE = (Path(factory.ROOT) / "workflows/org-team.yaml").read_text()


class FakeClient:
    def __init__(self, j):
        self.messages = self
        self.j = j

    def create(self, **kw):
        return NS(content=[NS(type="text", text=json.dumps(self.j))])


class FakeSwitch:
    def __init__(self, rooms=(), agents=(), fail=False):
        self.sent, self.rooms, self.agents, self.fail = [], rooms, agents, fail

    def op(self, name):
        if self.fail:
            raise RuntimeError("down")
        return [{"name": n} for n in (self.rooms if name == "list_rooms" else self.agents)]

    def room_from_yaml(self, y, inputs):
        self.sent.append((y, inputs))


def sandbox(tmp_path, monkeypatch):
    (tmp_path / "workflows").mkdir()
    (tmp_path / "workflows/org-team.yaml").write_text(TEMPLATE)
    monkeypatch.setattr(factory, "ROOT", tmp_path)


def test_render_one_intern_per_dev():
    y = factory.render(SPEC)
    d = yaml.safe_load(y)
    assert [a["name"] for a in d["agents"]] == ["intern-kiran"]
    assert d["agents"][0]["directory"] == "{repo_dir}/agents/intern-kiran"
    assert [r["name"] for r in d["rooms"]] == ["growth", "growth-eng"]
    assert factory.validate(y) == []


def test_marketer_gets_no_intern():
    assert "zoya" not in factory.render(SPEC)
    assert "Zoya (marketer) gets no intern" in factory.describe_for_approval(SPEC)


def test_invalid_team_name_rejected(tmp_path, monkeypatch):
    sandbox(tmp_path, monkeypatch)
    c = FakeClient({"team": "Bad Team!", "people": [], "data": False, "release_cadence": None})
    sw = FakeSwitch()
    assert factory.build("x", sw, approved=True, client=c).startswith("refused")
    assert sw.sent == []
    assert factory.validate(factory.render(TeamSpec("Bad_Team", [])))
    assert factory.validate(factory.render(SPEC), existing_rooms=["growth"])


def test_build_requires_approval_action(tmp_path, monkeypatch):
    sandbox(tmp_path, monkeypatch)
    sw = FakeSwitch()
    assert factory.build("a growth team", sw, approved=False) == "needs approval"
    assert sw.sent == []
    c = FakeClient({"team": "growth", "people": [{"name": "Kiran", "role": "dev"}], "data": False, "release_cadence": None})
    assert factory.build("x", sw, approved=True, client=c).startswith("Created #growth")
    assert len(sw.sent) == 1
    assert "intern-kiran" in (tmp_path / "agents/intern-kiran/CLAUDE.md").read_text()
    assert (tmp_path / "org/growth.yaml").exists()


KIRAN = {"team": "growth", "people": [{"name": "kiran", "role": "dev"}], "data": False, "release_cadence": None}


def test_rendered_has_machine_and_roles():
    d = yaml.safe_load(factory.render(SPEC))
    assert d["params"]["machine"]["default"] == ["$first"]
    assert d["agents"][0]["machine"] == "{machine}"
    assert [r["name"] for r in d["rooms"][1]["roles"]] == ["reviewer", "on-call"]


def test_build_refuses_existing_agent_dir_and_name(tmp_path, monkeypatch):
    sandbox(tmp_path, monkeypatch)
    (tmp_path / "agents/intern-kiran").mkdir(parents=True)
    sw = FakeSwitch()
    assert "directory exists" in factory.build("x", sw, approved=True, client=FakeClient(KIRAN))
    assert "agent already exists" in factory.build("x", FakeSwitch(agents=["intern-kiran"]), approved=True, client=FakeClient(KIRAN))
    dup = dict(KIRAN, people=KIRAN["people"] * 2)
    assert "duplicate agent" in factory.build("x", FakeSwitch(), approved=True, client=FakeClient(dup))
    assert sw.sent == []


def test_build_fails_closed_when_lookup_fails(tmp_path, monkeypatch):
    sandbox(tmp_path, monkeypatch)
    sw = FakeSwitch(fail=True)
    assert factory.build("x", sw, approved=True, client=FakeClient(KIRAN)).startswith("refused")
    assert sw.sent == []
    assert "room already exists" in factory.build("x", FakeSwitch(rooms=["growth"]), approved=True, client=FakeClient(KIRAN))


def test_build_passes_repo_dir_and_bad_json(tmp_path, monkeypatch):
    sandbox(tmp_path, monkeypatch)
    sw = FakeSwitch()
    factory.build("x", sw, approved=True, client=FakeClient(KIRAN))
    assert sw.sent[0][1] == {"repo_dir": str(tmp_path)}
    bad = FakeClient(KIRAN)
    bad.j = "nonsense"
    assert factory.build("x", FakeSwitch(), approved=True, client=bad).startswith("refused")
