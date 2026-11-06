"""Nightly memory consolidation: chat -> items, tidy each person, propose team knowledge."""
import json
import random
import re
import subprocess
from datetime import date, datetime
from pathlib import Path

from chief.memory.prompts import DEDUP, DREAM_PERSONAL, DREAM_TEAM, EXTRACT, KINDS, wrap
from chief.memory.store import MAX_ITEM, Item, MemoryStore
from chief.writer import SONNET, _call

STALE_DAYS = 90
MIN_PEOPLE = 3
MAX_CLUSTERS = 3
QUOTE_WORDS = 12
WHOLE_LESSON_WORDS = 6


def _json(text: str):
    """Parse a JSON value (one optional ``` fence tolerated); None when it is not JSON."""
    t = re.sub(r"^```(?:json)?\s*\n?(.*?)\n?```$", r"\1", text.strip(), flags=re.S)
    try:
        return json.loads(t)
    except ValueError:
        return None


def _list(text: str, cap: int) -> list[dict]:
    raw = _json(text)
    return [x for x in raw if isinstance(x, dict)][:cap] if isinstance(raw, list) else []


def _ok_text(t) -> bool:
    return isinstance(t, str) and 0 < len(t.strip()) <= MAX_ITEM


def _short_ids(items: list[Item]) -> tuple[dict[str, str], dict[str, str]]:
    alias = {i.id: f"m{n}" for n, i in enumerate(items, 1)}
    return alias, {a: i for i, a in alias.items()}


# ---- extract

def extract(room_threads: list[dict], people: list[str], client=None) -> list[dict]:
    lines = [f"[{e['sender_name']}] {e['body']}"
             for t in room_threads for e in [t["root"], *t.get("replies", [])]]
    if not lines:
        return []
    user = wrap("people", ", ".join(people)) + "\n\n" + wrap("new_chat", "\n".join(lines))
    out = []
    for x in _list(_call(client, SONNET, EXTRACT, user, 2000), 8):
        if x.get("owner") in people and x.get("kind") in KINDS and _ok_text(x.get("text")):
            hint = x.get("tierHint") if x.get("tierHint") in ("personal", "team", "company") else "personal"
            out.append({"owner": x["owner"], "kind": x["kind"], "text": x["text"].strip(), "tier_hint": hint})
    return out


# ---- dedup

def dedup(candidate: dict, similar: list[Item], client=None) -> tuple[str, str | None, str | None]:
    """-> (action, target_id, text). Fails closed to ("skip", None, None)."""
    similar = similar[:5]
    if not similar:
        return "store", None, None
    alias, real = _short_ids(similar)
    body = "\n".join([f"kind: {candidate['kind']}", f"text: {candidate['text']}", "similar:"]
                     + [f"- id {alias[s.id]} ({s.kind}): {s.text}" for s in similar])
    d = _json(_call(client, SONNET, DEDUP, wrap("candidate", body), 600))
    if not isinstance(d, dict):
        return "skip", None, None
    action = d.get("action")
    if action == "store":
        return "store", None, None
    if action in ("update", "merge") and d.get("targetId") in real and _ok_text(d.get("text")):
        return action, real[d["targetId"]], d["text"].strip()
    return "skip", None, None


# ---- dream personal

def dream_personal(slug: str, store: MemoryStore, episodes: list[str], client=None,
                   today: date | None = None) -> list[dict]:
    today = today or date.today()
    # unused for 90 days: archived by code, before and without the model
    for i in store.items(slug):
        if not i.edited_by_person and (today - datetime.fromisoformat(i.last_used_at).date()).days >= STALE_DAYS:
            store.archive(i.id)
    items = [i for i in store.items(slug) if not i.edited_by_person]  # person edits win
    if not items:
        return []
    alias, real = _short_ids(items)
    user = (f"Today: {today}\n\n"
            + wrap("items", "\n".join(f"- id {alias[i.id]} ({i.kind}, added {i.added_at[:10]}): {i.text}" for i in items))
            + "\n\n" + wrap("todays_chats", "\n".join(f"- {e}" for e in episodes) or "(none)"))
    applied, used = [], set()
    for o in _list(_call(client, SONNET, DREAM_PERSONAL, user, 3000), 30):
        op, ids, text = o.get("op"), o.get("ids"), o.get("text")
        if not (isinstance(ids, list) and ids and all(a in real for a in ids)):
            continue
        ids = [real[a] for a in ids]
        if len(set(ids)) != len(ids) or used & set(ids):
            continue
        if op == "retire":
            for i in ids:
                store.archive(i)
        elif op in ("merge", "update", "date") and _ok_text(text) and (len(ids) >= 2 if op == "merge" else len(ids) == 1):
            store.supersede(ids[0], text.strip(), "dreamer")
            for i in ids[1:]:
                store.archive(i)
        else:
            continue
        used.update(ids)
        applied.append({"op": op, "ids": ids, **({"text": text.strip()} if op != "retire" else {})})
    return applied


# ---- dream team

def _words(s: str) -> list[str]:
    return re.findall(r"\w+", s.lower())


