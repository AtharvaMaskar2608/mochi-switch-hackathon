"""Idempotent demo setup on a local Switch + its bundled Mattermost (judge path, story 1).

Steps, each skipped when already done:
  personal registration key -> Mattermost + Switch users (asha, ravi, meera, dev), linked ->
  register chief + standin-data -> launch org/brightcart-org.yaml -> Chief's addressing policy ->
  stand-in SQLite db.

usage: uv run python scripts/setup_demo.py [--dry-run]     (run from the repo root)
env:   SWITCH_DIR (default ./.switch): the Switch checkout whose .env holds the admin logins.
Never prints a secret. Generated passwords go to .local-test-users.json (mode 600).
"""
import json
import os
import secrets
import sys
import time
from pathlib import Path

import httpx
import yaml

ROOT = Path(__file__).resolve().parent.parent
PEOPLE = ["asha", "ravi", "meera", "dev"]
ROOMS = ["product", "leadership", "eng", "chief-ops"]
GROUP = "brightcart"
KEY_LABEL = "chief-hackathon-local"
USERS_FILE = ROOT / ".local-test-users.json"
SWITCH_ENV_FILE = ROOT / ".local-switch.env"
# name -> (creds file, description): the same files and descriptions chief/connectors use in connect()
AGENTS = {
    "chief": (ROOT / ".chief.json", "Chief, the team's chief of staff"),
    "standin-data": (ROOT / ".connector-standin-data.json", "standin-data connector; roles: data-steward"),
}


# ---- pure parts (tests/test_setup_demo.py) --------------------------------------------------

