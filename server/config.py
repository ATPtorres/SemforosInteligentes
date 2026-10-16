"""
SmartTraffic - Configuracion central del backend.
Cambia aqui el IP/host del broker MQTT si es necesario.
"""

# ------------------------- BROKER MQTT (Mosquitto) -------------------------
MQTT_HOST = "127.0.0.1"      # si Mosquitto corre en esta misma PC
MQTT_PORT = 1883
MQTT_USER = ""               # vacio = sin autenticacion
MQTT_PASSWORD = ""
MQTT_CLIENT_ID = "smarttraffic-server"

# ------------------------------ TOPICOS ------------------------------------
MQTT_TELEMETRY_TOPIC = "smarttraffic/telemetry"   # ESP32 -> servidor
MQTT_EVENTS_TOPIC    = "smarttraffic/events"      # ESP32 -> servidor
MQTT_CONTROL_TOPIC   = "smarttraffic/control"     # servidor -> ESP32
MQTT_COMMAND_TOPIC   = "smarttraffic/commands"    # servidor -> ESP32

# ------------------------------- BASE DE DATOS -----------------------------
DB_PATH = "smarttraffic.db"

# ------------------------------- SEMAFOROS ---------------------------------
ACCESS_IDS = ["norte", "sur", "este", "oeste"]
ACCESS_NAMES = {"norte": "Norte", "sur": "Sur", "este": "Este", "oeste": "Oeste"}

VALID_LIGHTS = ("red", "yellow", "green")
VALID_MODES = ("auto", "manual")

# ------------------------- LOGICA DE CONTROL AUTO --------------------------
MIN_GREEN_S = 6.0        # tiempo minimo de verde antes de poder cambiar
MAX_GREEN_S = 15.0       # tiempo maximo de verde (luego rota por equidad)
YELLOW_S = 2.0           # duracion del amarillo de transicion
SWITCH_MARGIN = 1.0      # cuanta mas densidad se necesita para robar el verde
WINDOW_S = 60            # ventana para medir llegadas de vehiculos
ONLINE_TIMEOUT_S = 10.0  # sin telemetria en este tiempo el ESP32 esta "offline"
CONTROL_PUBLISH_S = 2.0  # reenvio periodico del estado de luces al ESP32
