"""Jev gate: one cheap TypeSafe call per message decides for_me / rung / template / urgency."""
import sys
import time

from typesafe_sdk import Choice, Noul, RetryPolicy, Score, TypeSafeClient

from chief.models import Decision, Msg, Profile

MODEL = "jev-1.14.0"
TIMEOUT = 5.0
LEVELS = ["can wait until tomorrow", "today", "within the hour", "interrupt now"]


def _questions(templates: list[str]) -> dict:
    return {
        "for_me": Noul(
            instructions="Does this message ask for help, or raise a question or task that the team's AI chief of staff could take on, rather than being chat between people?"
        ),
        "rung": Choice(
            instructions="How should the team's AI chief of staff respond to this message?",
            criteria={
                "silent": "people chatting, or nothing useful to add",
                "offer": "Chief could help but was not asked",
                "act": "a direct, reversible request: answer, summarise, look up, open a work room",
                "ask_first": "the request would change code, deploy, message outside the team, or interrupt someone",
            },
        ),
        "template": Choice(
            instructions="Which listed workflow fits this request?",
            criteria={**{t: None for t in templates}, "none": "no listed workflow fits"},
        ),
        "fits": Noul(instructions="Does one of the listed workflows clearly fit this request?"),
        "urgency": Score(instructions="How urgent is this message?", criteria=LEVELS),
    }


QUESTIONS: dict = _questions([])  # shape reference; decide() builds with the real templates


def build_state(msg: Msg, sender: Profile, recipient_in_focus: bool, templates: list[str], roles: list[str]) -> dict:
    return {
        "message": msg.body,
        "room": msg.room_name,
        "in_thread": msg.thread_id is not None,
        "addressed_to_chief": msg.addressed,
        "sender": {"name": sender.name, "role": sender.role, "is_agent": msg.is_agent},
        "recipient_in_focus_time": recipient_in_focus,
        "workflows": templates,
        "team_roles": roles,
    }


_client: TypeSafeClient | None = None


def decide(msg: Msg, sender: Profile, recipient_in_focus: bool, templates: list[str], roles: list[str], client=None) -> Decision:
    global _client
    try:
        if client is None:
            client = _client = _client or TypeSafeClient(model=MODEL, timeout=TIMEOUT)
        r = client.system_one(
            build_state(msg, sender, recipient_in_focus, templates, roles),
            _questions(templates),
            model=MODEL,
            timeout=TIMEOUT,
            retry=RetryPolicy(max_retries=0),
        )
        tpl = r.choices["template"].choice
        return Decision(
            for_me=r.nouls["for_me"].noul,
            rung=r.choices["rung"].choice,
            template=None if tpl == "none" else tpl,
            fits=r.nouls["fits"].noul,
            urgency=r.scores["urgency"].score / 3,
        )
    except Exception as e:  # network, auth, timeout, malformed: never block the chat loop
        print(f"gate: jev failed ({type(e).__name__}); falling back", file=sys.stderr)
        return Decision(for_me=0, rung="offer" if msg.addressed else "silent", template=None, fits=0, urgency=0)


if __name__ == "__main__":
    m = Msg("r1", "#product", 1, "u1", "ravi", sys.argv[1], None, False, False, "2026-11-07T10:00:00+05:30")
    tpls = ["brightcart-org", "data-question", "shipped-digest", "bug-fix-review", "release-train", "org-team"]
    t0 = time.perf_counter()
    d = decide(m, Profile("ravi", "Ravi", "pm", "brief"), False, tpls, ["data-steward", "reviewer", "deployer", "on-call"])
    print(d, f"\nlatency: {(time.perf_counter() - t0) * 1000:.0f} ms")
