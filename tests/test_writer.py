from types import SimpleNamespace as NS

from chief import writer
from chief.models import Msg, Profile

RAVI = Profile(slug="ravi", name="Ravi", role="PM", style="tables and definitions")
MSG = Msg("r", "#product", 1, "u", "Asha", "ship it", None, False, False, "t")
HIT = NS(kind="fact", text="x")


class Fake:
    def __init__(self, reply):
        self.calls = []
        self.messages = self
        self.reply = reply

    def create(self, **kw):
        self.calls.append(kw)
        return NS(content=[NS(type="text", text=self.reply)])


def test_sanitize_strips_mentions():
    assert "@" not in writer.sanitize("ask @ship now")


def test_every_output_is_sanitized():
    c = Fake("@reviewer do it")
    for out in (
        writer.restyle("a", RAVI, client=c),
        writer.offer_line(MSG, "bug-fix-review", client=c),
        writer.batched_brief(["a"], RAVI, client=c),
        writer.brief(MSG, RAVI, [HIT], "t", client=c),
        writer.answer_from_memory("q", [HIT], RAVI, client=c),
    ):
        assert "@" not in out


def test_restyle_passes_reader_style():
    c = Fake("ok")
    writer.restyle("a", RAVI, client=c)
    assert "tables and definitions" in c.calls[0]["system"]
    assert c.calls[0]["model"] == writer.HAIKU


def test_answer_from_memory_none_on_NO_ANSWER():
    assert writer.answer_from_memory("q", [HIT], RAVI, client=Fake("NO_ANSWER")) is None
    assert writer.answer_from_memory("q", [HIT], RAVI, client=Fake("42")) == "42"


def test_video_mention_in_brief():
    asha = Msg("r", "#leadership", 1, "u", "asha", "what did we ship this week? make it a one-minute video",
               None, True, False, "t")
    out = writer.brief(asha, RAVI, [], "shipped-digest", client=Fake("digest brief"))
    assert out.startswith("digest brief") and "video" in out and "designer" in out and "@" not in out
    plain = writer.brief(MSG, RAVI, [], "shipped-digest", client=Fake("digest brief"))
    assert plain == "digest brief"


def test_split_requests():
    c = Fake('["what did we ship", "make a video of it", "why did refunds jump", "and a 4th"]')
    assert writer.split_requests("x", client=c) == ["what did we ship", "make a video of it", "why did refunds jump"]
    assert c.calls[0]["model"] == writer.HAIKU
    for bad in ("not json", "[]", '{"a": 1}'):
        assert writer.split_requests("the text", client=Fake(bad)) == ["the text"]


def test_call_disables_thinking_and_skips_thinking_blocks():
    class Thinky(Fake):
        def create(self, **kw):
            self.calls.append(kw)
            return NS(content=[NS(type="thinking", thinking="hmm"), NS(type="text", text="hi")])

    c = Thinky("")
    assert writer.restyle("a", RAVI, client=c) == "hi"
    assert c.calls[0]["thinking"] == {"type": "disabled"}
