# SmartTraffic — Maqueta inteligente de tráfico (ESP32 + FastAPI + MQTT + SQLite)

Sistema completo para una maqueta física en miniatura con 4 accesos (Norte, Sur, Este, Oeste),
sensores ultrasónicos HC-SR04 que simulan la llegada de carritos, semáforos LED, y un
dashboard web local en tiempo real.

```
 [Maqueta]                         [Tu PC]
 ESP32 ──WiFi (hotspot)──► Mosquitto (MQTT) ◄── FastAPI ──► SQLite
   ▲                                              │
   └──────── comandos de luces ◄──────────────────┘
                                                   └──WebSocket──► Navegador (dashboard)
```

## Estructura del proyecto

```
SemaforosInteligentes/
├── esp32/SmartTraffic_ESP32/SmartTraffic_ESP32.ino   ← PARTE 1 (firmware)
├── server/
│   ├── main.py            ← PARTE 2 (FastAPI + MQTT + lógica + WebSockets)
│   ├── config.py          ← configuración central (IP, tiempos, tópicos)
│   ├── db.py              ← capa SQLite
│   ├── requirements.txt
│   ├── iniciar_servidor.bat
│   └── static/index.html  ← PARTE 3 (dashboard)
└── README.md              ← este archivo
```

---

# PARTE 1 — Firmware del ESP32

## 1.1 Tabla de pines (obligatoria)

| ACCESO | GPIO TRIG | GPIO ECHO | GPIO ROJO | GPIO AMARILLO | GPIO VERDE |
|--------|-----------|-----------|-----------|---------------|------------|
| NORTE  | 13        | 34        | 32        | 33            | 25         |
| SUR    | 26        | 35        | 27        | 14            | 5          |
| ESTE   | 4         | 36        | 21        | 19            | 18         |
| OESTE  | 17        | 39        | 22        | 23            | 16         |

Notas:
- **GPIO 34/35/36/39 son solo entrada** → perfectos para ECHO.
- No se usan los pines de arranque (0, 2, 12, 15), ni UART (1, 3), ni de la flash (6–11).
- **VCC** de los 4 HC-SR04 → pin **5V (VIN)** del ESP32. **GND común** en toda la maqueta.
- **ECHO entrega 5V** y el ESP32 es de 3.3V: usa un **divisor de voltaje** (1 kΩ + 2 kΩ, o 10 kΩ + 20 kΩ)
  entre el cable ECHO y el GPIO del ESP32. Sin el divisor "funciona" muchas veces, pero daña el pin a largo plazo.
- LEDs con **resistencia de 220 Ω**. Usa módulos de **ánodo común** (lógica ALTA = encendido).

## 1.2 Código

Archivo: `esp32/SmartTraffic_ESP32/SmartTraffic_ESP32.ino`

Antes de programar, edita estas 4 líneas al inicio del archivo:

```cpp
const char* WIFI_SSID     = "Aqui_el_SSID_del_Hotspot";
const char* WIFI_PASSWORD = "Aqui_la_password";
const char* MQTT_HOST     = "192.168.137.1";   // IP de la PC (ver paso 3.3)
const uint16_t MQTT_PORT  = 1883;
```

Qué hace el firmware:
1. Se conecta al hotspot por Wi-Fi y al broker MQTT.
2. Lee los 4 sensores en modo *round-robin* (uno cada 200 ms) con histéresis y antirrebote.
3. Cuenta los carritos: cuando un objeto pasa de >25 cm a ≤25 cm se suma 1 vehículo.
4. Publica telemetría cada 2 s en `smarttraffic/telemetry` y eventos en `smarttraffic/events`.
5. Se suscribe a `smarttraffic/control` y `smarttraffic/commands` para recibir el estado de las luces.
6. **Seguridad**: si el servidor deja de responder por más de 20 s, el ESP32 opera solo con un
   ciclo local fijo (verde 8 s / amarillo 2 s) para que la maqueta nunca se quede congelada.

### Tópicos MQTT (contrato de comunicación)

| Tópico | Dirección | Ejemplo |
|--------|-----------|---------|
| `smarttraffic/telemetry` | ESP32 → servidor | `{"ts":123,"mode":"auto","norte":{"d":12.4,"v":3,"l":"green","o":1}, ...}` |
| `smarttraffic/events` | ESP32 → servidor | `{"type":"vehicle","access":"norte","vehicles":3,"distance":12.4,"ts":123}` |
| `smarttraffic/control` | servidor → ESP32 | `{"mode":"auto","lights":{"norte":"green","sur":"red","este":"red","oeste":"red"}}` |
| `smarttraffic/commands` | servidor → ESP32 | `{"access":"norte","color":"yellow"}` o `{"all":"red"}` o `{"mode":"manual"}` |

