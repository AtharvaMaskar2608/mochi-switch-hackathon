import sqlite3
from types import SimpleNamespace

import pytest

from chief.models import Msg
from connectors import standin_data as sd


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    p = tmp_path / "b.db"
    sd.init_db(p)
    monkeypatch.setattr(sd, "DB", p)


def fake(sql):
    c = SimpleNamespace(text=sql)
    return SimpleNamespace(messages=SimpleNamespace(create=lambda **k: SimpleNamespace(content=[c])))


def ask(sql):
    m = Msg("r", "#x", 1, "u", "U", "q", None, True, False, "t")
    return sd.handle(m, fake(sql)).text


def test_rejects_non_select():
    assert "Sorry" in ask("DELETE FROM tickets")
    assert "Sorry" in ask("select 1 where 1 in (select 1) -- x; drop table tickets")


def test_rejects_second_statement():
    assert "Sorry" in ask("select 1; select 2")


def test_masks_email():
    t = ask("select email, customer_name, id from tickets limit 3")
    assert "@" not in t and t.count("***") == 6


def test_db_opened_readonly():
    with pytest.raises(sqlite3.OperationalError):
        sd.connect().execute("delete from tickets")


def test_row_cap():
    assert len(ask("select id from tickets").splitlines()) == 22


def test_spike_visible():
    def avg(cond, days):
        sql = f"select count(*)/{days}.0 from tickets where queue='Refunds' and created_at {cond}"
        return sd.run_select(sql, 1)[1][0][0]

    cut = "date((select max(created_at) from tickets),'-6 days')"
    assert avg(f">= {cut}", 7) > 2 * avg(f"< {cut}", 23)


def test_alias_cannot_leak_email():
    t = ask("select email as e, substr(email,1,3) as s from tickets limit 3")
    assert "example" not in t and "@" not in t


def test_expression_cannot_leak_customer_name():
    t = ask("select upper(customer_name) as n, customer_name || 'x' as m from tickets limit 3")
    assert not any(n in t for n in ("AVA", "LIAM", "Ava", "Liam", "Sam", "Zoe"))


def test_thinking_blocks_skipped():
    blocks = [SimpleNamespace(type="thinking", thinking="hmm"), SimpleNamespace(type="text", text="select 1 as n")]
    client = SimpleNamespace(messages=SimpleNamespace(create=lambda **k: SimpleNamespace(content=blocks)))
    assert sd.handle(Msg("r", "#x", 1, "u", "U", "q", None, True, False, "t"), client).text == "| n |\n|---|\n| 1 |"


def test_reply_strips_mentions():
    t = ask('select 1 as "@reviewer"')
    assert "@" not in t and "\uff20reviewer" in t
    assert "@" not in ask("select nope@x from tickets")
