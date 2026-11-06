"""Timer ticks: Chief posts `[tick] <kind>` into #chief-ops, then reacts to its own post via on_tick."""
import dataclasses
import time
from datetime import datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
SCHEDULE = {"morning": "09:00", "evening": "18:00", "friday-digest": "Fri 16:00", "dream": "02:00"}
DREAM_ROOMS = ["chief-ops", "product", "leadership", "eng"]
_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_notes: dict[str, str] = {}  # last dream note, posted by the morning tick
# ponytail: in-memory; a restart between dream and morning posts "no dream note yet"


def _slot(spec: str, now: datetime) -> datetime | None:
    """Today's scheduled instant for spec, or None if today is not its day."""
    day, _, hm = spec.rpartition(" ")
    if day and _DAYS[now.weekday()] != day:
        return None
    h, m = map(int, hm.split(":"))
    return now.replace(hour=h, minute=m, second=0, microsecond=0)


def due(now: datetime, last: dict[str, datetime]) -> list[str]:
    """Kinds whose slot passed today (IST) and that have not fired since. Missed days are not replayed."""
    now = now.astimezone(IST)
    out = []
    for kind, spec in SCHEDULE.items():
        slot = _slot(spec, now)
        if slot and now >= slot and not (kind in last and last[kind] >= slot):
            out.append(kind)
    return out


def tick_loop(client, chief_ops_room_id: str, now=lambda: datetime.now(IST), sleep=time.sleep,
              every: float = 30, stop=lambda: False) -> None:
    last: dict[str, datetime] = {}
    while not stop():
        t = now()
        for kind in due(t, last):
            client.post(chief_ops_room_id, f"[tick] {kind}")
            last[kind] = t
        sleep(every)


def _room(ctx, slug: str) -> str:
    return getattr(ctx, "room_ids", {}).get(slug, slug)  # slug -> room id, if the wiring knows it


def on_tick(kind: str, ctx) -> list:
    ops = _room(ctx, "chief-ops")
    if kind == "dream":
        return [("dream", {"rooms": [_room(ctx, s) for s in DREAM_ROOMS], "room_id": ops})]
    if kind == "morning":
        return [("post", {"room_id": ops, "body": _notes.get("dream", "no dream note yet"), "thread_id": None})]
    if kind == "friday-digest":
        return [("open_work_room", {
            "template": "shipped-digest",
            "brief": "Write this week's shipped digest from merged PRs and releases of the last 7 days.",
            "asker": ctx.org_admin, "lead_role": "release-scribe", "room_id": ops, "thread_id": None})]
    if kind == "evening":  # release every hold, focus or not (mirrors main.release_held)
        root = ctx.store.root / "people"
        people = [ctx.store.profile(p.name) for p in sorted(root.glob("*")) if p.is_dir()] if root.exists() else []
        profiles = {p.slug: dataclasses.replace(p, focus=[]) for p in people}
        return [("target", {"room_id": ctx.hold_rooms[slug], "names": [slug], "roles": None, "thread_id": None,
                            "body": ctx.writer.batched_brief(items, profiles[slug])})
                for slug, items in ctx.ladder.release_due(profiles).items() if slug in ctx.hold_rooms]
    return []


def apply_dream(action: dict, client, ctx) -> None:
    from chief.memory.dreamer import run_nightly  # lazy: pulls in the model client

    note = run_nightly(client, ctx.store, action["rooms"])
    _notes["dream"] = ctx.writer.sanitize(note)
    client.post(action["room_id"], _notes["dream"])
