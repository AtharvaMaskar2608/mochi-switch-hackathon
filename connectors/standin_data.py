"""Stand-in data agent for judges: NL question -> one read-only SELECT over sample Brightcart data."""
import re
import sqlite3
import sys
from pathlib import Path

from chief.models import Msg

try:
    from connectors.base import Reply
except ImportError:  # Task 14 not merged yet; same shape as connectors.base.Reply
    from dataclasses import dataclass, field

    @dataclass
    class Reply:
        text: str
        files: list[Path] = field(default_factory=list)

SAMPLE = Path(__file__).resolve().parent.parent / "sample"
DB = SAMPLE / "brightcart.db"
MODEL = "claude-haiku-5-5"
MASKED = {"email", "customer_name"}
FORBIDDEN = re.compile(r"\b(attach|pragma|insert|update|delete|drop)\b", re.I)
SCHEMA = (
    "tickets(id, created_at ISO date, queue one of Refunds|Payments|Delivery|Returns, category, status, "
    "priority, subject, customer_name, email, first_response_time_hrs, resolution_time_hrs)"
)


def init_db(db: Path = DB) -> None:
    db.unlink(missing_ok=True)
    con = sqlite3.connect(db)
    con.executescript((SAMPLE / "brightcart.sql").read_text())
    con.close()


def check_sql(sql: str) -> str:
    sql = sql.strip().removesuffix(";").strip()
    if not sql.lower().startswith("select") or ";" in sql or FORBIDDEN.search(sql):
        raise ValueError("only a single read-only SELECT is allowed")
    return sql


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    # PII columns read as NULL however the query refers to them (alias, substr, ...)
    con.set_authorizer(
        lambda act, t, c, *_: sqlite3.SQLITE_IGNORE
        if act == sqlite3.SQLITE_READ and t == "tickets" and c in MASKED
        else sqlite3.SQLITE_OK
    )
    return con


def run_select(sql: str, limit: int = 20) -> tuple[list[str], list[tuple]]:
    sql = check_sql(sql)
    con = connect()
    try:
        cur = con.execute(sql)
        cols = [c[0] for c in cur.description]
        rows = [tuple("***" if c in MASKED else v for c, v in zip(cols, r)) for r in cur.fetchmany(limit)]
        return cols, rows
    finally:
        con.close()


def clean(v) -> str:
    return str(v).replace("@", "\uff20")  # no live @mentions in replies


def table(cols: list[str], rows: list[tuple]) -> str:
    if not rows:
        return "No rows."
    out = ["| " + " | ".join(map(clean, cols)) + " |", "|" + "---|" * len(cols)]
    out += ["| " + " | ".join(map(clean, r)) + " |" for r in rows]
    return "\n".join(out)


def _client():
    import anthropic

    return anthropic.Anthropic()


def handle(msg: Msg, client=None) -> Reply:
    client = client or _client()
    r = client.messages.create(
        model=MODEL,
        max_tokens=400,
        system=f"Write ONE SQLite SELECT statement answering the question. Schema: {SCHEMA}. Output only the SQL.",
        messages=[{"role": "user", "content": msg.body}],
    )
    sql = r.content[0].text.strip().strip("`").removeprefix("sql").strip()
    try:
        return Reply(table(*run_select(sql)))
    except (ValueError, sqlite3.Error) as e:
        return Reply(f"Sorry, I could not run that query: {clean(e)}")


if __name__ == "__main__":
    if "--init" in sys.argv:
        init_db()
        print(f"built {DB}")
    else:
        from connectors.base import run_connector

        run_connector("standin-data", handle, ["data-steward"])
