import os
import time
from typing import Optional, Dict, Any, List

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(title="HEMS Cloud API")

# ====== Simple shared-secret auth ======
# Set these in Render Environment variables
DEVICE_TOKEN = os.getenv("DEVICE_TOKEN", "dev_device_token_change_me")
APP_TOKEN = os.getenv("APP_TOKEN", "dev_app_token_change_me")


def require_bearer(auth_header: Optional[str], expected_token: str):
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    token = auth_header.split(" ", 1)[1].strip()
    if token != expected_token:
        raise HTTPException(status_code=403, detail="Invalid token")


# ====== Data models ======
class TelemetryIn(BaseModel):
    device_id: str = Field(..., examples=["uno_r4_001"])
    timestamp_ms: int = Field(..., examples=[1700000000000])
    voltage_v: float
    current_a: float
    power_w: float
    extra: Dict[str, Any] = Field(default_factory=dict)


class CommandIn(BaseModel):
    device_id: str
    command: str = Field(..., examples=["SET_RELAY"])
    args: Dict[str, Any] = Field(default_factory=dict)


class CommandAckIn(BaseModel):
    device_id: str
    command_id: str
    status: str = Field(..., examples=["OK", "FAILED"])
    detail: str = ""


# ====== In-memory state (OK for prototype; upgrade to Postgres later) ======
latest_state: Dict[str, Any] = {}
pending_commands: Dict[str, List[Dict[str, Any]]] = {}


def parse_relay_bool(extra: Dict[str, Any], relay_key: str, fallback_key: str) -> bool:
    """
    Accepts relay values as:
    - "1" / "0"
    - 1 / 0
    - True / False
    - "true" / "false"
    """
    raw = extra.get(relay_key, extra.get(fallback_key, 0))

    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return int(raw) == 1
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "on"}

    return False


def default_relays() -> Dict[str, bool]:
    return {
        "relay_1": False,
        "relay_2": False,
        "relay_3": False,
    }


@app.get("/healthz")
def healthz():
    return {"ok": True, "time": int(time.time())}


@app.post("/api/telemetry")
def post_telemetry(payload: TelemetryIn, authorization: Optional[str] = Header(None)):
    require_bearer(authorization, DEVICE_TOKEN)

    st = latest_state.get(payload.device_id, {})
    st["telemetry"] = payload.model_dump()
    st["last_seen"] = int(time.time())

    extra = payload.extra or {}
    st["relays"] = {
        "relay_1": parse_relay_bool(extra, "relay_1", "l1"),
        "relay_2": parse_relay_bool(extra, "relay_2", "l2"),
        "relay_3": parse_relay_bool(extra, "relay_3", "l3"),
    }
    st["relay_source"] = "telemetry"

    latest_state[payload.device_id] = st
    return {"ok": True}


@app.get("/api/state")
def get_state(device_id: str, authorization: Optional[str] = Header(None)):
    require_bearer(authorization, APP_TOKEN)

    st = latest_state.get(device_id)
    if not st:
        return {
            "device_id": device_id,
            "online": False,
            "relays": default_relays(),
            "relay_source": "default",
        }

    last_seen = st.get("last_seen", 0)
    online = (int(time.time()) - last_seen) < 10

    return {
        "device_id": device_id,
        "online": online,
        **st,
    }


@app.post("/api/command")
def post_command(cmd: CommandIn, authorization: Optional[str] = Header(None)):
    require_bearer(authorization, APP_TOKEN)

    if cmd.command == "SET_RELAY":
        relay = cmd.args.get("relay")
        state = cmd.args.get("state")

        if relay not in [1, 2, 3]:
            raise HTTPException(status_code=400, detail="relay must be 1, 2, or 3")
        if state not in [0, 1]:
            raise HTTPException(status_code=400, detail="state must be 0 or 1")

        # Immediately update remembered relay state so the app reflects
        # the last commanded state even before telemetry arrives.
        st = latest_state.get(cmd.device_id, {})
        st.setdefault("relays", default_relays())
        st["relays"][f"relay_{relay}"] = (state == 1)
        st["last_seen"] = int(time.time())
        st["relay_source"] = "command"
        latest_state[cmd.device_id] = st

    cmd_id = f"{int(time.time() * 1000)}_{cmd.device_id}"
    entry = {
        "id": cmd_id,
        "command": cmd.command,
        "args": cmd.args,
        "ts": int(time.time()),
    }
    pending_commands.setdefault(cmd.device_id, []).append(entry)
    return {"ok": True, "command_id": cmd_id}


@app.get("/api/commands/next")
def get_next_command(device_id: str, authorization: Optional[str] = Header(None)):
    require_bearer(authorization, DEVICE_TOKEN)

    q = pending_commands.get(device_id, [])
    if not q:
        return {"has_command": False}

    nxt = q.pop(0)  # FIFO
    return {"has_command": True, "command": nxt}


@app.post("/api/commands/ack")
def post_ack(ack: CommandAckIn, authorization: Optional[str] = Header(None)):
    require_bearer(authorization, DEVICE_TOKEN)

    st = latest_state.get(ack.device_id, {})
    st["last_ack"] = ack.model_dump()
    st["last_seen"] = int(time.time())
    latest_state[ack.device_id] = st
    return {"ok": True}
