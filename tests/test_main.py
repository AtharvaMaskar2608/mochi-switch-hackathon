from datetime import datetime
import pytest

from chief.approvals import Approvals
from chief.ladder import IST, Ladder
from chief.main import Ctx, apply, handle, release_held, step
from chief.memory.store import MemoryStore
from chief.models import Decision, Msg

NOON = datetime(2026, 11, 7, 11, 0, tzinfo=IST)  # inside meera's 10:00-15:00 focus


def _profile(root, slug, name, role, focus="[]", preapproved="[]"):
    d = root / "people" / slug
    (d / "items").mkdir(parents=True)
    (d / "profile.md").write_text(
        f"---\nname: {name}\nrole: {role}\nstyle: short and plain\nfocus: {focus}\npreapproved: {preapproved}\n---\ncard\n")


class FakeWriter:
    def __init__(self, answer=None):
        self.answer, self.calls = answer, []

    def sanitize(self, t):
        return t.replace("@", "＠")

    def brief(self, msg, sender, hits, template):
        self.calls.append("brief")
        return f"brief for {template} from {sender.slug}"

    def restyle(self, answer, reader):
        self.calls.append("restyle")
        return f"styled: {answer}"

    def offer_line(self, msg, template):
        self.calls.append("offer_line")
        return "want me to take this?"

    def batched_brief(self, items, reader):
        return " | ".join(items)

    def answer_from_memory(self, q, hits, reader):
        self.calls.append("answer_from_memory")
        return self.answer


class FakeClient:
    def __init__(self):
        self.log, self.gap_rooms = [], set()

    def post(self, room_id, body, thread_id=None):
        self.log.append(("post", room_id, body))

    def target(self, room_id, body, names=None, roles=None, thread_id=None):
        self.log.append(("target", room_id, body, names, roles))

    def context(self, room_id, limit=50):
        self.log.append(("context", room_id))
        return {"messages": []}

    def room_from_yaml(self, yaml, inputs):
        self.log.append(("room_from_yaml", yaml, inputs))
        return {"id": "W1", "name": "work-data-question-20261107-1100"}


def gate_of(**kw):
    d = dict(for_me=0.9, rung="act", template=None, fits=0.0, urgency=0.3) | kw
    calls = []

    def gate(msg, sender, focused, templates, roles):
        calls.append((sender.slug, focused))
        return Decision(**d)
    gate.calls = calls
    return gate


@pytest.fixture
def ctx(tmp_path):
    mem = tmp_path / "memory"
    _profile(mem, "ravi", "Ravi", "pm")
    _profile(mem, "meera", "Meera", "dev", focus='["10:00-15:00"]')
    _profile(mem, "asha", "Asha", "ceo")
    (mem / "team" / "items").mkdir(parents=True)
    wf = tmp_path / "workflows"
    wf.mkdir()
    (wf / "data-question.yaml").write_text("name: data-question\n")
    return Ctx(MemoryStore(mem), Ladder(now=lambda: NOON), Approvals(), ["data-question", "bug-fix-review", "org-team"],
               ["data-steward"], gate=gate_of(), writer=FakeWriter(), workflows_dir=wf, now=lambda: NOON)


def msg(body, sender="ravi", room="R", addressed=False, is_agent=False, room_name="product"):
    return Msg(room, room_name, 1, f"u-{sender}", sender, body, None, addressed, is_agent, "2026-11-07T11:00:00+05:30")


def test_ignores_own_messages(ctx):
    assert handle(msg("I opened a work room", sender="chief", addressed=True), ctx) == []
    assert ctx.gate.calls == []


def test_ignores_unaddressed_agent_posts(ctx):
    assert handle(msg("refunds were 4.1%", sender="mochi-data", is_agent=True), ctx) == []
    assert ctx.gate.calls == []


def test_data_question_opens_work_room_targets_data_steward(ctx):
    ctx.gate = gate_of(template="data-question", fits=0.9)
    acts = handle(msg("where's the refunds data and what did it do last month?"), ctx)
    assert [k for k, _ in acts] == ["open_work_room"]
    c = FakeClient()
    apply(acts, c, ctx)
    assert c.log[0] == ("room_from_yaml", "name: data-question\n",
                        {"brief": "brief for data-question from ravi", "asker": "ravi"})
    assert c.log[1] == ("target", "W1", "brief for data-question from ravi", None, ["data-steward"])
    assert ctx.work_rooms == {"W1": "ravi"}


