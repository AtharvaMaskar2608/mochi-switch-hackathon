"""Markdown memory store: one item per file, git-friendly, human-editable."""
import os
import re
import secrets
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import frontmatter

from chief.models import Profile

MAX_ITEM = 300


@dataclass
class Item:
    id: str
    tier: str
    owner: str
    kind: str
    status: str
    text: str
    added_at: str
    last_used_at: str
    use_count: int
    version: int
    supersedes: str | None
    edited_by_person: bool
    path: Path


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _words(s: str) -> set[str]:
    return set(re.findall(r"\w+", s.lower()))


class MemoryStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def _dir(self, tier: str, owner: str) -> Path:
        return self.root / ("team" if tier == "team" else f"people/{owner}") / "items"

    def _write(self, path: Path, post: frontmatter.Post) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            f.write(frontmatter.dumps(post) + "\n")
        os.replace(tmp, path)

    def _find(self, item_id: str) -> Path:
        for p in self.root.glob(f"**/{item_id}.md"):
            return p
        raise KeyError(item_id)

    def _item(self, path: Path) -> Item:
        post = frontmatter.load(path)
        m = post.metadata
        return Item(
            id=m["id"], tier=m["tier"], owner=m["owner"], kind=m["kind"], status=m["status"],
            text=post.content.strip(), added_at=str(m["added_at"]),
            last_used_at=str(m["last_used_at"]), use_count=int(m["use_count"]),
            version=int(m["version"]), supersedes=m.get("supersedes"),
            edited_by_person=bool(m.get("edited_by_person", False)), path=path,
        )

    def profile(self, slug: str) -> Profile:
        path = self.root / "people" / slug / "profile.md"
        if not path.exists():
            return Profile(slug, name=slug, role="unknown", style="short and plain",
                           focus=[], preapproved=[], card="")
        post = frontmatter.load(path)
        m = post.metadata
        focus = [tuple(r.split("-", 1)) for r in m.get("focus") or []]
        return Profile(slug, name=m.get("name", slug), role=m.get("role", "unknown"),
                       style=m.get("style", "short and plain"), focus=focus,
                       preapproved=list(m.get("preapproved") or []), card=post.content.strip())

    def add(self, tier: str, owner: str, kind: str, text: str, source: dict, *,
            status: str = "active", version: int = 1, supersedes: str | None = None) -> str:
        if len(text) > MAX_ITEM:
            raise ValueError(f"item text is {len(text)} chars, max {MAX_ITEM}")
        item_id = f"m_{secrets.token_hex(4)}"
        now = _now()
        post = frontmatter.Post(text, id=item_id, tier=tier, owner=owner, kind=kind,
                                status=status, source=source, added_at=now, last_used_at=now,
                                use_count=0, version=version, supersedes=supersedes,
                                edited_by_person=source.get("by") == "person")
        folder = self._dir(tier, owner)
        if status == "proposed":
            folder = folder.parent / "proposals"
        self._write(folder / f"{item_id}.md", post)
        return item_id

    def _update(self, item_id: str, **fields) -> None:
        path = self._find(item_id)
        post = frontmatter.load(path)
        post.metadata.update(fields)
        self._write(path, post)

    def supersede(self, old_id: str, new_text: str, by: str) -> str:
        old = self._item(self._find(old_id))
        new_id = self.add(old.tier, old.owner, old.kind, new_text,
                          {"room": "", "date": _now()[:10], "by": by},
                          version=old.version + 1, supersedes=old_id)
        self._update(old_id, status="superseded")
        return new_id

    def archive(self, item_id: str) -> None:
        self._update(item_id, status="archived")

    def touch(self, item_id: str) -> None:
        it = self._item(self._find(item_id))
        self._update(item_id, use_count=it.use_count + 1, last_used_at=_now())

    def items(self, owner: str, status: str = "active") -> list[Item]:
        folder = self._dir("team" if owner == "team" else "personal", owner)
        found = [self._item(p) for p in sorted(folder.glob("m_*.md"))]
        return [i for i in found if i.status == status]

    def search(self, query: str, owners: list[str], k: int = 5) -> list[Item]:
        q = _words(query)
        scored = [(len(q & _words(i.text)), i) for o in owners for i in self.items(o)]
        scored = [s for s in scored if s[0] > 0]
        scored.sort(key=lambda s: s[0], reverse=True)  # stable: ties keep owner order
        return [i for _, i in scored[:k]]

    def write_index(self) -> None:
        people = sorted(p.name for p in (self.root / "people").glob("*") if p.is_dir())
        lines = ["# Memory", "", "<!-- generated by Chief; edit the item files, not this one -->", ""]
        for owner in people + ["team"]:
            lines += [f"## {owner}", ""]
            for i in self.items(owner):
                rel = i.path.relative_to(self.root).as_posix()
                lines.append(f"- {i.text} ([{i.kind}]({rel}), added {i.added_at[:10]})")
            lines.append("")
        (self.root / "MEMORY.md").write_text("\n".join(lines))
