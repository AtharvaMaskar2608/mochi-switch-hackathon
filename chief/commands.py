import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Command:
    kind: str
    arg: str = ""


def parse(body: str) -> Command | None:
    t = body.strip()
    low = t.lower()
    if low == "!memory":
        return Command("memory")
    if low == "!quiet":
        return Command("quiet_on")
    if low == "!quiet off":
        return Command("quiet_off")
    if m := re.fullmatch(r"!tick\s+(\S+)", low):
        return Command("tick", m[1])
    if m := re.match(r"(?:chief\s+)?remember\s+(.+)", t, re.I | re.S):
        return Command("remember", m[1].strip())
    if m := re.match(r"forget\s+(.+)", t, re.I | re.S):
        return Command("forget", m[1].strip())
    if m := re.fullmatch(r"(approve|deny)\s+([A-Za-z0-9]{4})", t, re.I):
        return Command(m[1].lower(), m[2].upper())
    return None


def _words(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", s.lower()))


def forget_target(store, sender_slug: str, text: str) -> str | None:
    """Id of the sender's memory item sharing the most words with text, else None."""
    q = _words(text)
    best = max(store.search(text, [sender_slug]), key=lambda i: len(q & _words(i.text)), default=None)
    return best.id if best and q & _words(best.text) else None
