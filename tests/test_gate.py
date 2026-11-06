from types import SimpleNamespace as NS

from chief.gate import decide
from chief.models import Decision, Msg, Profile

P = Profile("ravi", "Ravi", "pm", "brief")
TPL = ["data-question", "bug-fix-review"]


def msg(addressed=False):
    return Msg("r", "#product", 1, "u", "ravi", "where is the refunds data?", None, addressed, False, "t")


class Fake:
    def __init__(self, tpl="data-question", boom=False):
        self.tpl, self.boom, self.calls = tpl, boom, []

    def system_one(self, state, questions, **kw):
        self.calls.append((state, questions, kw))
        if self.boom:
            raise RuntimeError("down")
        return NS(
            nouls={"for_me": NS(noul=0.9), "fits": NS(noul=0.8)},
            choices={"rung": NS(choice="act"), "template": NS(choice=self.tpl)},
            scores={"urgency": NS(score=1.5)},
        )


def run(fake, m=None):
    return decide(m or msg(), P, False, TPL, ["reviewer"], client=fake)


def test_maps_answers_to_decision():
    assert run(Fake()) == Decision(0.9, "act", "data-question", 0.8, 0.5)


def test_template_none_becomes_None():
    assert run(Fake(tpl="none")).template is None


def test_jev_error_falls_back_silent():
    assert run(Fake(boom=True)) == Decision(0, "silent", None, 0, 0)


def test_jev_error_when_addressed_falls_back_offer():
    assert run(Fake(boom=True), msg(addressed=True)).rung == "offer"


def test_single_call_per_message():
    f = Fake()
    run(f)
    assert len(f.calls) == 1
    assert set(f.calls[0][1]) == {"for_me", "rung", "template", "fits", "urgency"}
    assert set(f.calls[0][1]["template"].criteria) == {*TPL, "none"}
    assert f.calls[0][2]["model"] == "jev-1.14.0"