Claves del JSON de telemetría: `d` = distancia cm (-1 = sin dato), `v` = vehículos acumulados,
`l` = color del semáforo, `o` = 1 si hay carrito esperando.

---

# PARTE 2 — Backend (FastAPI + MQTT + SQLite)

Archivos: `server/main.py`, `server/config.py`, `server/db.py`

Qué hace:
1. **Cliente MQTT** integrado (hilo propio con paho-mqtt): recibe telemetría y eventos del ESP32.
2. **SQLite**: guarda cada muestra de telemetría y cada evento (`smarttraffic.db`).
3. **Lógica automática**: máquina de estados verde → amarillo → verde, que elige el próximo
   acceso por densidad (llegadas en los últimos 60 s + presencia actual), con tiempo mínimo
   de verde (6 s) y máximo (15 s) para equidad.
4. **API REST** + **WebSocket** para el navegador.
5. Publica el estado de las luces al ESP32 cada 2 s y en cada cambio.

### Endpoints

| Método | Ruta | Descripción |
|--------|------|-------------|
| GET | `/` | Dashboard web |
| GET | `/api/state` | Estado completo en JSON |
| GET | `/api/history?limit=100` | Últimas filas de telemetría |
| GET | `/api/events?limit=50` | Últimos eventos |
| GET | `/api/metrics` | Métricas por acceso |
| POST | `/api/mode` | `{"mode":"auto"}` o `{"mode":"manual"}` |
| POST | `/api/light` | `{"access":"norte","color":"green"}` (solo en manual) |
| POST | `/api/all-red` | Todos en rojo (solo en manual) |
| POST | `/api/reset` | Reinicia contadores |
| WS | `/ws` | Flujo en tiempo real (`state` y `event`) |

### Configuración (`server/config.py`)

```python
MQTT_HOST = "127.0.0.1"   # solo cámbialo si el broker está en otra máquina
MIN_GREEN_S, MAX_GREEN_S, YELLOW_S = 6.0, 15.0, 2.0
```

---

# PARTE 3 — Dashboard (frontend)

Archivo: `server/static/index.html` (HTML + JS puros, sin internet ni CDN).

- Se conecta por **WebSocket** a `/ws` y se reconecta solo si se cae.
- Muestra los 4 semáforos, distancia, vehículos, densidad y el log de eventos.
- Botones **Automático / Manual**; en modo manual aparecen botones R/A/V por acceso,
  "Todos en ROJO" y "Reiniciar contadores".

---

# Instalación y puesta en marcha (paso a paso para principiantes)

## Paso 0 — Material

- 1 × placa ESP32 (WROOM / DevKit) + cable micro-USB.
- 4 × HC-SR04 + 12 × LED (rojo/amarillo/verde) + 12 × resistencia 220 Ω
  (o 4 módulos semáforo de 3 LEDs, ánodo común).
- 8 × resistencia para divisores de voltaje (1 kΩ y 2 kΩ, o 10 kΩ y 20 kΩ).
- Protoboard y cables jumper. Alimentación por el cable USB de la placa.

## Paso 1 — Instalar software en la PC

1. **Arduino IDE 2.x** → <https://www.arduino.cc/en/software>
2. **Driver USB-serial** de tu placa (CH340 o CP210x). Si al conectar aparece un puerto
   COM nuevo en `Herramientas → Puerto`, ya está instalado.
3. **Soporte ESP32 en Arduino IDE**:
   - `Archivo → Preferencias → URLs de gestor de tarjetas adicionales` y pega:
     `https://espressif.github.io/arduino-esp32/package_esp32_index.json`
   - `Herramientas → Placa → Gestor de tarjetas` → busca "esp32" → instala **ESP32 by Espressif Systems**.
4. **Librerías** (`Herramientas → Administrar bibliotecas`):
   - `PubSubClient` de Nick O'Leary
   - `ArduinoJson` de Benoit Blanchon (**versión 6.21.x**)
5. **Python 3.10 o superior** → <https://www.python.org/downloads/>
   - ⚠️ En el instalador marca **"Add Python to PATH"**.
6. **Mosquitto (broker MQTT)** → <https://mosquitto.org/download/)
   - En Windows instala con "normal installation" (queda como servicio automático).
7. *(Opcional)* Visual Studio Code para editar los archivos con comodidad.

Verifica la instalación abrienda `Símbolo del sistema` y ejecutando:
```
python --version
pip --version
```

## Paso 2 — Configurar Mosquitto para aceptar al ESP32

Mosquitto 2.x por defecto solo escucha en localhost, y el ESP32 viene de fuera.
Edita `C:\Program Files\mosquitto\mosquitto.conf` y deja al final del archivo:

```
listener 1883
allow_anonymous true
```

