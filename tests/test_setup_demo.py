import importlib.util
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("setup_demo", ROOT / "scripts/setup_demo.py")
sd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sd)

GROUPS = [{"id": "g1", "name": "brightcart"}, {"id": "g2", "name": "other"}]


def test_parse_env_skips_comments_and_strips_quotes():
    assert sd.parse_env('# x=1\nA="b"\nC=d=e\n\nE=\'f\'') == {"A": "b", "C": "d=e", "E": "f"}


def test_merge_people_keeps_saved_passwords_and_fills_new_ones():
    out = sd.merge_people({"ravi": {"username": "ravi", "email": "r@x", "password": "keep", "mm_id": "m"}})
    assert out["ravi"] == {"username": "ravi", "email": "r@x", "password": "keep", "mm_id": "m"}
    assert set(out) == {"asha", "ravi", "meera", "dev"}
    assert out["asha"]["email"] == "asha@brightcart.test" and len(out["asha"]["password"]) > 20
    assert out["asha"]["password"] != out["dev"]["password"]


def test_is_linked_needs_a_verified_claim_by_the_same_person():
    users = [
        {"external_username": "ravi", "claimed_by": [{"user_name": "ravi", "verified": True}]},
        {"external_username": "asha", "claimed_by": [{"user_name": "asha", "verified": False}]},
        {"external_username": "meera", "claimed_by": [{"user_name": "Admin", "verified": True}]},
        {"external_username": "dev", "claimed_by": None},
    ]
    assert [p for p in sd.PEOPLE if sd.is_linked(users, p)] == ["ravi"]


def test_org_launched_needs_all_four_rooms_in_the_group():
    rooms = [{"name": n, "group_id": "g1", "archived": False} for n in sd.ROOMS]
    assert sd.org_launched(GROUPS, rooms)
    assert not sd.org_launched(GROUPS, rooms[:3])
    assert not sd.org_launched(GROUPS, rooms[:3] + [{"name": "chief-ops", "group_id": None}])  # a stray mirror
    assert not sd.org_launched(GROUPS, rooms[:3] + [{**rooms[3], "archived": True}])
    assert not sd.org_launched([], rooms)


def test_lite_org_keeps_only_chief_and_drops_the_machine():
    doc = yaml.safe_load((ROOT / "org/brightcart-org.yaml").read_text())
    lite = sd.lite_org(doc)
    agents = [k for k, v in lite["resources"].items() if v["type"] == "agent"]
    assert agents == ["chief"]
    assert "machine" not in lite["params"] and "machine" not in lite["resources"]["chief"]
    rooms = {v["name"]: v for v in lite["resources"].values() if v["type"] == "room"}
    assert set(rooms) == set(sd.ROOMS) and all(r["agents"] == ["chief"] for r in rooms.values())
    assert rooms["product"]["users"] == ["asha", "ravi"]  # people and roles survive
    assert "machine" in doc["params"]  # the input is not mutated


def test_launch_payload_fills_chief_with_the_existing_agent():
    p = sd.launch_payload("version: 2", Path("/repo"))
    assert p == {"template": "version: 2", "template_name": "brightcart-org",
                 "filled": {"chief": "chief"}, "inputs": {"repo_dir": "/repo"}}


def test_chief_rules_owner_plus_anyone_in_the_group():
    owner, group = sd.chief_rules("g1")
    assert owner["owner"] and owner["owner_agents"] and owner["users"] == [] and owner["agents"] == []
    assert group["room_groups"] == ["g1"] and group["users"] == "*" and group["agents"] == []
    assert not group["owner"] and not group["platform"]


def test_find_key_id_only_matches_a_registration_key_with_our_label():
    keys = [{"id": "a", "label": "chief-hackathon-local", "type": "agent"},
            {"id": "b", "label": "other", "type": "registration"},
            {"id": "c", "label": "chief-hackathon-local", "type": "registration"}]
    assert sd.find_key_id(keys) == "c"
    assert sd.find_key_id(keys[:2]) is None


def test_redact_hides_secrets_but_not_short_values():
    assert sd.redact("pw=hunter22 n=abc", ["hunter22", "abc", ""]) == "pw=<redacted> n=abc"


def test_write_private_is_mode_600(tmp_path):
    f = tmp_path / "x.json"
    sd.write_private(f, "{}")
    assert f.read_text() == "{}" and f.stat().st_mode & 0o777 == 0o600


class FakeSetup(sd.Setup):
    def __init__(self, dry):  # no network
        self.dry, self.changes = dry, 0


def test_say_counts_changes_and_dry_run_never_acts(capsys):
    s = FakeSetup(dry=True)
    assert s.say(True, "exists") is False and s.changes == 0
    assert s.say(False, "missing") is False and s.changes == 1
    live = FakeSetup(dry=False)
    assert live.say(False, "missing") is True and live.changes == 1
    assert "= exists" in capsys.readouterr().out


def test_needs_machine_only_for_the_specialist_errors():
    assert sd.needs_machine('{"detail": {"code": "machine_unresolved", "resource": "ship"}}')
    assert sd.needs_machine('{"detail": "agents_not_created: ship, sre"}')
    assert not sd.needs_machine("Internal Server Error")
    assert not sd.needs_machine('{"detail": "group brightcart already exists"}')
