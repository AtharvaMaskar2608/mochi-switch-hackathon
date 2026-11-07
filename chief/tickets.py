"""Tickets as GitHub Issues, through the `gh` CLI. The repo is env TICKETS_REPO (owner/name).

Every ticket also gets the `brightcart` label, so demo tickets stay apart from the project's real issues.
"""
import os
import re
import subprocess

DEMO_LABEL = "brightcart"
MAX_BODY = 4000  # GitHub allows 65536; a handoff brief never needs more
_made: set[tuple[str, str]] = set()  # (repo, label) created by this process


# ponytail: no TICKETS_REPO -> every call returns None and Chief tracks the handoff in the thread instead
def _repo() -> str | None:
    return os.environ.get("TICKETS_REPO") or None


def _clean(text: str) -> str:
    return text.replace("@", "").replace("＠", "")  # a GitHub @-mention notifies a real user


def _gh(args: list[str], run) -> str:
    p = run(["gh", *args, "--repo", _repo()], capture_output=True, text=True, check=True)
    return p.stdout.strip()


def _ensure_labels(labels: list[str], run) -> None:
    for name in labels:
        if (_repo(), name) not in _made:  # once per process; --force: create or update, never fail on "exists"
            _gh(["label", "create", name, "--force"], run)
            _made.add((_repo(), name))


def create(title: str, body: str, labels: list[str], run=subprocess.run) -> dict | None:
    """{number, url} of the new issue, or None when no tracker is configured."""
    if not _repo():
        return None
    labels = [*labels, DEMO_LABEL]
    _ensure_labels(labels, run)
    args = ["issue", "create", "--title", _clean(title), "--body", _clean(body)[:MAX_BODY]]
    for name in labels:
        args += ["--label", name]
    url = _gh(args, run).splitlines()[-1]
    return {"number": int(re.search(r"/(\d+)$", url)[1]), "url": url}


def comment(n: int, body: str, run=subprocess.run) -> None:
    if _repo():
        _gh(["issue", "comment", str(n), "--body", _clean(body)[:MAX_BODY]], run)


def close(n: int, comment: str, run=subprocess.run) -> None:
    if _repo():
        _gh(["issue", "close", str(n), "--comment", _clean(comment)], run)


def label(n: int, add: list[str], remove: list[str], run=subprocess.run) -> None:
    if not _repo():
        return None
    _ensure_labels(add, run)
    args = ["issue", "edit", str(n)]
    for name in add:
        args += ["--add-label", name]
    for name in remove:
        args += ["--remove-label", name]
    _gh(args, run)
