from dataclasses import replace
from datetime import datetime, timedelta
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
    def __init__(self, answer=None, split=None):
        self.answer, self.calls, self.split = answer, [], split

    def split_requests(self, text):
        return self.split or [text]

    def sanitize(self, t):
        return t.replace("@", "＠")

    def brief(self, msg, sender, hits, template):
        self.calls.append("brief")
        return f"brief for {template} from {sender.slug}"

    def restyle(self, answer, reader):
        self.calls.append(("restyle", reader.slug))
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
    assert [k for k, _ in acts] == ["hold", "post"] and acts[0][1]["for"] == "meera"
    assert ctx.gate.calls == [("ravi", True)]
    c = FakeClient()
    apply(acts, c, ctx)
    assert c.log == [("post", "R", "Meera is heads-down until 15:00; I'll pass this on.")]  # nothing to meera
    c.log.clear()
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


def test_approved_deploy_opens_release_train_as_the_requester(ctx):
    ctx.gate = gate_of(template="release-train", fits=0.9)
    code = _code(handle(msg("deploy the refunds fix", addressed=True), ctx), ctx)  # ravi asks
    (kind, a), = handle(msg(f"approve {code}", sender="meera"), ctx)  # meera owns the approval
    assert kind == "open_work_room" and a["asker"] == "ravi" and a["brief"] == "brief for release-train from ravi"
    (ctx.workflows_dir / "release-train.yaml").write_text("name: release-train\n")
    c = FakeClient()
    apply([(kind, a)], c, ctx)
    assert c.log[0][2]["asker"] == "ravi" and ctx.origins["W1"]["asker"] == "ravi"  # ravi is in the room


def test_within_the_hour_is_not_held_but_lower_urgency_is(ctx):
    ctx.gate = gate_of(urgency=2 / 3)  # "within the hour"
    assert [k for k, _ in handle(msg("meera checkout is slow"), ctx)] == ["none"]
    ctx.gate = gate_of(urgency=0.59)
    assert [k for k, _ in handle(msg("meera checkout is slow"), ctx)] == ["hold", "post"]


@pytest.mark.parametrize("body", ["chief, where is the refunds data?", "Hey Chief where is it", "hey, chief!",
                                  "  CHIEF: status?", "Chief"])
def test_spoken_chief_counts_as_addressed(body):
    from chief.main import spoken_to
    assert spoken_to(body)


@pytest.mark.parametrize("body", ["mischief is afoot", "the chief said no", "chiefly refunds", "hey there chief",
                                  "chief-ops is noisy today", "chief's brief was late"])
def test_chief_in_passing_is_not_addressed(body):
    from chief.main import spoken_to
    assert not spoken_to(body)


def test_handle_treats_spoken_chief_as_addressed(ctx):
    ctx.gate = gate_of(for_me=0.1, rung="offer", template="data-question", fits=0.9)
    acts = handle(msg("Hey chief, where is the refunds data?"), ctx)
    assert [k for k, _ in acts] == ["post"] and ctx.offers  # addressed: offered past for_me, offer kept for a yes
    assert handle(msg("the chief said refunds are fine"), ctx) == [("none", {})]  # ambient, for_me low


def test_intern_merges_on_exactly_the_phrase_chief_posts(ctx):
    from pathlib import Path
    ctx.work_rooms["W1"] = "dev"
    _profile(ctx.store.root, "dev", "Dev", "dev")
    code = _code(handle(msg("ship it", sender="dev", room="W1"), ctx), ctx)
    (_, a), = handle(msg(f"approve {code}", sender="dev", room="W1"), ctx)
    assert a["body"] == "merge approved by dev"
    for slug in ("meera", "dev"):
        doc = (Path(__file__).parent.parent / "agents" / f"intern-{slug}" / "CLAUDE.md").read_text()
        assert f"Merge only when Chief posts `merge approved by {slug}`" in doc and "Never merge" not in doc


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
    handle(msg("[tick] morning", sender="chief"), ctx)  # Chief's own post: never acted on (tick_loop runs it)
    handle(msg("!tick dream"), ctx)
    assert seen == ["dream"]


def test_remember_and_forget(ctx):
    c = FakeClient()
    apply(handle(msg("chief remember ravi prefers bullet summaries"), ctx), c, ctx)
    assert [i.text for i in ctx.store.items("ravi")] == ["ravi prefers bullet summaries"]
    apply(handle(msg("forget bullet summaries"), ctx), c, ctx)
    assert ctx.store.items("ravi") == []
    assert [e[2] for e in c.log] == ["remembered", "forgotten"]


