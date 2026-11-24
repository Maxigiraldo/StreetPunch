# Guía del código de StreetPunch

Documento para **entender y defender** todo el código. Explica cada archivo, clase,
método y función, cómo se conectan y dónde está lo importante. Las referencias
`archivo:línea` las genera un script leyendo el código real (si un nombre no
existiera, el documento no se construiría); valen para el commit `8b0ece8`.

## Cómo leerlo

| Si quieres... | Ve a |
| --- | --- |
| La película completa de qué pasa cuando alguien entra, juega o se va | §2 (flujos de ejecución) |
| Defender una decisión concreta | §5 (dónde está lo importante) y §6 (reglas de concurrencia) |
| Saber qué hace un método | §3 (servidor, archivo por archivo) y §4 (cliente) |
| Ubicar cualquier símbolo | Apéndice B (índice con líneas) |
| Saber qué NO está perfecto | §8 (límites honestos) |
| **Entender el conocimiento que hay detrás** (redes, concurrencia, WebSocket, patrones) y qué estudiar | **Anexo: [`docs/conceptos-y-estudio.md`](conceptos-y-estudio.md)** |

## 0. Estado de verificación (corrido al escribir este documento)

| Prueba | Resultado |
| --- | --- |
| `pytest` (unitarias del dominio, framing, codec, sesiones y end-to-end contra un servidor real) | **229 pasan** |
| Arnés de Node del cliente (DOM y WebSocket simulados) | **92 comprobaciones pasan** |
| `tools/scenario_check.py` (física y rondas contra el servidor real) | **11 PASS, 0 FAIL** |
| `tools/churn.py` 10 s con 20 trabajadores | sin problemas; descriptores vuelven a 7, `CLOSE_WAIT` 0 |
| `tools/leakcheck.py` (50 clientes) | sin fugas |
| Log del servidor durante todo lo anterior | 0 trazas de error |

### Lo que esta revisión encontró y corrigió

Al recorrer **todo** el código para escribir este documento se encontró (y se corrigió en el mismo commit):

1. **Dos constantes muertas en `Config`:** `MAILBOX_SIZE` y `TICK_P99_TARGET_MS` no las leía ningún código. El buzón es una casilla
   fija por diseño, así que cambiar `MAILBOX_SIZE` no habría hecho nada: una constante que engaña. Se eliminaron.
2. **Comentarios que seguían describiendo el diseño anterior** (una arena de 12 con cola y espectadores): `config.py`, `physics.py`, `commands.py`,
   `round_states.py`, `codec.py` y `client.js`. Se corrigieron, incluida una cuenta (8 discos en círculo al 60 % de la arena quedan a ~138 px, no a 94).
3. Un comentario de `client.js` estaba pegado a la función equivocada. Se movió.

Quedan **abiertos** (no se tocaron porque son decisiones tuyas o de diseño): los colores repetidos y la cola de entradas compartida (ver §8).

Qué **no** está probado automáticamente: el dibujo en un navegador real (lo probó
Max a mano), el rendimiento en otra máquina y el plan B de despliegue (P9).

## 1. El sistema en una página

StreetPunch es un juego de discos: cada jugador empuja a los demás fuera de una
arena circular. **Lo que se evalúa es el servidor**, así que el diseño gira en
torno a una idea: **un solo hilo, un solo escritor del estado**.

```
 NAVEGADOR                         SERVIDOR (un proceso, un hilo, asyncio)
 ─────────                         ───────────────────────────────────────────────
 client.js ──WebSocket──►  Listener ─► HttpHandshake ─► ClientSession (por cliente)
                              │                          ├─ tarea reader   (lee tramas)
                              │                          ├─ tarea writer   (ÚNICA que escribe el socket)
                              │                          └─ tarea ping
                              │                                │ comandos (cola acotada)
                              ▼                                ▼
                        semáforo 100               ┌──────────────────────────┐
                                                   │ GameSession  =  EL TICK  │  30 veces/s
                                                   │  (único escritor)        │
                                                   │  Hall ─ Room ─ World     │
                                                   │        └ fase (State)    │
                                                   └───────────┬──────────────┘
                                                               │ bytes ya codificados
                                       buzón de tamaño 1 por cliente ◄┘
```

Las cuatro capas (arquitectura **hexagonal**; las dependencias apuntan hacia adentro):

| Capa | Carpeta | Contiene | Sabe de red |
| --- | --- | --- | --- |
| Dominio | `server/domain/` | El juego: `Hall`, `Room`, `World`, fases, física, comandos | **No** (ni `asyncio` ni `socket`) |
| Aplicación | `server/application/` | El tick (`GameSession`), puertos (interfaces), políticas de envío | Solo interfaces |
| Adaptadores | `server/adapters/` | Socket, HTTP, WebSocket, JSON, `ClientSession` | Sí, todo |
| Arranque | `server/main.py` | Construye y conecta los objetos | — |

Las **tres ideas** que lo sostienen (si solo puedes explicar tres cosas, que sean estas):

1. **Único escritor:** solo el tick modifica el vestíbulo, las salas y la lista de
   observadores. Las demás tareas piden cambios con **comandos** que el tick ejecuta.
   Como hay un solo hilo y el tick no tiene `await`, no hace falta ningún cerrojo.
2. **Nunca esperar a un cliente en el tick:** el tick deja los bytes en el buzón de
   cada cliente y sigue. Un cliente lento solo frena a **su** tarea `writer`.
3. **Todo recurso se libera en un `finally`:** socket, cupo del semáforo, tareas.

## 2. Flujos de ejecución (la película)

Notación: `A → B` = "A llama a B". Cada paso lleva su `archivo:línea`.

### 2.1 Arranque del servidor

1. `python -m server.main` ejecuta `main` (`server/main.py:126`).
2. `main()` configura el log, instala el filtro de log contra inundaciones
   (`RateLimitFilter` (`server/log_filters.py:17`)) y llama a `asyncio.run(serve_until_signal(build(...)))`.
3. `load_static` (`server/main.py:40`) lee `client/` **una vez** a memoria (E/S síncrona permitida:
   aún no hay clientes). Solo se sirve lo que está en ese diccionario, así que no
   existe el "path traversal".
4. `build` (`server/main.py:73`) arma el grafo de objetos (inversión de dependencias):
   `Metrics` → `Arena` → `Hall` → `JsonFrameEncoder` → políticas de envío →
   `GameSession` → codecs → `HttpHandshake` → socket de escucha →
   `Listener` → `App`.
5. `create_listen_socket` (`server/adapters/listener.py:25`) crea el socket **a mano**: `SO_REUSEADDR`,
   `bind`, `listen(128)`, `setblocking(False)`.
6. `serve_until_signal` (`server/main.py:105`) registra SIGINT/SIGTERM y lanza `App.run` (`server/main.py:60`), que
   crea la tarea del tick (`GameSession.run_tick_loop` (`server/application/game_session.py:233`)) y llama a
   `Listener.serve` (`server/adapters/listener.py:78`).
7. `Listener.serve` entrega el socket a `asyncio.start_server(sock=..., backlog=128)`.
   Desde aquí `asyncio` usa `epoll` y llama a `Listener._on_connect` por cada conexión.

### 2.2 Una conexión nueva, de TCP al `welcome`

1. El kernel completa el saludo TCP y encola la conexión; `asyncio` la acepta y llama
   a `Listener._on_connect` (`server/adapters/listener.py:122`).
2. `_on_connect` fija el búfer de envío a 64 KB (`Listener._limit_send_buffer` (`server/adapters/listener.py:113`)),
   registra el writer (para el apagado) y decide: si el semáforo está lleno
   (`locked()`), `Listener._reject` (`server/adapters/listener.py:177`) (responde `full` + cierre 1013);
   si no, toma un cupo y llama a `Listener._serve_client` (`server/adapters/listener.py:146`).
3. `_serve_client` cuenta la conexión y llama a `Listener._handshake_with_timeout` (`server/adapters/listener.py:166`)
   → `HttpHandshake.handle` (`server/adapters/http_handshake.py:194`) con plazo de 5 s:
   `read_head` (`server/adapters/http_handshake.py:112`) (acumula hasta `\r\n\r\n`) →
   `parse_request` (`server/adapters/http_handshake.py:67`) → `HttpHandshake.respond` (`server/adapters/http_handshake.py:151`) →
   `HttpHandshake._upgrade` (`server/adapters/http_handshake.py:170`) (valida las cabeceras y calcula
   `Sec-WebSocket-Accept` con `accept_key` (`server/adapters/http_handshake.py:61`)). Responde `101`.
   Si era `GET /` o `/metrics`, sirve la página y cierra.
4. Si hubo upgrade, `_serve_client` crea la sesión con `session_factory`
   (definida en `build` (`server/main.py:73`)) y llama a `ClientSession.run` (`server/adapters/client_session.py:122`).
5. `run` crea **tres tareas**: `ClientSession._read_loop` (`server/adapters/client_session.py:149`),
   `ClientSession._write_loop` (`server/adapters/client_session.py:247`) y `ClientSession._ping_loop` (`server/adapters/client_session.py:232`),
   y espera a que termine la primera de reader/writer.
6. El navegador envía `{"t":"hello","name":"Ana"}`. El reader lo lee,
   `FrameReader.next_frame` (`server/adapters/ws_frames.py:97`) arma la trama, `ClientSession._on_text` (`server/adapters/client_session.py:190`)
   la decodifica con el codec de la etapa `hello` (`MessageCodec.decode` (`server/adapters/codec.py:49`) →
   `hello_codec` (`server/adapters/codec.py:130`)) y obtiene un `JoinCommand`.
7. `ClientSession._deliver_hello` (`server/adapters/client_session.py:195`) hace tres cosas: borra el plazo del
   hello, encola `Connect(join, self)` con `submit_membership` y **cambia de etapa**
   (`self._codec` y `self._deliver` pasan a los del juego; sin ningún `if`).
8. En el **siguiente tick**, `GameSession._apply_membership` (`server/application/game_session.py:131`) ejecuta
   `Connect.execute` (`server/application/session_commands.py:38`) → `GameSession.subscribe` (`server/application/game_session.py:84`):
   registra al jugador en el vestíbulo (`Hall.connect`), lo guarda como observador,
   codifica el `welcome` y lo deja en su casilla (`on_welcome`).
9. Ese mismo tick, `GameSession._publish_rooms_if_changed` (`server/application/game_session.py:167`) le deja la
   lista de salas. El `writer` del cliente despierta y envía `welcome` y luego `rooms`.
   El navegador pasa a mostrar el **vestíbulo**.

### 2.3 Crear una sala y unirse

1. Clic en **Crear sala** → `els.create` envía `{"t":"create"}` (`send` (`client/client.js:368`)).
2. Servidor: reader → `_on_text` → `MessageCodec.decode` (`server/adapters/codec.py:49`) con el codec del juego;
   el registro `{tipo: fábrica}` elige `game_codec` (`server/adapters/codec.py:134`) → `_create_room` →
   `CreateRoomCommand`.
3. `ClientSession._deliver_input` (`server/adapters/client_session.py:202`) → `GameSession.submit_input` (`server/application/game_session.py:70`)
   (`put_nowait`: nunca bloquea; si la cola está llena, descarta y cuenta).
4. Siguiente tick: `GameSession._apply_inputs` (`server/application/game_session.py:135`) (límite de 4 órdenes por
   cliente y tick) → `command.run(hall)`. `HallCommand.run` (`server/domain/commands.py:53`)
   llama a `execute(hall)` → `Hall.create_room` (`server/domain/hall.py:86`): valida (¿ya está en una sala?
   ¿demasiadas salas?), crea `Room` con su `World` y su fase `Waiting`, y mete al jugador.
5. Mismo tick, `GameSession._publish_notices` (`server/application/game_session.py:152`) compara la sala real con
   la última avisada y envía `{"t":"room","id":N}`; las demás publicaciones envían
   `roster` (a los de la sala) y la nueva lista `rooms` (al vestíbulo).
6. Navegador: `handlers` (`client/client.js:181`) → `room` llama a `resetRoomState` (`client/client.js:247`) y `showView` (`client/client.js:269`):
   se oculta el vestíbulo y aparece la partida.
7. **Unirse** es igual con `{"t":"join","room":N}` → `JoinRoomCommand` →
   `Hall.join_room` (`server/domain/hall.py:99`). Si la sala está llena o jugando no se lanza ninguna
   excepción: `Hall._reject` anota el motivo y el tick se lo envía **solo a ese
   cliente** como `{"t":"error","code":"room_full"}` (rechazo suave: sigue conectado).

### 2.4 Una partida, fase por fase

Cada sala tiene su propia máquina de estados (`RoundState` (`server/domain/round_states.py:34`)); en cada
tick, `Hall.step` (`server/domain/hall.py:124`) llama a `Room.step` (`server/domain/room.py:21`) de cada sala, que ejecuta
`self.state = self.state.on_tick(world)`.

```
 Waiting ──(anfitrión manda start y hay ≥ 2)──► Countdown (3 s) ──► Playing ──(queda ≤ 1 vivo)──► RoundOver (3 s)
    ▲                                              │ (alguien se va y no alcanza)                       │
    └──────────────────────────────────────────────┴────────────────────────────────────────────────────┘
```

1. El anfitrión pulsa **Iniciar partida** → `{"t":"start"}` → `StartCommand`. El tick lo
   ejecuta con `Command.run` (`server/domain/commands.py:35`): busca la sala del jugador
   (`hall.world_of`) y llama a `StartCommand.execute` (`server/domain/commands.py:82`) →
   `World.request_start` (`server/domain/world.py:62`), que solo marca la petición **si quien la manda es el anfitrión**.
2. `Waiting.on_tick` (`server/domain/round_states.py:73`) consume la petición (`take_start_request`, **siempre**,
   para que una orden sin jugadores suficientes no quede guardada) y, si hay mínimo,
   llama a `World.start_round` (`server/domain/world.py:79`) (un disco por jugador, repartidos en círculo,
   mirando al centro) y devuelve `Countdown`.
3. `Countdown.on_tick` (`server/domain/round_states.py:90`) cuenta ticks (3 s × 30 = 90); si alguien se va y
   quedan menos del mínimo, cancela y vuelve a `Waiting`.
4. `Playing.on_tick` (`server/domain/round_states.py:104`) cada tick: `Physics.step` (`server/domain/physics.py:20`) sobre los discos
   vivos → `World.eliminate_outside` (`server/domain/world.py:96`) → si queda ≤ 1 vivo, `World.award` (`server/domain/world.py:103`)
   y pasa a `RoundOver` con el ganador (o `None` si fue empate).
5. `RoundOver.on_tick` (`server/domain/round_states.py:128`) espera 3 s, llama a `World.end_round` (`server/domain/world.py:89`) y
   vuelve a `Waiting`: **no hay arranque automático**; el anfitrión debe iniciar otra.

### 2.5 Un movimiento y una embestida, del teclado a la pantalla

