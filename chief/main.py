"""Chief main loop: `handle` decides (returns Actions, no network), `apply` executes them on Switch."""
import logging
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import yaml

from chief import commands, factory, ticks, writer
from chief.approvals import Approvals
from chief.gate import decide, decide_with_claude
from chief.ladder import HARD_ASK_FIRST, IST, Ladder, final_rung, in_focus
from chief.memory.store import MemoryStore
from chief.models import Msg, Profile
from chief.switch_client import connect, resolve_rooms

log = logging.getLogger("chief")

Action = tuple[str, dict]  # kind: post|target|open_work_room|approval|hold|remember|forget|none

TEMPLATES = ["brightcart-org", "data-question", "shipped-digest", "bug-fix-review", "release-train", "org-team"]
ROLES = ["data-steward", "release-scribe", "reviewer", "security", "deployer", "on-call", "release-owner",
         "video", "intern-meera", "intern-dev"]
ACTION_FOR_TEMPLATE = {
    "data-question": "mochi_read",
    "shipped-digest": "summarize",
    "bug-fix-review": "open_work_room",
    "release-train": "deploy",
    "org-team": "create_agent",
}
LEAD_ROLE = {
    "data-question": "data-steward",
    "shipped-digest": "release-scribe",
    "bug-fix-review": "intern-{asker}",
    "release-train": "reviewer",
}
LEAD_AGENT = {  # whose long reply or table in the work room is the answer; release-train ends in a dry run instead
    "data-question": "{data_agent}",
    "shipped-digest": "ship",
    "bug-fix-review": "intern-{asker}",
}
FITS_MIN = 0.7
FOR_ME_NO = 0.3
URGENT = 0.67
RELEASE_EVERY_S = 60
MERGE_RE = re.compile(r"\b(merge|ship it)\b", re.I)
DONE_RE = re.compile(r"^DONE\b[ \t:.!-]*", re.M)
TABLE_RE = re.compile(r"^\s*\|.*\|\s*$", re.M)
LONG_ANSWER = 300  # ponytail: length heuristic for "this reply is the answer"; a lead posting chatter this long reposts
OFFER_TTL = timedelta(minutes=30)
# ponytail: only a message with a separator in it pays for a split call
SPLIT_HINT = re.compile(r"[,;?.]\s*\w|\band\b|\balso\b", re.I)


@dataclass
class Ctx:
    store: MemoryStore
    ladder: Ladder
    approvals: Approvals
    templates: list[str]
    roles: list[str]
    self_name: str = "chief"
    release_owner: str = "meera"  # owns deploy approvals
    org_admin: str = "asha"  # owns create_agent approvals
    gate: Callable = decide
    writer: Any = writer
    on_tick: Callable[[str, "Ctx"], list[Action]] = lambda kind, ctx: []  # Task 10
    factory_build: Callable[[str, "Ctx"], str] = lambda description, ctx: "factory not installed"  # Task 17
    workflows_dir: Path = Path("workflows")
    now: Callable[[], datetime] = lambda: datetime.now(IST)
    work_rooms: dict[str, str] = field(default_factory=dict)  # work room id -> asker slug
    recent: dict[str, dict] = field(default_factory=dict)  # room id -> context re-read after a gap
    room_ids: dict[str, str] = field(default_factory=dict)  # room slug ("chief-ops") -> Switch room id
    # (room id, thread root id, slug) -> {"msg", "template", "at"}: the last offer, so a "yes" can run it
    offers: dict[tuple, dict] = field(default_factory=dict)
    # work room id -> {"room_id", "thread_id", "asker", "template", "lead", "last", "sent"}: where its answer goes
    origins: dict[str, dict] = field(default_factory=dict)
    data_agent: str = "mochi-data"  # who answers data-question rooms; standin-data on the judge path


def _post(msg: Msg, body: str) -> Action:
    return ("post", {"room_id": msg.room_id, "body": body, "thread_id": msg.thread_id})


def _root(msg: Msg) -> str | None:
    """The thread this message lives in: its thread, or the thread it would start."""
    return msg.thread_id or msg.message_id


def _norm(name: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())  # "Mochi data" and "mochi-data" are one agent


def _people(ctx: Ctx) -> list[Profile]:
    root = ctx.store.root / "people"
    return [ctx.store.profile(p.name) for p in sorted(root.glob("*")) if p.is_dir()] if root.exists() else []


def _recipient_in_focus(msg: Msg, ctx: Ctx, sender: str) -> Profile | None:
    words = set(re.findall(r"[a-z0-9-]+", msg.body.lower()))
    now = ctx.now()
    for p in _people(ctx):
        if p.slug != sender and (p.slug in words or p.name.lower() in words) and in_focus(p, now):
            return p
    return None