def test_held_items_delivered_to_their_own_room(ctx):
    ctx.ladder.hold("meera", {"text": "asha: board deck?", "room_id": "LEAD", "thread_id": None})
    ctx.ladder.hold("meera", {"text": "dev: flaky test?", "room_id": "ENG", "thread_id": None})
    ctx.ladder.hold("meera", {"text": "dev: and the lint?", "room_id": "ENG", "thread_id": None})
    ctx.ladder._now = lambda: NOON.replace(hour=16)
    c = FakeClient()
    release_held(c, ctx)
    assert sorted(c.log) == [("target", "ENG", "dev: flaky test? | dev: and the lint?", ["meera"], None),
                             ("target", "LEAD", "asha: board deck?", ["meera"], None)]


def test_yes_after_offer_runs_the_offered_template(ctx):
    ctx.gate = gate_of(rung="offer", template="data-question", fits=0.9)
    acts = handle(msg("@chief where is the refunds data?", addressed=True), ctx)
    assert acts == [("post", {"room_id": "R", "body": "want me to take this?", "thread_id": None})]
    ctx.gate = gate_of(rung="silent")  # the yes is not re-gated
    acts = handle(msg("@chief yes", addressed=True), ctx)
    assert [k for k, _ in acts] == ["open_work_room"] and acts[0][1]["template"] == "data-question"
    assert handle(msg("@chief yes", addressed=True), ctx) == [("none", {})]  # an offer runs once


def test_yes_from_someone_else_or_unaddressed_does_not_run_offer(ctx):
    ctx.gate = gate_of(rung="offer", template="data-question", fits=0.9)
    handle(msg("@chief where is the refunds data?", addressed=True), ctx)
    ctx.gate = gate_of(rung="silent")
    assert handle(msg("@chief yes", sender="asha", addressed=True), ctx) == [("none", {})]
    assert handle(msg("yes"), ctx) == [("none", {})]
    assert [k for k, _ in handle(msg("@chief go ahead", addressed=True), ctx)] == ["open_work_room"]


def test_yes_settles_an_unsure_template_but_never_a_hard_ask_first_one(ctx):
    ctx.gate = gate_of(rung="offer", template="data-question", fits=0.5)
    handle(msg("@chief refunds have felt high lately", addressed=True), ctx)
    assert ctx.offers[("R", None, "ravi")]["template"] == "data-question"
    assert [k for k, _ in handle(msg("@chief yes please", addressed=True), ctx)] == ["open_work_room"]
    ctx.gate = gate_of(rung="offer", template="release-train", fits=0.5)
    handle(msg("@chief maybe we should ship the refunds fix", addressed=True), ctx)
    assert ctx.offers[("R", None, "ravi")]["template"] is None
    assert handle(msg("@chief yes", addressed=True), ctx) == [("none", {})]


def test_yes_phrasings():
    from chief.main import YES_RE

    for ok in ("yes", "@chief yes please", "@chief yes, go ahead", "Sure, thanks!", "go ahead."):
        assert YES_RE.fullmatch(ok), ok
    for no in ("yes but only for march", "no", "@chief what about yesterday"):
        assert not YES_RE.fullmatch(no), no


def test_profile_slug_sanitized():
    from chief.main import profile_slug

    assert profile_slug("Ravi") == "ravi"
    for bad in ("../etc", "ravi/../../x", "", "1abc", "a" * 40, "ra vi"):
        assert profile_slug(bad) == "unknown", bad


def test_path_traversal_sender_gets_unknown_profile(ctx):
    seen = []
    act = gate_of(rung="act")
    ctx.gate = lambda m, sender, *a: (seen.append(sender), act(m, sender, *a))[1]
    handle(msg("help", sender="../etc", addressed=True), ctx)
    assert seen[0].slug == "unknown" and seen[0].role == "unknown"


def test_data_agent_passed_when_workflow_declares_it(ctx):
    (ctx.workflows_dir / "data-question.yaml").write_text("params: {data_agent: {type: agent}}\n")
    ctx.data_agent = "standin-data"
    c = FakeClient()
    apply([("open_work_room", {"template": "data-question", "brief": "b", "asker": "ravi",
                               "lead_role": "data-steward", "room_id": "R", "thread_id": None})], c, ctx)
    assert c.log[0][2] == {"brief": "b", "asker": "ravi", "data_agent": "standin-data"}
    assert c.log[1] == ("target", "W1", "b", ["standin-data"], None)  # by name: nobody holds the role yet
    assert c.log[2] == ("post", "R", "opened work-data-question-20261107-1100 for this")