1. `keydown` → `syncIntent` (`client/client.js:388`): calcula la intención `(dx, dy)` con `Math.sign` y
   **solo envía si cambió** (el servidor recuerda la dirección).
2. Servidor: `input` → `game_codec` (`server/adapters/codec.py:134`) valida (enteros en {-1,0,1}, no booleanos)
   → `MoveCommand` → cola → tick → `MoveCommand.execute` (`server/domain/commands.py:117`) →
   `Disc.set_move` (`server/domain/disc.py:36`), que solo guarda la **intención** normalizada. **Los comandos no
   mueven discos**: la física lee la intención en el siguiente paso.
3. Carga: `charge on` → `Disc.start_charge` (`server/domain/disc.py:43`). `charge off` →
   `Disc.release_charge` (`server/domain/disc.py:46`) marca `release_pending`.
4. `Physics.step` (`server/domain/physics.py:20`) (orden importante): fricción → `Physics._accelerate` (`server/domain/physics.py:55`)
   → `Physics._update_charge` (`server/domain/physics.py:70`) (sube la carga, o al soltar lanza la embestida:
   `vel = facing × 600 × fracción`, `dashing`, enfriamiento 1,5 s; menos de
   `MIN_CHARGE_S` no hace nada) → `Physics._cap_speed` (`server/domain/physics.py:89`) → mover →
   `Physics.resolve_collisions` (`server/domain/physics.py:97`) (`Physics._collide` (`server/domain/physics.py:104`), choque elástico
   con masa ×2 en embestida).
5. `GameSession._publish_states` (`server/application/game_session.py:211`): por sala, si la política de la fase
   dice enviar (`SendPolicy` (`server/application/send_policies.py:12`)), `GameSession._snapshot` (`server/application/game_session.py:201`) +
   `JsonFrameEncoder.encode_state` (`server/adapters/codec.py:160`) **una vez** y el mismo objeto `bytes` va al buzón de cada jugador.
6. Navegador: `state` → `pushSnapshot` (`client/client.js:126`); `interpolatedDiscs` (`client/client.js:162`) dibuja ~100 ms
   en el pasado mezclando dos estados; `draw` (`client/client.js:830`) corre con `requestAnimationFrame`.

### 2.6 Un cliente se va (de cualquier forma)

1. Causas: `close` del cliente, TCP cortado, framing/mensaje inválido, timeout del hello,
   30 s sin tráfico, o `write timeout`. Todas terminan el reader o el writer.
2. En `ClientSession.run` (`server/adapters/client_session.py:122`) el `finally` cancela las tres tareas y encola
   `Disconnect(player_id)` (la cola de membresía **nunca descarta**).
3. Tick: `Disconnect.execute` (`server/application/session_commands.py:46`) → `GameSession.unsubscribe` (`server/application/game_session.py:98`)
   → `Hall.disconnect` (`server/domain/hall.py:59`) → `Hall.leave_room` (`server/domain/hall.py:113`): quita al jugador de su sala (su
   disco desaparece; si era el último, la sala se borra).
4. En paralelo, `Listener._serve_client` (`server/adapters/listener.py:146`) y `Listener._on_connect` (`server/adapters/listener.py:122`)
   cierran el socket (`Listener._close` (`server/adapters/listener.py:199`)) y devuelven el cupo del semáforo en sus `finally`.

### 2.7 Un cliente lento (backpressure)

1. El tick llama a `on_snapshot` de cada jugador (`ClientSession.on_snapshot` (`server/adapters/client_session.py:86`)):
   si el buzón aún tiene el estado anterior, **lo reemplaza** y suma
   `mailbox_overwrites`. No hay `await`.
2. El `writer` (`ClientSession._write_loop` (`server/adapters/client_session.py:247`)) toma lo que haya, hace `write()`
   y `ClientSession._drain` (`server/adapters/client_session.py:282`) con plazo de 10 s. Si el cliente no lee, el
   `drain` se suspende **solo en esta tarea**; el tick y los demás clientes no lo notan.
3. Pasados 10 s: `abort_with_reset` (`server/adapters/net_util.py:11`) (RST) y se libera el descriptor.

### 2.8 Apagado con `kill -TERM`

1. El manejador de señal (`serve_until_signal` (`server/main.py:105`)) solo marca un `Event`
   (un manejador no puede hacer `await`).
2. La corrutina principal llama a `App.shutdown` (`server/main.py:67`) → `Listener.shutdown` (`server/adapters/listener.py:97`):
   cierra el socket de escucha, pide a cada sesión `request_shutdown` (close 1001),
   espera `SHUTDOWN_GRACE_S` (2 s) y aborta a los que no cooperen.
3. Medido: 23 ms con 20 clientes, todos reciben `close 1001`.


## 3. El servidor, archivo por archivo

Cada tabla dice **qué hace** el elemento, **con quién se conecta** (quién lo llama /
a quién llama) y, cuando importa, **qué defender**.

### 3.1 Dominio (`server/domain/`): el juego, sin red

Regla: aquí **no se importa** `asyncio` ni `socket` (hay una prueba que lo comprueba,
`tests/test_world.py::test_dominio_no_importa_red`). Por eso se prueba con `pytest` sin abrir un socket.

#### `Vec2` (`server/domain/vector.py:12`) — vector 2D inmutable

Posiciones, velocidades y direcciones. Es `frozen`: nadie modifica por accidente una
posición que otro objeto también guarda; cada operación devuelve un vector nuevo.

| Método | Qué hace | Lo usa |
| --- | --- | --- |
| `__add__`, `__sub__`, `__mul__`, `__neg__` | Suma, resta, escalado y negación | `Physics`, `Disc`, `Arena` |
| `Vec2.dot` (`server/domain/vector.py:28`) | Producto punto (cuánto se acercan dos discos a lo largo de la normal) | `Physics._collide` |
| `Vec2.length` (`server/domain/vector.py:31`) | Longitud (`math.hypot`) | `Physics`, `Arena.contains` |
| `Vec2.is_zero` (`server/domain/vector.py:34`) | ¿Es el vector cero? | `Disc.set_move`, `Physics._accelerate` |
| `Vec2.normalized` (`server/domain/vector.py:37`) | Mismo sentido, longitud 1; el cero se queda en cero (evita dividir entre 0) | `Disc.set_move`, `with_length` |
| `Vec2.with_length` (`server/domain/vector.py:45`) | Mismo sentido, longitud dada | `Physics` (recortar velocidades) |

#### `Disc` (`server/domain/disc.py:16`) — el disco de un jugador en una ronda

Guarda dos clases de datos: **intenciones** (las escriben los comandos: `move_dir`,
`charging`, `release_pending`) y **estado físico** (lo escribe solo `Physics`: `pos`,
`vel`, `charge_time`, `cooldown`, `dashing`, `alive`). `facing` es hacia dónde saldría la embestida.

| Método | Qué hace | Detalle |
| --- | --- | --- |
| `Disc.set_move` (`server/domain/disc.py:36`) | Guarda la dirección deseada **normalizada** (largo 1 o 0) | Aunque llegara `dx=1000` no corre más rápido. Actualiza `facing` solo si la dirección no es cero (soltar las teclas no cambia la puntería) |
| `Disc.start_charge` (`server/domain/disc.py:43`) | `charging = True` | Lo llama `ChargeCommand` |
| `Disc.release_charge` (`server/domain/disc.py:46`) | Si estaba cargando: `charging = False` y `release_pending = True` | Soltar sin estar cargando no hace nada (idempotente) |

**Defender:** los comandos **no mueven** el disco; solo escriben intenciones. Toda la
física ocurre en un solo lugar (`Physics.step`), así es determinista y probable.

#### `Arena` (`server/domain/arena.py:13`) — la arena circular

| Método | Qué hace |
| --- | --- |
| `__init__(radius, fall_margin)` | `radius` es el borde que se dibuja; `elimination_radius = radius + fall_margin` |
| `Arena.contains` (`server/domain/arena.py:19`) | Un disco sigue vivo mientras su **centro** esté dentro del radio de eliminación (borde + margen). Con `ELIMINATION_MARGIN_DISCS = 1.0` cae cuando está entero fuera |
| `Arena.spawn_points` (`server/domain/arena.py:25`) | `n` puntos repartidos por igual en un círculo al 60 % del radio (`SPAWN_RING_FRACTION`): lejos del borde y del centro |

#### `World` (`server/domain/world.py:30`) — UNA sala: sus jugadores, discos y marcador

Solo el tick lo toca (único escritor): sin cerrojos.

| Elemento | Qué hace | Se conecta con |
| --- | --- | --- |
| `Player` (`server/domain/world.py:17`) | `id`, `name`, `color` (inmutable) | `Hall`, roster |
| `color_for` (`server/domain/world.py:23`) | Color por `id % 12`: igual en vestíbulo y en cualquier sala | `World.add_player`, `Hall.color_of` |
| `players` | Dict en **orden de llegada a la sala**; nunca se reordena. El primero es el anfitrión | `host()`, `start_round()` |
| `discs`, `scores` | Discos de la ronda (vacío fuera de ronda) y victorias por jugador | `Playing`, snapshot |
| `World.add_player` (`server/domain/world.py:45`) | Registra un jugador en la sala | `Hall._enter` |
| `World.remove_player` (`server/domain/world.py:50`) | Lo quita (pop con valor por defecto: repetir no falla), y **su disco desaparece** (RF-04) y su marcador | `Hall.leave_room` |
| `World.host` (`server/domain/world.py:56`) | El primero de `players`. Se **deduce**, no se guarda: si se va, lo es el siguiente sin código de traspaso | `request_start`, roster |
| `World.request_start` (`server/domain/world.py:62`) | Marca la petición de iniciar **solo si** `player_id == host()` | `StartCommand` |
| `World.take_start_request` (`server/domain/world.py:67`) | Lee y borra la petición (la consume) | `Waiting.on_tick` |
| `World.player_count` (`server/domain/world.py:71`) | Cuántos hay en la sala | `Hall`, `Waiting`, `Room` |
| `World.disc` (`server/domain/world.py:74`) | Disco de un jugador o `None` | `MoveCommand`, `ChargeCommand` |
| `World.start_round` (`server/domain/world.py:79`) | Crea un disco por jugador en los puntos de salida, mirando al centro | `Waiting.on_tick` |
| `World.end_round` (`server/domain/world.py:89`) | Borra los discos y **cualquier petición de iniciar pendiente** (una orden dada en plena ronda no vale para la siguiente) | `RoundOver`, `Countdown` |
| `World.alive_discs` (`server/domain/world.py:93`) | Discos con `alive` | `Playing`, `Countdown` |
| `World.eliminate_outside` (`server/domain/world.py:96`) | Marca `alive = False` a quien salió (el disco se queda en la lista para dibujarlo apagado) | `Playing` |
| `World.award` (`server/domain/world.py:103`) | Suma una victoria; `None` (empate) no suma | `Playing` |

#### `Room` (`server/domain/room.py:15`) — una sala = su mundo + su fase

| Elemento | Qué hace |
| --- | --- |
| `id`, `name`, `world`, `state` | Identidad, el `World` propio y la fase actual (State) |
| `Room.step` (`server/domain/room.py:21`) | `self.state = self.state.on_tick(self.world)`: avanza un tick; la fase decide la siguiente |
| `Room.phase` (`server/domain/room.py:26`) | Nombre de la fase (`"waiting"`...) |

#### `Hall` (`server/domain/hall.py:43`) — el vestíbulo y las reglas de las salas

Sabe **quién está conectado, qué salas hay y en cuál está cada jugador**. Aplica las
reglas sin red. Es la pieza nueva que reemplazó a la cola de turnos.

| Método | Qué hace | Se conecta con |
| --- | --- | --- |
| `__init__` | Guarda config, arena y `RoundContext`; `rooms`, `_names` (todos los conectados), `_room_of` (jugador → sala; ausente = vestíbulo), contador de ids de sala, `rejections` | `main.build` |
| `Hall.connect` (`server/domain/hall.py:56`) | Registra el nombre de un conectado (queda en el vestíbulo) | `GameSession.subscribe` |
| `Hall.disconnect` (`server/domain/hall.py:59`) | Lo saca de su sala (`silent=True`: sin avisarle) y lo olvida. Repetible | `GameSession.unsubscribe` |
| `Hall.is_connected` (`server/domain/hall.py:64`), `Hall.name_of` (`server/domain/hall.py:67`), `Hall.color_of` (`server/domain/hall.py:70`) | Consultas | `GameSession`, `_can_act` |
| `Hall.lobby_members` (`server/domain/hall.py:73`) | Los conectados sin sala (reciben la lista de salas) | `_publish_rooms_if_changed` |
| `Hall.room_of` (`server/domain/hall.py:78`), `Hall.world_of` (`server/domain/hall.py:82`) | Sala (o su mundo) de un jugador; `None` si está en el vestíbulo | `Command.run`, publicaciones |
| `Hall.create_room` (`server/domain/hall.py:86`) | Valida (no estar ya en una sala; `len(rooms) < MAX_ROOMS`), crea `Room(World, Waiting)` y mete al creador (será el anfitrión) | `CreateRoomCommand` |
| `Hall.join_room` (`server/domain/hall.py:99`) | Valida **en este orden**: ya en sala → sala inexistente → llena (`>= MAX_ROOM_PLAYERS`) → jugando (`phase != "waiting"`). Si pasa, entra | `JoinRoomCommand` |
| `Hall.leave_room` (`server/domain/hall.py:113`) | Quita al jugador de su sala; **si queda vacía, la borra**. Fuera de sala y sin `silent`: rechazo `not_in_room` | `LeaveRoomCommand`, `disconnect` |
| `Hall.step` (`server/domain/hall.py:124`) | Un tick de cada sala (`list(...)` para poder borrar mientras se recorre) | `GameSession.tick` |
| `Hall.summary` (`server/domain/hall.py:129`) | Tupla inmutable de `RoomInfo` (`server/domain/hall.py:32`) (lista para el vestíbulo); `open` = hay sitio **y** está en espera | `_publish_rooms_if_changed` |
| `Hall.drain_rejections` (`server/domain/hall.py:138`) | Entrega y vacía los rechazos pendientes `(jugador, código)` | `_publish_notices` |
| `Hall._can_act` (`server/domain/hall.py:144`) | Ignora la orden de quien ya se fue o no ha saludado | create/join |
| `Hall._enter` (`server/domain/hall.py:148`) | Añade al `World` de la sala y anota `_room_of` | create/join |
| `Hall._reject` (`server/domain/hall.py:152`) | Anota un rechazo (**no lanza excepción**) | todos |

**Defender:** los rechazos no son excepciones: "sala llena" es una situación normal, no
un error. Se anotan y el tick los entrega al cliente que los provocó.

#### `Command` (`server/domain/commands.py:28`) y `HallCommand` (`server/domain/commands.py:44`) — el patrón Command

Separan **cuándo llega** una orden (la tarea `reader`, en cualquier momento) de **cuándo
se ejecuta** (dentro del tick). Es la base del único escritor.