def _command(cmd: commands.Command, msg: Msg, slug: str, ctx: Ctx) -> list[Action]:
    w = ctx.writer
    if cmd.kind == "memory":
        items = ctx.store.items(slug) + ctx.store.items("team")
        lines = [f"- {i.text} ({i.id})" for i in items] or ["(nothing remembered yet)"]
        return [_post(msg, w.sanitize("\n".join(lines)))]
    if cmd.kind in ("quiet_on", "quiet_off"):
        on = cmd.kind == "quiet_on"
        ctx.ladder.quiet(msg.room_id, on)
        return [_post(msg, "ok, quiet here until !quiet off" if on else "ok, back to normal here")]
    person = not msg.is_agent and ctx.store.profile(slug).role != "unknown"
    if cmd.kind == "tick":
        return ctx.on_tick(cmd.arg, ctx) if person else []
    if cmd.kind == "remember":
        return [("remember", {"owner": slug, "text": cmd.arg, "room_name": msg.room_name,
                              "room_id": msg.room_id, "thread_id": msg.thread_id})]
    if cmd.kind == "forget":
        item_id = commands.forget_target(ctx.store, slug, cmd.arg)
        if not item_id:
            return [_post(msg, "I couldn't find that in memory")]
        return [("forget", {"id": item_id, "room_id": msg.room_id, "thread_id": msg.thread_id})]
    # approve / deny: only a known person, never an agent or unlinked account
    if not person:
        return [_post(msg, "only a teammate can approve or deny")]
    payload = ctx.approvals.resolve(cmd.arg, slug, cmd.kind == "approve")
    if payload is None:
        return [_post(msg, f"no pending approval {cmd.arg} for {slug}")]
    if cmd.kind == "deny":
        return [_post(msg, f"ok, dropped {cmd.arg}")]
    return _approved(payload, ctx)


def _approved(payload: dict, ctx: Ctx) -> list[Action]:
    m = Msg(**payload["msg"])
    if "notes" in payload:  # release-train's last step: say what would deploy; nothing runs
        line = dry_run_line(payload["notes"])
        o = ctx.origins.get(m.room_id)
        back = [("post", {"room_id": o["room_id"], "body": line, "thread_id": o["thread_id"]})] if o else []
        return [_post(m, line)] + back
    if payload["action"] == "merge":
        asker = payload["owner"]
        return [("target", {"room_id": m.room_id, "body": f"merge approved by {asker}",
                            "roles": [f"intern-{asker}"], "thread_id": m.thread_id})]
    if payload["action"] == "create_agent":
        return [_post(m, ctx.writer.sanitize(ctx.factory_build(m.body, ctx)))]
    sender = ctx.store.profile(payload["owner"])
    return _act(m, sender, payload["template"], ctx)


def _act(msg: Msg, sender: Profile, template: str | None, ctx: Ctx) -> list[Action]:
    w = ctx.writer
    hits = ctx.store.search(msg.body, [sender.slug, "team"])
    answer = w.answer_from_memory(msg.body, hits, sender) if hits else None
    if answer:
        return [_post(msg, w.restyle(answer, sender))]
    if template not in LEAD_ROLE:
        return [("none", {})]  # nothing to answer from, no workflow to hand it to
    return [("open_work_room", {
        "template": template,
        "brief": w.brief(msg, sender, hits, template),
        "asker": sender.slug,
        "lead_role": LEAD_ROLE[template].format(asker=sender.slug),
        "room_id": msg.room_id,
        "thread_id": _root(msg),  # the answer comes back in this thread
    })]


def _approval(msg: Msg, owner: str, action: str, template: str | None, **extra) -> Action:
    return ("approval", {"action": action, "owner": owner, "template": template, **extra,
                         "msg": msg.__dict__.copy(), "room_id": msg.room_id, "thread_id": msg.thread_id})


def dry_run_line(notes: str) -> str:
    version = re.search(r"\bv\d+(?:\.\d+)+\b", notes)
    services = re.search(r"(\d+)\s+services?\b", notes, re.I)
    migrations = re.search(r"(\d+)\s+migrations?\b", notes, re.I)
    return (f"would deploy {version[0] if version else 'vNEXT'} to staging: "
            f"{services[1] if services else '?'} services, {migrations[1] if migrations else '?'} migrations")


