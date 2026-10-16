"""
PARTE 2 - BACKEND SMARTTRAFFIC (FastAPI + MQTT + SQLite + WebSockets)
=====================================================================
Que hace este servidor:
  1. Se suscribe al broker MQTT (Mosquitto) y recibe la telemetria del ESP32.
  2. Guarda todo en SQLite (telemetria + eventos de vehiculos).
  3. Ejecuta la logica AUTOMATICA de semaforos segun la densidad de trafico.
  4. Publica en MQTT el estado de las luces para que el ESP32 las encienda.
  5. Expone una API REST + un WebSocket para el dashboard en tiempo real.

Ejecutar:
    python -m uvicorn main:app --host 0.0.0.0 --port 8000
"""

import asyncio
import json
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Set

import paho.mqtt.client as mqtt
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

import config
import db

BASE_DIR = Path(__file__).resolve().parent

# =============================== ESTADO GLOBAL ==============================
# El hilo de MQTT (paho) escribe aqui; el hilo de FastAPI lee/escribe tambien.
# Por eso TODOS los accesos a STATE se hacen con state_lock.

state_lock = threading.RLock()

STATE: Dict[str, Any] = {
    "mode": "auto",                 # auto | manual (autoridad = servidor)
    "reported_mode": "auto",        # modo que reporta el ESP32 (diagnostico)
    "online": False,                # el ESP32 esta mandando telemetria?
    "last_telemetry_ts": 0.0,       # epoch del ultimo paquete recibido
    "phase": "green",               # fase del ciclo automatico
    "phase_started": 0.0,           # epoch en que empezo la fase actual
    "green_access": "norte",        # cual acceso tiene el verde
    "next_access": "sur",           # cual accedera al verde tras el amarillo
    "access": {
        a: {
            "light": "red",
            "distance": None,       # cm (None = sin dato)
            "vehicles": 0,          # acumulado de la sesion
            "occupied": False,      # hay carrito esperando
            "arrivals_60s": 0,      # llegadas en la ultima ventana
            "last_change": None,    # ISO8601 del ultimo cambio de luz
        }
        for a in config.ACCESS_IDS
    },
}

# llegadas recientes por acceso (para la ventana de densidad)
arrivals: Dict[str, List[float]] = {a: [] for a in config.ACCESS_IDS}

# clientes WebSocket conectados
ws_clients: Set[WebSocket] = set()

# cola de mensajes para reenviar desde el hilo MQTT al event loop de asyncio
loop_ref: asyncio.AbstractEventLoop | None = None
out_queue: asyncio.Queue | None = None
mqtt_client: mqtt.Client | None = None
last_control_publish = 0.0


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ================================ SNAPSHOTS =================================

def snapshot() -> Dict[str, Any]:
    """Estado completo para la API / el WebSocket (copia, no referencia)."""
    with state_lock:
        age = time.time() - STATE["last_telemetry_ts"]
        STATE["online"] = STATE["last_telemetry_ts"] > 0 and age <= config.ONLINE_TIMEOUT_S
        last_ts = STATE["last_telemetry_ts"]
        data = {
            "type": "state",
            "mode": STATE["mode"],
            "reported_mode": STATE.get("reported_mode", "auto"),
            "online": STATE["online"],
            "phase": STATE["phase"],
            "green": STATE["green_access"],
            "server_time": now_iso(),
            "last_telemetry": (
                datetime.fromtimestamp(last_ts).isoformat(timespec="seconds")
                if last_ts > 0 else None
            ),
            "access": {},
            "totals": {},
        }
        total_vehicles = 0
        for a in config.ACCESS_IDS:
            acc = STATE["access"][a]
            total_vehicles += acc["vehicles"]
            data["access"][a] = {
                "name": config.ACCESS_NAMES[a],
                "light": acc["light"],
                "distance": acc["distance"],
                "vehicles": acc["vehicles"],
                "occupied": acc["occupied"],
                "arrivals_60s": acc["arrivals_60s"],
                "last_change": acc["last_change"],
            }
        data["totals"]["vehicles"] = total_vehicles
    return data


def push_state() -> None:
    """Envia el snapshot a todos los navegadores conectados (thread-safe)."""
    if loop_ref is None or out_queue is None:
        return
    snap = snapshot()
    loop_ref.call_soon_threadsafe(out_queue.put_nowait, snap)


def push_event(kind: str, access: str | None, detail: str) -> None:
    if loop_ref is None or out_queue is None:
        return
    msg = {"type": "event", "kind": kind, "access": access,
           "detail": detail, "ts": now_iso()}
    loop_ref.call_soon_threadsafe(out_queue.put_nowait, msg)


# ============================== CONTROL DE LUCES ============================