| Clase | Qué hace | Sobre qué actúa |
| --- | --- | --- |
| `Command` (`server/domain/commands.py:28`) | Base de las órdenes **dentro de una sala**. `execute(world)` abstracto. `Command.run` (`server/domain/commands.py:35`) busca la sala del jugador (`hall.world_of`) y ejecuta ahí; si está en el vestíbulo, **se ignora** | `World` |
| `HallCommand` (`server/domain/commands.py:44`) | Base de las órdenes **sobre las salas**. `run(hall)` → `execute(hall)` | `Hall` |
| `JoinCommand` (`server/domain/commands.py:58`) | Producto del `hello`: `(id, nombre)`. En el servidor solo transporta datos hacia `Connect`; su `execute` solo se usa en pruebas | — |
| `LeaveCommand` (`server/domain/commands.py:70`) | Quita al jugador del mundo (pruebas) | `World` |
| `StartCommand` (`server/domain/commands.py:78`) | `world.request_start(id)` | `World` |
| `MoveCommand` (`server/domain/commands.py:112`) | `disc.set_move(Vec2(dx, dy))`; sin disco (no hay ronda / ya se fue) se ignora | `Disc` |
| `ChargeCommand` (`server/domain/commands.py:125`) | `on=True` → `start_charge`; `on=False` → `release_charge` | `Disc` |
| `CreateRoomCommand` (`server/domain/commands.py:87`) | `hall.create_room(id)` | `Hall` |
| `JoinRoomCommand` (`server/domain/commands.py:95`) | `hall.join_room(id, room_id)` | `Hall` |
| `LeaveRoomCommand` (`server/domain/commands.py:104`) | `hall.leave_room(id)` | `Hall` |

**Defender (doble despacho):** el tick hace `command.run(hall)` sin saber de qué tipo es.
`Command.run` y `HallCommand.run` hacen cada uno lo suyo. No hay `if isinstance`.

#### `RoundState` (`server/domain/round_states.py:34`) — el patrón State (las fases)

El tick no pregunta "¿en qué fase estoy?": llama a `state.on_tick(world)` y recibe la
siguiente fase. **Contrato (LSP):** `on_tick` nunca lanza y siempre devuelve un
`RoundState` (hay una prueba de fuzz con 5000 ticks al azar).

| Elemento | Qué hace |
| --- | --- |
| `RoundContext` (`server/domain/round_states.py:25`) | Lo común a todas las fases: `config` y `physics`. `RoundContext.ticks_for` (`server/domain/round_states.py:30`) convierte segundos a **ticks enteros** (con decimales, `3.0 - 90*(1/30)` no da exactamente 0 y la fase duraría un tick de más o de menos) |
| `RoundState` (`server/domain/round_states.py:34`) | Base abstracta: `PHASE`, `on_tick`, `remaining_s` (None si no tiene plazo), `winner` |
| `Waiting` (`server/domain/round_states.py:65`) | Sala de espera. `Waiting.on_tick` (`server/domain/round_states.py:73`): consume la petición de iniciar (siempre); si fue pedida y hay `MIN_PLAYERS_TO_START`, `start_round` y devuelve `Countdown`. **No arranca sola** |
| `Countdown` (`server/domain/round_states.py:84`) | 3 s. Si los discos vivos bajan del mínimo (alguien se fue), cancela: `end_round` y `Waiting` |
| `Playing` (`server/domain/round_states.py:101`) | Cada tick: `physics.step`, `eliminate_outside`; con ≤ 1 vivo, suma la victoria (o nada si empate) y pasa a `RoundOver` |
| `RoundOver` (`server/domain/round_states.py:117`) | 3 s mostrando el ganador; luego `end_round` y `Waiting` |
| `_TimedState` (`server/domain/round_states.py:53`) | Base de las fases con plazo: cuenta de ticks restante |

#### `Physics` (`server/domain/physics.py:16`) — la física, y nada más

Paso fijo (`dt = 1/30`), determinista. No decide eliminaciones ni fases.

| Método | Qué hace | Por qué en ese orden |
| --- | --- | --- |
| `Physics.step` (`server/domain/physics.py:20`) | Por disco: fricción → aceleración → carga/embestida → tope de velocidad → mover; luego choques | Fricción primero para que la velocidad caminando sea exactamente `MAX_SPEED`; el tope antes de mover garantiza **contra el tunneling** |
| `Physics._is_charging` (`server/domain/physics.py:49`) | Carga real = tecla pulsada **y** sin enfriamiento | En enfriamiento mantener la tecla no inmoviliza |
| `Physics._accelerate` (`server/domain/physics.py:55`) | Suma `ACCELERATION × dt` en la dirección pedida, sin pasar de `MAX_SPEED` (ni acelerar a quien ya va más rápido); cargando no acelera | — |
| `Physics._update_charge` (`server/domain/physics.py:70`) | Baja el enfriamiento, sube la carga (hasta `CHARGE_MAX_S`); al soltar: bajo `MIN_CHARGE_S` se descarta, si no sale la embestida (`vel = facing × 600 × fracción`, `dashing`, enfriamiento) | Un toque corto daba una embestida más lenta que caminar y gastaba el enfriamiento (por eso `MIN_CHARGE_S`) |
| `Physics._cap_speed` (`server/domain/physics.py:89`) | Recorta a `DASH_MAX_SPEED` | Un choque contra un disco en embestida puede lanzar a 4/3 de esa velocidad |
| `Physics.mass` (`server/domain/physics.py:93`) | `DISC_MASS`, ×2 si está en embestida | Así el choque sigue siendo elástico y conserva el momento (RF-09) |
| `Physics.resolve_collisions` (`server/domain/physics.py:97`) | Todos los pares, O(n²): con 8 discos son 28 | — |
| `Physics._collide` (`server/domain/physics.py:104`) | Si se solapan: **separa** (cada uno retrocede según la masa del otro) y, **solo si se acercan**, aplica el choque elástico 1D sobre la normal | Si ya se alejan no se reaplica el impulso |

**Defender:** dos fricciones (caminar frena rápido; por encima de `MAX_SPEED` frena menos para
que el impulso llegue lejos). La validación de `Config` impide configurar una velocidad
que cause *tunneling* (`DASH_MAX_SPEED × dt < DISC_RADIUS`).

### 3.2 Aplicación (`server/application/`): el tick y los puertos

#### `SnapshotObserver` (`server/application/ports.py:52`) y `SnapshotEncoder` (`server/application/ports.py:76`) — los puertos (interfaces)

`GameSession` solo conoce estas dos interfaces, **nunca** un WebSocket. Los adaptadores
las implementan; las pruebas usan falsas.

| Interfaz | Métodos | Quién la implementa |
| --- | --- | --- |
| `SnapshotObserver` (`server/application/ports.py:52`) (patrón **Observer**) | `on_snapshot`, `on_welcome`, `on_roster`, `on_rooms`, `on_notice`: reciben **bytes ya codificados**. REGLA: sin `await` ni E/S | `ClientSession` |
| `SnapshotEncoder` (`server/application/ports.py:76`) | `encode_state`, `encode_welcome`, `encode_roster`, `encode_rooms`, `encode_room`, `encode_error` | `JsonFrameEncoder` |

Datos que viajan por los puertos (dataclasses inmutables): `DiscView` (`server/application/ports.py:14`) (un disco),
`Snapshot` (`server/application/ports.py:23`) (foto del estado de una sala en un tick), `WelcomeInfo` (`server/application/ports.py:34`) (lo que va
en el `welcome`), `RosterEntry` (`server/application/ports.py:46`) (un jugador del roster).

#### `SendPolicy` (`server/application/send_policies.py:12`) — el patrón Strategy

| Clase | Qué hace |
| --- | --- |
| `SendPolicy` (`server/application/send_policies.py:12`) | `should_send(tick) -> bool` |
| `EveryTick` (`server/application/send_policies.py:17`) | Siempre `True` (30 Hz) |
| `EveryNthTick` (`server/application/send_policies.py:22`) | `tick % n == 0` (con `n = 3` son 10 Hz) |

Registro por **fase** (en `main.build`): `playing → EveryTick`; `waiting`, `countdown` y
`round_over → EveryNthTick(3)` (nada se mueve, ahorra 2/3 del tráfico). Una política nueva es una clase nueva.

#### `MembershipCommand` (`server/application/session_commands.py:28`) — conectarse y desconectarse

Viajan por una cola **distinta** a la de entradas y **nunca descartan**: perder un
`Disconnect` dejaría un jugador fantasma y una sesión que no se libera.

| Clase | Qué hace |
| --- | --- |
| `Connect` (`server/application/session_commands.py:34`) | `session.subscribe(id, nombre, observador)` |
| `Disconnect` (`server/application/session_commands.py:43`) | `session.unsubscribe(id)` |

#### `GameSession` (`server/application/game_session.py:37`) — EL TICK (lo más importante del servidor)

**Único escritor** del vestíbulo, las salas y los observadores. `tick()` es **síncrono**:
no tiene ningún `await`, así que nada puede intercalarse mientras modifica o recorre estas estructuras.

| Elemento | Qué hace | Se conecta con |
| --- | --- | --- |
| `GameSession.__init__` (`server/application/game_session.py:38`) | `_inputs` (cola **acotada**, `INPUT_QUEUE_SIZE`), `_membership` (deque sin tope pero con un solo Connect y un Disconnect por conexión), `_observers`, `_announced_room`, `_rooms_sent`, `_rooms_data`, `_rooms_summary`, `_last_roster` | `main.build` |
| `GameSession.submit_input` (`server/application/game_session.py:70`) | `put_nowait`; si está llena **descarta y cuenta** (`inputs_dropped_queue`). **Nunca** `await put()` | `ClientSession._deliver_input` |
| `GameSession.submit_membership` (`server/application/game_session.py:79`) | `deque.append` (atómico, sin `await`) | `ClientSession` |
| `GameSession.subscribe` (`server/application/game_session.py:84`) | Conecta en el `Hall`, guarda el observador, arma el `welcome` y lo entrega. Marca al cliente "en el vestíbulo" | `Connect.execute` |
| `GameSession.unsubscribe` (`server/application/game_session.py:98`) | Desconecta del `Hall` y olvida al observador | `Disconnect.execute` |
| `GameSession.stats` (`server/application/game_session.py:104`) | Solo lectura para `/metrics`: tick, salas, jugando, en salas, en el vestíbulo | `main.metrics_page` |
| `GameSession.tick` (`server/application/game_session.py:121`) | El orden: membresía → entradas → `hall.step()` → avisos → lista de salas → rosters → estados | `run_tick_loop` |
| `GameSession._apply_membership` (`server/application/game_session.py:131`) | Vacía la cola de membresía ejecutando cada comando | `tick` |
| `GameSession._apply_inputs` (`server/application/game_session.py:135`) | Vacía la cola; **máximo `MAX_INPUTS_PER_CLIENT_PER_TICK` (4) por cliente y tick**, el resto se descarta y cuenta (`inputs_dropped_rate`); ejecuta `command.run(hall)` | `tick` |
| `GameSession._publish_notices` (`server/application/game_session.py:152`) | (1) Entrega los rechazos del `Hall` a quien los provocó. (2) Compara la sala real de cada cliente con la **última avisada** y envía `room` si cambió: ningún comando tiene que acordarse de avisar | `tick` |
| `GameSession._publish_rooms_if_changed` (`server/application/game_session.py:167`) | Codifica la lista de salas **solo si cambió** y la entrega a los del vestíbulo que aún no la tengan. Se compara por **identidad** (`is`) del objeto `bytes`: barato y exacto | `tick` |
| `GameSession._publish_rosters_if_changed` (`server/application/game_session.py:181`) | Por sala, si cambió la lista o el anfitrión, codifica **una vez** y entrega a los de esa sala; olvida las salas borradas | `tick` |
| `GameSession._snapshot` (`server/application/game_session.py:201`) | Foto inmutable de una sala (discos, fase, tiempo, ganador, marcador) | `_publish_states` |
| `GameSession._publish_states` (`server/application/game_session.py:211`) | Por sala, si la política de su fase dice enviar: codifica **una vez** y entrega el mismo objeto a cada jugador | `tick` |
| `GameSession._notify` (`server/application/game_session.py:223`) | Llama al método del observador dentro de `try/except`: un observador roto no detiene la difusión ni el tick (AGENTS §4.9) | todas las publicaciones |
| `GameSession.run_tick_loop` (`server/application/game_session.py:233`) | Bucle de **paso fijo**: `next_at += dt` (no "ahora + dt") para que los retrasos no se acumulen; si un tick se pasa de su presupuesto, cuenta `tick_overruns` y reprograma desde ahora (sin espiral de recuperación). Captura excepciones de la lógica: un bug no congela a todos. El **único `await`** es `asyncio.sleep` entre ticks | `App.run` |

**Región crítica (AGENTS §4.10):** en `tick()` no hay ningún `await` entre leer y
modificar el estado; por eso es seguro que una sola tarea lo modifique sin cerrojo.


### 3.3 Adaptadores (`server/adapters/`): toda la red

#### `FrameReader` (`server/adapters/ws_frames.py:78`) y `FrameWriter` (`server/adapters/ws_frames.py:175`) — el WebSocket (RFC 6455) escrito a mano

TCP es un **flujo de bytes**, no de mensajes: un `read()` puede traer media trama, una
y media o diez. Por eso `FrameReader` acumula y solo entrega una trama cuando la tiene completa.

| Elemento | Qué hace | Detalle a defender |
| --- | --- | --- |
| `Opcode` (`server/adapters/ws_frames.py:27`) | Tipos de trama (texto, ping, pong, close...) | Parte del protocolo, no configurable |
| `CloseCode` (`server/adapters/ws_frames.py:36`) | Códigos de cierre (1000, 1001, 1002, 1003, 1007, 1008, 1009, 1013) | Viven aquí y no en `config.py` porque son del RFC |
| `Frame` (`server/adapters/ws_frames.py:64`) | `(opcode, payload)` inmutable | — |
| `ProtocolError` (`server/adapters/ws_frames.py:69`) | Lleva el `CloseCode` con que hay que cerrar | La captura `ClientSession._read_loop` |
| `FrameReader.feed` (`server/adapters/ws_frames.py:94`) | Agrega bytes al búfer | Se llama tras cada `read()` |
| `FrameReader.next_frame` (`server/adapters/ws_frames.py:97`) | Devuelve una trama completa o `None`. **Se llama en bucle hasta `None`** (puede haber varias). Valida: máscara obligatoria (si no, 1002), longitudes de 7, 16 y 64 bits, **tope `MAX_FRAME_BYTES` comprobado ANTES de reservar memoria**, control ≤ 125 bytes, close de 1 byte inválido | Sin el tope temprano, un cliente que anuncie 2^62 bytes nos haría acumular sin límite |
| `FrameReader._check_opcode` (`server/adapters/ws_frames.py:147`) | Rechaza bits RSV, opcodes desconocidos, binarias (1003) y fragmentación (no soportada) | — |
| `FrameReader` (`server/adapters/ws_frames.py:78`) (interno) `_unmask` | `_unmask` (`server/adapters/ws_frames.py:162`): XOR con la clave de 4 bytes. En vez de un bucle byte a byte, repite la clave y hace **un solo XOR entre dos enteros grandes** | Mucho más rápido en Python |
| `FrameWriter.encode` (`server/adapters/ws_frames.py:184`) | Cabecera (7/16/64 bits) + payload en **un solo `bytes`** | Cada trama sale en **una** llamada `write()`: dos tramas nunca se entremezclan. El servidor **nunca enmascara** |
| `FrameWriter.text` (`server/adapters/ws_frames.py:198`), `FrameWriter.ping` (`server/adapters/ws_frames.py:203`), `FrameWriter.pong` (`server/adapters/ws_frames.py:207`), `FrameWriter.close` (`server/adapters/ws_frames.py:212`) | Atajos por tipo; el pong repite el payload del ping (§5.5.3) | — |