def _work_room_reply(msg: Msg, ctx: Ctx) -> list[Action] | None:
    """An agent's post in a work room Chief opened. None: not for this watch (normal rules apply)."""
    o = ctx.origins[msg.room_id]
    body = msg.body.strip()
    if o["template"] == "release-train":
        if msg.addressed and _norm(msg.sender_name) == "ship":  # release-scribe's notes are in: ask the owner
            notes = "\n".join(filter(None, [o["last"], body]))
            return [_approval(msg, ctx.release_owner, "deploy", "release-train", notes=notes)]
        o["last"] = body
        return None
    done = DONE_RE.search(body)
    text = DONE_RE.sub("", body).strip()
    lead = _norm(msg.sender_name) == _norm(o["lead"])
    if not done and not (lead and (len(body) >= LONG_ANSWER or TABLE_RE.search(body))):
        o["last"] = body or o["last"]
        return None
    answer = text or o["last"]
    o["last"] = answer
    if not answer or answer == o["sent"]:
        return []
    body = ctx.writer.restyle(answer, ctx.store.profile(o["asker"]))  # may raise: then not marked sent
    o["sent"] = answer
    return [("post", {"room_id": o["room_id"], "body": body, "thread_id": o["thread_id"]})]


def handle(msg: Msg, ctx: Ctx) -> list[Action]:
    try:
        return _handle(msg, ctx)
    except Exception as e:  # writer / gate / store errors: drop this message, keep the loop alive
        print(f"handle: {type(e).__name__}: {e}", file=sys.stderr)
        return []


TEAMMATE_MUST_ASK = "a teammate needs to ask for that one"
YES_RE = re.compile(r"(@\S+\s+)*(yes|yeah|yep|sure|ok|okay|go ahead|please do|do it)"
                    r"([\s,]+(yes|sure|ok|go ahead|please|do it|thanks))*[\s.!]*", re.I)
SLUG_RE = re.compile(r"[a-z][a-z0-9-]{0,30}")


def profile_slug(sender_name: str) -> str:
    """Profile folder name for a sender; anything that isn't a plain slug reads as the unknown profile."""
    s = sender_name.lower()
    return s if SLUG_RE.fullmatch(s) else "unknown"
VISIBLE = {"post", "target", "approval", "open_work_room"}


def _handle(msg: Msg, ctx: Ctx) -> list[Action]:
    if ctx.self_name in (msg.sender, msg.sender_name.lower()):
        return []  # never answer itself; timer ticks run in-process (run(): tick_loop on_fire)
    if msg.is_agent and msg.room_id in ctx.origins:  # Chief's own work room: the answer may be in
        acts = _work_room_reply(msg, ctx)
        if acts is not None:
            return acts
    if msg.is_agent and not msg.addressed:
        return []
    if re.fullmatch(r"\[tick\]\s+(\S+)", msg.body.strip()):
        return []  # a [tick] body from anyone else is ignored
    slug = profile_slug(msg.sender_name)
    if cmd := commands.parse(msg.body):
        return _command(cmd, msg, slug, ctx)
    offer = _take_offer(msg, slug, ctx) if msg.addressed else None
    sender = ctx.store.profile(slug)
    if offer and YES_RE.fullmatch(msg.body.strip()) and not msg.is_agent and sender.role != "unknown":
        m = Msg(**offer["msg"])  # the offered request, now accepted: act on it as first decided
        return _act(m, sender, offer["template"], ctx)
    if not msg.addressed and ctx.ladder.is_quiet(msg.room_id):
        return [("none", {})]  # skip gate/writer calls whose output would be dropped anyway
    parts = [msg.body]
    if msg.room_id not in ctx.work_rooms and SPLIT_HINT.search(msg.body):
        parts = ctx.writer.split_requests(msg.body)
    if len(parts) == 1:
        return _capped(_decide(msg, slug, ctx), msg, ctx)
    out = _capped([_post(msg, ctx.writer.sanitize(f"On it, {len(parts)} jobs: " + "; ".join(parts)))], msg, ctx)
    for part in parts:  # each part: its own gate, ladder and cap slot
        out += _capped(_decide(replace(msg, body=part), slug, ctx), msg, ctx)
    return [a for a in out if a[0] != "none"] or [("none", {})]


def _capped(acts: list[Action], msg: Msg, ctx: Ctx) -> list[Action]:
    """Unprompted: a visible reply takes one of the room's 3-per-hour slots; without one, only holds stay."""
    if msg.addressed or not any(k in VISIBLE for k, _ in acts) or ctx.ladder.allow_unprompted(msg.room_id):
        return acts
    return [a for a in acts if a[0] not in VISIBLE] or [("none", {})]