def set_light(access: str, color: str) -> bool:
    """Cambia la luz de un acceso. Devuelve True si hubo cambio real."""
    with state_lock:
        acc = STATE["access"][access]
        if acc["light"] == color:
            return False
        acc["light"] = color
        acc["last_change"] = now_iso()
    return True


def set_lights(mapping: Dict[str, str]) -> bool:
    changed = False
    with state_lock:
        for a, c in mapping.items():
            acc = STATE["access"][a]
            if acc["light"] != c:
                acc["light"] = c
                acc["last_change"] = now_iso()
                changed = True
    return changed


def publish_control(force: bool = False) -> None:
    """Publica el estado completo de las luces + modo hacia el ESP32."""
    global last_control_publish
    now = time.time()
    if not force and (now - last_control_publish) < config.CONTROL_PUBLISH_S:
        return
    last_control_publish = now
    if mqtt_client is None:
        return
    with state_lock:
        msg = {
            "mode": STATE["mode"],
            "lights": {a: STATE["access"][a]["light"] for a in config.ACCESS_IDS},
        }
    mqtt_client.publish(config.MQTT_CONTROL_TOPIC, json.dumps(msg))


# ============================ LOGICA AUTOMATICA =============================

def compute_scores() -> Dict[str, float]:
    """Puntaje de demanda por acceso: llegadas recientes + presencia actual."""
    prune_window()
    with state_lock:
        scores = {}
        for a in config.ACCESS_IDS:
            acc = STATE["access"][a]
            score = float(acc["arrivals_60s"])
            if acc["occupied"]:
                score += 2.0
            scores[a] = score
    return scores


def prune_window() -> None:
    limit = time.time() - config.WINDOW_S
    with state_lock:
        for a in config.ACCESS_IDS:
            lst = arrivals[a]
            while lst and lst[0] < limit:
                lst.pop(0)
            STATE["access"][a]["arrivals_60s"] = len(lst)


def step_auto() -> bool:
    """Un paso del semaforo automatico. Devuelve True si cambio algo."""
    now = time.time()
    with state_lock:
        if STATE["mode"] != "auto":
            return False
        phase = STATE["phase"]
        started = STATE["phase_started"]
        current = STATE["green_access"]

    # --- fase amarillo: esperar y luego darle el verde al siguiente ---------
    if phase == "yellow":
        if now - started >= config.YELLOW_S:
            with state_lock:
                target = STATE["next_access"]
            set_lights({a: ("green" if a == target else "red")
                        for a in config.ACCESS_IDS})
            with state_lock:
                STATE["phase"] = "green"
                STATE["phase_started"] = now
                STATE["green_access"] = target
            print(f"[AUTO] verde -> {target.upper()}")
            return True
        return False

    # --- fase verde: evaluar si conviene cambiar ---------------------------
    elapsed = now - started
    if elapsed < config.MIN_GREEN_S:
        return False

    scores = compute_scores()
    best = max(scores, key=scores.get)

    # sin demanda en ninguna parte: rotacion por equidad
    if all(v <= 0 for v in scores.values()) and elapsed >= config.MAX_GREEN_S:
        idx = config.ACCESS_IDS.index(current)
        best = config.ACCESS_IDS[(idx + 1) % len(config.ACCESS_IDS)]

    if best == current:
        return False

    interesting = elapsed >= config.MAX_GREEN_S or \
        scores[best] >= scores[current] + config.SWITCH_MARGIN
    if not interesting:
        return False

    with state_lock:
        STATE["phase"] = "yellow"
        STATE["phase_started"] = now
        STATE["next_access"] = best
    set_lights({a: ("yellow" if a == current else "red")
                for a in config.ACCESS_IDS})
    print(f"[AUTO] {current.upper()} -> amarillo (luego {best.upper()})")
    return True


# ================================== MQTT ===================================

def mqtt_ok(rc: Any) -> bool:
    if isinstance(rc, int):
        return rc == 0
    return not getattr(rc, "is_failure", True)


def on_connect(client, userdata, flags, reason_code, properties=None):
    if not mqtt_ok(reason_code):
        print(f"[MQTT] fallo de conexion: {reason_code}")
        return
    print("[MQTT] conectado al broker")
    client.subscribe(config.MQTT_TELEMETRY_TOPIC)
    client.subscribe(config.MQTT_EVENTS_TOPIC)
    db.save_event("server", None, "Servidor conectado al broker MQTT")
    publish_control(force=True)


def on_disconnect(client, userdata, *args):
    # paho 1.x: (rc,)   |   paho 2.x: (flags, reason_code, properties)
    rc = args[0] if len(args) == 1 else args[1]
    print(f"[MQTT] desconectado del broker ({rc})")
    with state_lock:
        STATE["online"] = False


