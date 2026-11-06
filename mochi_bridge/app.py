"""Fake Slack gateway: implements Mochi's /internal/* contract and posts alerts into Switch rooms."""
import hashlib
import hmac
import inspect
import json
import os
import time

from fastapi import FastAPI, HTTPException, Request

WINDOW_SEC = 300
CHANNEL_MAP = {"C0SWITCH01": "#product", "C0SWITCH02": "#leadership"}  # Slack channel id -> Switch room name


def sign(secret: str, ts: int | str, body: str) -> str:
    return "v0=" + hmac.new(secret.encode(), f"v0:{ts}:{body}".encode(), hashlib.sha256).hexdigest()


def format_card(card: dict) -> str:
    nums = ", ".join(
        f"{n.get('label', '')}: {n.get('value', '')}" + (f" ({n['change']})" if n.get("change") else "")
        for n in card.get("keyNumbers") or []
    )
    parts = [f"🔔 {card.get('headline', '')}", card.get("body"), nums, (card.get("artifact") or {}).get("url")]
    return "\n".join(p for p in parts if p)


def create_app(switch, secret: str, channel_map: dict[str, str]) -> FastAPI:
    """switch: any object with .post(room_id, body, thread_id=None). channel_map: Slack channel id -> room id."""
    app = FastAPI()

    async def verified(request: Request) -> dict:
        raw = (await request.body()).decode()
        ts = request.headers.get("x-mochi-timestamp", "")
        sig = request.headers.get("x-mochi-signature", "")
        try:
            fresh = abs(time.time() - float(ts)) <= WINDOW_SEC
        except ValueError:
            fresh = False
        # ponytail: no replay cache (the real gateway claims each signature once); add if replays matter
        if not (sig and fresh and hmac.compare_digest(sig, sign(secret, ts, raw))):
            raise HTTPException(401, "bad signature")
        return json.loads(raw)

    @app.post("/internal/slack-channels")
    async def channels(request: Request):
        await verified(request)
        return {
            "status": "ok",
            "channels": [
                {"id": cid, "name": CHANNEL_MAP[cid].lstrip("#"), "members": 4, "isPrivate": False, "sharedExternally": False}
                for cid in channel_map
                if cid in CHANNEL_MAP
            ],
        }

    @app.post("/internal/deliver")
    async def deliver(request: Request):
        d = await verified(request)
        room = channel_map.get(d.get("channelId", ""))
        if room is None:
            return {"status": "failed", "reason": "unknown channel"}
        r = switch.post(room, format_card(d.get("card") or {}))
        if inspect.isawaitable(r):
            await r
        return {"status": "sent", "channel": d["channelId"]}

    return app


if __name__ == "__main__":
    import uvicorn

    from pathlib import Path

    from chief.switch_client import connect, resolve_rooms

    switch = connect("mochi-alerts", "Posts Mochi automation alerts into Switch rooms",
                     Path(".connector-mochi-alerts.json"))
    found = resolve_rooms(switch, [n.lstrip("#") for n in CHANNEL_MAP.values()])
    ids = {n: found[n.lstrip("#")] for n in CHANNEL_MAP.values() if n.lstrip("#") in found}
    uvicorn.run(
        create_app(switch, os.environ["INTERNAL_HMAC_SECRET"], {c: ids[n] for c, n in CHANNEL_MAP.items() if n in ids}),
        port=4040,
    )
