"""Shared types. Every module imports these; change them only with every caller."""
from dataclasses import dataclass, field
from typing import Literal

Rung = Literal["silent", "offer", "act", "ask_first"]


@dataclass
class Msg:
    room_id: str
    room_name: str
    sequence: int
    sender: str
    sender_name: str
    body: str
    thread_id: str | None
    addressed: bool
    is_agent: bool
    ts: str
    message_id: str | None = None  # this message's own id: the thread root when it starts one


@dataclass
class Decision:
    for_me: float
    rung: Rung
    template: str | None
    fits: float
    urgency: float  # 0..1 (Jev score / 3)


@dataclass
class Profile:
    slug: str
    name: str
    role: str
    style: str
    focus: list[tuple[str, str]] = field(default_factory=list)  # [("10:00","15:00")] IST
    preapproved: list[str] = field(default_factory=list)
    card: str = ""