def parse_env(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def merge_people(creds: dict, people: list[str] = PEOPLE) -> dict:
    """Keep what we already know; give new people an email and a fresh password."""
    out = {p: dict(c) for p, c in creds.items()}
    for p in people:
        c = out.setdefault(p, {})
        c.setdefault("username", p)
        c.setdefault("email", f"{p}@brightcart.test")
        c.setdefault("password", "Bc-" + secrets.token_urlsafe(18))
    return out


def is_linked(bridge_users: list[dict], person: str) -> bool:
    return any(u.get("external_username") == person
               and any(c.get("user_name") == person and c.get("verified") for c in u.get("claimed_by") or [])
               for u in bridge_users)


def group_id(groups: list[dict], name: str = GROUP) -> str | None:
    return next((g["id"] for g in groups if g["name"] == name), None)


def org_launched(groups: list[dict], rooms: list[dict]) -> bool:
    gid = group_id(groups)
    have = {r["name"] for r in rooms if gid and r.get("group_id") == gid and not r.get("archived")}
    return set(ROOMS) <= have


def lite_org(doc: dict) -> dict:
    """brightcart-org without the machine-bound specialists: only Chief, no machine param."""
    doc = json.loads(json.dumps(doc))
    res = doc["resources"]
    for k in [k for k, v in res.items() if v["type"] == "agent" and k != "chief"]:
        del res[k]
    for v in res.values():
        if v["type"] == "room":
            v["agents"] = [a for a in v.get("agents", []) if a == "chief"]
    doc["params"].pop("machine", None)
    res["chief"].pop("machine", None)
    return doc


def launch_payload(template: str, repo_dir: Path) -> dict:
    return {"template": template, "template_name": "brightcart-org", "filled": {"chief": "chief"},
            "inputs": {"repo_dir": str(repo_dir)}}


def chief_rules(gid: str) -> list[dict]:
    """The owner (and the owner's agents) anywhere, plus any person in a brightcart room.
    Set by hand because a filled template slot ignores the template's `anyone_in`."""
    rule = {"rooms": "*", "room_groups": "*", "users": [], "agents": [],
            "owner": False, "owner_agents": False, "platform": False}
    return [{**rule, "owner": True, "owner_agents": True}, {**rule, "room_groups": [gid], "users": "*"}]


def find_key_id(keys: list[dict], label: str = KEY_LABEL) -> str | None:
    return next((k["id"] for k in keys if k.get("label") == label and k.get("type") == "registration"), None)


def redact(text: str, secret_values) -> str:
    for v in secret_values:
        if v and len(v) > 5:
            text = text.replace(v, "<redacted>")
    return text


def write_private(path: Path, text: str) -> None:
    path.touch(mode=0o600)
    os.chmod(path, 0o600)
    path.write_text(text)


# ---- live parts --------------------------------------------------------------------------

class Setup:
    def __init__(self, switch_env: dict, dry: bool):
        self.env, self.dry, self.changes = switch_env, dry, 0
        self.url = f"http://localhost:{switch_env.get('API_HOST_PORT') or 8000}"
        self.mm = f"http://localhost:{switch_env.get('MATTERMOST_HOST_PORT') or 8065}/api/v4"
        self.secrets = [v for k, v in switch_env.items()
                        if any(s in k for s in ("PASSWORD", "TOKEN", "SECRET", "KEY"))]
        self.gw = self.login(switch_env["GATEWAY_ADMIN_EMAIL"], switch_env["GATEWAY_ADMIN_PASSWORD"])
        self.mm_admin = self.mm_login(switch_env["MATTERMOST_ADMIN_USER"], switch_env["MATTERMOST_ADMIN_PASSWORD"])

    def say(self, done: bool, what: str) -> bool:
        """Log one step. True means: go ahead and make the change."""
        if done:
            print(f"  = {what}")
            return False
        self.changes += 1
        print(f"  + {what}{' (dry run: skipped)' if self.dry else ''}")
        return not self.dry

    def check(self, r: httpx.Response, what: str) -> httpx.Response:
        if r.is_error:
            sys.exit(f"{what}: HTTP {r.status_code} {redact(r.text, self.secrets)[:400]}")
        return r

    def login(self, email: str, password: str) -> httpx.Client:
        c = httpx.Client(base_url=self.url, timeout=120)
        self.check(c.post("/gateway/auth/login", json={"email": email, "password": password}), "Switch login")
        return c

    def mm_login(self, user: str, password: str) -> dict:
        r = self.check(httpx.post(f"{self.mm}/users/login", json={"login_id": user, "password": password},
                                  timeout=30), f"Mattermost login {user}")
        return {"Authorization": f"Bearer {r.headers['Token']}"}

    def get(self, path: str):
        return self.check(self.gw.get(path), f"GET {path}").json()

    # -- steps

    def registration_key(self) -> str:
        known = parse_env(SWITCH_ENV_FILE.read_text()) if SWITCH_ENV_FILE.exists() else {}
        if known.get("SWITCH_REGISTRATION_TOKEN"):
            self.say(True, "personal registration key (.local-switch.env)")
            return known["SWITCH_REGISTRATION_TOKEN"]
        kid = find_key_id(self.get("/gateway/api-keys"))
        if kid:  # exists on the server, not saved here: reveal it (the admin owns it)
            key = self.get(f"/gateway/api-keys/{kid}/reveal")["key"]
            print(f"  = personal registration key '{KEY_LABEL}' (saved to .local-switch.env)")
        elif self.say(False, f"personal registration key '{KEY_LABEL}'"):
            key = self.check(self.gw.post("/gateway/api-keys", json={"label": KEY_LABEL, "expires_in_days": 30}),
                             "create registration key").json()["key"]
        else:
            return ""
        if not self.dry:
            write_private(SWITCH_ENV_FILE, f"SWITCH_URL={self.url}\nSWITCH_REGISTRATION_TOKEN={key}\n")
        return key

    def people(self) -> dict:
        saved = json.loads(USERS_FILE.read_text()) if USERS_FILE.exists() else {}
        creds = merge_people(saved)
        self.secrets += [c["password"] for c in creds.values()]
        team = self.check(httpx.get(f"{self.mm}/teams/name/{self.env.get('MATTERMOST_TEAM_NAME') or 'switch'}",
                                    headers=self.mm_admin), "Mattermost team").json()
        switch_users = {u["email"]: u for u in self.get("/gateway/users")}
        for p in PEOPLE:
            c = creds[p]
            r = httpx.get(f"{self.mm}/users/username/{p}", headers=self.mm_admin)
            if r.status_code == 200:
                self.say(True, f"Mattermost user {p}")
                c["mm_id"] = r.json()["id"]
                if p not in saved:  # made elsewhere: the password we just generated is not theirs
                    print(f"  ! {p}'s password is not in {USERS_FILE.name}; their logins are unknown")
                    del c["password"]
            elif self.say(False, f"Mattermost user {p}"):
                c["mm_id"] = self.check(httpx.post(f"{self.mm}/users", headers=self.mm_admin, json={
                    "username": p, "email": c["email"], "password": c["password"]}), f"create {p}").json()["id"]
            if "mm_id" in c:
                member = httpx.get(f"{self.mm}/teams/{team['id']}/members/{c['mm_id']}", headers=self.mm_admin)
                if self.say(member.status_code == 200, f"{p} in Mattermost team {team['name']}"):
                    self.check(httpx.post(f"{self.mm}/teams/{team['id']}/members", headers=self.mm_admin,
                                          json={"team_id": team["id"], "user_id": c["mm_id"]}), f"team add {p}")
            if self.say(c["email"] in switch_users, f"Switch user {p}") and "password" in c:
                self.check(self.gw.post("/gateway/users", json={"name": p, "email": c["email"],
                                                                "password": c["password"]}), f"Switch user {p}")
        if not self.dry:
            write_private(USERS_FILE, json.dumps(creds, indent=1))
        return creds

    def link(self, creds: dict) -> None:
        for _ in range(60):  # Switch seeds the Mattermost bridge shortly after it boots
            bridge = next((b["bridge_id"] for b in self.get("/gateway/collaborations")
                           if b.get("bridge_type") == "mattermost"), None)
            if bridge:
                break
            time.sleep(2)
        else:
            sys.exit("no Mattermost bridge on this Switch: is `just up` running Mattermost?")
        users = self.get(f"/gateway/collaborations/{bridge}/users")
        todo = [p for p in PEOPLE if self.say(is_linked(users, p), f"{p}: Mattermost account linked to Switch user")]
        for p in todo:  # claim as the person, then post the one-time code from their Mattermost account
            c = creds[p]
            if "password" not in c:
                print(f"  ! cannot link {p}: password unknown")
                continue
            r = self.check(self.login(c["email"], c["password"]).post(
                f"/gateway/collaborations/{bridge}/identities",
                json={"external_user_id": c["mm_id"], "username": p, "expects_confirmation": True}), f"claim {p}")
            code = ((r.json().get("link_request") or {}).get("code") or {}).get("code")
            if not code:
                continue  # already confirmed
            h = self.mm_login(p, c["password"])
            team = self.env.get("MATTERMOST_TEAM_NAME") or "switch"
            tid = self.check(httpx.get(f"{self.mm}/teams/name/{team}", headers=h), "team").json()["id"]
            town = self.check(httpx.get(f"{self.mm}/teams/{tid}/channels/name/town-square", headers=h), "town-square")
            self.check(httpx.post(f"{self.mm}/posts", headers=h,
                                  json={"channel_id": town.json()["id"], "message": f"!link-account {code}"}), "post code")
        for _ in range(30 if todo else 0):
            users = self.get(f"/gateway/collaborations/{bridge}/users")
            if all(is_linked(users, p) for p in todo):
                break
            time.sleep(1)
        else:
            if todo:
                print("  ! some links are not confirmed yet; re-run this script in a minute")

    def register(self, reg_key: str) -> None:
        from chief.switch_client import register

        existing = {a["name"]: a for a in self.get("/gateway/agents")}
        for name, (path, desc) in AGENTS.items():
            if path.exists():
                aid = json.loads(path.read_text())["agent_id"]
                if aid not in {a["id"] for a in existing.values()}:
                    sys.exit(f"{path.name} names an agent this Switch does not have: delete {path.name} and re-run")
                self.say(True, f"agent {name} registered ({path.name})")
            elif name in existing:
                sys.exit(f"agent {name} exists on Switch but {path.name} is missing here: "
                         f"delete the agent in the dashboard, or restore {path.name}")
            elif self.say(False, f"register agent {name} (owned by the admin's personal key)"):
                aid, key = register(self.url, reg_key, name, desc)
                write_private(path, json.dumps({"agent_id": aid, "api_key": key}))

    def org(self) -> None:
        if not self.say(org_launched(self.get("/gateway/room-groups"), self.get("/gateway/rooms")),
                        "launch org/brightcart-org.yaml (group brightcart, rooms " + ", ".join(ROOMS) + ")"):
            return
        text = (ROOT / "org/brightcart-org.yaml").read_text()
        r = self.gw.post("/gateway/rooms/from-template-document", json=launch_payload(text, ROOT), timeout=300)
        if r.is_error:
            print(f"    full org failed (HTTP {r.status_code}: {redact(r.text, self.secrets)[:300]})")
            print("    the specialist agents need a Switch Console machine; launching Chief's rooms without them")
            lite = yaml.safe_dump(lite_org(yaml.safe_load(text)), sort_keys=False)
            self.check(self.gw.post("/gateway/rooms/from-template-document",
                                    json=launch_payload(lite, ROOT), timeout=300), "launch org (no specialists)")

    def addressing(self) -> None:
        gid = group_id(self.get("/gateway/room-groups"))
        path = AGENTS["chief"][0]
        if not gid or not path.exists():  # dry run before the org or chief exist
            self.say(False, "Chief's addressing policy")
            return
        aid = json.loads(path.read_text())["agent_id"]
        agent = next(a for a in self.get("/gateway/agents") if a["id"] == aid)
        want = chief_rules(gid)
        if self.say(((agent.get("addressing_policy") or {}).get("rules")) == want,
                    "Chief's addressing policy: owner + anyone in a brightcart room"):
            self.check(self.gw.put(f"/gateway/agents/{aid}/addressing-policy", json={"policy": {"rules": want}}),
                       "addressing policy")

    def standin_db(self) -> None:
        from connectors.standin_data import DB, init_db

        if self.say(DB.exists(), f"stand-in data db ({DB.relative_to(ROOT)})"):
            init_db()


def main() -> None:
    dry = "--dry-run" in sys.argv
    switch_dir = Path(os.environ.get("SWITCH_DIR") or ROOT / ".switch")
    s = Setup(parse_env((switch_dir / ".env").read_text()), dry)
    print(f"Setting up the Chief demo on {s.url}{' (dry run)' if dry else ''}")
    key = s.registration_key()
    creds = s.people()
    s.link(creds)
    s.register(key)
    s.org()
    s.addressing()
    s.standin_db()
    print(f"{s.changes} change(s){' planned' if dry else ''}.")


if __name__ == "__main__":
    main()
