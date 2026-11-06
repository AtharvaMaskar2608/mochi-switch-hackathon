"""Timer ticks: Chief posts `[tick] <kind>` into #chief-ops (a visible log), then runs on_tick in-process."""
import dataclasses
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
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


def load_last(path: Path | None) -> dict[str, datetime]:
    """Fired times saved by a previous run, so a restart never re-fires a slot already done."""
    try:
        return {k: datetime.fromisoformat(v) for k, v in json.loads(path.read_text()).items()}
    except Exception:  # no file yet, or unreadable: start fresh
        return {}


def save_last(path: Path, last: dict[str, datetime]) -> None:
    path.touch(mode=0o600)
    os.chmod(path, 0o600)
    path.write_text(json.dumps({k: v.isoformat() for k, v in last.items()}))


def tick_loop(client, chief_ops_room_id: str, now=lambda: datetime.now(IST), sleep=time.sleep,
              every: float = 30, stop=lambda: False, on_fire=lambda kind: None, state_path: Path | None = None) -> None:
    last = load_last(state_path) if state_path else {}
    while not stop():
        t = now()
        for kind in due(t, last):
            try:
                client.post(chief_ops_room_id, f"[tick] {kind}")
                last[kind] = t
            except Exception as e:  # a network blip must not kill the timer; retried next round
                print(f"tick {kind}: {type(e).__name__}: {e}", file=sys.stderr)
                continue
            try:
                on_fire(kind)
            except Exception as e:
                print(f"tick {kind}: {type(e).__name__}: {e}", file=sys.stderr)
            try:
                if state_path:
                    save_last(state_path, last)
            except OSError as e:  # unsaved: a restart may re-fire this slot, nothing worse
                print(f"tick state: {type(e).__name__}: {e}", file=sys.stderr)
        sleep(every)


def held_actions(released: dict[str, list[dict]], profiles: dict, writer, ladder) -> list:
    """One batched brief per (person, room): a held item only ever goes back to the room it came from.
    A brief that fails puts its group back on hold, so nothing is lost and the other groups still go."""
    out = []
    for slug, items in released.items():
        by_room: dict[str, list[dict]] = {}
        for i in items:
            by_room.setdefault(i["room_id"], []).append(i)
        for room_id, group in by_room.items():
            try:
                body = writer.batched_brief([i["text"] for i in group], profiles[slug])
            except Exception as e:
                print(f"held brief for {slug}: {type(e).__name__}: {e}", file=sys.stderr)
                for i in group:
                    ladder.hold(slug, i)
                continue
            out.append(("target", {"room_id": room_id, "names": [slug], "roles": None, "thread_id": None,
                                   "body": body}))
    return out


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
        return held_actions(ctx.ladder.release_due(profiles), profiles, ctx.writer, ctx.ladder)
    return []


def apply_dream(action: dict, client, ctx) -> None:
    from chief.memory.dreamer import run_nightly  # lazy: pulls in the model client

    note = run_nightly(client, ctx.store, action["rooms"])
    _notes["dream"] = ctx.writer.sanitize(note)
    client.post(action["room_id"], _notes["dream"])
