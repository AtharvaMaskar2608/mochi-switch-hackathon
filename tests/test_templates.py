import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = sorted((ROOT / "workflows").glob("*.yaml"))
FILES = sorted((ROOT / "org").glob("*.yaml")) + WORKFLOWS
WORKFLOW_NAMES = {"data-question", "shipped-digest", "bug-fix-review", "release-train"}

PEOPLE = {"asha", "ravi", "meera", "dev"}
AGENTS = {"chief", "mochi-data", "ship", "reviewer", "security", "sre", "designer",
          "intern-meera", "intern-dev", "standin-data"}
ROLES = {"data-steward", "release-scribe", "reviewer", "security", "deployer", "on-call",
         "release-owner", "video", "intern-meera", "intern-dev"}
ROOMS = {"#product", "#leadership", "#eng", "#chief-ops"}
INTERNS = ("intern-meera", "intern-dev")


def load(p):
    return yaml.safe_load(p.read_text())


def expand(s):
    """Expand {asker} to each developer (the only askers who own an intern) so placeholders can be checked against the table."""
    return {s.replace("{asker}", p) for p in ("meera", "dev")} if "{asker}" in s else {s}


def rooms_of(doc):
    if doc.get("version") == 2:
        return [r for r in doc["resources"].values() if r["type"] == "room"]
    return doc.get("rooms") or [doc["room"]]


def test_all_yaml_parses():
    assert len(FILES) >= 5
    for p in FILES:
        assert isinstance(load(p), dict), p


def test_names_match_global_names_table():
    for p in FILES:
        doc = load(p)
        if doc.get("version") == 2:
            for r in doc["resources"].values():
                if r["type"] == "agent":
                    assert r["name"] in AGENTS, (p, r["name"])
                if r["type"] == "group":
                    assert r["name"] == "brightcart"
        else:
            assert doc["group"]["name"] == "brightcart", p
        for room in rooms_of(doc):
            name = room["name"]
            assert name in ROOMS or re.fullmatch(rf"work-({'|'.join(WORKFLOW_NAMES)})-\{{\$timestamp\}}", name), (p, name)
            for role in room.get("roles", []):
                assert expand(role["name"]) <= ROLES, (p, role["name"])
            for u in room.get("users", []):
                assert u in PEOPLE or u in ("{asker}", "{$creator}"), (p, u)
            if doc.get("version") != 2:
                for a in room["agents"]:
                    assert expand(a) <= AGENTS, (p, a)
    assert {p.stem for p in WORKFLOWS} <= WORKFLOW_NAMES


def org_agents():
    doc = load(ROOT / "org" / "brightcart-org.yaml")
    return {r["name"]: r for r in doc["resources"].values() if r["type"] == "agent"}


def test_interns_auto_mode_false():
    ag = org_agents()
    for n in INTERNS:
        assert ag[n]["auto_mode"] is False
        assert ag[n]["addressing"] == "owner-agents"


def test_no_agent_addressing_anyone():
    for n, a in org_agents().items():
        if n == "chief":
            assert "anyone_in" in a["addressing"] and a["allow_existing"] is True
        else:
            assert a["addressing"] == "owner-agents", n
    for p in FILES:
        assert not re.search(r"addressing:\s*anyone\s*$", p.read_text(), re.M), p


def test_every_workflow_has_brief_and_asker_params():
    assert {p.stem for p in WORKFLOWS} == WORKFLOW_NAMES
    for p in WORKFLOWS:
        params = load(p)["params"]
        assert params["brief"]["type"] == "string" and params["brief"]["multiline"] is True, p
        assert params["asker"]["type"] == "user", p


def test_kickoff_addresses_exactly_one_role():
    for p in WORKFLOWS:
        room = load(p)["rooms"][0]
        mentions = set(re.findall(r"@([\w{}.-]+)", room["kickoff"]))
        assert len(mentions) == 1, (p, mentions)
        assert expand(mentions.pop()) <= {a for x in room["agents"] for a in expand(x)}, p


def test_bug_fix_review_end_condition_mentions_asker():
    room = load(ROOT / "workflows" / "bug-fix-review.yaml")["rooms"][0]
    assert re.search(r"only \{asker\}.*approve", room["instructions"], re.S | re.I)


def test_release_train_has_no_real_deploy_command():
    text = (ROOT / "workflows" / "release-train.yaml").read_text()
    for bad in ("kubectl", "helm", "terraform", "deploy.sh"):
        assert bad not in text


def test_agent_dirs():
    for n in ("mochi-data", "ship", "reviewer", "sre", "intern-meera", "intern-dev"):
        lines = (ROOT / "agents" / n / "CLAUDE.md").read_text().splitlines()
        assert 0 < len(lines) <= 40, n
    assert (ROOT / "agents/reviewer/.claude/skills/code-review/SKILL.md").exists()
    for n in ("mochi-data", "sre"):
        json.loads((ROOT / "agents" / n / ".mcp.json").read_text())
