"""dev -> QA -> PM handoff: ticket, QA room, the QA person's verdict. Fakes only; no real gh."""
import subprocess
from types import SimpleNamespace

import pytest

from chief import tickets
from chief.main import apply, handle, qa_owner
from connectors import qa
from test_main import FakeClient, FakeWriter, _profile, ctx, gate_of, msg  # noqa: F401  (ctx is a fixture)

READY = "chief, the refunds rounding fix is ready for QA on fix/bc-142-rounding"


class FakeTickets:
    def __init__(self, configured=True, fail=()):
        self.calls, self.configured, self.fail = [], configured, set(fail)

    def _maybe_fail(self, op):
        if op in self.fail:
            raise subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 401\ngh: not logged in")

    def create(self, title, body, labels):
        self.calls.append(("create", title, body, labels))
        self._maybe_fail("create")
        return {"number": 12, "url": "https://github.com/o/r/issues/12"} if self.configured else None

    def comment(self, n, body):
        self.calls.append(("comment", n, body))
        self._maybe_fail("comment")

    def close(self, n, comment):
        self.calls.append(("close", n, comment))
        self._maybe_fail("close")

    def label(self, n, add, remove):
        self.calls.append(("label", n, add, remove))


@pytest.fixture
def qctx(ctx):
    mem = ctx.store.root
    _profile(mem, "priya", "Priya", "QA")
    _profile(mem, "dev", "Dev", "dev")
    (ctx.workflows_dir / "ready-for-qa.yaml").write_text("name: ready-for-qa\n")
    ctx.gate = gate_of(template="ready-for-qa", fits=0.9)
    ctx.tickets = FakeTickets()
    ctx.room_ids = {"product": "P"}
    return ctx


def handed_off(qctx, sender="dev", body=READY):
    c = FakeClient()
    apply(handle(msg(body, sender=sender, room="E", room_name="eng", addressed=True), qctx), c, qctx)
    return c


def test_ready_for_qa_files_ticket_and_opens_room(qctx):
    c = handed_off(qctx)
    (_, title, body, labels), = qctx.tickets.calls
    assert labels == ["ready-for-qa"] and title == "Ready for QA: the refunds rounding fix on fix/bc-142-rounding"
    assert "branch: fix/bc-142-rounding" in body and "asked by: dev" in body and "qa: priya" in body
    assert c.log[0] == ("room_from_yaml", "name: ready-for-qa\n", {"brief": body, "asker": "dev", "qa": "priya",
                                                                   "ticket_url": "https://github.com/o/r/issues/12"})
    assert c.log[1] == ("target", "W1", body, ["qa-agent"], None)
    assert c.log[2] == ("post", "E", "Filed #12 https://github.com/o/r/issues/12 and handed it to Priya (QA).")
    assert qctx.qa_rooms["W1"]["qa"] == "priya" and qctx.qa_rooms["W1"]["ticket"] == 12


def test_qa_room_failure_says_so_in_thread(qctx):
    class Broken(FakeClient):
        def room_from_yaml(self, yaml, inputs):
            raise RuntimeError("400")
    c = Broken()
    apply(handle(msg(READY, sender="dev", room="E", room_name="eng", addressed=True), qctx), c, qctx)
    assert c.log == [("post", "E", "Filed #12 https://github.com/o/r/issues/12, but I couldn't open a ready-for-qa "
                                   "room (Switch said RuntimeError); qa-agent may not be running")]
    assert qctx.qa_rooms == {}


def test_only_qa_person_can_pass_or_fail(qctx):
    handed_off(qctx)
    refused = handle(msg("pass", sender="dev", room="W1"), qctx)
    assert refused == [("post", {"room_id": "W1", "body": "only Priya (QA) can pass or fail this", "thread_id": None})]
    assert handle(msg("qa pass", sender="qa-agent", room="W1", addressed=True, is_agent=True), qctx) == []
    assert handle(msg("QA PASSED: pytest is green", sender="qa-agent", room="W1", is_agent=True), qctx) == []
    assert handle(msg("pass", sender="stranger", room="W1"), qctx)[0][1]["body"].startswith("only Priya")
    assert "W1" in qctx.qa_rooms and len(qctx.tickets.calls) == 1  # nothing decided