Pruebas: `tests/test_ws_frames.py` (tramas partidas en pedazos de 1 byte, varias juntas,
longitudes 126 y 127, sin máscara, por encima del máximo).

#### `HttpHandshake` (`server/adapters/http_handshake.py:136`) — el saludo HTTP y el upgrade

Antes de este saludo todavía no existe la tarea `writer` del cliente, así que `handle()` es el único que
escribe en el socket en ese momento: la regla de un solo escritor se mantiene.

| Elemento | Qué hace | Detalle |
| --- | --- | --- |
| `HttpError` (`server/adapters/http_handshake.py:31`) | Error HTTP con su estado y cabeceras extra | Se convierte en respuesta con `error_response` |
| `HttpRequest` (`server/adapters/http_handshake.py:39`), `StaticFile` (`server/adapters/http_handshake.py:47`), `HandshakeResult` (`server/adapters/http_handshake.py:53`) | Datos: petición parseada, archivo en memoria, resultado del saludo (`upgraded` y el `leftover`) | `leftover` son bytes que llegaron **después** de la cabecera en el mismo `read()`: pueden ser el comienzo de la primera trama, y hay que dárselos al `FrameReader` o se pierde |
| `accept_key` (`server/adapters/http_handshake.py:61`) | `base64(SHA-1(clave + GUID))`, §4.2.2 | Demuestra que el servidor "entiende" WebSocket |
| `parse_request` (`server/adapters/http_handshake.py:67`) | Interpreta la cabecera (función pura, sin sockets) | Rechaza lo mal formado con 400 |
| `read_head` (`server/adapters/http_handshake.py:112`) | Lee hasta `\r\n\r\n` **acumulando a mano** | No usa `readuntil()`. El tope se aplica a la cabecera, no al búfer: aunque un solo `read()` traiga 12 KB que incluyan el final, la cabecera sigue siendo demasiado grande |
| `HttpHandshake.respond` (`server/adapters/http_handshake.py:151`) | Decide: GET → `/ws` es upgrade; rutas dinámicas (`/metrics`) o estáticas; si no, 404; otro método, 405 | Solo se sirve lo cargado en memoria: sin *path traversal* |
| `HttpHandshake._upgrade` (`server/adapters/http_handshake.py:170`) | Valida HTTP/1.1, `Host`, `Upgrade: websocket`, `Connection: Upgrade`, versión 13 (si no, 426) y clave de 16 bytes en base64; responde 101 | — |
| `HttpHandshake.handle` (`server/adapters/http_handshake.py:194`) | Orquesta: `read_head` → `parse_request` → `respond` → **una** `write()` → `drain()` | El `drain()` aquí sí puede esperar: es la tarea de este cliente, no el tick |

#### `MessageCodec` (`server/adapters/codec.py:44`) — JSON ↔ comandos (patrón Factory Method con registro)

Decodificar: el campo `t` elige una fábrica en un diccionario `{tipo: fábrica}`, **sin `if/elif` por tipo**.
**Toda la validación de lo que manda el cliente vive aquí**: el dominio recibe comandos ya válidos.

| Elemento | Qué hace | Detalle |
| --- | --- | --- |
| `MessageError` (`server/adapters/codec.py:32`) | Mensaje inválido: lleva el código de error y el de cierre (por defecto 1008) | La captura el reader |
| `MessageCodec.decode` (`server/adapters/codec.py:49`) | UTF-8 → JSON → debe ser objeto → `t` debe ser string → fábrica del registro | Captura `RecursionError` (4 KB de `[[[[`) y el `t` no hashable (`{"t":[1]}`): sin esos chequeos una excepción inesperada tumbaría la sesión |
| `_is_int` (`server/adapters/codec.py:74`) | Entero **no booleano** | En Python `True` es `int` |
| `_make_hello` (`server/adapters/codec.py:79`) | Fábrica de `hello`: nombre string, sin espacios, 1 a 16 caracteres imprimibles → `JoinCommand` | `isprintable` rechaza saltos de línea |
| `_input` (`server/adapters/codec.py:93`) | `dx`, `dy` enteros en {-1, 0, 1} → `MoveCommand` | |
| `_charge` (`server/adapters/codec.py:100`) | `on` booleano → `ChargeCommand` | |
| `_start` (`server/adapters/codec.py:107`), `_create_room` (`server/adapters/codec.py:111`), `_leave_room` (`server/adapters/codec.py:122`) | Sin campos → `StartCommand`, `CreateRoomCommand`, `LeaveRoomCommand` | |
| `_join_room` (`server/adapters/codec.py:115`) | `room` entero no booleano → `JoinRoomCommand`; si no, `bad_message` | |
| `_second_hello` (`server/adapters/codec.py:126`) | Un segundo `hello` es un error (`not_hello`) | |
| `hello_codec` (`server/adapters/codec.py:130`), `game_codec` (`server/adapters/codec.py:134`) | Los **dos registros**: antes de `hello` solo `hello`; después `input`, `charge`, `start`, `create`, `join`, `leave` | `ClientSession` cambia de codec al entrar: sin `if ya_entré` |

#### `JsonFrameEncoder` (`server/adapters/codec.py:148`) — estado → bytes listos para el cable

Implementa `SnapshotEncoder` (`server/application/ports.py:76`). JSON compacto **ya envuelto en trama WebSocket**: el tick obtiene
bytes listos para `write()` y los **comparte entre todos los clientes** (una codificación por mensaje, no una por cliente).

| Método | Mensaje | Campos |
| --- | --- | --- |
| `JsonFrameEncoder._frame` (`server/adapters/codec.py:157`) | (interno) JSON sin espacios → `FrameWriter.text` | — |
| `JsonFrameEncoder.encode_state` (`server/adapters/codec.py:160`) | `state` | `tick`, `phase`, `ms`, `winner`, `scores`, `d` (discos con posición a 1 decimal y carga a 2) |
| `JsonFrameEncoder.encode_welcome` (`server/adapters/codec.py:173`) | `welcome` | id, color, arena, `max_players`, enfriamiento, `min_charge`, `min_players` |
| `JsonFrameEncoder.encode_roster` (`server/adapters/codec.py:181`) | `roster` | `host` y jugadores `(id, nombre, color)` |
| `JsonFrameEncoder.encode_rooms` (`server/adapters/codec.py:185`) | `rooms` | lista de salas con `open` |
| `JsonFrameEncoder.encode_room` (`server/adapters/codec.py:190`) | `room` | `id` o `null` (vestíbulo) |
| `JsonFrameEncoder.encode_error` (`server/adapters/codec.py:194`) | `error` | `code` |

#### `ClientSession` (`server/adapters/client_session.py:38`) — una conexión WebSocket ya abierta (EL ARCHIVO MÁS DELICADO)

Implementa `SnapshotObserver`. **Tres tareas por cliente y un buzón**:

```
 reader ──comandos──► GameSession (cola, put_nowait)
   └─ pong / close ─► casillas ─┐
 ping ───────────────► casilla ─┤
 tick ─on_snapshot/on_welcome...► casillas ─┴─► writer ──► socket   (ÚNICA tarea que escribe)
```

Las casillas (pong, ping, close, welcome, aviso, rooms, roster, buzón de estado) guardan **UN valor
cada una**: si llega uno nuevo antes de que el `writer` envíe el anterior, lo **reemplaza**.
Así un cliente lento nunca acumula memoria.

| Método | Qué hace | Detalle a defender |
| --- | --- | --- |
| `ClientSession.__init__` (`server/adapters/client_session.py:39`) | Crea el `FrameReader`, el plazo total del hello, el contador de pings, el `Event` y las casillas; `self._codec` y `self._deliver` empiezan en la etapa `hello`; `self._handlers` es un **registro `{opcode: método}`** | Sin `if/elif` por tipo de trama |
| `ClientSession.on_snapshot` (`server/adapters/client_session.py:86`) | Deja el estado en el buzón; si ya había uno, **lo pisa** y cuenta `mailbox_overwrites`; `event.set()` | **Sin `await`**: lo llama el tick |
| `ClientSession.on_welcome` (`server/adapters/client_session.py:98`), `ClientSession.on_roster` (`server/adapters/client_session.py:102`), `ClientSession.on_rooms` (`server/adapters/client_session.py:108`), `ClientSession.on_notice` (`server/adapters/client_session.py:113`) | Dejan su mensaje en su casilla y despiertan al writer | Roster y rooms van **completos** (el nuevo reemplaza al viejo sin perder nada). El aviso: gana el último |
| `ClientSession.request_shutdown` (`server/adapters/client_session.py:92`) | Deja un `close 1001` en la casilla (la llama el `Listener` al apagar) | El `writer` lo envía: sigue habiendo un único escritor |
| `ClientSession.run` (`server/adapters/client_session.py:122`) | Crea reader, writer y ping; espera al primero de reader/writer; si el reader pidió cerrar, da al writer `WRITE_TIMEOUT_S` para enviar el close. **`finally`**: cancela las tres, registra excepciones propias sin propagarlas y encola `Disconnect` | **Ninguna excepción de un cliente tumba el servidor.** El `Disconnect` se encola pase lo que pase |
| `ClientSession._read_loop` (`server/adapters/client_session.py:149`) | Alimenta el `FrameReader` con el sobrante del saludo y luego con cada `read()`; saca **todas** las tramas completas y las despacha por el registro; el `read` lleva plazo. Captura `TimeoutError`, `ProtocolError` y `MessageError` y pide cerrar con el código adecuado | **Nunca asume que `read()` trae un mensaje completo** |
| `ClientSession._read_timeout` (`server/adapters/client_session.py:172`) | Antes del hello: plazo **total** (no se renueva con cada byte, anti-*slowloris*). Después: inactividad (`IDLE_TIMEOUT_S`, 30 s) | Cualquier dato, incluido el pong, renueva la inactividad |
| `ClientSession._on_read_timeout` (`server/adapters/client_session.py:181`) | Cuenta el tipo de timeout y pide cerrar con `error timeout` + 1008 | |
| `ClientSession._on_text` (`server/adapters/client_session.py:190`) | Decodifica con el codec de la etapa actual y llama a `self._deliver` | El cambio de etapa es **cambiar dos atributos**, no un `if` |
| `ClientSession._deliver_hello` (`server/adapters/client_session.py:195`) | Borra el plazo del hello, encola `Connect(join, self)` y pasa a la etapa de juego | |
| `ClientSession._deliver_input` (`server/adapters/client_session.py:202`) | `game.submit_input(player_id, command)` | `put_nowait`: si está llena, se descarta |
| `ClientSession._on_ping` (`server/adapters/client_session.py:205`) | Deja un pong con el mismo payload | |
| `ClientSession._on_pong` (`server/adapters/client_session.py:210`) | Si coincide con el ping pendiente, mide el RTT y lo registra | Un pong con otro payload se ignora |
| `ClientSession._on_close` (`server/adapters/client_session.py:219`) | Responde close (§5.5.1) y deja de leer | |
| `ClientSession._request_close` (`server/adapters/client_session.py:224`) | Deja `error + close` juntos en **una** `write()`: ningún estado puede colarse entre ellos | |
| `ClientSession._ping_loop` (`server/adapters/client_session.py:232`) | Cada `PING_INTERVAL_S` (10 s) deja un ping en su casilla (no escribe) | Detecta clientes muertos y mide el RTT |
| `ClientSession._write_loop` (`server/adapters/client_session.py:247`) | **El único que escribe el socket.** `await event.wait()` → `event.clear()` → **y después** toma y vacía las casillas (sin `await` en medio). Orden: cierre (envía y termina) → pong, ping, welcome, aviso, rooms, roster, estado; `drain` al final | **Orden `wait`/`clear`/leer (AGENTS §4.5):** si se leyera antes del `clear`, un `set()` del tick entre ambos se perdería |
| `ClientSession._drain` (`server/adapters/client_session.py:282`) | `drain()` con plazo `WRITE_TIMEOUT_S`; si vence, cuenta `write_timeouts` y aborta con RST (`abort_with_reset`) | Un cliente que no lee solo detiene **su** `writer` |

#### `Listener` (`server/adapters/listener.py:49`) — el socket de escucha, el semáforo y el apagado

| Elemento | Qué hace | Detalle a defender |
| --- | --- | --- |
| `create_listen_socket` (`server/adapters/listener.py:25`) | `socket(AF_INET, SOCK_STREAM)`, `SO_REUSEADDR`, `bind`, `listen(backlog)`, `setblocking(False)`; cierra el socket si algo falla | `SO_REUSEADDR` permite reiniciar de inmediato aunque queden conexiones en `TIME_WAIT` |
| `Listener.__init__` (`server/adapters/listener.py:50`) | `asyncio.Semaphore(MAX_CONNECTIONS)`, contador de ids, conjuntos de writers y sesiones, `Event` "sin conexiones" | Los conjuntos solo los toca el event loop; `add/discard` no tienen `await` |
| `Listener.port` (`server/adapters/listener.py:75`) | Puerto real (útil con `PORT=0` en pruebas) | |
| `Listener.serve` (`server/adapters/listener.py:78`) | `asyncio.start_server(sock=..., backlog=...)` y `serve_forever` | **`backlog=` explícito**: `start_server` llamaría a `listen(100)` y pisaría nuestro `listen(128)` (se ve con `ss -tlnp`, columna Send-Q). Si `accept()` falla por falta de descriptores, `asyncio` reintenta cada 1 s |
| `Listener.shutdown` (`server/adapters/listener.py:97`) | Cierra el socket de escucha, pide `close 1001` a cada sesión, espera `SHUTDOWN_GRACE_S` y **aborta a los rezagados** | |
| `Listener._limit_send_buffer` (`server/adapters/listener.py:113`) | `SO_SNDBUF = 64 KB` | El kernel autoajusta hasta ~2,6 MB: un zombi tardaría más de 10 min en llenarlo |
| `Listener._on_connect` (`server/adapters/listener.py:122`) | Registra el writer; **si el semáforo está lleno, rechaza en vez de esperar** (una conexión en espera también ocupa un descriptor); si no, toma cupo y atiende. **`finally`**: `_close(writer)` siempre, y libera el cupo | **Región crítica:** entre `locked()` y `acquire()` no hay suspensión (con cupo libre, `acquire` no cede el loop), así que nadie puede robar el cupo |
| `Listener._serve_client` (`server/adapters/listener.py:146`) | Cuenta la conexión, hace el saludo con plazo, crea la sesión y la corre. `except ConnectionError` (normal) y `except Exception` (se registra) | Ninguna excepción tumba el servidor |
| `Listener._handshake_with_timeout` (`server/adapters/listener.py:166`) | `wait_for(handle, HANDSHAKE_TIMEOUT_S)`; `None` si vence | Anti-*slowloris* |
| `Listener._reject` (`server/adapters/listener.py:177`) | Servidor lleno: contesta el saludo y, si era WebSocket, manda `error full` + `close 1013` en una `write()`. Acotado por `MAX_REJECTING_HANDSHAKES` (16); pasado ese número, cierra con RST directo | Rechazar no puede agotar los descriptores |
| `Listener._close` (`server/adapters/listener.py:199`) | `close()` + `wait_closed()` con plazo; si vence, `abort_with_reset` | Libera el descriptor en **todos** los casos |

