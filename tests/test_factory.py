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
    def __init__(self):
        self.sent = []

    def room_from_yaml(self, y):
        self.sent.append(y)


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