def test_work_room_that_fails_to_open_is_reported_in_thread(ctx):
    (ctx.workflows_dir / "shipped-digest.yaml").write_text("name: x\n")
    c = FakeClient()

    def fail(yaml, inputs):
        raise RuntimeError("400")
    c.room_from_yaml = fail
    apply([("open_work_room", {"template": "shipped-digest", "brief": "b", "asker": "asha", "lead_role": "x",
                               "room_id": "R", "thread_id": "M1"})], c, ctx)
    assert c.log == [("post", "R", "couldn't open a shipped-digest room (Switch said RuntimeError); "
                                   "its agents may not be running")]


def test_dream_action_routed_to_ticks(ctx, monkeypatch):
    from chief import ticks

    got = []
    monkeypatch.setattr(ticks, "apply_dream", lambda a, client, ctx: got.append(a))
    apply([("dream", {"rooms": ["R"], "room_id": "OPS"})], FakeClient(), ctx)
    assert got == [{"rooms": ["R"], "room_id": "OPS"}]


# ---- Task J: story gaps ----

def _open_room(ctx, template="data-question", asker="ravi", thread="M1"):
    (ctx.workflows_dir / f"{template}.yaml").write_text(f"name: {template}\n")
    apply([("open_work_room", {"template": template, "brief": "b", "asker": asker, "lead_role": "x",
                               "room_id": "R", "thread_id": thread})], FakeClient(), ctx)


def agent(body, sender="mochi-data", room="W1", addressed=False):
    return msg(body, sender=sender, room=room, addressed=addressed, is_agent=True)


def test_done_in_work_room_restyled_to_origin_thread(ctx):
    ctx.gate = gate_of(template="data-question", fits=0.9)
    acts = handle(replace(msg("where's the refunds data?"), message_id="M1"), ctx)
    assert acts[0][1]["thread_id"] == "M1"  # the ask's own thread
    apply(acts, FakeClient(), ctx)
    assert ctx.origins["W1"]["room_id"] == "R" and ctx.origins["W1"]["thread_id"] == "M1"
    assert handle(agent("refunds were 4.1% in October"), ctx) == []  # not DONE, not long: just noted
    acts = handle(agent("DONE"), ctx)  # bare DONE: the thread's last agent reply is the answer
    assert acts == [("post", {"room_id": "R", "body": "styled: refunds were 4.1% in October", "thread_id": "M1"})]
    assert ("restyle", "ravi") in ctx.writer.calls  # in the asker's style
    assert handle(agent("DONE"), ctx) == []  # answered once
    assert handle(agent("DONE: mine", sender="reviewer"), ctx) == []  # only the lead's DONE counts
    assert handle(agent("short note\nDONE"), ctx) == []  # DONE must start the message
    acts = handle(agent("DONE: and 3.2% in September"), ctx)  # DONE with its own text
    assert acts[0][1]["body"] == "styled: and 3.2% in September"


def test_lead_agent_table_without_done_or_address_is_the_answer(ctx):
    ctx.data_agent = "standin-data"
    _open_room(ctx)
    table = "| month | refunds |\n|---|---|\n| Oct | 4.1% |"
    assert handle(agent(table, sender="reviewer"), ctx) == []  # not the room's lead
    acts = handle(agent(table, sender="Standin data"), ctx)  # display name of the lead
    assert acts == [("post", {"room_id": "R", "body": f"styled: {table}", "thread_id": "M1"})]
    assert handle(agent(table, sender="standin-data", room="R"), ctx) == []  # agents elsewhere: still ignored
    assert ctx.gate.calls == []


def test_three_requests_open_three_work_rooms(ctx):
    parts = ["what did we ship this week", "why did refunds jump", "fix the checkout bug"]
    tpl = dict(zip(parts, ["shipped-digest", "data-question", "bug-fix-review"]))
    ctx.gate = lambda m, *a: Decision(for_me=0.9, rung="act", template=tpl[m.body], fits=0.9, urgency=0.3)
    ctx.writer = FakeWriter(split=parts)
    body = "what did we ship this week, why did refunds jump, fix the checkout bug"
    acts = handle(msg(body, sender="asha", addressed=True), ctx)
    assert [k for k, _ in acts] == ["post", "open_work_room", "open_work_room", "open_work_room"]
    assert acts[0][1]["body"] == "On it, 3 jobs: " + "; ".join(parts)
    assert [a["template"] for _, a in acts[1:]] == ["shipped-digest", "data-question", "bug-fix-review"]


