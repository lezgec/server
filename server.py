from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
import json
import time
from pathlib import Path
from typing import Dict, Set, List

app = FastAPI()

# (opcional) CORS para permitir tu dominio luego; WS no depende mucho de CORS,
# pero sirve para endpoints HTTP si agregas health checks.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

MAX_HISTORY = 50

clients: Dict[str, WebSocket] = {}        # username -> websocket
user_rooms: Dict[str, str] = {}           # username -> room
rooms: Dict[str, Set[str]] = {"general": set()}  # room -> set(usernames)
history_cache: Dict[str, List[dict]] = {}  # room -> last messages in memory


def room_log_path(room: str) -> Path:
    safe = "".join(ch for ch in room if ch.isalnum() or ch in ("-", "_")).strip() or "general"
    return DATA_DIR / f"history_{safe}.jsonl"


def ensure_room(room: str):
    if room not in rooms:
        rooms[room] = set()
    if room not in history_cache:
        history_cache[room] = load_history(room, MAX_HISTORY)


def load_history(room: str, max_items: int) -> List[dict]:
    p = room_log_path(room)
    if not p.exists():
        return []
    try:
        with p.open("r", encoding="utf-8") as f:
            lines = f.readlines()[-max_items:]
        out = []
        for ln in lines:
            ln = ln.strip()
            if ln:
                out.append(json.loads(ln))
        return out
    except Exception:
        return []


def append_history(room: str, payload: dict):
    p = room_log_path(room)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")

    history_cache[room].append(payload)
    if len(history_cache[room]) > MAX_HISTORY:
        history_cache[room].pop(0)


async def send_json(ws: WebSocket, obj: dict):
    await ws.send_text(json.dumps(obj, ensure_ascii=False))


def online_users() -> List[str]:
    return sorted(clients.keys())


def list_rooms() -> List[str]:
    mem_rooms = set(rooms.keys())
    file_rooms = set()
    for f in DATA_DIR.glob("history_*.jsonl"):
        name = f.stem.replace("history_", "")
        if name:
            file_rooms.add(name)
    return sorted(mem_rooms.union(file_rooms))


def room_users(room: str) -> List[str]:
    return sorted(list(rooms.get(room, set())))


async def broadcast(obj: dict, room=None, exclude=None):
    for u, ws in list(clients.items()):
        if exclude and u == exclude:
            continue
        if room and user_rooms.get(u) != room:
            continue
        try:
            await send_json(ws, obj)
        except Exception:
            pass


async def send_history(ws: WebSocket, room: str):
    ensure_room(room)
    for msg in history_cache[room]:
        await send_json(ws, msg)


async def join_room(username: str, new_room: str):
    ensure_room(new_room)
    old_room = user_rooms.get(username, "general")

    if old_room != new_room:
        rooms.setdefault(old_room, set()).discard(username)
        rooms[new_room].add(username)
        user_rooms[username] = new_room

    return old_room, new_room


@app.get("/health")
def health():
    return {"ok": True, "users": len(clients), "rooms": list_rooms()}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()

    username = None
    buffer_room = "general"

    try:
        # primer mensaje debe ser login
        raw = await ws.receive_text()
        msg = json.loads(raw)

        if msg.get("type") != "login":
            await send_json(ws, {"type": "error", "message": "Primero debes iniciar sesión (type=login)."})
            await ws.close()
            return

        requested = (msg.get("username") or "").strip()
        if not requested:
            await send_json(ws, {"type": "error", "message": "Username vacío."})
            await ws.close()
            return

        if requested in clients:
            await send_json(ws, {"type": "error", "message": "Ese username ya está en uso."})
            await ws.close()
            return

        username = requested
        clients[username] = ws

        ensure_room("general")
        rooms["general"].add(username)
        user_rooms[username] = "general"
        buffer_room = "general"

        await send_json(ws, {"type": "login_ok", "username": username, "users": online_users(), "room": "general"})
        await send_history(ws, "general")

        await broadcast(
            {"type": "user_joined", "username": username, "room": "general", "room_users": room_users("general")},
            room="general",
            exclude=username,
        )

        # loop
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            mtype = msg.get("type")
            current_room = user_rooms.get(username, "general")

            if mtype == "message":
                text = (msg.get("text") or "").strip()
                if not text:
                    continue

                payload = {
                    "type": "message",
                    "from": username,
                    "text": text,
                    "ts": int(time.time()),
                    "room": current_room,
                }
                ensure_room(current_room)
                append_history(current_room, payload)
                await broadcast(payload, room=current_room)

            elif mtype == "rooms":
                await send_json(ws, {"type": "rooms", "rooms": list_rooms()})

            elif mtype == "room_who":
                await send_json(ws, {"type": "room_users", "room": current_room, "room_users": room_users(current_room)})

            elif mtype == "join_room":
                new_room = (msg.get("room") or "").strip()
                if not new_room:
                    await send_json(ws, {"type": "error", "message": "Debes indicar una sala válida."})
                    continue

                old_room, new_room = await join_room(username, new_room)
                buffer_room = new_room

                await send_json(ws, {"type": "info", "message": f"Te uniste a la sala '{new_room}'", "room": new_room})
                await send_history(ws, new_room)

                await broadcast(
                    {"type": "user_left_room", "username": username, "room": old_room, "room_users": room_users(old_room)},
                    room=old_room,
                )
                await broadcast(
                    {"type": "user_joined_room", "username": username, "room": new_room, "room_users": room_users(new_room)},
                    room=new_room,
                    exclude=username,
                )

            else:
                await send_json(ws, {"type": "error", "message": f"Tipo no soportado: {mtype}"})

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        if username:
            clients.pop(username, None)
            room = user_rooms.get(username, "general")
            user_rooms.pop(username, None)
            rooms.get(room, set()).discard(username)

            try:
                await broadcast(
                    {"type": "user_left", "username": username, "room": room, "room_users": room_users(room)},
                    room=room,
                )
            except Exception:
                pass