#### `abort_with_reset` (`server/adapters/net_util.py:11`) — cortar YA con un RST

`transport.abort()` solo hace `close()` del socket, y `close()` envía un **FIN** que TCP entrega
**después** de los datos pendientes. Con un cliente que no lee (ventana cero), ese FIN nunca sale: el
descriptor se libera pero el socket queda en `FIN-WAIT-1`. `SO_LINGER` con 0 s hace que `close()`
descarte lo pendiente y mande **RST**. Verificado con `ss -tan`.

### 3.4 Transversales: arranque, configuración, métricas y logs

#### `main` (`server/main.py:126`) — el único lugar que construye y conecta (`server/main.py`)

| Elemento | Qué hace |
| --- | --- |
| `load_static` (`server/main.py:40`) | Carga los archivos de `client/` (`.html`, `.js`, `.css`, `.woff2`, `.mp3`) en un dict `{ruta: StaticFile}` al arrancar |
| `App` (`server/main.py:55`) | Agrupa `listener`, `game`, `metrics`. `App.run` (`server/main.py:60`): lanza el tick y sirve; `App.shutdown` (`server/main.py:67`): apagado ordenado |
| `build` (`server/main.py:73`) | Arma el grafo de objetos. Define `new_session` (fábrica de `ClientSession`) y `metrics_page` (`GET /metrics`: solo lectura, así que no rompe el único escritor) |
| `serve_until_signal` (`server/main.py:105`) | Instala SIGINT/SIGTERM (marcan un `Event`: un manejador no puede hacer `await`), corre el servidor y, al recibir la señal, llama a `app.shutdown()` |
| `main` (`server/main.py:126`) | Configura el log, filtro anti-inundación y `asyncio.run(...)` |

#### `Config` (`server/config.py:16`) — todos los números, cada uno con su porqué (`server/config.py`)

Dataclass **congelada** (nadie la modifica en caliente; se pasa por constructor, no es variable global).
`Config.TICK_DT` (`server/config.py:267`) y `Config.QUIET_EVERY_N_TICKS` (`server/config.py:272`) son derivados. `Config.__post_init__` (`server/config.py:281`)
**valida al arrancar** (una configuración incoherente falla al inicio, no a mitad de partida):
*tunneling* (`max(MAX_SPEED, DASH_MAX_SPEED) × dt < DISC_RADIUS`), `SLIDE_FRICTION ≤ FRICTION`,
`QUIET_PHASE_HZ` divide a `TICK_HZ`, `2 ≤ MIN_PLAYERS ≤ MAX_ROOM_PLAYERS ≤ MAX_CONNECTIONS`,
`MAX_ROOMS ≥ 1`, `IDLE_TIMEOUT > PING_INTERVAL`, el disco cabe en la arena, `0 ≤ MIN_CHARGE_S < CHARGE_MAX_S`,
margen de caída no negativo y tamaños de buzón/cola ≥ 1. La tabla completa de constantes está en el **Apéndice A**.

#### `Metrics` (`server/metrics.py:15`) — contadores (`server/metrics.py`)

Varias tareas incrementan contadores sin lock: `x += 1` sin `await` en medio se ejecuta completo antes de que otra
tarea corra (un solo hilo). `Metrics.record_tick` (`server/metrics.py:43`) y `Metrics.record_rtt` (`server/metrics.py:40`) guardan duraciones en
ventanas acotadas (`deque(maxlen)`). `Metrics.snapshot` (`server/metrics.py:46`) calcula **al consultar** (no en cada tick, para no
gastar presupuesto del tick) los percentiles con `_percentile_ms` (`server/metrics.py:84`) (rango más cercano) y es lo que sirve `GET /metrics`.

#### `RateLimitFilter` (`server/log_filters.py:17`) — anti-inundación de logs (`server/log_filters.py`)

Con `ulimit -n 64`, `asyncio` registra "out of system resource" con traceback en cada intento de `accept`
(83 768 líneas y 37 MB en 35 s). `logging` escribe **de forma síncrona**: ese volumen frena el event loop. El filtro deja
pasar uno cada `LOG_REPEAT_INTERVAL_S` (5 s), cuenta los suprimidos y quita el traceback tras el primero.
`RateLimitFilter.filter` (`server/log_filters.py:32`) es el método que decide.


## 4. El cliente (`client/`): HTML + JavaScript + canvas, sin framework

El cliente **no decide nada del juego**: envía **intenciones** (hacia dónde quiere ir, si carga),
nunca posiciones, y dibuja lo que el servidor le manda. El servidor es la autoridad.

### 4.1 `index.html` y `style.css`

El HTML es solo estructura; el aspecto neón (Orbitron, brillos, tarjetas) está en `style.css`. Pantallas: un botón de
sonido (`id="sound"` (`client/index.html:15`)), la **entrada** (`id="entry"` (`client/index.html:23`), con el
formulario `id="join"` (`client/index.html:25`)), el **vestíbulo** (`id="lobby"` (`client/index.html:33`): lista de salas y
*Crear sala*) y la **partida** (`id="game"` (`client/index.html:42`): barra con *Iniciar partida* y *Salir de la sala*, y un `#stage`
(`client/index.html:48`) con el `canvas`, la capa central `#overlay` (`client/index.html:51`: espera y cuenta regresiva), el
marcador `#board` (`client/index.html:63`) y el resultado `#result` (`client/index.html:68`)). `showView()` alterna entrada,
vestíbulo y partida con el atributo `hidden`. Todo el texto de DOM que cambia con el juego (HUD, marcador, capa central,
resultado) se reescribe **solo cuando llega un tick nuevo y su contenido cambió**, no a 60 Hz.

### 4.2 `client.js`: estado y mensajes

| Elemento | Qué hace | Se conecta con |
| --- | --- | --- |
| `els` (`client/client.js:61`) | Referencias a los elementos del DOM | todo el archivo |
| `session` (`client/client.js:107`) | **Todo el estado del cliente en un objeto**: socket, mi id, sala actual (`room`, `null` = vestíbulo), lista de salas, anfitrión, roster, último estado | los manejadores |
| `handlers` (`client/client.js:181`) | **Registro `{tipo de mensaje: manejador}`** (igual que el codec del servidor): `welcome`, `rooms`, `room`, `roster`, `state`, `error`. Un tipo nuevo es una entrada nueva | `onMessage` |
| `onMessage` (`client/client.js:232`) | `JSON.parse` y despacha; un tipo desconocido se ignora (un servidor más nuevo no rompe a un cliente viejo). Usa `Object.hasOwn` para que `t:"constructor"` no encuentre métodos heredados | `socket.message` |
| `connect` (`client/client.js:305`) | Limpia el estado, abre el WebSocket (`wss` si la página es https), envía `hello` al abrir, y al cerrar vuelve a la pantalla inicial | formulario de nombre |
| `resetRoomState` (`client/client.js:247`) | Borra lo que pertenece a **una sala** (estado, jugadores, controles, interpolación, marcador) | `room`, `connect`, `close` |
| `showView` (`client/client.js:269`) | Muestra el vestíbulo o la partida | `room`, `connect`, `close` |
| `renderRooms` (`client/client.js:277`) | Dibuja la lista de salas con **`textContent`**, nunca HTML (los nombres los eligen otros jugadores). El botón se deshabilita si `!room.open` | `rooms` |
| `send` (`client/client.js:368`) | `socket.send(JSON.stringify(msg))` solo si el socket está abierto | controles y botones |

### 4.3 Interpolación (dibujar en el pasado)

Los estados llegan a 30 Hz (o 10 Hz fuera de juego) con jitter; dibujarlos tal cual se ve a tirones. Se guardan con su **hora de llegada**
y se dibuja ~100 ms en el pasado mezclando los dos estados que rodean ese instante.

| Función | Qué hace |
| --- | --- |
| `timeline` (`client/client.js:119`) | Estados recibidos con su hora de llegada y el intervalo medio |
| `pushSnapshot` (`client/client.js:126`) | Guarda el estado y actualiza el intervalo medio (media móvil). Tope `MAX_SNAPSHOTS` |
| `interpolatedDiscs` (`client/client.js:162`) | Calcula los discos en `now - retraso`; el retraso es `max(100 ms, 1,5 × intervalo)`; si no hay dos estados que mezclar, mantiene el último |
| `lerpDiscs` (`client/client.js:150`), `lerp` (`client/client.js:146`) | Mezcla lineal de posición y carga; el `vivo` se toma del estado anterior |
| `startsNewRound` (`client/client.js:142`) | Al empezar la cuenta atrás los discos **saltan** a su punto de salida: no se mezclan (se deslizarían por la arena) |

### 4.4 Controles

| Elemento | Qué hace |
| --- | --- |
| `MOVE_KEYS` (`client/client.js:345`) | `event.code` (tecla **física**, igual en cualquier teclado) → dirección. `dy = -1` es arriba porque el canvas crece hacia abajo |
| `CHARGE_CODES` (`client/client.js:353`) | Espacio, J y K cargan (con varias, el *ghosting* del teclado no impide cargar) |
| `controls` (`client/client.js:355`) | Teclas pulsadas, última intención enviada, si se carga, fin del enfriamiento (estimado) y hacia dónde apunta |
| `currentIntent` (`client/client.js:373`) | Suma las direcciones pulsadas con `Math.sign` (ejes en -1/0/1) |
| `syncIntent` (`client/client.js:388`) | **Solo envía si la intención cambió**: el servidor recuerda la dirección y limita a 4 órdenes por tick |
| `ownCharge` (`client/client.js:396`), `setCharging` (`client/client.js:401`) | Carga de mi disco; al soltar con carga suficiente (`min_charge`) estima el enfriamiento para el anillo gris |
| `resetControls` (`client/client.js:417`), `newRound` (`client/client.js:429`), `resyncControls` (`client/client.js:439`) | Olvidan teclas/puntería; al empezar a jugar se **reenvía** la tecla que el jugador siga manteniendo (el cliente creería que ya la mandó) |
| `phaseEntered` (`client/client.js:455`) | Registro `{fase: función}` que se ejecuta al entrar a una fase: `countdown` → `newRound`, `playing` → `resyncControls`, `round_over` → `roundOver` (`client/client.js:451`: corta la carga y, si ganaste, suena el jingle) |
| `isTyping` (`client/client.js:462`) | Si se escribe el nombre, las teclas no son órdenes de juego |
| `window.addEventListener("keydown"` (`client/client.js:466`), `window.addEventListener("keyup"` (`client/client.js:483`) | Pulsar/soltar; `keyup` **no** mira `isTyping` a propósito (soltar siempre debe contar) |
| `window.addEventListener("blur"` (`client/client.js:496`) | Si la ventana pierde el foco con una tecla pulsada el navegador no manda `keyup`: se suelta todo |
| `els.create.addEventListener` (`client/client.js:503`), `els.leave.addEventListener` (`client/client.js:504`), `els.start.addEventListener` (`client/client.js:509`) | Botones: crear sala, salir, iniciar (hacen `blur()` para que Espacio no los "pulse" en plena ronda) |

### 4.5 Pantalla

| Función | Qué hace |
| --- | --- |
| `frame` (`client/client.js:892`) | Bucle con `requestAnimationFrame`: dibuja a la frecuencia de la pantalla y reescribe el DOM (HUD, marcador, capa central, resultado, botón de iniciar) **solo cuando llega un tick nuevo**. Con `?fps` en la URL mide FPS |
| `draw` (`client/client.js:830`) | Orquesta UN fotograma: copia la capa estática de la arena, interpola los discos, detecta eliminaciones, llama a `paintDisc` por robot y al final dibuja columnas y partículas |
| `observePair` (`client/client.js:762`) | **Deduce lo que el servidor no manda**: velocidad por disco = desplazamiento entre dos `state` / diferencia de `tick` (no depende del jitter de red). De ahí salen embestida (soltar la carga), golpe (cambio brusco de velocidad) y el sonido correspondiente |
| `aimOf` (`client/client.js:806`) | Hacia dónde apunta la flecha del propio disco: la última dirección enviada o, sin ninguna, el centro (como el servidor) |
| `pixelsPerUnit` (`client/client.js:813`), `warmSprites` (`client/client.js:820`) | Escala mundo→píxeles; hornea los sprites de todos los colores al llegar el `roster` (sala de espera), para que ningún fotograma de la ronda pague ese coste |
| `overlayFor` (`client/client.js:568`), `renderOverlay` (`client/client.js:730`), `hideOverlay` (`client/client.js:659`) | Texto central por fase (registro `{fase: función}`) **en DOM**: sala de espera (distinto para el anfitrión), cuenta atrás, empate/ganador. Solo toca el DOM si el texto cambió |
| `syncCountdownRing` (`client/client.js:714`) | Anillo SVG de la cuenta regresiva: se vacía en cada segundo con la API de animaciones (WAAPI), empezando por lo que queda de ese segundo |
| `renderResult` (`client/client.js:677`), `hideResult` (`client/client.js:670`) | Pantalla de resultado en `round_over`: copa, título, top 5 por victorias y botón *Volver al vestíbulo* (envía `leave`). Dura lo que dure la fase (`RESULTS_S`, 3 s) |
| `describe` (`client/client.js:556`) | Texto del HUD (fase, segundos, "embestida lista/recargando") |
| `scoreRows` (`client/client.js:603`), `renderScores` (`client/client.js:620`) | Filas del marcador sobre el canvas (más victorias primero, corona al líder, contador `n/máx`); el DOM solo se reconstruye si cambió |
| `isHost` (`client/client.js:591`), `syncStartButton` (`client/client.js:597`) | El botón *Iniciar partida* solo para el anfitrión y solo en espera. Es comodidad: el servidor ignora la orden de cualquier otro |
| `discColor` (`client/client.js:543`), `nameOf` (`client/client.js:547`), `amPlaying` (`client/client.js:552`), `setStatus` (`client/client.js:539`) | Utilidades; `setStatus` usa `textContent` (nunca `innerHTML`) |

