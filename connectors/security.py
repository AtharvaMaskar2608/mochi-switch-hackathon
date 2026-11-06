"""Security connector: runs the cyber-security scan and gates on severity in code.

Work-room brief convention: lines in the message body, e.g.
    owner: meera          -> findings go to role `intern-meera`
    path: sample/brightcart-shop   -> scan target (default sample/brightcart-shop; relative to cyber-security)
"""
import json
import re
import subprocess
import time
from pathlib import Path

from chief.models import Msg
from connectors.base import Reply, run_connector

CYBER = Path("/Users/choice/projects/cyber-security")
DEFAULT_PATH = "sample/brightcart-shop"


def _field(body: str, key: str) -> str | None:
    m = re.search(rf"^\s*{key}:\s*(\S+)\s*$", body, re.M | re.I)
    return m.group(1) if m else None


def _slug(target: Path) -> str:  # mirrors bin/scan
    s = str(target.resolve())
    prefix = str(Path.home() / "projects") + "/"
    return (s[len(prefix):] if s.startswith(prefix) else s).replace("/", "-")


def _block_roles(owner: str | None) -> list[str]:
    return [f"intern-{owner}"] if owner else ["security"]


def handle(msg: Msg, run=subprocess.run, cyber: Path = CYBER) -> Reply:
    owner = _field(msg.body, "owner")
    target = Path(_field(msg.body, "path") or DEFAULT_PATH)
    if not target.is_absolute():
        target = cyber / target
    start = time.time()
    p = run([str(cyber / "bin/scan"), str(target), "--fail-on", "high"],
            capture_output=True, text=True, cwd=cyber)
    if p.returncode not in (0, 1):  # fail closed: a crashed scan never passes
        return Reply(f"BLOCKED: scan failed (exit {p.returncode})", roles=_block_roles(owner))
    path = cyber / "reports" / _slug(target) / "latest" / "summary.json"
    if not path.exists() or path.stat().st_mtime < start:
        return Reply("BLOCKED: no fresh report", roles=_block_roles(owner))
    s = json.loads(path.read_text())
    v = s["vulnerabilities"]
    # gate in code: scan exit 1, or any high/critical, or secrets
    blocked = p.returncode == 1 or v["high"] > 0 or v["critical"] > 0 or s["policy"]["breached"]
    detail = f"critical {v['critical']}, high {v['high']}, secrets {s['sast']['secrets']}"
    if not blocked:
        return Reply(f"PASSED: {detail}", roles=["release-scribe"])
    top = "; ".join(f"{t['id']} {t['package']} {t['version']} -> fix {t['fix']}"
                    for t in s["top_vulnerabilities"][:5])
    return Reply(f"BLOCKED: {detail}. {top}", roles=_block_roles(owner))


if __name__ == "__main__":
    run_connector("security", handle, ["security"])