def quotes(text: str, lessons: list[str]) -> bool:
    """True when text copies a 12-word run of a lesson, or a whole lesson of 6+ words."""
    w = _words(text)
    runs = {" ".join(w[i:i + QUOTE_WORDS]) for i in range(len(w) - QUOTE_WORDS + 1)}
    joined = f" {' '.join(w)} "
    for l in lessons:
        lw = _words(l)
        if any(" ".join(lw[i:i + QUOTE_WORDS]) in runs for i in range(len(lw) - QUOTE_WORDS + 1)):
            return True
        if len(lw) >= WHOLE_LESSON_WORDS and f" {' '.join(lw)} " in joined:
            return True
    return False


def _similar(a: str, b: str) -> bool:
    x, y = set(_words(a)), set(_words(b))
    both = len(x & y)
    return both > 0 and both / (len(x) + len(y) - both) >= 0.5


def _clusters(docs: list[tuple[str, str]]) -> list[list[tuple[str, str]]]:
    """Greedy word-overlap clusters of (owner, text); kept when >= MIN_PEOPLE distinct owners."""
    # ponytail: Jaccard >= 0.5, greedy and order-dependent; use embeddings if clusters are poor
    out: list[list[tuple[str, str]]] = []
    for d in docs:
        c = next((c for c in out if _similar(c[0][1], d[1])), None)
        if c:
            c.append(d)
        else:
            out.append([d])
    return [c for c in out if len({o for o, _ in c}) >= MIN_PEOPLE]


def dream_team(store: MemoryStore, client=None, today: date | None = None) -> list[str]:
    """Writes proposals (never active team items); returns the proposed texts."""
    today = today or date.today()
    people = sorted(p.name for p in (store.root / "people").glob("*") if p.is_dir())
    docs = [(p, i.text) for p in people for i in store.items(p)]
    known = [i.text for i in store.items("team")]
    known += [store._item(p).text for p in (store.root / "team" / "proposals").glob("m_*.md")]
    found = [c for c in _clusters(docs) if not any(_similar(k, c[0][1]) for k in known)][:MAX_CLUSTERS]
    proposed = []
    for c in found:
        lessons = [t for _, t in c]
        shuffled = random.sample(lessons, len(lessons))  # no order that hints at who wrote what
        user = wrap("lessons", "\n".join(f"- {l}" for l in shuffled))
        for p in _list(_call(client, SONNET, DREAM_TEAM, user, 2000), 5):
            if p.get("kind") not in KINDS or not _ok_text(p.get("text")):
                continue
            if quotes(p["text"], lessons) or quotes(str(p.get("reason", "")), lessons):
                continue
            store.add("team", "team", p["kind"], p["text"].strip(),
                      {"room": "", "date": str(today), "by": "dreamer"}, status="proposed")
            proposed.append(p["text"].strip())
    return proposed


# ---- nightly

def _git_commit(root: Path, day: str) -> None:
    git = ["git", "-c", "user.name=Chief", "-c", "user.email=chief@localhost"]
    subprocess.run([*git, "add", "--", "."], cwd=root, check=True)
    if subprocess.run([*git, "diff", "--cached", "--quiet", "--", "."], cwd=root).returncode == 0:
        return  # nothing changed in memory
    subprocess.run([*git, "commit", "-m", f"memory: dream {day}", "--", "."], cwd=root, check=True)


def run_nightly(client_switch, store: MemoryStore, rooms: list[str], client=None,
                today: date | None = None, commit=None) -> str:
    today = today or date.today()
    people = sorted(p.name for p in (store.root / "people").glob("*") if p.is_dir())
    episodes: dict[str, list[str]] = {p: [] for p in people}
    stored = updated = 0
    for room in rooms:
        threads = client_switch.context(room, limit=200)["threads"]
        for t in threads:
            for e in [t["root"], *t.get("replies", [])]:
                if e["sender_name"] in episodes:
                    episodes[e["sender_name"]].append(f"{room}: {e['body'][:200]}")
        for c in extract(threads, people, client):
            hits = store.search(c["text"], [c["owner"]])
            similar = [i for i in hits if not i.edited_by_person]  # person edits win
            if hits and not similar:
                continue  # only matches the person's own words: skip
            action, target, text = dedup(c, similar, client)
            if action == "store":
                store.add("personal", c["owner"], c["kind"], c["text"],
                          {"room": room, "date": str(today), "by": "dreamer"})
                stored += 1
            elif action in ("update", "merge"):
                store.supersede(target, text, "dreamer")
                updated += 1
    tidied = sum(len(dream_personal(p, store, episodes[p], client, today)) for p in people)
    proposals = dream_team(store, client, today)
    store.write_index()
    (commit or _git_commit)(store.root, str(today))
    doc = "\n".join(f"- {i.text}" for i in store.items("team")) or "(nothing yet)"
    for room in rooms:
        client_switch.room_doc(room, "Team knowledge", doc)
    note = f"What I learned: {stored} new, {updated} updated, {tidied} tidied."
    if proposals:
        note += " Proposed for the team:\n" + "\n".join(f"- {p}" for p in proposals)
    return note
