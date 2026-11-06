"""Generic connector: an existing agent joins Switch rooms as a teammate."""
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from chief.models import Msg

LONG_JOB_S = 20
MAX_ATTACH = 20 * 1024 * 1024


@dataclass
class Reply:
    text: str
    files: list[Path] = field(default_factory=list)
    roles: list[str] = field(default_factory=list)  # set -> client.target(roles=...) instead of post
    names: list[str] = field(default_factory=list)


def _clean(t: str) -> str:
    return t.replace("@", "\uff20")  # a literal @name would re-address an agent


def _body(r: Reply) -> str:
    # ponytail: SwitchClient.post has no attachment arg, so every file is posted as a path;
    # files <= 20 MB could be attached once the client supports it.
    lines = [_clean(r.text)]
    for f in r.files:
        big = f.exists() and f.stat().st_size > MAX_ATTACH
        lines.append(f"file{' (over 20 MB)' if big else ''}: {f}")
    return "\n".join(lines)


def _send(client, msg: Msg, r: Reply) -> None:
    body, tid = _body(r), msg.thread_id
    if r.roles or r.names:
        client.target(msg.room_id, body, names=r.names or None, roles=r.roles or None, thread_id=tid)
    else:
        client.post(msg.room_id, body, thread_id=tid)


def _work(client, msg: Msg, handler: Callable[[Msg], Reply], long_s: float) -> None:
    box: list[Reply] = []

    def run():
        try:
            box.append(handler(msg))
        except Exception as e:  # report instead of dying silently
            box.append(Reply(f"failed: {e}"))

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(long_s)
    if t.is_alive():
        one = (msg.body.strip().splitlines() or [""])[0][:80]
        client.post(msg.room_id, f"working on it: {_clean(one)}", thread_id=msg.thread_id)
        t.join()
    _send(client, msg, box[0])


def _default_client(name: str, roles: list[str]):
    from chief.switch_client import connect

    # roles are hats granted per room by the templates; the description just says which it wears
    return connect(name, f"{name} connector; roles: {', '.join(roles) or 'none'}", Path(f".connector-{name}.json"))


def run_connector(name: str, handler: Callable[[Msg], Reply], roles: list[str],
                  client=None, long_s: float = LONG_JOB_S, once: bool = False) -> None:
    """Listen for addressed messages; reply in the same thread. `once` = stop when events end."""
    client = client or _default_client(name, roles)
    workers = []
    for msg in client.events():
        if not msg.addressed or msg.sender_name == name:
            continue
        w = threading.Thread(target=_work, args=(client, msg, handler, long_s), daemon=True)
        w.start()
        workers.append(w)
    if once:
        for w in workers:
            w.join()
