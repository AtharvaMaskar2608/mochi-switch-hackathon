"""QA agent: checks out the brief's branch in a local clone of the sample shop and runs its tests.

Deterministic, no LLM: the verdict is pytest's exit code. Advisory only; the QA person decides.
Brief convention (lines in the message body):  branch: feat/partial-refunds
Clone: env QA_REPO_DIR (default .run/brightcart-shop), built with sample/make_shop_repo.sh --local on first use.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

from chief.models import Msg
from connectors.base import Reply, run_connector
from connectors.security import _field

ROOT = Path(__file__).resolve().parent.parent
BRANCH_OK = re.compile(r"[A-Za-z0-9][\w./-]{0,100}")  # never an option-looking "-x"


def repo_dir() -> Path:
    return Path(os.environ.get("QA_REPO_DIR") or ROOT / ".run" / "brightcart-shop").resolve()


def failing_tests(output: str) -> list[str]:
    return re.findall(r"^(?:FAILED|ERROR) (\S+)", output, re.M)


def handle(msg: Msg, run=subprocess.run, repo: Path | None = None) -> Reply:
    repo = repo or repo_dir()
    if not (repo / ".git").exists():
        b = run(["bash", str(ROOT / "sample/make_shop_repo.sh"), "--local", str(repo)], capture_output=True, text=True)
        if b.returncode != 0:
            return Reply(f"QA FAILED: could not build the sample repo (exit {b.returncode})")
    branch = _field(msg.body, "branch")
    if branch:
        if not BRANCH_OK.fullmatch(branch):
            return Reply("QA FAILED: that branch name is not one I will check out")
        co = run(["git", "checkout", "-q", branch, "--"], capture_output=True, text=True, cwd=repo)
        if co.returncode != 0:
            return Reply(f"QA FAILED: no branch {branch} in the sample repo")
    where = branch or "the current checkout (no branch in the brief)"
    p = run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], capture_output=True, text=True, cwd=repo)
    if p.returncode == 0:
        return Reply(f"QA PASSED: pytest is green on {where}. Advisory: the QA person decides.")
    names = failing_tests(p.stdout)  # fail closed: any non-zero exit is a failure
    detail = ", ".join(names) if names else f"pytest exit {p.returncode}"
    return Reply(f"QA FAILED on {where}: {detail}. Advisory: the QA person decides.")


if __name__ == "__main__":
    run_connector("qa-agent", handle, ["qa"])