def test_memory_hit_answers_directly_no_work_room(ctx):
    ctx.store.add("team", "team", "fact", "refunds data lives in the orders_refunds table", {"by": "person"})
    ctx.gate = gate_of(template="data-question", fits=0.9)
    ctx.writer = FakeWriter(answer="it's in orders_refunds")
    acts = handle(msg("where's the refunds data?"), ctx)
    assert acts == [("post", {"room_id": "R", "body": "styled: it's in orders_refunds", "thread_id": None})]
    assert "brief" not in ctx.writer.calls


def test_non_urgent_question_to_meera_in_focus_is_held(ctx):
    acts = handle(msg("meera can you look at the refund rounding bug?"), ctx)
    assert [k for k, _ in acts] == ["hold"] and acts[0][1]["for"] == "meera"
    assert ctx.gate.calls == [("ravi", True)]
    c = FakeClient()
    apply(acts, c, ctx)
    assert c.log == []  # nothing posted while she's in focus
    ctx.ladder._now = lambda: NOON.replace(hour=16)
    release_held(c, ctx)
    assert c.log == [("target", "R", "ravi in product: meera can you look at the refund rounding bug?", ["meera"], None)]


def test_urgent_ping_to_meera_in_focus_is_not_held(ctx):
    ctx.gate = gate_of(urgency=1.0)
    assert [k for k, _ in handle(msg("meera checkout is down"), ctx)] == ["none"]  # act, no memory, no workflow


def test_merge_request_creates_approval_for_meera(ctx):
    ctx.work_rooms["W1"] = "meera"
    acts = handle(msg("looks good, ship it", sender="ravi", room="W1", room_name="work-bug-fix-review"), ctx)
    assert [k for k, _ in acts] == ["approval"]
    assert (acts[0][1]["action"], acts[0][1]["owner"]) == ("merge", "meera")
    c = FakeClient()
    apply(acts, c, ctx)
    code = c.log[0][2].split("approve ")[1][:4]
    assert "no pending" in handle(msg(f"approve {code}", sender="ravi", room="W1"), ctx)[0][1]["body"]
    out = handle(msg(f"approve {code}", sender="meera", room="W1"), ctx)
    assert out == [("target", {"room_id": "W1", "body": "merge approved by meera",
                               "roles": ["intern-meera"], "thread_id": None})]


def test_create_agent_always_asks_then_calls_factory(ctx):
    ctx.gate = gate_of(template="org-team", fits=0.9)
    acts = handle(msg("make us a support team", addressed=True), ctx)
    assert [k for k, _ in acts] == ["approval"]
    c = FakeClient()
    apply(acts, c, ctx)
    assert acts[0][1]["owner"] == "asha"  # org admin, not the requester
    code = c.log[0][2].split("approve ")[1][:4]
    assert "no pending" in handle(msg(f"approve {code}"), ctx)[0][1]["body"]  # ravi asked, can't approve
    assert handle(msg(f"approve {code}", sender="asha"), ctx)[0][1]["body"] == "factory not installed"


def _code(acts, ctx):
    c = FakeClient()
    apply(acts, c, ctx)
    return c.log[0][2].split("approve ")[1][:4]


def test_agent_cannot_self_approve_create_agent(ctx):
    ctx.gate = gate_of(template="org-team", fits=0.9)
    agent = dict(sender="mochi-data", addressed=True, is_agent=True)
    acts = handle(msg("make us a support team", **agent), ctx)
    assert [k for k, _ in acts] == ["post"] and "teammate" in acts[0][1]["body"]
    code = _code(handle(msg("make us a support team", addressed=True), ctx), ctx)  # ravi asks; asha owns
    ctx.org_admin = "mochi-data"  # even if it somehow owned one, an agent can't approve
    code2 = _code(handle(msg("make us a support team", addressed=True), ctx), ctx)
    for c in (code, code2):
        assert handle(msg(f"approve {c}", **agent), ctx)[0][1]["body"] == "only a teammate can approve or deny"


