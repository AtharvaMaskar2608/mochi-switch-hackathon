"""Trust ladder: code-enforced ceilings on what Chief may do without approval."""
from collections import defaultdict, deque
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from chief.models import Decision, Profile, Rung

IST = ZoneInfo("Asia/Kolkata")
HARD_ASK_FIRST = frozenset({"merge", "deploy", "external_message", "interrupt_focus", "create_agent"})
_ORDER: list[Rung] = ["silent", "offer", "act", "ask_first"]


def final_rung(d: Decision, action: str, sender: Profile) -> Rung:
    if action in HARD_ASK_FIRST:
        return "ask_first"
    if sender.role == "unknown":  # unlinked sender: never above offer, even if preapproved
        return min(d.rung, "offer", key=_ORDER.index)
    if action in sender.preapproved and d.rung == "offer":
        return "act"
    return d.rung


def in_focus(p: Profile, now: datetime) -> bool:
    t = (now.astimezone(IST) if now.tzinfo else now).strftime("%H:%M")
    return any(a <= t < b for a, b in p.focus)


class Ladder:
    # ponytail: in-process state; persist to memory/ if Chief restarts matter
    def __init__(self, now=lambda: datetime.now(IST)):
        self._now = now
        self._posts: dict[str, deque] = defaultdict(deque)
        self._quiet: set[str] = set()
        self._held: dict[str, list] = defaultdict(list)  # items are opaque to the ladder

    def allow_unprompted(self, room_id: str) -> bool:
        now, q = self._now(), self._posts[room_id]
        while q and now - q[0] >= timedelta(hours=1):
            q.popleft()
        if room_id in self._quiet or len(q) >= 3:
            return False
        q.append(now)
        return True

    def quiet(self, room_id: str, on: bool) -> None:
        (self._quiet.add if on else self._quiet.discard)(room_id)

    def is_quiet(self, room_id: str) -> bool:
        return room_id in self._quiet

    def hold(self, for_slug: str, item) -> None:
        self._held[for_slug].append(item)

    def release_due(self, profiles: dict[str, Profile]) -> dict[str, list]:
        now = self._now()
        out = {}
        for slug in list(self._held):
            p = profiles.get(slug)
            if p and not in_focus(p, now):
                out[slug] = self._held.pop(slug)
        return out