def test_one_question_split_in_two_opens_one_room_and_summary_stays_in_thread(ctx):
    parts = ["where is the refunds data", "what did it do last month"]
    ctx.gate = gate_of(rung="act", template="data-question", fits=0.9)
    ctx.writer = FakeWriter(split=parts)
    m = replace(msg("@chief where is the refunds data and what did it do last month?", addressed=True),
                message_id="M9")
    acts = handle(m, ctx)
    assert [k for k, _ in acts] == ["open_work_room"]  # same template twice: one job, not two rooms
    tpl = dict(zip(parts, ["shipped-digest", "data-question"]))
    ctx.gate = lambda m, *a: Decision(for_me=0.9, rung="act", template=tpl[m.body], fits=0.9, urgency=0.3)
    acts = handle(m, ctx)
    assert acts[0][0] == "post" and acts[0][1]["thread_id"] == "M9"  # "On it" goes in the asker's thread


def test_unaddressed_message_with_and_is_not_split(ctx):
    ctx.writer = FakeWriter(split=["lunch at 1", "bring the deck"])
    ctx.writer.split_requests = lambda text: pytest.fail("ambient chat must not be split")
    ctx.gate = gate_of(for_me=0.1, rung="silent")
    assert handle(msg("ravi, lunch at 1? and bring the deck", sender="asha"), ctx) == [("none", {})]
    assert len(ctx.gate.calls) == 1  # one gate call per ambient message


def test_summary_only_when_two_parts_act(ctx):
    parts = ["why did refunds jump", "thanks for the deck", "also hi"]
    tpl = {"why did refunds jump": "data-question"}
    body = "why did refunds jump, thanks for the deck, also hi"

    def gate(m, *a):
        return Decision(for_me=0.9, rung="act" if m.body in tpl else "silent", template=tpl.get(m.body),
                        fits=0.9, urgency=0.3)
    ctx.gate = gate
    ctx.writer = FakeWriter(split=parts)
    acts = handle(msg(body, addressed=True), ctx)
    assert [k for k, _ in acts] == ["open_work_room"]  # one part acts: just its output, no summary
    tpl["thanks for the deck"] = "bug-fix-review"
    acts = handle(msg(body, addressed=True), ctx)
    assert [k for k, _ in acts] == ["post", "open_work_room", "open_work_room"]
    assert acts[0][1]["body"] == "On it, 2 jobs: why did refunds jump; thanks for the deck"
    tpl.clear()
    assert handle(msg(body, addressed=True), ctx) == [("none", {})]  # none act: nothing extra


def test_dev_asks_meera_in_focus_held_without_addressing_chief(ctx):
    _profile(ctx.store.root, "dev", "Dev", "dev")
    ctx.store.add("team", "team", "fact", "the refund rounding bug is tracked in BUG-12", {"by": "person"})
    ctx.writer = FakeWriter(answer="it's tracked in BUG-12")
    ctx.gate = gate_of(for_me=0.1, rung="silent", urgency=0.3)  # chat between people, can wait
    m = replace(msg("meera is the refund rounding bug fixed?", sender="dev", room="ENG"), message_id="M7")
    acts = handle(m, ctx)
    assert acts[0] == ("hold", {"for": "meera", "item": {
        "text": "dev in product: meera is the refund rounding bug fixed?", "room_id": "ENG", "thread_id": None}})
    assert acts[1] == ("post", {"room_id": "ENG", "thread_id": "M7", "body":
                                "Meera is heads-down until 15:00; I'll pass this on. Meanwhile: it's tracked in BUG-12"})
    kinds = [[k for k, _ in handle(m, ctx)] for _ in range(3)]
    assert kinds == [["hold", "post"], ["hold", "post"], ["hold"]]  # the reply is unprompted: capped; the hold is not
    ctx.gate = gate_of(for_me=0.1, rung="silent", urgency=1.0)
    assert handle(replace(m, room_id="ENG2"), ctx) == [("none", {})]  # urgent: not held