### 4.6 `neon.js`: el dibujo (todo con código, sin imágenes)

Regla de rendimiento que lo gobierna: **`shadowBlur` hace un desenfoque por cada trazo**, así que solo se usa al *construir*
capas y sprites (una vez), nunca dentro de un fotograma. Cada fotograma hace `drawImage`, arcos y líneas. Medido en Chrome
con 8 jugadores: ~143 FPS (pantalla a 144 Hz), también con la CPU frenada 4×.

| Elemento | Qué hace |
| --- | --- |
| `buildArenaLayer` (`client/neon.js:425`) | Capa **estática** en un `OffscreenCanvas`: cielo, ciudad, luna, props de azotea, piso en placas, reborde, anillo de neón en tramos y corona. Se pinta UNA vez; cada fotograma es un `drawImage` |
| `paintRobot` (`client/neon.js:631`), `ROBOT_ACCESSORIES` (`client/neon.js:571`) | Sprite de robot visto desde arriba (cuerpo, visor, dos ojos, antenas) + un accesorio por color. Los accesorios son un **registro** `{nombre: accesorio}`, no una cadena de `if` |
| `Sprites` (`client/neon.js:705`) | Cachés de sprites horneados: robot, silueta blanca, halo, punto, cuadrado, estrella, columna, etiquetas de nombre (con tope `LABEL_CACHE_MAX`: los nombres los elige la gente) |
| `ParticlePool` (`client/neon.js:891`), `EliminationBeams` (`client/neon.js:984`) | Partículas en arreglos de tamaño fijo con **tope** `MAX_PARTICLES` (al llenarse se pisa la más antigua); columna de luz violeta al eliminar |
| `DiscFx` (`client/neon.js:1034`), `FxTracker` (`client/neon.js:1077`) | Estado de animación por disco: rumbo, velocidad, destello, muerte, estela |
| `paintDisc` (`client/neon.js:1173`) | Un robot con todas sus animaciones: balanceo en reposo, brillo y anillo al cargar, estela y líneas de velocidad en la embestida, destello y estrellas al golpe, encoger y apagar al eliminar |
| `glowArc` (`client/neon.js:1101`), `drawAimArrow` (`client/neon.js:1117`), `drawOwnMarker` (`client/neon.js:1146`) | Anillo con "brillo" de dos trazos (ancho y tenue + fino y vivo, sin sombras); flecha de puntería; triángulo sobre el propio robot |
| `TRAIL_MIN_UNITS_PER_TICK` (`client/neon.js:127`), `HIT_MIN_DV_UNITS_PER_TICK` (`client/neon.js:128`), `DASH_GRACE_MS` (`client/neon.js:129`) | Umbrales con que el cliente **infiere** embestida y golpe. Límite honesto: dependen de `config.py` (`MAX_SPEED`, `DASH_MAX_SPEED`, `TICK_HZ`) y el cliente no los recibe; si cambian allí hay que reajustarlos |
| `PerfMeter` (`client/neon.js:1307`) | Con `?fps` en la URL: FPS, "1 % peor" y ms de dibujo; deja `window.__perf` legible desde fuera |

### 4.7 `audio.js`: música y efectos

| Elemento | Qué hace |
| --- | --- |
| `MUSIC_TRACKS` (`client/audio.js:20`), `SFX_TRACKS` (`client/audio.js:25`) | Tabla de pistas y volúmenes (medidos en dBFS; la razón está en el comentario). Cambiar de pista es cambiar una entrada. Origen y licencia: `client/CREDITS.txt` |
| `GameAudio` (`client/audio.js:43`) | Música desde un `Blob` (el servidor no entiende `Range`) y efectos con Web Audio (`AudioBuffer`, se pueden solapar). Hasta el primer gesto del jugador (`unlock`) todo es mudo: es la política de autoplay del navegador |
| `applyMusic` (`client/audio.js:99`), `transitionTo` (`client/audio.js:109`) | Las transiciones de música se **encadenan en serie**: la pista que sale se apaga entera antes de que entre la otra. Nunca suenan dos a la vez (medido: 0 solapes en 1083 muestras de una partida y 0 en un estrés cada 120 ms) |
| `play` (`client/audio.js:160`), `startCharge` (`client/audio.js:177`) | Un efecto = un `BufferSource` nuevo; limitador de 70 ms por efecto; la carga suena mientras se mantiene la tecla y se corta al soltar |
| `musicForPhase` (`client/client.js:27`), `syncMusic` (`client/client.js:458`) | Registro `{fase: pista}` (batalla solo jugando; el resto, la del vestíbulo) y quien la aplica |

Si un archivo de audio no carga, el juego sigue sin ese sonido: el audio nunca debe romper la partida.

## 5. Dónde está lo importante (para la defensa)

Ordenado por peso en la rúbrica. Si te preguntan una cosa, **ve directo a esta línea**.

| # | Tema | Dónde | Qué decir |
| --- | --- | --- | --- |
| 1 | **Único escritor y tick sin `await`** | `GameSession.tick` (`server/application/game_session.py:121`) | Solo el tick modifica vestíbulo, salas y observadores; no hay `await` entre leer y modificar, así que no hacen falta cerrojos |
| 2 | **Nunca esperar a un cliente en el tick** (HOL blocking) | `GameSession._publish_states` (`server/application/game_session.py:211`), `ClientSession.on_snapshot` (`server/adapters/client_session.py:86`) | El tick deja bytes en el buzón y sigue; un cliente lento solo frena su `writer`. Demo: rama `demo/race-bug` |
| 3 | **Backpressure: buzón de tamaño 1 + plazo de escritura** | `ClientSession.on_snapshot` (`server/adapters/client_session.py:86`), `ClientSession._drain` (`server/adapters/client_session.py:282`) | Si no lee, se le pisa el estado viejo; a los 10 s se le aborta |
| 4 | **Un solo escritor por socket y orden `wait`/`clear`/leer** | `ClientSession._write_loop` (`server/adapters/client_session.py:247`) | Si se leyera antes del `clear`, se perdería un aviso |
| 5 | **Lecturas parciales de TCP** | `FrameReader.next_frame` (`server/adapters/ws_frames.py:97`), `ClientSession._read_loop` (`server/adapters/client_session.py:149`) | Acumula y entrega tramas completas; `next_frame` se llama en bucle |
| 6 | **Tope antes de reservar memoria** | `FrameReader.next_frame` (`server/adapters/ws_frames.py:97`), `read_head` (`server/adapters/http_handshake.py:112`) | Se valida la longitud anunciada apenas se conoce |
| 7 | **Socket a mano y `SO_REUSEADDR`** | `create_listen_socket` (`server/adapters/listener.py:25`) | `bind`, `listen(128)`, no bloqueante; reinicio inmediato sin `Address already in use` |
| 8 | **`backlog=` explícito en `start_server`** | `Listener.serve` (`server/adapters/listener.py:78`) | Si no, `asyncio` vuelve a llamar a `listen(100)` |
| 9 | **Semáforo y rechazo (`full`/1013)** | `Listener._on_connect` (`server/adapters/listener.py:122`), `Listener._reject` (`server/adapters/listener.py:177`) | Cupo tomado sin suspensión; rechazar también está acotado |
| 10 | **Todo recurso en un `finally`** | `Listener._on_connect` (`server/adapters/listener.py:122`), `ClientSession.run` (`server/adapters/client_session.py:122`) | Socket, cupo, tareas y `Disconnect` se liberan pase lo que pase |
| 11 | **Tres timeouts** | `Listener._handshake_with_timeout` (`server/adapters/listener.py:166`), `ClientSession._read_timeout` (`server/adapters/client_session.py:172`), `ClientSession._drain` (`server/adapters/client_session.py:282`) | Saludo (5 s, total), inactividad (30 s con ping cada 10 s) y escritura (10 s) |
| 12 | **Apagado con SIGTERM** | `serve_until_signal` (`server/main.py:105`), `Listener.shutdown` (`server/adapters/listener.py:97`) | Handler → `Event`; close 1001 a todos; gracia de 2 s; aborta rezagados |
| 13 | **`abort_with_reset` (RST)** | `abort_with_reset` (`server/adapters/net_util.py:11`) | `abort()` deja el socket en `FIN-WAIT-1` con ventana cero |
| 14 | **Patrón Command + doble despacho** | `Command.run` (`server/domain/commands.py:35`), `HallCommand.run` (`server/domain/commands.py:53`), `GameSession._apply_inputs` (`server/application/game_session.py:135`) | El tick no distingue tipos; el límite por cliente y tick es parejo |
| 15 | **Patrón State** | `RoundState` (`server/domain/round_states.py:34`), `Room.step` (`server/domain/room.py:21`) | El tick no pregunta la fase; cada fase devuelve la siguiente |
| 16 | **Reglas de las salas** | `Hall.join_room` (`server/domain/hall.py:99`), `Hall.leave_room` (`server/domain/hall.py:113`) | Solo se entra con sitio y en espera; sala vacía desaparece; rechazos sin excepciones |
| 17 | **Patrón Factory Method (registro)** | `game_codec` (`server/adapters/codec.py:134`), `MessageCodec.decode` (`server/adapters/codec.py:49`) | Un mensaje nuevo es una entrada, no un `elif`; toda la validación está aquí |
| 18 | **Patrón Strategy** | `SendPolicy` (`server/application/send_policies.py:12`), `build` (`server/main.py:73`) | Frecuencia de envío por fase de la sala |
| 19 | **Patrón Observer / puertos** | `SnapshotObserver` (`server/application/ports.py:52`) | `GameSession` no conoce el WebSocket |
| 20 | **Configuración validada** | `Config.__post_init__` (`server/config.py:281`) | Una configuración incoherente falla al arrancar |
| 21 | **Física determinista** | `Physics.step` (`server/domain/physics.py:20`) | Paso fijo, orden documentado, tope de velocidad contra el *tunneling* |
| 22 | **Paso fijo del tick** | `GameSession.run_tick_loop` (`server/application/game_session.py:233`) | `next_at += dt`; sin espiral de recuperación |
| 23 | **Anti-inundación de logs** | `RateLimitFilter.filter` (`server/log_filters.py:32`) | `logging` es síncrono |

## 6. Reglas de concurrencia: dónde se cumple cada una (lista de auditoría de `AGENTS.md` §9)

| Pregunta de auditoría | Respuesta | Evidencia |
| --- | --- | --- |
| ¿Hay alguna llamada bloqueante en el event loop? | No. La E/S de archivos solo ocurre al arrancar | `load_static` (`server/main.py:40`) |
| ¿Hay un `await` sobre un socket dentro del tick? | No. `tick()` es síncrono; el único `await` del bucle es `asyncio.sleep` | `GameSession.tick` (`server/application/game_session.py:121`), `GameSession.run_tick_loop` (`server/application/game_session.py:233`) |
| ¿Alguna tarea distinta del tick modifica estado, registro u observadores? | No. Entrar, salir, crear, unirse, iniciar, mover y cargar son comandos | `GameSession.submit_input` (`server/application/game_session.py:70`), `GameSession.submit_membership` (`server/application/game_session.py:79`) |
| ¿Hay un `await` entre leer y modificar un recurso compartido? | No en el tick. En `_on_connect`, entre `locked()` y `acquire()` no hay suspensión (comentado en el código) | `Listener._on_connect` (`server/adapters/listener.py:122`) |
| ¿Dos tareas escriben en el mismo socket? | No. Solo el `writer`. Antes del hello escribe `HttpHandshake.handle` (el writer aún no existe) | `ClientSession._write_loop` (`server/adapters/client_session.py:247`), `HttpHandshake.handle` (`server/adapters/http_handshake.py:194`) |
| ¿Se asume que `read()` trae un mensaje completo? | No | `FrameReader.next_frame` (`server/adapters/ws_frames.py:97`) |
| ¿Todos los caminos de salida cierran el socket y liberan el semáforo? | Sí, en `finally` | `Listener._on_connect` (`server/adapters/listener.py:122`), `Listener._close` (`server/adapters/listener.py:199`) |
| ¿Una excepción de un cliente puede tumbar el servidor? | No: se captura y se cierra solo esa conexión; un observador roto no detiene la difusión; un bug en el tick no lo congela | `Listener._serve_client` (`server/adapters/listener.py:146`), `ClientSession.run` (`server/adapters/client_session.py:122`), `GameSession._notify` (`server/application/game_session.py:223`) |
| ¿Hay números mágicos fuera de `config.py`? | Solo constantes del protocolo (códigos de cierre, formato de trama) y de formato del cliente | `Config` (`server/config.py:16`), `CloseCode` (`server/adapters/ws_frames.py:36`) |
| ¿El dominio importa algo de red? | No (hay una prueba que lo comprueba) | `tests/test_world.py::test_dominio_no_importa_red` |
| ¿Hay un `if/elif` que debería ser polimorfismo o registro? | Los despachos son registros: codec, opcodes de trama, fases del cliente, manejadores del cliente. El `if/elif` de `Hall.join_room` son **reglas de negocio** en cadena, no un despacho por tipo | `game_codec` (`server/adapters/codec.py:134`), `ClientSession.__init__` (`server/adapters/client_session.py:39`), `handlers` (`client/client.js:181`) |

## 7. Patrones y SOLID

| Patrón | Dónde | Problema que resuelve |
| --- | --- | --- |
| **State** | `RoundState` (`server/domain/round_states.py:34`) y sus 4 fases | El tick no tiene un `if fase == ...` que crezca con cada fase |
| **Command** | `Command` (`server/domain/commands.py:28`), `HallCommand` (`server/domain/commands.py:44`), `MembershipCommand` (`server/application/session_commands.py:28`) | Separa cuándo llega una orden de cuándo se ejecuta: base del único escritor |
| **Observer** | `SnapshotObserver` (`server/application/ports.py:52`) ← `ClientSession` (`server/adapters/client_session.py:38`) | El tick publica sin saber quién recibe ni cómo se envía |
| **Strategy** | `SendPolicy` (`server/application/send_policies.py:12`) | Frecuencia de envío por fase sin tocar el tick |
| **Factory Method (registro)** | `MessageCodec` (`server/adapters/codec.py:44`) | Mensaje nuevo = entrada nueva, no otro `elif` |

SOLID en una línea cada uno: **S** una clase, una razón de cambio (`FrameReader` solo parte bytes; `Physics` solo mueve discos; `Hall` solo
decide quién está en qué sala). **O** agregar una fase, un mensaje o una política es agregar una clase o una entrada de registro. **L** toda
fase y todo comando cumplen su contrato (prueba de fuzz de 5000 ticks). **I** los puertos son pequeños. **D** `GameSession` depende de
`SnapshotObserver`/`SnapshotEncoder`; `main.build` inyecta las concretas.

## 8. Límites honestos (lo que NO está perfecto)

Si te lo preguntan, es mejor decirlo tú que que lo encuentren.

