import json
import time
from types import SimpleNamespace

import httpx

from chief.models import Msg
from connectors import designer, security
from connectors.base import Reply, run_connector


def msg(body="hi", addressed=True, thread="t1"):
    return Msg("r1", "work", 1, "u", "asha", body, thread, addressed, False, "now")


class FakeClient:
    def __init__(self, msgs):
        self.msgs, self.calls = msgs, []

    def events(self):
        return iter(self.msgs)

    def post(self, room_id, body, thread_id=None):
        self.calls.append(("post", body, thread_id))

    def target(self, room_id, body, names=None, roles=None, thread_id=None):
        self.calls.append(("target", body, roles, thread_id))


def fake_scan(tmp_path, vuln, rc):
    s = {"vulnerabilities": {"critical": 0, "high": vuln, "medium": 0}, "sast": {"secrets": 0},
         "policy": {"breached": bool(vuln)},
         "top_vulnerabilities": [{"id": "CVE-1", "package": "lodash", "version": "1", "fix": "2"}] * vuln}
    d = tmp_path / "reports" / security._slug(tmp_path / "x") / "latest"
    d.mkdir(parents=True)
    (d / "summary.json").write_text(json.dumps(s))
    return lambda *a, **k: SimpleNamespace(returncode=rc)


def test_security_exit1_blocks_and_targets_intern(tmp_path):
    r = security.handle(msg(f"owner: meera\npath: {tmp_path/'x'}"), run=fake_scan(tmp_path, 2, 1), cyber=tmp_path)
    assert r.text.startswith("BLOCKED") and r.roles == ["intern-meera"] and "CVE-1" in r.text


def test_security_clean_targets_release_scribe(tmp_path):
    r = security.handle(msg(f"path: {tmp_path/'x'}"), run=fake_scan(tmp_path, 0, 0), cyber=tmp_path)
    assert r.text.startswith("PASSED") and r.roles == ["release-scribe"]


def test_designer_polls_until_final(tmp_path):
    n = {"u": 0}

    def h(req):
        p = req.url.path
        assert req.headers["authorization"].startswith("Bearer ")
        if p.endswith("/messages"):
            return httpx.Response(200, json={"reply": "on it", "files": [], "working": True})
        if p.endswith("/updates"):
            n["u"] += 1
            u = [] if n["u"] < 2 else ["done: all-hands.mp4"]
            return httpx.Response(200, json={"updates": u, "cursor": n["u"]})
        return httpx.Response(200, content=b"vid")

    r = designer.handle(msg("video"), transport=httpx.MockTransport(h), poll_s=0, out_dir=tmp_path)
    assert r.text == "done: all-hands.mp4" and r.files[0].read_bytes() == b"vid" and n["u"] == 2


def test_connector_ignores_unaddressed():
    c = FakeClient([msg(addressed=False)])
    run_connector("x", lambda m: Reply("no"), [], client=c, once=True)
    assert c.calls == []


def test_long_job_posts_working_first():
    c = FakeClient([msg("make video\nmore")])
    run_connector("x", lambda m: (time.sleep(0.2), Reply("done"))[1], [], client=c, long_s=0.05, once=True)
    assert [x[1] for x in c.calls] == ["working on it: make video", "done"]
    assert all(x[-1] == "t1" for x in c.calls)