def _take_offer(msg: Msg, slug: str, ctx: Ctx) -> dict | None:
    """The sender's live offer for this thread (a top-level reply takes their newest in the room); spent once taken."""
    now = ctx.now()
    for k in [k for k, o in ctx.offers.items() if now - o["at"] >= OFFER_TTL]:
        del ctx.offers[k]
    if (key := (msg.room_id, _root(msg), slug)) in ctx.offers:
        return ctx.offers.pop(key)
    if msg.thread_id is None:
        mine = [k for k in ctx.offers if k[0] == msg.room_id and k[2] == slug]
        if mine:
            return ctx.offers.pop(max(mine, key=lambda k: ctx.offers[k]["at"]))
    return None


def _focus_hold(msg: Msg, p: Profile, sender: Profile, ctx: Ctx) -> list[Action]:
    """Hold it for the person in focus, and tell the asker in-thread what memory already says."""
    t = ctx.now().astimezone(IST).strftime("%H:%M")
    until = next((b for a, b in p.focus if a <= t < b), "later")
    hits = ctx.store.search(msg.body, [sender.slug, "team"])
    meanwhile = ctx.writer.answer_from_memory(msg.body, hits, sender) if hits else None
    body = f"{p.name} is heads-down until {until}; I'll pass this on." + (f" Meanwhile: {meanwhile}" if meanwhile else "")
    return [("hold", {"for": p.slug, "item": {"text": f"{msg.sender_name} in {msg.room_name}: {msg.body}",
                                              "room_id": msg.room_id, "thread_id": msg.thread_id}}),
            ("post", {"room_id": msg.room_id, "body": ctx.writer.sanitize(body), "thread_id": _root(msg)})]


def _decide(msg: Msg, slug: str, ctx: Ctx) -> list[Action]:
    sender = ctx.store.profile(slug)
    person = not msg.is_agent and sender.role != "unknown"  # only known people may start approvals
    if msg.room_id in ctx.work_rooms and MERGE_RE.search(msg.body):
        if not person:
            return [_post(msg, TEAMMATE_MUST_ASK)]
        return [_approval(msg, ctx.work_rooms[msg.room_id], "merge", "bug-fix-review")]

    focused = _recipient_in_focus(msg, ctx, slug)
    d = ctx.gate(msg, sender, focused is not None, ctx.templates, ctx.roles)
    if focused and d.urgency < URGENT:  # person-to-person too: focus protection needs no for_me
        return _focus_hold(msg, focused, sender, ctx)
    if not msg.addressed and d.for_me <= FOR_ME_NO:
        return [("none", {})]
    template = d.template if d.fits >= FITS_MIN else None
    action = ACTION_FOR_TEMPLATE.get(template, "answer")
    rung = final_rung(d, action, sender)
    if rung == "ask_first" and action not in HARD_ASK_FIRST:
        rung = "offer"  # approvals exist only for the hard list; anything else the asker just confirms
    if rung == "silent":
        return [("none", {})]
    if rung == "ask_first" and not person:
        return [_post(msg, TEAMMATE_MUST_ASK)]  # capped at offer: no self-owned approvals
    if rung == "offer":
        # Not sure it fits? Name the gate's best guess in the offer and let the person's "yes" settle it,
        # unless that guess is on the hard ask-first list (a yes must never skip an approval).
        guess = d.template if ACTION_FOR_TEMPLATE.get(d.template, "answer") not in HARD_ASK_FIRST else None
        offered = template or guess
        if msg.addressed:  # a "yes" from them in this thread runs it (see _handle)
            ctx.offers[(msg.room_id, _root(msg), slug)] = {"msg": msg.__dict__.copy(), "template": offered,
                                                            "at": ctx.now()}
        return [("post", {"room_id": msg.room_id, "body": ctx.writer.offer_line(msg, offered),
                          "thread_id": _root(msg)})]
    if rung == "ask_first":
        owner = {"deploy": ctx.release_owner, "create_agent": ctx.org_admin}.get(action, slug)
        return [_approval(msg, owner, action, template)]
    return _act(msg, sender, template, ctx)


def apply(actions: list[Action], client, ctx: Ctx) -> None:
    for kind, a in actions:
        try:
            _apply_one(kind, a, client, ctx)
        except Exception as e:
            print(f"apply {kind}: {type(e).__name__}: {e}", file=sys.stderr)