1. **Colores repetidos.** El color sale de `id % 12`: dos jugadores de una misma sala cuyos ids difieran en un múltiplo de 12 comparten color
   (el nombre sobre el disco los distingue). Arreglo posible: asignar color por puesto en la sala.
2. **Un cliente puede llenar la cola de entradas antes de que se le aplique su tope.** El límite de 4 órdenes por tick se aplica al **consumir**
   (`GameSession._apply_inputs` (`server/application/game_session.py:135`)); la cola compartida (1024) se llena antes si un cliente envía decenas de miles de mensajes por
   segundo, y entonces se descartan también entradas de otros (`inputs_dropped_queue`). Mitigación posible: tope por cliente al encolar.
3. **`logging` es síncrono.** Un destino lento frena el event loop. Solo hay filtro para el error de `accept` sin descriptores; la mitigación
   completa sería un `QueueHandler` con un hilo aparte (no hecho).
4. **La inactividad no distingue actividad útil.** Cualquier dato recibido renueva el plazo de 30 s. Los bytes basura se cierran por framing
   inválido, pero un cliente que envíe **tramas válidas** sin parar (p. ej. pings vacíos) nunca vence por inactividad, aunque no juegue.
5. **Un anfitrión inactivo bloquea su sala.** Si el anfitrión no pulsa *Iniciar*, la sala espera (sus clientes contestan ping, así que no vencen).
   No hay "pasar el turno de anfitrión" automático ni expulsión por inactividad en la sala.
6. **Nombres no únicos.** Dos jugadores pueden llamarse igual.
7. **Los avisos comparten una casilla:** si llegan un rechazo y un cambio de sala en el mismo tick, gana el último (el estado final queda bien; un
   texto de error podría no verse).
8. **Sin TLS en el servidor.** Para `wss://` hace falta un proxy (Railway, `cloudflared`). El plan B (P9) no está hecho: `HOST` es `127.0.0.1` y el
   puerto es fijo en `Config`.
9. **`GET /metrics` no tiene autenticación** (solo escucha en localhost por defecto).
10. **Techo de un núcleo.** Medido: ~1000 clientes × 30 Hz ya consumen ~90 % de un núcleo (el tope de diseño es 100). Para pasar de ahí habría
    que repartir salas entre procesos (`SO_REUSEPORT`).
11. **El anillo de enfriamiento del cliente es una estimación** (el servidor no manda su enfriamiento).
12. **No se probó automáticamente en un navegador real:** el cliente se verifica con un arnés de Node (DOM simulado) y a mano.

## 9. Pruebas y herramientas: qué demuestra cada una

| Archivo | Qué demuestra |
| --- | --- |
| `tests/test_ws_frames.py` | Framing: pedazos de 1 byte, varias tramas, longitudes 126/127, sin máscara, tope de tamaño |
| `tests/test_http_handshake.py` | Saludo, upgrade (101), cabeceras sin distinguir mayúsculas, peticiones inválidas, 426 con la versión soportada, cabecera en pedazos de 1 byte, sobrante, tope de cabecera (431) |
| `tests/test_codec.py` | Validación de cada mensaje y formato de cada respuesta (incluidos `rooms`, `room`) |
| `tests/test_physics.py` | Velocidad máxima, fricción, colisiones, embestida, enfriamiento, carga mínima, anti-*tunneling* |
| `tests/test_world.py` | Un mundo: registro, discos, eliminación, el dominio no importa red |
| `tests/test_hall.py` | Reglas de salas: tope de 8, sala llena/jugando/inexistente, salas vacías, independencia entre salas |
| `tests/test_round_states.py` | Cada fase, el anfitrión, el fuzz de 5000 ticks |
| `tests/test_game_session.py` | El tick sin sockets: una codificación por sala, aislamiento entre salas, avisos, roster, frecuencia por fase, límites |
| `tests/test_client_session_write.py`, `tests/test_net_util.py`, `tests/test_log_filters.py`, `tests/test_metrics.py` | Escritura con plazo, RST, filtro de logs, percentiles |
| `tests/test_server_e2e.py` | Servidor **real** con clientes reales: 30 Hz, entradas, salas por el cable, tope de conexiones, timeouts, apagado, `full`/1013 |
| `tools/monitor.py` | Una línea por segundo: ticks/s, p50/p99 del tick, RTT, salas, descriptores, memoria, CPU |
| `tools/bots.py` | N jugadores simulados que forman salas |
| `tools/slow_client.py` | Cliente que no lee: no frena a los demás |
| `tools/churn.py`, `tools/leakcheck.py`, `tools/fd_watch.sh` | Fugas de descriptores y `CLOSE_WAIT` |
| `tools/scenario_check.py` | 11 comprobaciones de física y rondas contra el servidor real |
| `tools/model_bench.py` | Compara asyncio, hilo por cliente, pool y proceso por cliente con la misma carga |
| `tools/race_demo.py` | La versión mala (`demo/race-bug`) contra la buena |

---

## Apéndice A. Todas las constantes de `Config` y dónde se usan

Generada leyendo el código. "Dónde se usa" lista los archivos que mencionan la constante (si solo aparece
`config.py`, solo se usa en la validación o en un valor derivado). El **porqué** de cada valor está en el comentario sobre cada una en `server/config.py`.

| Constante | Valor | Dónde se define | Dónde se usa (archivos) |
| --- | --- | --- | --- |
| `HOST` | `'127.0.0.1'` | `config.py:23` | main.py |
| `PORT` | `8080` | `config.py:26` | main.py |
| `LISTEN_BACKLOG` | `128` | `config.py:33` | listener.py, main.py |
| `MAX_CONNECTIONS` | `100` | `config.py:39` | listener.py, metrics.py |
| `SEND_BUFFER_BYTES` | `65536` | `config.py:50` | listener.py |
| `MAX_REJECTING_HANDSHAKES` | `16` | `config.py:56` | listener.py |
| `MAX_FRAME_BYTES` | `4096` | `config.py:61` | client_session.py |
| `MAX_HTTP_HEADER_BYTES` | `8192` | `config.py:67` | main.py |
| `READ_CHUNK_BYTES` | `4096` | `config.py:72` | client_session.py, main.py |
| `TICK_HZ` | `30` | `config.py:80` | main.py, physics.py, round_states.py |
| `QUIET_PHASE_HZ` | `10` | `config.py:86` | (solo validación en config.py) |
| `INPUT_QUEUE_SIZE` | `1024` | `config.py:99` | game_session.py |
| `MAX_INPUTS_PER_CLIENT_PER_TICK` | `4` | `config.py:104` | game_session.py, metrics.py |
| `MAX_NAME_LEN` | `16` | `config.py:107` | codec.py |
| `POSITION_DECIMALS` | `1` | `config.py:111` | codec.py |
| `CHARGE_DECIMALS` | `2` | `config.py:112` | codec.py |
| `METRICS_TICK_WINDOW` | `300` | `config.py:116` | main.py |
| `LOG_REPEAT_INTERVAL_S` | `5.0` | `config.py:121` | main.py |
| `HANDSHAKE_TIMEOUT_S` | `5.0` | `config.py:132` | client_session.py, http_handshake.py, listener.py |
| `PING_INTERVAL_S` | `10.0` | `config.py:137` | client_session.py, metrics.py |
| `IDLE_TIMEOUT_S` | `30.0` | `config.py:138` | client_session.py, metrics.py |
| `WRITE_TIMEOUT_S` | `10.0` | `config.py:142` | client_session.py, listener.py |
| `SHUTDOWN_GRACE_S` | `2.0` | `config.py:146` | listener.py |
| `MAX_ROOM_PLAYERS` | `8` | `config.py:157` | game_session.py, hall.py |
| `MAX_ROOMS` | `25` | `config.py:162` | hall.py |
| `MIN_PLAYERS_TO_START` | `2` | `config.py:165` | game_session.py, round_states.py |
| `COUNTDOWN_S` | `3.0` | `config.py:169` | round_states.py |
| `RESULTS_S` | `3.0` | `config.py:170` | round_states.py |
| `CHARGE_MAX_S` | `1.0` | `config.py:174` | disc.py, game_session.py, physics.py |
| `MIN_CHARGE_S` | `0.2` | `config.py:180` | game_session.py, physics.py |
| `DASH_COOLDOWN_S` | `1.5` | `config.py:181` | game_session.py, physics.py |
| `ARENA_RADIUS` | `300.0` | `config.py:189` | game_session.py, main.py |
| `DISC_RADIUS` | `24.0` | `config.py:193` | game_session.py, main.py, physics.py |
| `ELIMINATION_MARGIN_DISCS` | `1.0` | `config.py:201` | main.py |
| `ACCELERATION` | `850.0` | `config.py:206` | physics.py |
| `MAX_SPEED` | `170.0` | `config.py:211` | physics.py |
| `FRICTION_PER_S` | `4.0` | `config.py:216` | physics.py |
| `SLIDE_FRICTION_PER_S` | `2.0` | `config.py:225` | physics.py |
| `DASH_MAX_SPEED` | `600.0` | `config.py:232` | physics.py |
| `DASH_MASS_FACTOR` | `2.0` | `config.py:238` | physics.py |
| `DISC_MASS` | `1.0` | `config.py:242` | physics.py |
| `SPAWN_RING_FRACTION` | `0.6` | `config.py:248` | world.py |
| `PLAYER_COLORS` | `('#e4572e', '#29b6f6', '#...` | `config.py:254` | world.py |
| `TICK_DT` (derivado) | calculado | `config.py:264` | game_session.py, round_states.py |
| `QUIET_EVERY_N_TICKS` (derivado) | calculado | `config.py:269` | main.py |

## Apéndice B. Índice de símbolos con su línea

Generado leyendo el código: clases y funciones de primer nivel y sus métodos.

**`server/adapters/client_session.py`**
- `log` — línea 35
- `ClientSession` — línea 38
- `ClientSession.__init__` — línea 39
- `ClientSession.on_snapshot` — línea 86
- `ClientSession.request_shutdown` — línea 92
- `ClientSession.on_welcome` — línea 98
- `ClientSession.on_roster` — línea 102
- `ClientSession.on_rooms` — línea 108
- `ClientSession.on_notice` — línea 113
- `ClientSession.run` — línea 122
- `ClientSession._read_loop` — línea 149
- `ClientSession._read_timeout` — línea 172
- `ClientSession._on_read_timeout` — línea 181
- `ClientSession._on_text` — línea 190
- `ClientSession._deliver_hello` — línea 195
- `ClientSession._deliver_input` — línea 202
- `ClientSession._on_ping` — línea 205
- `ClientSession._on_pong` — línea 210
- `ClientSession._on_close` — línea 219
- `ClientSession._request_close` — línea 224
- `ClientSession._ping_loop` — línea 232
- `ClientSession._write_loop` — línea 247
- `ClientSession._drain` — línea 282

**`server/adapters/codec.py`**
- `_AXIS_VALUES` — línea 28
- `_COMPACT` — línea 29
- `MessageError` — línea 32
- `MessageError.__init__` — línea 35
- `Factory` — línea 41
- `MessageCodec` — línea 44
- `MessageCodec.__init__` — línea 45
- `MessageCodec.decode` — línea 49
- `_is_int` — línea 74
- `_make_hello` — línea 79
- `_make_hello.hello` — línea 80
- `_input` — línea 93
- `_charge` — línea 100
- `_start` — línea 107
- `_create_room` — línea 111
- `_join_room` — línea 115
- `_leave_room` — línea 122
- `_second_hello` — línea 126
- `hello_codec` — línea 130
- `game_codec` — línea 134
- `JsonFrameEncoder` — línea 148
- `JsonFrameEncoder.__init__` — línea 152
- `JsonFrameEncoder._frame` — línea 157
- `JsonFrameEncoder.encode_state` — línea 160
- `JsonFrameEncoder.encode_welcome` — línea 173
- `JsonFrameEncoder.encode_roster` — línea 181
- `JsonFrameEncoder.encode_rooms` — línea 185
- `JsonFrameEncoder.encode_room` — línea 190
- `JsonFrameEncoder.encode_error` — línea 194

**`server/adapters/http_handshake.py`**
- `_WS_GUID` — línea 25
- `_WS_VERSION` — línea 26
- `_WS_KEY_BYTES` — línea 27
- `_HEAD_END` — línea 28
- `HttpError` — línea 31
- `HttpError.__init__` — línea 32
- `HttpRequest` — línea 39
- `StaticFile` — línea 47
- `HandshakeResult` — línea 53
- `accept_key` — línea 61
- `parse_request` — línea 67
- `_header_has_token` — línea 90
- `_response` — línea 95
- `error_response` — línea 101
- `read_head` — línea 112
- `HttpHandshake` — línea 136
- `HttpHandshake.__init__` — línea 137
- `HttpHandshake.respond` — línea 151
- `HttpHandshake._upgrade` — línea 170
- `HttpHandshake.handle` — línea 194
- `HttpRequest.method` — línea 40
- `HttpRequest.path` — línea 41
- `HttpRequest.version` — línea 42
- `HttpRequest.headers` — línea 43
- `StaticFile.content` — línea 48
- `StaticFile.content_type` — línea 49
- `HandshakeResult.upgraded` — línea 54
- `HandshakeResult.leftover` — línea 58

**`server/adapters/listener.py`**
- `log` — línea 20
- `SessionFactory` — línea 22
- `create_listen_socket` — línea 25
- `Listener` — línea 49
- `Listener.__init__` — línea 50
- `Listener.port` — línea 75
- `Listener.serve` — línea 78
- `Listener.shutdown` — línea 97
- `Listener._limit_send_buffer` — línea 113
- `Listener._on_connect` — línea 122
- `Listener._serve_client` — línea 146
- `Listener._handshake_with_timeout` — línea 166
- `Listener._reject` — línea 177
- `Listener._close` — línea 199

**`server/adapters/net_util.py`**
- `_LINGER_RST` — línea 8
- `abort_with_reset` — línea 11

**`server/adapters/ws_frames.py`**
- `Opcode` — línea 27
- `CloseCode` — línea 36
- `_FIN` — línea 50
- `_RSV` — línea 51
- `_OPCODE` — línea 52
- `_MASK` — línea 53
- `_LEN7` — línea 54
- `_LEN16_MARK` — línea 55
- `_LEN64_MARK` — línea 56
- `_MAX_CONTROL_PAYLOAD` — línea 57
- `_MASK_KEY_LEN` — línea 58
- `_CONTROL` — línea 60
- `Frame` — línea 64
- `ProtocolError` — línea 69
- `ProtocolError.__init__` — línea 72
- `FrameReader` — línea 78
- `FrameReader.__init__` — línea 90
- `FrameReader.feed` — línea 94
- `FrameReader.next_frame` — línea 97
- `FrameReader._check_opcode` — línea 147
- `_unmask` — línea 162
- `FrameWriter` — línea 175
- `FrameWriter.encode` — línea 184
- `FrameWriter.text` — línea 198
- `FrameWriter.ping` — línea 203
- `FrameWriter.pong` — línea 207
- `FrameWriter.close` — línea 212
- `Frame.opcode` — línea 65
- `Frame.payload` — línea 66

