"""Designer connector: bridges a Switch message to design-agent's HTTP API."""
import os
import re
import tempfile
import time
from pathlib import Path

import httpx

from chief.models import Msg
from connectors.base import Reply, run_connector

CAP_S = 15 * 60
FILE_RE = re.compile(r"[\w.-]+\.(?:mp4|mov|webm|png|jpg|jpeg|gif|pdf|svg|pptx|zip)")


def handle(msg: Msg, transport: httpx.BaseTransport | None = None, poll_s: float = 5,
           cap_s: float = CAP_S, out_dir: Path | None = None) -> Reply:
    tid = msg.thread_id or msg.room_id
    with httpx.Client(base_url=os.environ.get("DESIGN_AGENT_URL", "http://localhost:8000"),
                      headers={"Authorization": f"Bearer {os.environ.get('DESIGN_AGENT_TOKEN', '')}"},
                      transport=transport, timeout=60) as c:
        r = c.post(f"/threads/{tid}/messages", json={"text": msg.body})
        r.raise_for_status()
        out = r.json()
        text, names, working, after = out["reply"], list(out["files"]), out["working"], 0
        deadline = time.monotonic() + cap_s
        while working:
            if time.monotonic() > deadline:
                return Reply("designer timed out after 15 minutes; check design-agent")
            time.sleep(poll_s)
            u = c.get(f"/threads/{tid}/updates", params={"after": after}).json()
            after = u["cursor"]
            # design-agent's updates carry no "final" marker (surfaces/api/app.py): progress notes come first,
            # the update naming an output file is the delivery. A status field ends it too, should one be sent.
            for update in u["updates"]:
                text = update
                if found := FILE_RE.findall(update):
                    names += found
                    working = False
                    break
            if u.get("status") in ("idle", "complete"):
                working = False
        d = out_dir or Path(tempfile.mkdtemp(prefix="designer-"))
        files = []
        for n in dict.fromkeys(names):
            fr = c.get(f"/threads/{tid}/files/{n}")
            if fr.status_code == 200:
                (d / n).write_bytes(fr.content)
                files.append(d / n)
    return Reply(text, files)


if __name__ == "__main__":
    run_connector("designer", handle, ["video"])