Luego reinicia el servicio: `Herramientas de administración → Servicios → Mosquitto → Reiniciar`
(o en consola como administrador: `net stop mosquitto` y `net start mosquitto`).

Permite el puerto en el firewall de Windows: cuando Windows pregunte, acepta para
**Redes privadas**, o ejecuta en consola **como administrador**:
```
netsh advfirewall firewall add rule name="Mosquitto 1883" dir=in action=allow protocol=TCP localport=1883
netsh advfirewall firewall add rule name="SmartTraffic 8000" dir=in action=allow protocol=TCP localport=8000
```

## Paso 3 — Red (hotspot) y dirección de la PC

**Opción A — Hotspot de Windows:**
1. `Configuración → Red e Internet → Hotspot móvil` → actívalo (nota el SSID y la contraseña).
2. La PC queda con la IP **192.168.137.1** → deja `MQTT_HOST = "192.168.137.1"` en el `.ino`.

**Opción B — Hotspot del celular:**
1. Activa el hotspot del celular y conecta **también la PC** a esa misma red Wi-Fi.
2. Descubre la IP de la PC: `ipconfig` en el símbolo del sistema → IPv4 (algo como 192.168.43.x).
3. Pon esa IP en `MQTT_HOST`.

Comprueba la IP con:
```
ipconfig
```

## Paso 4 — Programar el ESP32

1. Abre `esp32/SmartTraffic_ESP32/SmartTraffic_ESP32.ino` en Arduino IDE.
2. Edita `WIFI_SSID`, `WIFI_PASSWORD` y `MQTT_HOST`.
3. Selecciona la placa: `Herramientos → Placa → esp32 → ESP32 Dev Module` y el puerto COM.
4. Pulsa **Verificar** y luego **Subir**.
5. Abre el Monitor Serial a **115200 baudios**: debe decir `WiFi OK -> IP local: ...`.
   - Si ves `[MQTT] conectado al broker`, el firmware ya está publicando.

## Paso 5 — Arrancar el backend

**Forma fácil:** doble clic en `server\iniciar_servidor.bat`
(crea un entorno virtual `.venv`, instala las dependencias la primera vez y arranca el servidor).

**Forma manual:**
```
cd server
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m uvicorn main:app --host 0.0.0.0 --port 8000
```

En la consola deberías ver:
```
[DB] base de datos lista: smarttraffic.db
[MQTT] conectado al broker
```

## Paso 6 — Abrir el dashboard

En el navegador (en la misma PC o en el celular usando `http://IP-de-la-PC:8000`):

```
http://localhost:8000
```

Debes ver el punto verde **ESP32: en línea**. Si está en rojo, revisa el Monitor Serial
del ESP32 (Wi-Fi/MQTT) y que el IP de `MQTT_HOST` sea correcto.

## Paso 7 — Probar la maqueta

1. **Modo automático (por defecto)**: acerca la mano o un objeto a un sensor.
   En el dashboard verás bajar la distancia, subir los vehículos y, en unos segundos,
   el semáforo de ese acceso cambiará a verde (primero amarillo 2 s).
2. **Modo manual**: pulsa "Manual" → aparecen los botones R/A/V de cada semáforo
   y "Todos en ROJO".
3. Pulsa "Reiniciar contadores" para empezar una nueva prueba.
4. Los datos históricos quedan en `server\smarttraffic.db`
   (puedes inspeccionarlos con DB Browser for SQLite).

---

# Solución de problemas

| Síntoma | Causa probable | Solución |
|---|---|---|
| ESP32 no conecta al Wi-Fi | SSID/contraseña mal escritos | Revisa el Monitor Serial, vuelve a grabar |
| `[MQTT] error rc=-2` | IP incorrecta o broker apagado | Verifica `MQTT_HOST` con `ipconfig` y el servicio Mosquitto |
| Mosquitto no acepta al ESP32 | Solo escucha en localhost | Agrega `listener 1883` + `allow_anonymous true` y reinicia |
| Dashboard dice "sin telemetria" | Firewall bloquea, o IP del hotspot equivocada | Abre los puertos 1883/8000, prueba con la IP del paso 3 |
| El sensor mide siempre 0 | Echo sin divisor de voltaje o cables invertidos | Coloca el divisor 1k/2k, revisa GND común |
| Un semáforo no cambia | LED mal cableado o GPIO ocupado | Sigue la tabla de pines de la Parte 1 |
| `StaticJsonDocument` da error | Tienes ArduinoJson 7 | Instala ArduinoJson **6.21.x** desde el gestor de librerías |

# Ideas para ampliar

- Agregar un cuarto carril peatonal con botón.
- Dashboard con mapa de la maqueta y animación de carritos.
- Historial con gráficas (Chart.js servido en local).
- Cambiar el ESP32 por ESP32-CAM para ver la intersección en video.