**`server/application/game_session.py`**
- `log` — línea 34
- `GameSession` — línea 37
- `GameSession.__init__` — línea 38
- `GameSession.submit_input` — línea 70
- `GameSession.submit_membership` — línea 79
- `GameSession.subscribe` — línea 84
- `GameSession.unsubscribe` — línea 98
- `GameSession.stats` — línea 104
- `GameSession.tick` — línea 121
- `GameSession._apply_membership` — línea 131
- `GameSession._apply_inputs` — línea 135
- `GameSession._publish_notices` — línea 152
- `GameSession._publish_rooms_if_changed` — línea 167
- `GameSession._publish_rosters_if_changed` — línea 181
- `GameSession._snapshot` — línea 201
- `GameSession._publish_states` — línea 211
- `GameSession._notify` — línea 223
- `GameSession.run_tick_loop` — línea 233

**`server/application/ports.py`**
- `DiscView` — línea 14
- `Snapshot` — línea 23
- `WelcomeInfo` — línea 34
- `RosterEntry` — línea 46
- `SnapshotObserver` — línea 52
- `SnapshotObserver.on_snapshot` — línea 61
- `SnapshotObserver.on_welcome` — línea 64
- `SnapshotObserver.on_roster` — línea 67
- `SnapshotObserver.on_rooms` — línea 70
- `SnapshotObserver.on_notice` — línea 73
- `SnapshotEncoder` — línea 76
- `SnapshotEncoder.encode_state` — línea 81
- `SnapshotEncoder.encode_welcome` — línea 84
- `SnapshotEncoder.encode_roster` — línea 87
- `SnapshotEncoder.encode_rooms` — línea 90
- `SnapshotEncoder.encode_room` — línea 93
- `SnapshotEncoder.encode_error` — línea 96
- `DiscView.id` — línea 15
- `DiscView.x` — línea 16
- `DiscView.y` — línea 17
- `DiscView.charge` — línea 18
- `DiscView.alive` — línea 19
- `Snapshot.tick` — línea 25
- `Snapshot.phase` — línea 26
- `Snapshot.remaining_s` — línea 27
- `Snapshot.winner` — línea 28
- `Snapshot.scores` — línea 29
- `Snapshot.discs` — línea 30
- `WelcomeInfo.id` — línea 35
- `WelcomeInfo.color` — línea 36
- `WelcomeInfo.arena_radius` — línea 37
- `WelcomeInfo.disc_radius` — línea 38
- `WelcomeInfo.max_players` — línea 39
- `WelcomeInfo.dash_cooldown_s` — línea 40
- `WelcomeInfo.min_charge` — línea 41
- `WelcomeInfo.min_players` — línea 42
- `RosterEntry.id` — línea 47
- `RosterEntry.name` — línea 48
- `RosterEntry.color` — línea 49

**`server/application/send_policies.py`**
- `SendPolicy` — línea 12
- `SendPolicy.should_send` — línea 14
- `EveryTick` — línea 17
- `EveryTick.should_send` — línea 18
- `EveryNthTick` — línea 22
- `EveryNthTick.__init__` — línea 23
- `EveryNthTick.should_send` — línea 26

**`server/application/session_commands.py`**
- `MembershipCommand` — línea 28
- `MembershipCommand.execute` — línea 30
- `Connect` — línea 34
- `Connect.execute` — línea 38
- `Disconnect` — línea 43
- `Disconnect.execute` — línea 46
- `Connect.join` — línea 35
- `Connect.observer` — línea 36
- `Disconnect.player_id` — línea 44

**`server/config.py`**
- `Config` — línea 16
- `Config.TICK_DT` — línea 264
- `Config.QUIET_EVERY_N_TICKS` — línea 269
- `Config.__post_init__` — línea 278
- `Config.HOST` — línea 23
- `Config.PORT` — línea 26
- `Config.LISTEN_BACKLOG` — línea 33
- `Config.MAX_CONNECTIONS` — línea 39
- `Config.SEND_BUFFER_BYTES` — línea 50
- `Config.MAX_REJECTING_HANDSHAKES` — línea 56
- `Config.MAX_FRAME_BYTES` — línea 61
- `Config.MAX_HTTP_HEADER_BYTES` — línea 67
- `Config.READ_CHUNK_BYTES` — línea 72
- `Config.TICK_HZ` — línea 80
- `Config.QUIET_PHASE_HZ` — línea 86
- `Config.INPUT_QUEUE_SIZE` — línea 99
- `Config.MAX_INPUTS_PER_CLIENT_PER_TICK` — línea 104
- `Config.MAX_NAME_LEN` — línea 107
- `Config.POSITION_DECIMALS` — línea 111
- `Config.CHARGE_DECIMALS` — línea 112
- `Config.METRICS_TICK_WINDOW` — línea 116
- `Config.LOG_REPEAT_INTERVAL_S` — línea 121
- `Config.HANDSHAKE_TIMEOUT_S` — línea 132
- `Config.PING_INTERVAL_S` — línea 137
- `Config.IDLE_TIMEOUT_S` — línea 138
- `Config.WRITE_TIMEOUT_S` — línea 142
- `Config.SHUTDOWN_GRACE_S` — línea 146
- `Config.MAX_ROOM_PLAYERS` — línea 157
- `Config.MAX_ROOMS` — línea 162
- `Config.MIN_PLAYERS_TO_START` — línea 165
- `Config.COUNTDOWN_S` — línea 169
- `Config.RESULTS_S` — línea 170
- `Config.CHARGE_MAX_S` — línea 174
- `Config.MIN_CHARGE_S` — línea 180
- `Config.DASH_COOLDOWN_S` — línea 181
- `Config.ARENA_RADIUS` — línea 189
- `Config.DISC_RADIUS` — línea 193
- `Config.ELIMINATION_MARGIN_DISCS` — línea 201
- `Config.ACCELERATION` — línea 206
- `Config.MAX_SPEED` — línea 211
- `Config.FRICTION_PER_S` — línea 216
- `Config.SLIDE_FRICTION_PER_S` — línea 225
- `Config.DASH_MAX_SPEED` — línea 232
- `Config.DASH_MASS_FACTOR` — línea 238
- `Config.DISC_MASS` — línea 242
- `Config.SPAWN_RING_FRACTION` — línea 248
- `Config.PLAYER_COLORS` — línea 254

**`server/domain/arena.py`**
- `Arena` — línea 13
- `Arena.__init__` — línea 14
- `Arena.contains` — línea 19
- `Arena.spawn_points` — línea 25

**`server/domain/commands.py`**
- `Command` — línea 28
- `Command.execute` — línea 33
- `Command.run` — línea 35
- `HallCommand` — línea 44
- `HallCommand.execute` — línea 51
- `HallCommand.run` — línea 53
- `JoinCommand` — línea 58
- `JoinCommand.execute` — línea 62
- `LeaveCommand` — línea 70
- `LeaveCommand.execute` — línea 73
- `StartCommand` — línea 78
- `StartCommand.execute` — línea 82
- `CreateRoomCommand` — línea 87
- `CreateRoomCommand.execute` — línea 90
- `JoinRoomCommand` — línea 95
- `JoinRoomCommand.execute` — línea 99
- `LeaveRoomCommand` — línea 104
- `LeaveRoomCommand.execute` — línea 107
- `MoveCommand` — línea 112
- `MoveCommand.execute` — línea 117
- `ChargeCommand` — línea 125
- `ChargeCommand.execute` — línea 129
- `Command.player_id` — línea 30
- `HallCommand.player_id` — línea 48
- `JoinCommand.player_id` — línea 59
- `JoinCommand.name` — línea 60
- `LeaveCommand.player_id` — línea 71
- `StartCommand.player_id` — línea 80
- `CreateRoomCommand.player_id` — línea 88
- `JoinRoomCommand.player_id` — línea 96
- `JoinRoomCommand.room_id` — línea 97
- `LeaveRoomCommand.player_id` — línea 105
- `MoveCommand.player_id` — línea 113
- `MoveCommand.dx` — línea 114
- `MoveCommand.dy` — línea 115
- `ChargeCommand.player_id` — línea 126
- `ChargeCommand.on` — línea 127

**`server/domain/disc.py`**
- `Disc` — línea 16
- `Disc.set_move` — línea 36
- `Disc.start_charge` — línea 43
- `Disc.release_charge` — línea 46
- `Disc.id` — línea 17
- `Disc.pos` — línea 18
- `Disc.vel` — línea 19
- `Disc.facing` — línea 23
- `Disc.move_dir` — línea 26
- `Disc.charging` — línea 27
- `Disc.release_pending` — línea 28
- `Disc.charge_time` — línea 31
- `Disc.cooldown` — línea 32
- `Disc.dashing` — línea 33
- `Disc.alive` — línea 34

**`server/domain/hall.py`**
- `ALREADY_IN_ROOM` — línea 23
- `NOT_IN_ROOM` — línea 24
- `NO_SUCH_ROOM` — línea 25
- `ROOM_FULL` — línea 26
- `ROOM_BUSY` — línea 27
- `TOO_MANY_ROOMS` — línea 28
- `RoomInfo` — línea 32
- `Hall` — línea 43
- `Hall.__init__` — línea 44
- `Hall.connect` — línea 56
- `Hall.disconnect` — línea 59
- `Hall.is_connected` — línea 64
- `Hall.name_of` — línea 67
- `Hall.color_of` — línea 70
- `Hall.lobby_members` — línea 73
- `Hall.room_of` — línea 78
- `Hall.world_of` — línea 82
- `Hall.create_room` — línea 86
- `Hall.join_room` — línea 99
- `Hall.leave_room` — línea 113
- `Hall.step` — línea 124
- `Hall.summary` — línea 129
- `Hall.drain_rejections` — línea 138
- `Hall._can_act` — línea 144
- `Hall._enter` — línea 148
- `Hall._reject` — línea 152
- `RoomInfo.id` — línea 34
- `RoomInfo.name` — línea 35
- `RoomInfo.players` — línea 36
- `RoomInfo.max_players` — línea 37
- `RoomInfo.phase` — línea 38
- `RoomInfo.host` — línea 39
- `RoomInfo.open` — línea 40

**`server/domain/physics.py`**
- `Physics` — línea 16
- `Physics.__init__` — línea 17
- `Physics.step` — línea 20
- `Physics._is_charging` — línea 49
- `Physics._accelerate` — línea 55
- `Physics._update_charge` — línea 70
- `Physics._cap_speed` — línea 89
- `Physics.mass` — línea 93
- `Physics.resolve_collisions` — línea 97
- `Physics._collide` — línea 104

**`server/domain/room.py`**
- `Room` — línea 15
- `Room.step` — línea 21
- `Room.phase` — línea 26
- `Room.id` — línea 16
- `Room.name` — línea 17
- `Room.world` — línea 18
- `Room.state` — línea 19

**`server/domain/round_states.py`**
- `RoundContext` — línea 25
- `RoundContext.ticks_for` — línea 30
- `RoundState` — línea 34
- `RoundState.__init__` — línea 37
- `RoundState.on_tick` — línea 41
- `RoundState.remaining_s` — línea 44
- `RoundState.winner` — línea 49
- `_TimedState` — línea 53
- `_TimedState.__init__` — línea 56
- `_TimedState.remaining_s` — línea 61
- `Waiting` — línea 65
- `Waiting.on_tick` — línea 73
- `Countdown` — línea 84
- `Countdown.__init__` — línea 87
- `Countdown.on_tick` — línea 90
- `Playing` — línea 101
- `Playing.on_tick` — línea 104
- `RoundOver` — línea 117
- `RoundOver.__init__` — línea 120
- `RoundOver.winner` — línea 125
- `RoundOver.on_tick` — línea 128
- `RoundContext.config` — línea 27
- `RoundContext.physics` — línea 28
- `RoundState.PHASE` — línea 35

**`server/domain/vector.py`**
- `Vec2` — línea 12
- `Vec2.__add__` — línea 16
- `Vec2.__sub__` — línea 19
- `Vec2.__mul__` — línea 22
- `Vec2.__neg__` — línea 25
- `Vec2.dot` — línea 28
- `Vec2.length` — línea 31
- `Vec2.is_zero` — línea 34
- `Vec2.normalized` — línea 37
- `Vec2.with_length` — línea 45
- `Vec2.x` — línea 13
- `Vec2.y` — línea 14

**`server/domain/world.py`**
- `Player` — línea 17
- `color_for` — línea 23
- `World` — línea 30
- `World.__init__` — línea 31
- `World.add_player` — línea 45
- `World.remove_player` — línea 50
- `World.host` — línea 56
- `World.request_start` — línea 62
- `World.take_start_request` — línea 67
- `World.player_count` — línea 71
- `World.disc` — línea 74
- `World.start_round` — línea 79
- `World.end_round` — línea 89
- `World.alive_discs` — línea 93
- `World.eliminate_outside` — línea 96
- `World.award` — línea 103
- `Player.id` — línea 18
- `Player.name` — línea 19
- `Player.color` — línea 20

**`server/log_filters.py`**
- `RateLimitFilter` — línea 17
- `RateLimitFilter.__init__` — línea 23
- `RateLimitFilter.filter` — línea 32

**`server/main.py`**
- `WS_PATH` — línea 28
- `METRICS_PATH` — línea 29
- `CLIENT_DIR` — línea 30
- `CONTENT_TYPES` — línea 31
- `load_static` — línea 38
- `App` — línea 53
- `App.run` — línea 58
- `App.shutdown` — línea 65
- `build` — línea 71
- `build.new_session` — línea 85
- `build.metrics_page` — línea 89
- `serve_until_signal` — línea 103
- `main` — línea 124
- `App.listener` — línea 54
- `App.game` — línea 55
- `App.metrics` — línea 56

**`server/metrics.py`**
- `Metrics` — línea 15
- `Metrics.__post_init__` — línea 34
- `Metrics.record_rtt` — línea 40
- `Metrics.record_tick` — línea 43
- `Metrics.snapshot` — línea 46
- `_percentile_ms` — línea 84
- `Metrics.window` — línea 16
- `Metrics.connections_active` — línea 17
- `Metrics.connections_total` — línea 18
- `Metrics.connections_rejected` — línea 19
- `Metrics.inputs_dropped_queue` — línea 20
- `Metrics.inputs_dropped_rate` — línea 21
- `Metrics.mailbox_overwrites` — línea 22
- `Metrics.tick_overruns` — línea 23
- `Metrics.handshake_timeouts` — línea 24
- `Metrics.idle_timeouts` — línea 25
- `Metrics.write_timeouts` — línea 26
- `Metrics.bytes_sent` — línea 27
- `Metrics.frames_sent` — línea 28
- `Metrics.inputs_applied` — línea 29
- `Metrics.started_at` — línea 30
- `Metrics.tick_durations` — línea 31
- `Metrics.rtt_samples` — línea 32