def test_qa_pass_closes_ticket_and_tells_pm(qctx):
    handed_off(qctx)
    c = FakeClient()
    apply(handle(msg("qa pass", sender="priya", room="W1"), qctx), c, qctx)
    assert qctx.tickets.calls[-1] == ("close", 12, "Verified by Priya (QA)")
    assert ("post", "E", "the refunds rounding fix on fix/bc-142-rounding: verified by Priya (QA), #12 closed") in c.log
    assert ("target", "P", "the refunds rounding fix on fix/bc-142-rounding verified by QA, ready for the next release (#12)",
            ["ravi"], None) in c.log
    assert "W1" not in qctx.qa_rooms
    assert handle(msg("pass", sender="priya", room="W1"), qctx) != []  # spent: now an ordinary message


def test_qa_fail_comments_relabels_and_routes_to_dev(qctx):
    handed_off(qctx)
    c = FakeClient()
    apply(handle(msg("qa fail 25 paise at 50% refunds 12, expected 13", sender="priya", room="W1"), qctx), c, qctx)
    assert qctx.tickets.calls[1:] == [("comment", 12, "QA failed (Priya): 25 paise at 50% refunds 12, expected 13"),
                                      ("label", 12, ["qa-failed"], ["ready-for-qa"])]
    assert ("target", "E", "QA failed the refunds rounding fix on fix/bc-142-rounding (#12), from Priya: "
            "25 paise at 50% refunds 12, expected 13", ["dev"], None) in c.log


def test_qa_fail_respects_focus_hold(qctx):
    handed_off(qctx, sender="meera", body="@chief ready for QA: refunds rounding, branch fix/bc-142")  # meera in focus
    acts = handle(msg("fail: rounding still off", sender="priya", room="W1"), qctx)
    hold = [a for k, a in acts if k == "hold"]
    assert len(hold) == 1 and hold[0]["for"] == "meera" and hold[0]["item"]["room_id"] == "E"
    assert "rounding still off" in hold[0]["item"]["text"]
    assert not any(k == "target" for k, _ in acts)
    qctx.qa_rooms["W2"] = {"qa": "priya", "asker": "meera", "title": "t", "ticket": 3, "room_id": "E", "thread_id": None}
    urgent = handle(msg("qa fail - urgent blocker, refunds broken", sender="priya", room="W2"), qctx)
    assert any(k == "target" and a["names"] == ["meera"] for k, a in urgent)  # urgent: no hold


def test_no_tracker_configured_still_hands_off(qctx):
    qctx.tickets = FakeTickets(configured=False)
    c = handed_off(qctx)
    assert c.log[0][2]["ticket_url"] == "none"
    assert c.log[2] == ("post", "E", "No ticket tracker configured; tracking in this thread. Handed it to Priya (QA).")
    c = FakeClient()
    apply(handle(msg("approved", sender="priya", room="W1"), qctx), c, qctx)
    assert [x[0] for x in qctx.tickets.calls] == ["create"]  # no close without a ticket
    assert any(x[0] == "target" and x[3] == ["ravi"] and "(no ticket)" in x[2] for x in c.log)


def test_qa_owner_from_memory(qctx):
    _profile(qctx.store.root, "kiran", "Kiran", "QA")
    qctx.store.add("team", "team", "fact", "qa owner: payments → kiran", {"room": "#eng", "date": "x", "by": "person"})
    assert qa_owner("the payments retry fix is ready for QA", qctx) == "kiran"
    assert qa_owner("the refunds fix is ready for QA", qctx) == "priya"  # no item for refunds: the default
    qctx.store.add("team", "team", "fact", "qa owner: search → nobody", {"room": "#eng", "date": "x", "by": "person"})
    assert qa_owner("search fix ready for QA", qctx) == "priya"  # unknown person: the default


def fake_gh(out="https://github.com/o/r/issues/7\n"):
    calls = []

    def run(args, **kw):
        calls.append(args)
        return SimpleNamespace(stdout=out, returncode=0)
    run.calls = calls
    return run


def test_tickets_strip_mentions(monkeypatch):
    monkeypatch.setenv("TICKETS_REPO", "o/r")
    monkeypatch.setattr(tickets, "_made", set())
    run = fake_gh()
    assert tickets.create("fix for @meera", "cc @ravi and ＠asha", ["ready-for-qa"], run=run) == \
        {"number": 7, "url": "https://github.com/o/r/issues/7"}
    assert run.calls[0] == ["gh", "label", "create", "ready-for-qa", "--force", "--repo", "o/r"]
    assert run.calls[1] == ["gh", "label", "create", "brightcart", "--force", "--repo", "o/r"]
    create = run.calls[2]
    assert create[:2] == ["gh", "issue"] and "@" not in " ".join(create) and "＠" not in " ".join(create)
    assert create[create.index("--title") + 1] == "fix for meera"
    assert [create[i + 1] for i, a in enumerate(create) if a == "--label"] == ["ready-for-qa", "brightcart"]
    tickets.comment(7, "@dev see steps", run=run)
    assert run.calls[-1] == ["gh", "issue", "comment", "7", "--body", "dev see steps", "--repo", "o/r"]
    tickets.label(7, ["qa-failed"], ["ready-for-qa"], run=run)
    assert run.calls[-1] == ["gh", "issue", "edit", "7", "--add-label", "qa-failed", "--remove-label", "ready-for-qa",
                             "--repo", "o/r"]
    tickets.close(7, "Verified by Priya (QA)", run=run)
    assert run.calls[-1][:4] == ["gh", "issue", "close", "7"]