def on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode("utf-8", errors="ignore"))
    except Exception:
        return

    if msg.topic == config.MQTT_TELEMETRY_TOPIC:
        handle_telemetry(payload)
    elif msg.topic == config.MQTT_EVENTS_TOPIC:
        handle_event(payload)


def normalize_distance(raw: Any) -> float | None:
    """El ESP32 manda -1 cuando no hay eco (nada en rango)."""
    try:
        d = float(raw)
    except (TypeError, ValueError):
        return None
    return d if d >= 0 else None


def handle_telemetry(payload: Dict[str, Any]) -> None:
    now = time.time()

    with state_lock:
        STATE["last_telemetry_ts"] = now
        # El servidor (dashboard) es la AUTORIDAD del modo; el ESP32 solo
        # reporta cual esta aplicando, aqui lo guardamos para diagnostico.
        reported = payload.get("mode")
        if reported in config.VALID_MODES:
            STATE["reported_mode"] = reported
        current_mode = STATE["mode"]

    for a in config.ACCESS_IDS:
        block = payload.get(a)
        if not isinstance(block, dict):
            continue
        distance = normalize_distance(block.get("d"))
        vehicles = int(block.get("v") or 0)
        light = block.get("l") if block.get("l") in config.VALID_LIGHTS else "red"
        occupied = bool(block.get("o"))

        with state_lock:
            acc = STATE["access"][a]
            was_occupied = acc["occupied"]
            acc["distance"] = distance
            acc["vehicles"] = vehicles
            acc["occupied"] = occupied
            # en modo manual el servidor refleja lo que mando el ESP32;
            # en auto la autoridad es la maquina de estados de aqui arriba.
            if current_mode == "manual" and acc["light"] != light:
                acc["light"] = light
                acc["last_change"] = now_iso()

        # cada transicion a "ocupado" cuenta como una llegada en la ventana
        if occupied and not was_occupied:
            arrivals[a].append(now)

        db.save_telemetry(a, distance, vehicles, light, int(occupied), current_mode)

    prune_window()
    push_state()


def handle_event(payload: Dict[str, Any]) -> None:
    if payload.get("type") != "vehicle":
        return
    access = payload.get("access")
    vehicles = payload.get("vehicles")
    detail = f"Vehiculo detectado en {access} (total {vehicles})"
    print(f"[EVENT] {detail}")
    db.save_event("vehicle", access, detail)
    push_event("vehicle", access, detail)


def start_mqtt() -> None:
    global mqtt_client
    try:
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id=config.MQTT_CLIENT_ID
        )
    except AttributeError:  # paho-mqtt 1.6
        client = mqtt.Client(client_id=config.MQTT_CLIENT_ID)

    if config.MQTT_USER:
        client.username_pw_set(config.MQTT_USER, config.MQTT_PASSWORD)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=15)

    try:
        client.connect(config.MQTT_HOST, config.MQTT_PORT, keepalive=30)
    except Exception as exc:
        print(f"[MQTT] no se pudo conectar a {config.MQTT_HOST}:{config.MQTT_PORT} -> {exc}")
        print("[MQTT] se reintenta automaticamente...")

    client.loop_start()          # hilo de red propio (no bloquea a FastAPI)
    mqtt_client = client


def stop_mqtt() -> None:
    global mqtt_client
    if mqtt_client is not None:
        mqtt_client.loop_stop()
        try:
            mqtt_client.disconnect()
        except Exception:
            pass
        mqtt_client = None


# ============================= TAREAS ASYNCIO ===============================

async def broadcast_task() -> None:
    """Toma mensajes de la cola y los envia a todos los navegadores."""
    assert out_queue is not None
    while True:
        msg = await out_queue.get()
        dead = []
        payload = json.dumps(msg, default=str)
        for ws in list(ws_clients):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            ws_clients.discard(ws)


async def control_loop() -> None:
    """Maquina de estados del semaforo + reenvio periodico al ESP32."""
    pruner = 0.0
    while True:
        await asyncio.sleep(0.25)
        try:
            step_auto()
            publish_control()

            now = time.time()
            if now - pruner >= 60.0:      # limpia la base de datos 1 vez/min
                pruner = now
                db.prune()

            with state_lock:
                dirty = _DIRTY["flag"]
                if dirty:
                    _DIRTY["flag"] = False
            if dirty:
                publish_control(force=True)
                push_state()
        except Exception as exc:          # que un error no mate el bucle
            print(f"[LOOP] error: {exc}")


_DIRTY = {"flag": False}


def mark_dirty() -> None:
    _DIRTY["flag"] = True