def _apply_one(kind: str, a: dict, client, ctx: Ctx) -> None:
    if kind == "post":
        client.post(a["room_id"], a["body"], thread_id=a.get("thread_id"))
    elif kind == "target":
        client.target(a["room_id"], a["body"], names=a.get("names"), roles=a.get("roles"),
                      thread_id=a.get("thread_id"))
    elif kind == "open_work_room":
        yaml_text = (ctx.workflows_dir / f"{a['template']}.yaml").read_text()
        inputs = {"brief": a["brief"], "asker": a["asker"]}
        names = None
        if "data_agent" in (yaml.safe_load(yaml_text).get("params") or {}):
            inputs["data_agent"] = ctx.data_agent
            names = [ctx.data_agent]  # by name: the role has no holder until the agent assumes it at kickoff
        room = client.room_from_yaml(yaml_text, inputs)
        rid = room.get("id") or room.get("room_id")
        ctx.work_rooms[rid] = a["asker"]
        lead = LEAD_AGENT.get(a["template"])
        ctx.origins[rid] = {"room_id": a["room_id"], "thread_id": a.get("thread_id"), "asker": a["asker"],
                            "template": a["template"], "last": None, "sent": None,
                            "lead": lead and lead.format(asker=a["asker"], data_agent=ctx.data_agent)}
        client.target(rid, a["brief"], names=names, roles=None if names else [a["lead_role"]])
        name = room.get("room_name") or room.get("name") or "a work room"
        client.post(a["room_id"], f"opened {name} for this", thread_id=a.get("thread_id"))
    elif kind == "approval":
        code = ctx.approvals.request(a["action"], a["owner"], a)
        client.post(a["room_id"], f"{a['owner']}: this needs your ok ({a['action']}). "
                    f"Reply 'approve {code}' or 'deny {code}'.", thread_id=a.get("thread_id"))
    elif kind == "hold":
        ctx.ladder.hold(a["for"], a["item"])  # the item carries its own room
    elif kind == "dream":
        ticks.apply_dream(a, client, ctx)
    elif kind == "remember":
        ctx.store.add("personal", a["owner"], "fact", a["text"],
                      {"room": a["room_name"], "date": ctx.now().date().isoformat(), "by": "person"})
        client.post(a["room_id"], "remembered", thread_id=a.get("thread_id"))
    elif kind == "forget":
        ctx.store.archive(a["id"])
        client.post(a["room_id"], "forgotten", thread_id=a.get("thread_id"))


def step(msg: Msg, client, ctx: Ctx) -> None:
    """One event: re-read room context first if the stream had a gap there, then decide and act."""
    if msg.room_id in client.gap_rooms:
        ctx.recent[msg.room_id] = client.context(msg.room_id)
        client.gap_rooms.discard(msg.room_id)
    apply(handle(msg, ctx), client, ctx)


def release_held(client, ctx: Ctx) -> None:
    profiles = {p.slug: p for p in _people(ctx)}
    apply(ticks.held_actions(ctx.ladder.release_due(profiles), profiles, ctx.writer, ctx.ladder), client, ctx)


ROOM_SLUGS = ["product", "leadership", "eng", "chief-ops"]


def run() -> None:
    logging.basicConfig(level=os.environ.get("CHIEF_LOG", "INFO"), format="%(levelname)s %(name)s: %(message)s")
    client = connect("chief", "Chief, the team's chief of staff", Path(".chief.json"))
    gate = decide if os.environ.get("TYPESAFE_API_KEY") else decide_with_claude
    if gate is decide_with_claude:
        log.warning("TYPESAFE_API_KEY is not set: gating with Claude (decide_with_claude) instead of Jev")
    else:
        log.info("gating with Jev")
    ctx = Ctx(MemoryStore(Path("memory")), Ladder(), Approvals(), TEMPLATES, ROLES, gate=gate,
              on_tick=ticks.on_tick, data_agent=os.environ.get("CHIEF_DATA_AGENT", "mochi-data"))
    ctx.factory_build = lambda text, ctx: factory.build(text, client, approved=True)
    ctx.room_ids = resolve_rooms(client, ROOM_SLUGS)
    lock = threading.Lock()  # ponytail: one lock for loop + releaser; Ladder is not thread-safe

    def releaser():
        while True:
            time.sleep(RELEASE_EVERY_S)
            with lock:
                release_held(client, ctx)

    threading.Thread(target=releaser, daemon=True).start()
    def fire(kind: str) -> None:  # Switch does not echo Chief's own [tick] post back, so run it here
        with lock:
            apply(ctx.on_tick(kind, ctx), client, ctx)

    if "chief-ops" in ctx.room_ids:
        threading.Thread(target=ticks.tick_loop, args=(client, ctx.room_ids["chief-ops"]),
                         kwargs={"on_fire": fire, "state_path": Path(".chief-ticks.json")}, daemon=True).start()
    else:
        log.warning("no chief-ops room found: timer ticks are off")
    for msg in client.events():
        with lock:
            step(msg, client, ctx)
