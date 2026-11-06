from datetime import datetime, timedelta

from chief.ladder import IST, Ladder, final_rung, in_focus
from chief.models import Decision, Profile


def D(r):
    return Decision(for_me=1, rung=r, template=None, fits=0, urgency=0)


def P(role="dev", **k):
    return Profile(slug="meera", name="Meera", role=role, style="", **k)


def at(h, m=0):
    return datetime(2026, 11, 7, h, m, tzinfo=IST)


def test_hard_list_overrides_act():
    assert final_rung(D("act"), "merge", P()) == "ask_first"


def test_jev_cannot_raise_past_ceiling():
    for a in ("deploy", "external_message", "interrupt_focus", "create_agent"):
        assert final_rung(D("act"), a, P(preapproved=[a])) == "ask_first"


def test_preapproved_lifts_offer_to_act_only_for_that_person():
    assert final_rung(D("offer"), "summarize", P(preapproved=["summarize"])) == "act"
    assert final_rung(D("offer"), "summarize", P()) == "offer"


def test_unknown_sender_capped_at_offer():
    u = P(role="unknown", preapproved=["x"])
    assert final_rung(D("act"), "x", u) == "offer"
    assert final_rung(D("silent"), "x", u) == "silent"


def test_fourth_unprompted_post_in_hour_refused():
    t = [at(10)]
    l = Ladder(lambda: t[0])
    assert [l.allow_unprompted("r") for _ in range(4)] == [True, True, True, False]
    t[0] += timedelta(hours=1)
    assert l.allow_unprompted("r")


def test_quiet_room_blocks_unprompted():
    l = Ladder(lambda: at(10))
    l.quiet("r", True)
    assert l.is_quiet("r") and not l.allow_unprompted("r")
    l.quiet("r", False)
    assert l.allow_unprompted("r")


def test_meera_in_focus_at_11_ist_not_at_15_01():
    m = P(focus=[("10:00", "15:00")])
    assert in_focus(m, at(11)) and not in_focus(m, at(15, 1))


def test_hold_released_after_focus_ends():
    t = [at(11)]
    l = Ladder(lambda: t[0])
    ps = {"meera": P(focus=[("10:00", "15:00")])}
    l.hold("meera", "digest")
    assert l.release_due(ps) == {}
    t[0] = at(15, 1)
    assert l.release_due(ps) == {"meera": ["digest"]}
    assert l.release_due(ps) == {}