def test_tickets_unconfigured_returns_none(monkeypatch):
    monkeypatch.delenv("TICKETS_REPO", raising=False)
    run = fake_gh()
    assert tickets.create("t", "b", ["ready-for-qa"], run=run) is None
    assert tickets.close(1, "c", run=run) is None and tickets.label(1, ["x"], [], run=run) is None
    assert run.calls == []


def fake_repo_run(pytest_rc, pytest_out="", checkout_rc=0):
    calls = []

    def run(args, **kw):
        calls.append(args)
        if args[0] == "git":
            return SimpleNamespace(returncode=checkout_rc, stdout="")
        return SimpleNamespace(returncode=pytest_rc, stdout=pytest_out)
    run.calls = calls
    return run


def qa_msg(body):
    return msg(body, sender="chief", room="W1", addressed=True, is_agent=True)


def test_qa_connector_reports_failing_tests(tmp_path):
    (tmp_path / ".git").mkdir()
    out = ("F.\n=== short test summary info ===\n"
           "FAILED tests/test_refunds.py::test_partial_refund_half_paisa_rounds_up - assert 12 == 13\n"
           "1 failed, 1 passed in 0.01s\n")
    run = fake_repo_run(1, out)
    r = qa.handle(qa_msg("brief\n\nbranch: fix/bc-142\nqa: priya"), run=run, repo=tmp_path)
    assert run.calls[0] == ["git", "checkout", "-q", "fix/bc-142", "--"]
    assert r.text.startswith("QA FAILED on fix/bc-142: tests/test_refunds.py::test_partial_refund_half_paisa_rounds_up")
    assert qa.handle(qa_msg("branch: fix/bc-142"), run=fake_repo_run(0), repo=tmp_path).text.startswith("QA PASSED")
    assert "no branch" in qa.handle(qa_msg("branch: nope/x"), run=fake_repo_run(0, checkout_rc=1), repo=tmp_path).text
    assert qa.handle(qa_msg("branch: --orphan"), run=fake_repo_run(0), repo=tmp_path).text.startswith("QA FAILED")
    assert "pytest exit 2" in qa.handle(qa_msg("x"), run=fake_repo_run(2), repo=tmp_path).text  # fail closed


def test_qa_connector_builds_missing_clone(tmp_path):
    run = fake_repo_run(0)
    qa.handle(qa_msg("x"), run=run, repo=tmp_path / "shop")
    assert run.calls[0][0] == "bash" and run.calls[0][2:] == ["--local", str(tmp_path / "shop")]


class BranchyWriter(FakeWriter):
    def brief(self, msg, sender, hits, template):
        return "QA: refunds rounding\nBranch: none\n**branch:** main\nqa: dev\nTest 25 paise at 50%."


def test_model_brief_cannot_choose_the_branch(qctx, tmp_path):
    qctx.writer = BranchyWriter()
    c = handed_off(qctx)
    body = c.log[1][2]  # what qa-agent receives
    assert body.splitlines()[:3] == ["branch: fix/bc-142-rounding", "asked by: dev", "qa: priya"]
    assert "none" not in body and "main" not in body and "qa: dev" not in body
    assert "Test 25 paise at 50%." in body and "QA: refunds rounding" in qctx.tickets.calls[0][2]
    (tmp_path / ".git").mkdir()
    run = fake_repo_run(0)
    qa.handle(qa_msg(body), run=run, repo=tmp_path)
    assert run.calls[0] == ["git", "checkout", "-q", "fix/bc-142-rounding", "--"]


def test_asker_cannot_be_own_qa(qctx):
    qctx.store.add("team", "team", "fact", "qa owner: payments → dev", {"room": "#eng", "date": "x", "by": "person"})
    handed_off(qctx, sender="dev", body="chief, the payments retry fix is ready for QA")
    assert qctx.tickets.calls[0][2].splitlines()[:2] == ["asked by: dev", "qa: priya"]  # memory said dev: default
    qctx.tickets.calls.clear()
    c = handed_off(qctx, sender="priya", body="chief, the refunds export fix is ready for QA")
    assert c.log == [("post", "E", "needs a different person to QA this; who should test it?")]
    assert qctx.tickets.calls == []