def test_release_train_dry_run_after_release_owner_approves(ctx):
    _open_room(ctx, "release-train")
    assert ctx.origins["W1"]["lead"] is None  # ends in a dry run, not an answer
    assert handle(agent("PASSED: no high findings", sender="security"), ctx) == []
    notes = "@chief release notes for v1.4.2: refunds rounding fix. Touches 3 services and 1 migration."
    acts = handle(agent(notes, sender="ship", addressed=True), ctx)
    assert [k for k, _ in acts] == ["approval", "post"]
    assert (acts[0][1]["action"], acts[0][1]["owner"]) == ("deploy", "meera")
    code = _code(acts, ctx)
    assert "no pending" in handle(msg(f"approve {code}", sender="ravi"), ctx)[0][1]["body"]
    out = handle(msg(f"approve {code}", sender="meera"), ctx)
    line = "would deploy v1.4.2 to staging: 3 services, 1 migration"
    assert out == [("post", {"room_id": "W1", "body": line, "thread_id": None}),
                   ("post", {"room_id": "R", "body": line, "thread_id": "M1"})]  # posts only: nothing runs


def test_release_owner_notified_in_origin_room(ctx):
    _open_room(ctx, "release-train")
    acts = handle(agent("@chief release notes for v1.4.2", sender="ship", addressed=True), ctx)
    c = FakeClient()
    apply(acts, c, ctx)
    (kind, room, body, names, roles), note = c.log
    assert (kind, room, names) == ("target", "R", ["meera"]) and "approve " in body  # origin room, targeted
    assert note == ("post", "W1", "asked meera to approve the deploy in the requesting thread")


def test_dry_run_line_fallbacks():
    from chief.main import dry_run_line

    assert dry_run_line("notes with nothing countable") == "would deploy vNEXT to staging: ? services, ? migrations"


def test_dry_run_line_reads_release_scribe_lines_and_singularizes():
    from chief.main import dry_run_line

    notes = "Release v1.4.2\n- refunds rounding fix (#12)\nservices: 3\nmigrations: 1"
    assert dry_run_line(notes) == "would deploy v1.4.2 to staging: 3 services, 1 migration"
    assert dry_run_line("v2.0.0\nServices: 1\nMigrations: 0") == "would deploy v2.0.0 to staging: 1 service, 0 migrations"
    assert dry_run_line("v2.0.1 touches 2 services and 2 migrations") == \
        "would deploy v2.0.1 to staging: 2 services, 2 migrations"


def test_release_scribe_is_told_to_write_the_counted_lines():
    from pathlib import Path

    root = Path(__file__).parent.parent
    for f in ("agents/ship/CLAUDE.md", "workflows/release-train.yaml"):
        text = (root / f).read_text()
        assert "services: N" in text or "services: <N>" in text, f
        assert "migrations: M" in text or "migrations: <M>" in text, f


def test_non_hard_ask_first_becomes_offer(ctx):
    ctx.gate = gate_of(rung="ask_first")  # a plain answer: not on the hard list
    acts = handle(msg("@chief can you sort this out", addressed=True), ctx)
    assert acts == [("post", {"room_id": "R", "body": "want me to take this?", "thread_id": None})]
    assert ctx.approvals._pending == {}
    ctx.gate = gate_of(rung="act", template="release-train", fits=0.9)  # hard: still an approval
    assert [k for k, _ in handle(msg("@chief deploy it", addressed=True), ctx)] == ["approval"]


def test_offers_expire_after_30_minutes(ctx):
    clock = [NOON]
    ctx.now = lambda: clock[0]
    ctx.gate = gate_of(rung="offer", template="data-question", fits=0.9)
    handle(msg("@chief where is the refunds data?", addressed=True), ctx)
    ctx.gate = gate_of(rung="silent")
    clock[0] = NOON + timedelta(minutes=31)
    assert handle(msg("@chief yes", addressed=True), ctx) == [("none", {})]
    assert ctx.offers == {}


def test_yes_in_thread_matches_offer_on_root_message(ctx):
    ctx.gate = gate_of(rung="offer", template="data-question", fits=0.9)
    acts = handle(replace(msg("@chief where is the refunds data?", addressed=True), message_id="M1"), ctx)
    assert acts[0][1]["thread_id"] == "M1"  # offered in the asker's thread
    assert list(ctx.offers) == [("R", "M1", "ravi")]
    ctx.gate = gate_of(rung="silent")
    assert handle(replace(msg("@chief yes", addressed=True), thread_id="OTHER", message_id="M2"), ctx) == [("none", {})]
    yes = replace(msg("@chief yes", addressed=True), thread_id="M1", message_id="M3")
    assert [k for k, _ in handle(yes, ctx)] == ["open_work_room"]