def test_unknown_sender_cannot_create_approval(ctx):
    ctx.gate = gate_of(template="org-team", fits=0.9)
    acts = handle(msg("make us a support team", sender="stranger", addressed=True), ctx)
    assert [k for k, _ in acts] == ["post"] and "teammate" in acts[0][1]["body"]
    ctx.work_rooms["W1"] = "meera"
    assert [k for k, _ in handle(msg("ship it", sender="stranger", room="W1", addressed=True), ctx)] == ["post"]
    assert "only a teammate" in handle(msg("approve ABCD", sender="stranger"), ctx)[0][1]["body"]


def test_deploy_approval_owned_by_release_owner(ctx):
    ctx.gate = gate_of(template="release-train", fits=0.9)
    acts = handle(msg("deploy the refunds fix", addressed=True), ctx)
    assert [k for k, _ in acts] == ["approval"]
    assert (acts[0][1]["action"], acts[0][1]["owner"]) == ("deploy", "meera")


def test_tick_from_person_ignored(ctx):
    seen = []
    ctx.on_tick = lambda kind, c: seen.append(kind) or []
    assert handle(msg("[tick] dream"), ctx) == []
    assert handle(msg("[tick] dream", sender="mochi-data", is_agent=True), ctx) == []
    assert handle(msg("[tick] dream", sender="mochi-data", is_agent=True, addressed=True), ctx) == []
    handle(msg("!tick dream", sender="stranger"), ctx)
    assert seen == [] and ctx.gate.calls == []


def test_unaddressed_act_respects_quiet_and_cap(ctx):
    ctx.store.add("team", "team", "fact", "refunds data lives in the orders_refunds table", {"by": "person"})
    ctx.writer = FakeWriter(answer="it's in orders_refunds")
    ctx.ladder.quiet("R", True)
    assert handle(msg("where's the refunds data?"), ctx) == [("none", {})]
    ctx.ladder.quiet("R", False)
    kinds = [handle(msg("where's the refunds data?"), ctx)[0][0] for _ in range(4)]
    assert kinds == ["post", "post", "post", "none"]
    assert handle(msg("where's the refunds data?", addressed=True), ctx)[0][0] == "post"  # asked directly: no cap


def test_quiet_room_offer_suppressed(ctx):
    ctx.gate = gate_of(rung="offer")
    ctx.ladder.quiet("R", True)
    assert handle(msg("anyone know why refunds spiked?"), ctx) == [("none", {})]
    assert "offer_line" not in ctx.writer.calls
    ctx.ladder.quiet("R", False)
    assert handle(msg("anyone know why refunds spiked?"), ctx)[0][1]["body"] == "want me to take this?"


def test_unknown_sender_capped_at_offer(ctx):
    acts = handle(msg("where's the refunds data?", sender="stranger"), ctx)
    assert acts[0][1]["body"] == "want me to take this?"


def test_after_gap_reads_context_before_acting(ctx):
    ctx.gate = gate_of(rung="offer")
    c = FakeClient()
    c.gap_rooms = {"R"}
    step(msg("anyone know why refunds spiked?"), c, ctx)
    assert [e[0] for e in c.log] == ["context", "post"]
    assert c.gap_rooms == set() and "R" in ctx.recent


def test_writer_error_means_no_action(ctx):
    def boom(*a):
        raise RuntimeError("anthropic down")
    ctx.gate = gate_of(rung="offer")
    ctx.writer.offer_line = boom
    assert handle(msg("anyone?"), ctx) == []


def test_ticks_and_tick_command_go_to_hook(ctx):
    seen = []
    ctx.on_tick = lambda kind, c: seen.append(kind) or []
    handle(msg("[tick] morning", sender="chief"), ctx)
    handle(msg("!tick dream"), ctx)
    assert seen == ["morning", "dream"]


def test_remember_and_forget(ctx):
    c = FakeClient()
    apply(handle(msg("chief remember ravi prefers bullet summaries"), ctx), c, ctx)
    assert [i.text for i in ctx.store.items("ravi")] == ["ravi prefers bullet summaries"]
    apply(handle(msg("forget bullet summaries"), ctx), c, ctx)
    assert ctx.store.items("ravi") == []
    assert [e[2] for e in c.log] == ["remembered", "forgotten"]