def test_ticket_create_failure_still_hands_off(qctx):
    qctx.tickets = FakeTickets(fail={"create"})
    c = handed_off(qctx)
    assert c.log[0][2]["ticket_url"] == "none" and c.log[1][0] == "target"
    assert c.log[2] == ("post", "E", "I couldn't file the ticket (gh error: gh: not logged in); "
                                     "tracking in this thread. Handed it to Priya (QA).")
    assert qctx.qa_rooms["W1"]["ticket"] is None


def test_close_failure_is_reported_and_retried(qctx):
    handed_off(qctx)
    qctx.tickets.fail = {"close"}
    c = FakeClient()
    apply(handle(msg("qa pass", sender="priya", room="W1"), qctx), c, qctx)
    assert not any(x[0] == "target" for x in c.log)  # the PM is not told
    said = [x[2] for x in c.log if x[0] == "post"]
    assert said == ["verified by Priya, but closing #12 failed: gh: not logged in. Say it again to retry."] * 2
    assert {x[1] for x in c.log} == {"W1", "E"} and "W1" in qctx.qa_rooms  # kept for a retry
    qctx.tickets.fail = set()
    c = FakeClient()
    apply(handle(msg("qa pass", sender="priya", room="W1"), qctx), c, qctx)
    assert qctx.tickets.calls[-1] == ("close", 12, "Verified by Priya (QA)")
    assert any(x[0] == "target" and x[3] == ["ravi"] for x in c.log) and "W1" not in qctx.qa_rooms


def test_fail_verdict_ticket_error_still_routes_to_dev(qctx):
    handed_off(qctx)
    qctx.tickets.fail = {"comment"}
    c = FakeClient()
    apply(handle(msg("fail: still rounds down", sender="priya", room="W1"), qctx), c, qctx)
    assert any(x[0] == "target" and x[3] == ["dev"] for x in c.log)
    assert "QA failed, but updating #12 failed: gh: not logged in. Say it again to retry." in [x[2] for x in c.log]
    assert "W1" in qctx.qa_rooms


@pytest.mark.parametrize("body", ["fail to see why this matters", "passed the review earlier", "failed to load",
                                  "fail-safe ok", "approved by whom?"])
def test_chat_is_not_a_verdict(qctx, body):
    handed_off(qctx)
    out = handle(msg(body, sender="dev", room="W1"), qctx)
    assert not any("only Priya" in a.get("body", "") for _, a in out)
    handle(msg(body, sender="priya", room="W1"), qctx)
    assert "W1" in qctx.qa_rooms  # nothing decided


@pytest.mark.parametrize("body", ["qa pass", "pass", "Passed.", "approved", "qa fail rounding", "fail: rounding",
                                  "fail - rounding", "QA failed: x", "fail"])
def test_whole_message_verdicts(qctx, body):
    handed_off(qctx)
    handle(msg(body, sender="priya", room="W1"), qctx)
    assert "W1" not in qctx.qa_rooms


def test_labels_created_once_and_body_capped(monkeypatch):
    monkeypatch.setenv("TICKETS_REPO", "o/r")
    monkeypatch.setattr(tickets, "_made", set())
    run = fake_gh()
    tickets.create("a", "x" * 9000, ["ready-for-qa"], run=run)
    tickets.create("b", "y", ["ready-for-qa"], run=run)
    assert sum(c[1] == "label" for c in run.calls) == 2  # ready-for-qa + brightcart, once each
    first = next(c for c in run.calls if c[1] == "issue")
    assert len(first[first.index("--body") + 1]) == tickets.MAX_BODY


def test_qa_agent_reads_branch_after_switch_mention_prefix(tmp_path):
    (tmp_path / ".git").mkdir()
    run = fake_repo_run(0)
    qa.handle(qa_msg("@qa-agent branch: fix/refund-rounding\nasked by: meera\n\nbrief"), run=run, repo=tmp_path)
    assert run.calls[0][3] == "fix/refund-rounding"  # live: Switch prepends the mention to a targeted body


def test_ticket_title_from_the_request():
    from chief.main import _subject
    assert _subject("@chief the refunds rounding fix on fix/refund-rounding is ready for QA") == \
        "the refunds rounding fix on fix/refund-rounding"
    assert _subject("chief, ready for testing: coupons") == "coupons"
    assert _subject("@chief ready for QA") == "the change"
