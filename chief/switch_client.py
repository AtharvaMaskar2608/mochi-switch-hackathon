"""Switch agent-protocol client: register, SSE stream + heartbeat, ops."""
import argparse
import json
import os
import threading
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone

import httpx

from chief.models import Msg

# Same profile as loadtest/seed.py in the Switch repo.
PROFILE = {
    "connection_model": "always_on",
    "message_exchange": True,
    "pre_invocation_mediation": [],
    "post_invocation_mediation": [],
    "event_reporting": [],
    "task_protocol": {"can_delegate": False, "can_accept": False},
}


def register(base_url: str, registration_token: str, name: str, description: str) -> tuple[str, str]:
    r = httpx.post(
        f"{base_url.rstrip('/')}/agents",
        headers={"Authorization": f"Bearer {registration_token}"},
        json={"name": name, "description": description, "connector_type": "e2e",
              "integration_profile": PROFILE},
        timeout=30,
    )
    r.raise_for_status()
    b = r.json()
    return b["id"], b["api_key"]


class SwitchClient:
    def __init__(self, base_url: str, api_key: str, agent_id: str, http: httpx.Client | None = None,
                 retry_delay: float = 1.0):
        self._lock = threading.RLock()  # _enter + op must be atomic across threads
        self._covered: set[str] = set()  # rooms the stream covers (connection_state / subscription_changed)
        self._agent_ids: set[str] = set()  # agent participant ids seen in connect_to_room
        self.base = f"{base_url.rstrip('/')}/agents/{agent_id}"
        self.agent_id = agent_id
        self.http = http or httpx.Client(timeout=httpx.Timeout(30, read=None))
        self.headers = {"Authorization": f"Bearer {api_key}"}
        self.connection_id = str(uuid.uuid4())  # reused across reconnects
        self.retry_delay = retry_delay
        self.gap_rooms: set[str] = set()  # rooms to re-read via context() after a gap
        self._gap_pending = False
        self._last_seq = 0
        self._room: str | None = None  # room claimed by this connection
        self._room_names: dict[str, str] = {}
        self._stop = threading.Event()
        self._beat: threading.Thread | None = None
        self._resp: httpx.Response | None = None

    def close(self) -> None:
        self._stop.set()
        if self._resp is not None:
            self._resp.close()

    # ---- stream ----
    @staticmethod
    def _frames(resp: httpx.Response) -> Iterator[tuple[str, dict]]:
        event, data = "message", []
        for line in resp.iter_lines():
            if line == "":
                if data:
                    try:
                        yield event, json.loads("\n".join(data))
                    except json.JSONDecodeError:
                        pass
                event, data = "message", []
            elif line.startswith(":"):
                continue
            elif line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].lstrip())
            # id: is also in data["sequence"]

    def _heartbeat(self, interval: float) -> None:
        while not self._stop.wait(interval):
            try:
                r = self.http.post(f"{self.base}/connection/beat", headers=self.headers,
                                   json={"connection_id": self.connection_id, "cursor": self._last_seq})
                if r.status_code in (404, 409) and self._resp is not None:
                    self._resp.close()  # forces the read loop to reconnect
            except httpx.HTTPError:
                pass

    def _room_name(self, room_id: str) -> str:
        if room_id not in self._room_names:
            try:
                for r in self.op("list_rooms"):
                    self._room_names[r["room_id"]] = r["name"]
            except Exception:
                pass
        return self._room_names.get(room_id, room_id)

    def events(self, rooms: list[str] | None = None) -> Iterator[Msg]:
        params = {"connection_id": self.connection_id, "scope": "all", "filter": "all"}
        if rooms:
            params["rooms"] = ",".join(rooms)
        while not self._stop.is_set():
            headers = {**self.headers, "Accept": "text/event-stream"}
            if self._last_seq:
                headers["Last-Event-ID"] = str(self._last_seq)
            try:
                with self.http.stream("GET", f"{self.base}/events", params=params, headers=headers) as resp:
                    resp.raise_for_status()
                    self._resp = resp
                    for kind, d in self._frames(resp):
                        if kind in ("connection_state", "subscription_changed"):
                            self._covered = set(d.get("rooms", []))
                        if kind in ("evicted", "room_released"):
                            self._room = None
                        if kind == "connection_state":
                            if self._beat is None:
                                self._beat = threading.Thread(
                                    target=self._heartbeat, daemon=True,
                                    args=(d.get("heartbeat_interval_seconds", 2),))
                                self._beat.start()
                        elif kind == "gap":
                            self._gap_pending = True
                            self.gap_rooms |= self._covered  # scope=all: a gap hits every room
                            # server sequence may have reset (restart): resume from its view
                            self._last_seq = max(0, d.get("resumed_at", d.get("from_sequence", 1)) - 1)
                        elif d.get("sequence"):  # any domain event advances the cursor
                            seq = d["sequence"]
                            if seq <= self._last_seq:
                                continue
                            self._last_seq = seq
                            if kind != "message":
                                continue
                            if self._gap_pending:
                                self.gap_rooms.add(d["room_id"])
                                self._gap_pending = False
                            yield self._msg(d)
            except (httpx.HTTPError, httpx.StreamError):
                pass
            finally:
                self._resp = None
                self._room = None  # claim may be lost across a reconnect
            self._stop.wait(self.retry_delay)

    def _msg(self, d: dict) -> Msg:
        p = d.get("payload", {})
        sender = p.get("sender", "")
        ms = p.get("timestamp")
        ts = datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat() if ms else ""
        return Msg(
            room_id=d["room_id"], room_name=self._room_name(d["room_id"]), sequence=d["sequence"],
            sender=sender, sender_name=p.get("sender_name", sender), body=p.get("body", ""),
            thread_id=p.get("thread_id"), addressed=bool(p.get("addressed")),
            # ponytail: "@switch-agent-…" id shape is a convention, not guaranteed by the protocol
            is_agent=sender in self._agent_ids or sender.startswith("@switch-agent"), ts=ts)

    # ---- ops ----
    def op(self, name: str, **args):
        r = self.http.post(f"{self.base}/ops/{name}", headers={
            **self.headers, "X-Switch-Connection-Id": self.connection_id}, json=args)
        if r.status_code == 409:
            self._room = None  # connection/room claim is gone; re-enter next time
        r.raise_for_status()
        return r.json().get("result")

    def _learn(self, info) -> None:
        for p in (info or {}).get("participants", []):
            if isinstance(p, dict) and p.get("type") == "agent" and p.get("id"):
                self._agent_ids.add(p["id"])

    def _enter(self, room_id: str) -> None:
        if self._room != room_id:
            self._learn(self.op("connect_to_room", room_id=room_id))
            self._room = room_id

    def post(self, room_id: str, body: str, thread_id: str | None = None):
        with self._lock:
            self._enter(room_id)
            return self.op("post_message", body=body, thread_id=thread_id)

    def target(self, room_id: str, body: str, names=None, roles=None, thread_id=None):
        with self._lock:
            self._enter(room_id)
            return self.op("send_targeted_message", body=body, target_names=names,
                           target_roles=roles, thread_id=thread_id)

    def context(self, room_id: str, limit: int = 50):
        with self._lock:
            self._enter(room_id)
            return self.op("read_context", limit=limit)

    def room_from_yaml(self, yaml: str, inputs: dict) -> dict:
        return self.op("create_room_from_yaml", yaml=yaml, inputs=inputs)

    def run_template(self, template_id: str, inputs: dict) -> dict:
        return self.op("run_template", template_id=template_id, inputs=inputs)

    def room_doc(self, room_id: str, name: str, content: str) -> str:
        with self._lock:
            info = self.op("connect_to_room", room_id=room_id)  # also lists the room's documents
            self._room = room_id
            self._learn(info)
            for d in info.get("documents", []):
                if d.get("name") == name:
                    self.op("update_room_document", document_id=d["id"], content=content)
                    return d["id"]
            return self.op("create_room_document", name=name, description=name,
                           instructions="Maintained by Chief.", content=content)["document_id"]


def _from_env() -> SwitchClient:
    cfg = json.load(open(".chief.json")) if os.path.exists(".chief.json") else {}

    def g(k):
        return os.environ.get(k) or cfg.get(k.lower())

    return SwitchClient(g("SWITCH_URL"), g("SWITCH_API_KEY"), g("SWITCH_AGENT_ID"))


if __name__ == "__main__":
    argparse.ArgumentParser().add_argument("--tail", action="store_true")
    for m in _from_env().events():
        print(m, flush=True)