# ================================== API ====================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    global loop_ref, out_queue
    loop_ref = asyncio.get_running_loop()
    out_queue = asyncio.Queue()

    db.init(config.DB_PATH)
    print(f"[DB] base de datos lista: {config.DB_PATH}")

    # arranque: todos en rojo, verde inicial en el norte
    set_lights({a: "red" for a in config.ACCESS_IDS})
    with state_lock:
        STATE["green_access"] = "norte"
        STATE["next_access"] = "sur"
        STATE["phase"] = "green"
        STATE["phase_started"] = time.time()
    set_light("norte", "green")

    start_mqtt()
    db.save_event("server", None, "Backend SmartTraffic iniciado")

    tasks = [
        asyncio.create_task(broadcast_task()),
        asyncio.create_task(control_loop()),
    ]
    yield
    for t in tasks:
        t.cancel()
    stop_mqtt()
    db.close()


app = FastAPI(title="SmartTraffic API", version="1.0", lifespan=lifespan)


class ModeBody(BaseModel):
    mode: str = Field(..., examples=["auto"])


class LightBody(BaseModel):
    access: str = Field(..., examples=["norte"])
    color: str = Field(..., examples=["green"])


@app.get("/")
async def index():
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/api/state")
async def api_state():
    return snapshot()


@app.get("/api/history")
async def api_history(limit: int = 100):
    limit = max(1, min(limit, 1000))
    return {"rows": db.recent_telemetry(limit)}


@app.get("/api/events")
async def api_events(limit: int = 50):
    limit = max(1, min(limit, 500))
    return {"events": db.recent_events(limit)}


@app.get("/api/metrics")
async def api_metrics():
    prune_window()
    rows = db.summary(config.WINDOW_S)
    with state_lock:
        vehicles = {a: STATE["access"][a]["vehicles"] for a in config.ACCESS_IDS}
        arrivals_w = {a: STATE["access"][a]["arrivals_60s"] for a in config.ACCESS_IDS}
    return {
        "window_s": config.WINDOW_S,
        "vehicles_total": vehicles,
        "arrivals_window": arrivals_w,
        "db_summary": rows,
    }


@app.post("/api/mode")
async def api_mode(body: ModeBody):
    if body.mode not in config.VALID_MODES:
        raise HTTPException(400, f"mode debe ser uno de {config.VALID_MODES}")
    with state_lock:
        STATE["mode"] = body.mode
        if body.mode == "auto":
            # reinicia el ciclo con el acceso que tenga el verde ahora mismo
            green = STATE["green_access"]
            STATE["phase"] = "green"
            STATE["phase_started"] = time.time()
            idx = config.ACCESS_IDS.index(green)
            STATE["next_access"] = config.ACCESS_IDS[(idx + 1) % len(config.ACCESS_IDS)]
            for a in config.ACCESS_IDS:
                STATE["access"][a]["light"] = "green" if a == green else "red"
                STATE["access"][a]["last_change"] = now_iso()
    db.save_event("mode", None, f"Modo cambiado a {body.mode}")
    mark_dirty()
    publish_control(force=True)
    push_state()
    return {"ok": True, "mode": body.mode}


@app.post("/api/light")
async def api_light(body: LightBody):
    if body.access not in config.ACCESS_IDS:
        raise HTTPException(400, f"access debe ser uno de {config.ACCESS_IDS}")
    if body.color not in config.VALID_LIGHTS:
        raise HTTPException(400, f"color debe ser uno de {config.VALID_LIGHTS}")
    with state_lock:
        if STATE["mode"] != "manual":
            raise HTTPException(409, "Cambia a modo manual para controlar las luces")
    if set_light(body.access, body.color):
        db.save_event("manual", body.access,
                      f"{body.access} -> {body.color} (control manual)")
    mark_dirty()
    publish_control(force=True)
    push_state()
    return {"ok": True, "access": body.access, "color": body.color}


@app.post("/api/all-red")
async def api_all_red():
    with state_lock:
        if STATE["mode"] != "manual":
            raise HTTPException(409, "Cambia a modo manual para controlar las luces")
    set_lights({a: "red" for a in config.ACCESS_IDS})
    db.save_event("manual", None, "Todos los semaforos en ROJO")
    mark_dirty()
    publish_control(force=True)
    push_state()
    return {"ok": True}


@app.post("/api/reset")
async def api_reset():
    with state_lock:
        for a in config.ACCESS_IDS:
            STATE["access"][a]["vehicles"] = 0
    arrivals.clear()
    arrivals.update({a: [] for a in config.ACCESS_IDS})
    prune_window()
    db.save_event("system", None, "Contadores de vehiculos reiniciados")
    push_state()
    return {"ok": True}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    ws_clients.add(ws)
    await ws.send_text(json.dumps(snapshot(), default=str))
    try:
        while True:
            data = await ws.receive_text()
            if data == "ping":
                await ws.send_text('{"type":"ping"}')
    except WebSocketDisconnect:
        pass
    finally:
        ws_clients.discard(ws)


# ================================ MAIN =====================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
