# Anexo: los conceptos que necesitas dominar (y qué estudiar)

Anexo de [`docs/guia-del-codigo.md`](guia-del-codigo.md). La guía explica **qué hace el código**; este anexo explica
**el conocimiento que hay detrás** para que puedas defender *por qué* está hecho así.

Para cada concepto: **qué es**, **dónde aparece en tu proyecto** (`archivo:línea`, generado leyendo el código real),
**qué buscar** (términos exactos para buscar o documentos concretos) y **qué te preguntarían**.

## 0. Cómo usarlo

Cada tema lleva una prioridad:

| Marca | Significa |
| --- | --- |
| **[A]** | Imprescindible: la rúbrica lo evalúa o te lo van a preguntar en la defensa |
| **[B]** | Importante: sostiene decisiones del proyecto; conviene entenderlo |
| **[C]** | Contexto: útil para responder con solvencia, no es el centro |

Los criterios de la rúbrica del **Proyecto Final 2** (Opción A) y dónde vive cada uno:

| Criterio (valor) | Conceptos que lo cubren |
| --- | --- |
| 1. Arquitectura y modelo de concurrencia (30) | §3 (modelos, event loop, asyncio, HOL blocking, backpressure), §5 (tiempo real), §6 (arquitectura) |
| 2. Manejo de sockets y protocolo de red (20) | §1 (TCP y sockets), §2 (HTTP y WebSocket) |
| 3. Sincronización, estado y resiliencia (20) | §3 (único escritor, región crítica), §4 (timeouts, cierre limpio, fugas de descriptores) |
| 4. Uso crítico y auditoría de la IA (20) | §10 (la bitácora y qué enseña cada error) |
| 5. Pruebas de carga y defensa oral (10) | §9 (medición y carga), §1 y §4 (kernel, colas de conexión) |

Si tienes **poco tiempo**, estudia en este orden: §1 → §3 → §2 → §4 → §6 → §10, y haz los experimentos de §11.

---

## 1. Redes y sockets [A]

### 1.1 TCP: un flujo de bytes, no de mensajes [A]

**Qué es.** TCP te da un canal **confiable y ordenado de bytes**. No conserva los límites de los mensajes: si envías
"hola" y "mundo", el otro lado puede leer `ho`, `lamun` y `do`, o `holamundo` de una vez. Por eso hay que **enmarcar** (poner
longitud o delimitadores) y **acumular** lo que llega hasta tener un mensaje completo.

**Dónde está.** `FrameReader.next_frame` (`server/adapters/ws_frames.py:97`) devuelve `None` hasta tener la trama entera, y
`ClientSession._read_loop` (`server/adapters/client_session.py:149`) lo llama en bucle. `read_head` (`server/adapters/http_handshake.py:112`) hace lo mismo con la cabecera HTTP.
Las pruebas parten las tramas en pedazos de 1 byte (`tests/test_ws_frames.py`).

**Qué buscar.** "TCP stream vs message boundaries", "TCP framing", "short read recv", "partial reads sockets".
**Te preguntarán.** "¿`recv()` siempre devuelve un mensaje completo?" — No: puede traer media trama o varias; la rúbrica lo pide explícitamente.

### 1.2 La API de sockets: `socket`, `bind`, `listen`, `accept` [A]

**Qué es.** Un *socket* es un descriptor de archivo que representa un extremo de comunicación. Un servidor TCP hace:
`socket()` (crea) → `setsockopt()` (opciones) → `bind()` (asocia dirección y puerto) → `listen()` (queda en escucha pasiva) →
`accept()` (toma una conexión ya establecida y devuelve **otro socket**, uno por cliente). El de escucha nunca transporta datos.

**Dónde está.** `create_listen_socket` (`server/adapters/listener.py:25`) hace cada paso a mano. Después se entrega a asyncio con
`Listener.serve` (`server/adapters/listener.py:78`) (`start_server(sock=...)`). La restricción del proyecto es **no** usar `start_server(host, port)`.

**Qué buscar.** `man 2 socket`, `man 2 bind`, `man 2 listen`, `man 2 accept`; "Beej's Guide to Network Programming" (gratis, capítulos 5 y 6);
documentación de Python: módulo `socket` y su "Socket Programming HOWTO".
**Te preguntarán.** "¿Qué diferencia hay entre el socket de escucha y el de conexión?"

### 1.3 `SO_REUSEADDR`, `SO_REUSEPORT` y `TIME_WAIT` [A]

**Qué es.** Cuando **tú** cierras una conexión TCP primero, tu lado queda en `TIME_WAIT` (~60 s en Linux, `2×MSL`) para
no confundir paquetes retrasados con una conexión nueva. Mientras haya sockets así en ese puerto, un `bind()` normal falla con
`Address already in use`. **`SO_REUSEADDR`** permite hacer `bind` pese a `TIME_WAIT` (reinicio inmediato).
**`SO_REUSEPORT`** es otra cosa: permite que **varios** sockets/procesos escuchen en el mismo puerto y el kernel reparte las conexiones.

**Dónde está.** `create_listen_socket` (`server/adapters/listener.py:25`) (con su comentario). Medido en la demo: tras `kill -TERM` quedan ~40 sockets en
`TIME-WAIT` y el reinicio inmediato funciona sin error.

**Qué buscar.** "TCP TIME_WAIT state", "SO_REUSEADDR vs SO_REUSEPORT", "2MSL". Estados TCP: el diagrama de estados está en el RFC 9293 (busca "State Machine Overview").
**Te preguntarán.** "¿Por qué reiniciar el servidor daba *Address already in use*?" "¿Quién queda en `TIME_WAIT`, el cliente o el servidor?" (el que cierra primero).

### 1.4 Cola SYN y cola de aceptación; el `backlog` [A]

**Qué es.** El saludo TCP tiene tres pasos (SYN, SYN-ACK, ACK) y el **kernel** lo completa por ti, antes de que llames a `accept()`.
Dos colas: la **cola SYN** guarda conexiones a medio abrir; la **cola de aceptación** guarda las ya completas esperando `accept()`.
Su tamaño es `min(backlog, net.core.somaxconn)`. Si se llena, el kernel descarta el último ACK o el SYN y el cliente **reintenta**
(1 s, 3 s, 7 s...). En `ss -ltn` de un socket en `LISTEN`: **Recv-Q = ocupación actual** de la cola de aceptación, **Send-Q = su tamaño máximo**.

**Dónde está.** `LISTEN_BACKLOG = 128` en `Config`, `sock.listen(backlog)` en `create_listen_socket` (`server/adapters/listener.py:25`). Un hallazgo del proyecto:
`Listener.serve` (`server/adapters/listener.py:78`) pasa `backlog=` explícito porque `asyncio.start_server` volvería a llamar a `listen(100)` y pisaría el valor
(bitácora, entrada 4; y en el benchmark, 580 de 1000 clientes "sanos" con backlog 100 contra 1000 de 1000 con 1024).

**Qué buscar.** "SYN queue vs accept queue", "listen backlog somaxconn", "tcp_max_syn_backlog", "SYN cookies", `ss -ltn` Recv-Q Send-Q,
`nstat -az TcpExtListenOverflows TcpExtListenDrops` (cuenta los desbordes; **no lo medí**, la explicación del benchmark es consistente pero no probada con esos contadores).
**Te preguntarán.** "¿Qué pasa si llegan 1000 conexiones de golpe y el backlog es 128?" — Es una pregunta explícita de la rúbrica (criterio 5, kernel y colas).

### 1.5 Descriptores de archivo, `ulimit -n` y `EMFILE` [A]

**Qué es.** En Unix todo es un archivo: sockets, archivos, tuberías. Cada uno ocupa un **descriptor** (un entero). Cada proceso tiene un tope
(`ulimit -n`). Al llegar al tope, `accept()` falla con `EMFILE` ("Too many open files"). Una **fuga de descriptores** es no cerrar lo que abriste: el
número crece hasta el tope y el servidor deja de aceptar.

**Dónde está.** Cada conexión libera su socket en un `finally` (`Listener._on_connect` (`server/adapters/listener.py:122`), `Listener._close` (`server/adapters/listener.py:199`)). La demo
`ulimit -n 64` prueba que el servidor **sobrevive** a `EMFILE` (asyncio reintenta cada 1 s). `tools/leakcheck.py` y `tools/fd_watch.sh` miran
`/proc/<pid>/fd`.

**Qué buscar.** "file descriptor leak", "EMFILE vs ENFILE", `ulimit -n`, `getrlimit(2)`, `/proc/<pid>/fd`, `lsof -p`.
**Te preguntarán.** "¿Cómo demuestras que no hay fugas?" — Contando `/proc/<pid>/fd` antes y después de la carga (vuelve a 7) y `CLOSE_WAIT` en 0.

### 1.6 Cierre de conexiones: FIN, RST, `CLOSE_WAIT`, `FIN-WAIT-1` [A]

**Qué es.** Cierre limpio: cada lado envía **FIN** (y espera el del otro). **RST** corta de golpe. Si el cliente cierra y tu servidor **no**, tu socket
queda en `CLOSE_WAIT` para siempre: es la firma de una fuga. Un **FIN** viaja *detrás* de los datos aún no enviados: si el receptor no lee (ventana cero), nunca sale,
y tu socket queda en `FIN-WAIT-1`. `SO_LINGER` con `{1, 0}` hace que `close()` mande RST y descarte lo pendiente.

**Dónde está.** `abort_with_reset` (`server/adapters/net_util.py:11`) (con la explicación) y la bitácora, entrada 11. `CLOSE_WAIT` se vigila en `tools/monitor.py` (columna `cw`).

**Qué buscar.** "TCP connection termination four-way handshake", "CLOSE_WAIT leak", "SO_LINGER zero RST", `ss -tan state close-wait`.
**Te preguntarán.** "¿Qué es `CLOSE_WAIT` y qué indica?"

### 1.7 Ventana TCP, búfer de envío y backpressure del kernel [A]

**Qué es.** El receptor anuncia cuánto puede recibir (**ventana**). Si no lee, su búfer se llena y anuncia **ventana cero**: el emisor para. Entonces
los datos se acumulan en el **búfer de envío** del emisor (`SO_SNDBUF`) y, cuando se llena, `write` bloquearía (o, en asyncio, `drain()` se suspende). Linux **autoajusta**
ese búfer hasta megabytes, lo que retrasa detectar a un cliente lento.

**Dónde está.** `Listener._limit_send_buffer` (`server/adapters/listener.py:113`) fija 64 KB. `ClientSession._drain` (`server/adapters/client_session.py:282`) pone plazo a la espera. Medido: ~2,6 MB por conexión sin límite
(más de 10 min para notar a un zombi).

**Qué buscar.** "TCP flow control receive window", "zero window probe", "SO_SNDBUF autotuning", `man 7 tcp`, `ss -tm` (memoria del socket).

### 1.8 Otros que conviene conocer [C]

* **Nagle y `TCP_NODELAY`:** el servidor no lo configura; en localhost no se nota. Si preguntan por latencia de mensajes pequeños, es el algoritmo de Nagle.
* **Keepalive TCP:** por defecto salta tras ~2 h; por eso el servidor manda su propio `ping` cada 10 s (§4.3).
* **IPv4 vs IPv6, `127.0.0.1` vs `0.0.0.0`:** `127.0.0.1` solo acepta desde la propia máquina (plan A); `0.0.0.0` acepta de cualquier interfaz (plan B).
* **Puertos efímeros** y límites de conexiones del lado cliente (relevante para `tools/bots.py`).
* **WSL2:** la red está virtualizada; `localhost` de Windows y de WSL2 se reenvían entre sí, pero el rendimiento y los límites no son los de un Linux nativo.

---

## 2. HTTP y WebSocket (RFC 6455) [A]

### 2.1 El saludo HTTP y el *upgrade* [A]

**Qué es.** Un WebSocket **empieza como una petición HTTP/1.1** `GET` con cabeceras `Upgrade: websocket`, `Connection: Upgrade`, `Sec-WebSocket-Key` (16 bytes
aleatorios en base64) y `Sec-WebSocket-Version: 13`. El servidor responde `101 Switching Protocols` con `Sec-WebSocket-Accept` =
`base64(SHA-1(clave + GUID fijo))`. Desde ahí, la misma conexión TCP deja de hablar HTTP y habla tramas WebSocket.

**Dónde está.** `HttpHandshake._upgrade` (`server/adapters/http_handshake.py:170`) valida cada requisito; `accept_key` (`server/adapters/http_handshake.py:61`) calcula la respuesta;
`parse_request` (`server/adapters/http_handshake.py:67`) interpreta la cabecera. El GUID es una constante del RFC (§1.3).

**Qué buscar.** RFC 6455 §1.3 y §4 (opening handshake); "WebSocket handshake Sec-WebSocket-Accept"; MDN "Writing WebSocket servers".
**Te preguntarán.** "¿Cómo sabe el navegador que tu servidor entiende WebSocket?" — Por el `Sec-WebSocket-Accept` correcto.

### 2.2 Las tramas (*frames*) [A]

**Qué es.** Cada mensaje viaja en una trama con: bit `FIN`, `opcode` (texto 0x1, close 0x8, ping 0x9, pong 0xA), bit `MASK`, **longitud** (7 bits; si vale 126,
los 2 bytes siguientes; si vale 127, los 8 siguientes), **clave de máscara** (4 bytes) y el payload. Las tramas **de control** no pueden pasar de 125 bytes.

**Dónde está.** `FrameReader.next_frame` (`server/adapters/ws_frames.py:97`) (lectura, con todas las validaciones) y `FrameWriter.encode` (`server/adapters/ws_frames.py:184`) (escritura).
Se prueba con longitudes 126 y 127 (los bordes de cada formato).

**Qué buscar.** RFC 6455 §5.2 (base framing protocol). Es corto y tiene el diagrama de bits.
**Te preguntarán.** "¿Cómo sabes dónde termina una trama?" "¿Por qué hay tres formatos de longitud?"

### 2.3 El enmascarado (*masking*) [A]

**Qué es.** Toda trama **del cliente al servidor** debe ir enmascarada (XOR de cada byte con una clave de 4 bytes que viaja en la trama); las del servidor **nunca**.
El motivo no es cifrar: es impedir que un **proxy intermedio** confunda el payload con HTTP (ataques de *cache poisoning*). Si llega una trama sin máscara,
el servidor **debe** cerrar con 1002.

**Dónde está.** `FrameReader.next_frame` (`server/adapters/ws_frames.py:97`) (rechazo sin máscara) y `_unmask` (`server/adapters/ws_frames.py:162`) (un solo XOR de enteros grandes en vez de un bucle byte a byte).

**Qué buscar.** RFC 6455 §5.3; "why websocket client frames are masked".

### 2.4 Ping/pong, cierre y códigos de cierre [A]

**Qué es.** `ping`/`pong` comprueban que el otro extremo sigue vivo (el pong repite el payload del ping). `close` lleva un **código** (1000 normal, 1001 el servidor
se apaga, 1002 error de protocolo, 1003 dato no soportado, 1007 texto no UTF-8, 1008 violación de política, 1009 mensaje demasiado grande, 1013 vuelve a intentarlo luego)
y se **responde** con otro close.

**Dónde está.** `CloseCode` (`server/adapters/ws_frames.py:36`) (los códigos), `ClientSession._on_ping` (`server/adapters/client_session.py:205`), `ClientSession._on_pong` (`server/adapters/client_session.py:210`) (mide el RTT),
`ClientSession._on_close` (`server/adapters/client_session.py:219`), `ClientSession._ping_loop` (`server/adapters/client_session.py:232`). En `docs/protocolo.md` §5 está la tabla de qué usa el proyecto y cuándo.

**Qué buscar.** RFC 6455 §5.5 (control frames), §5.5.1 (close), §7.4.1 (status codes).

### 2.5 HTTP básico y ataques típicos [B]

* **Estructura de una petición:** línea de petición, cabeceras, línea en blanco (`\r\n\r\n`), cuerpo. RFC 9112 (HTTP/1.1) y 9110 (semántica).
* **Códigos que aparecen:** 101, 200, 400, 404, 405, 426 (Upgrade Required), 431 (cabecera demasiado grande).
* **Path traversal:** pedir `../../etc/passwd`. Se evita sirviendo solo lo que está en un diccionario cargado al arrancar (`load_static` (`server/main.py:38`)).
* **Slowloris:** abrir conexiones y mandar la cabecera **muy despacio** para agotar descriptores. Se evita con un plazo **total** (`Listener._handshake_with_timeout` (`server/adapters/listener.py:166`)).
* **Límite de tamaño:** sin tope de cabecera o de trama, un cliente nos haría reservar memoria sin límite (`read_head` (`server/adapters/http_handshake.py:112`), `FrameReader.next_frame` (`server/adapters/ws_frames.py:97`)).
* **XSS:** el cliente escribe los nombres con `textContent`, nunca con `innerHTML` (`renderRooms` (`client/client.js:248`), `renderScores` (`client/client.js:542`)).

**Qué buscar.** "slowloris attack", "HTTP request smuggling" (solo contexto), "OWASP path traversal", "XSS textContent vs innerHTML".

### 2.6 JSON y el protocolo de aplicación [B]

El servidor define su **propio protocolo** sobre WebSocket: mensajes JSON con un campo `t` (tipo). `docs/protocolo.md` es la especificación. Puntos de defensa: el servidor **nunca**
acepta posiciones del cliente (solo intenciones), todo se **valida** en el codec (`MessageCodec.decode` (`server/adapters/codec.py:49`)), y cada `state` se basta a sí mismo (si un estado se descarta, el siguiente trae todo).

**Qué buscar.** "application-layer protocol design", "idempotent state messages vs deltas", "JSON parsing recursion limit" (el `RecursionError` que captura el codec).

---

## 3. Concurrencia y `asyncio` [A]

### 3.1 Concurrencia vs paralelismo; I/O-bound vs CPU-bound [A]

**Qué es.** **Concurrencia** = manejar muchas cosas a la vez (intercaladas). **Paralelismo** = ejecutarlas literalmente a la vez (varios núcleos). Un servidor de juego con
muchas conexiones es sobre todo **I/O-bound** (espera a la red), no CPU-bound; ahí un solo hilo bien organizado alcanza. El tick de este proyecto usa ~3 % de su presupuesto.

**Qué buscar.** "concurrency vs parallelism" (Rob Pike, charla), "I/O bound vs CPU bound".

### 3.2 Modelos de concurrencia para un servidor [A]

| Modelo | Idea | Costo | En este proyecto |
| --- | --- | --- | --- |
| Hilo por conexión | Un hilo bloqueante por cliente | Memoria por hilo, cambios de contexto, **GIL** en CPython | Medido: 165 % de CPU a 300 clientes |
| Pool de hilos | N workers fijos | Una conexión larga ocupa un worker **entero**: con pool de 16 solo atiende a 16 | Medido: 284 de 300 clientes sin recibir nada |
| Multiproceso | Un proceso por cliente o por grupo | Memoria por proceso, estado compartido por IPC | ~0,8 MB por proceso aun con copy-on-write |
| **Event loop (`epoll`) / `asyncio`** | **Un hilo; atiende a quien esté listo** | Cooperativo: **una llamada bloqueante congela a todos** (por eso nada bloqueante en el event loop) | **Elegido**: 26 % de CPU a 300 clientes |

**Dónde está.** `docs/modelo-concurrencia.md` (con todos los números medidos con `tools/model_bench.py`).

**Qué buscar.** "The C10K problem" (Dan Kegel, el texto clásico), "thread-per-connection vs event-driven server", "Python GIL", `man 7 epoll`.
**Te preguntarán.** "¿Por qué asyncio y no hilos?" — Es el criterio 1 (30 puntos). Responde con la carga (I/O, conexiones largas, difusión) **y con los números**.

### 3.3 El GIL [A]

**Qué es.** En CPython estándar, **solo un hilo ejecuta bytecode de Python a la vez** (*Global Interpreter Lock*). Los hilos no dan paralelismo de CPU; sí ayudan con I/O porque el GIL se libera
mientras esperan. Con muchos hilos compitiendo por el GIL, el cambio de contexto cuesta (medido: 1000 hilos → 7,6 Hz en vez de 30).

**Qué buscar.** "Python GIL explained", "David Beazley GIL" (charlas), `sys.getswitchinterval`; contexto: PEP 703 (build sin GIL opcional).
**Te preguntarán.** "¿Por qué no mover la física a otro hilo?" — Por el GIL no habría paralelismo y añadiría cerrojos; además el tick cuesta ~1 % del presupuesto.

### 3.4 `epoll` y el multiplexado de E/S [A]

**Qué es.** En vez de un hilo dormido por cliente, registras todos los sockets en el kernel (`epoll`) y le preguntas "¿cuáles están listos?". El event loop de `asyncio`
usa `selectors.DefaultSelector`, que en Linux es `EpollSelector`, con **sockets no bloqueantes** (`setblocking(False)`).

**Dónde está.** `create_listen_socket` (`server/adapters/listener.py:25`) (`setblocking(False)`); `asyncio` lo hace por debajo.
**Qué buscar.** `man 7 epoll`, "select vs poll vs epoll", "level-triggered vs edge-triggered" (asyncio usa level-triggered), documentación de `selectors`.

### 3.5 `asyncio`: corrutinas, tareas y *puntos de suspensión* [A]

**Qué es.** Una **corrutina** (`async def`) es una función que puede suspenderse. `await` cede el control al event loop **solo si** lo que esperas realmente se suspende.
Una **tarea** (`asyncio.create_task`) programa una corrutina para que corra "en paralelo" (concurrentemente). Es **multitarea cooperativa**: entre dos `await`, **nada** más puede correr.
Eso es lo que hace seguro modificar estructuras compartidas **sin cerrojos** mientras no haya un `await` en medio.

**Matiz importante.** `await x` no siempre cede: si `x` no necesita esperar (p. ej. `Semaphore.acquire()` con cupo libre), sigue sin ceder el loop. El proyecto lo usa a propósito en
`Listener._on_connect` (`server/adapters/listener.py:122`) (entre `locked()` y `acquire()` nadie puede robar el cupo).

**Dónde está.** Tres tareas por cliente (`ClientSession.run` (`server/adapters/client_session.py:122`)) y una del tick (`App.run` (`server/main.py:58`)).
**Qué buscar.** Documentación de Python: *Coroutines and Tasks*, *Event Loop*; "asyncio cooperative multitasking", "asyncio await point yield control".

### 3.6 Condiciones de carrera **sin hilos** [A]

**Qué es.** Una *race condition* es un resultado que depende del orden en que se intercalan operaciones sobre un dato compartido. En `asyncio` no hacen falta hilos: **basta un `await` entre leer
y modificar**. Ejemplo del proyecto (la versión mala, rama `demo/race-bug`): el tick hace `await drain()` **dentro** de un `for` sobre los jugadores; mientras espera, otra tarea borra de ese
diccionario y el `for` falla con `RuntimeError: dictionary changed size during iteration`.

**Región crítica.** El trozo de código donde se lee y modifica un recurso compartido y que debe ser atómico. Aquí lo garantiza que **no hay `await` dentro** (`GameSession.tick` (`server/application/game_session.py:121`)).

**Qué buscar.** "race condition", "critical section", "asyncio race condition await", "check-then-act".
**Te preguntarán.** "¿Necesitas un `Lock`?" — No: un solo hilo y sin `await` en la región crítica. (En hilos reales sí harían falta `threading.Lock`/`RLock`.)

### 3.7 El patrón de **único escritor** y el productor-consumidor [A]

**Qué es.** Para evitar corrupción de datos hay dos caminos: proteger con cerrojos, o **que un solo actor sea el único que modifica**. Aquí: los `reader` (muchos) **producen** comandos en una **cola acotada**
y el tick (uno) los **consume**. Es el patrón *productor-consumidor*.

**Dónde está.** `GameSession.submit_input` (`server/application/game_session.py:70`) (productor, `put_nowait`), `GameSession._apply_inputs` (`server/application/game_session.py:135`) (consumidor).
**Qué buscar.** "single writer principle" (Martin Thompson, *Mechanical Sympathy*), "producer consumer queue", "actor model".

### 3.8 *Backpressure* y *head-of-line blocking* [A]

**Qué es.** **Backpressure**: mecanismo para que un consumidor lento frene (o descarte) al productor en vez de acumular sin límite. **Head-of-line blocking** (aquí, a nivel de aplicación): un
consumidor lento bloquea a los que vienen *detrás* en la fila (un solo cliente que no lee congelaría la difusión a todos). El requisito central del profe es **evitarlo**.

**Cómo se evita aquí.** (1) El tick **nunca** espera a un socket: deja los bytes en un **buzón de tamaño 1** por cliente y sigue. (2) Si el cliente no lo vació, el estado nuevo **reemplaza** al viejo. (3) Un
plazo de escritura corta al que no lee. (4) La cola de entradas es **acotada** y descarta (`put_nowait`).

**Dónde está.** `ClientSession.on_snapshot` (`server/adapters/client_session.py:86`) (el reemplazo), `ClientSession._write_loop` (`server/adapters/client_session.py:247`), `ClientSession._drain` (`server/adapters/client_session.py:282`), `GameSession.submit_input` (`server/application/game_session.py:70`).
**La prueba contundente:** la rama `demo/race-bug` rompe esto a propósito: 0 estados/s durante ~9 s para todos (`docs/demo-race-bug.md`).

**Qué buscar.** "backpressure in streaming systems", "head-of-line blocking", "bounded queue drop policy", "asyncio drain flow control high water mark".

### 3.9 `drain()` y el control de flujo de los *streams* de `asyncio` [A]

**Qué es.** `StreamWriter.write()` **no espera**: deja los bytes en un búfer del transporte (en espacio de usuario). Si ese búfer pasa su **marca alta** (por defecto 64 KiB), `await writer.drain()`
**se suspende** hasta que baje de la marca baja. Es el mecanismo que expone la presión del kernel al programa. **Regla del proyecto:** solo el `writer` de cada cliente hace `drain`, nunca el tick.

**Dónde está.** `ClientSession._write_loop` (`server/adapters/client_session.py:247`) y `ClientSession._drain` (`server/adapters/client_session.py:282`).
**Qué buscar.** Documentación de `asyncio`: *Streams* (`StreamWriter.drain`), "asyncio transport set_write_buffer_limits", "flow control high-water low-water mark".

### 3.10 Primitivas de sincronización de `asyncio` [B]

| Primitiva | Para qué se usa aquí |
| --- | --- |
| `asyncio.Event` | El `writer` espera `event.wait()` hasta que alguien le deja algo (`ClientSession._write_loop` (`server/adapters/client_session.py:247`)); `Listener._no_connections` para el apagado |
| `asyncio.Semaphore` | Tope de 100 conexiones (`Listener._on_connect` (`server/adapters/listener.py:122`)) |
| `asyncio.Queue(maxsize)` | Cola de entradas acotada (`GameSession.__init__` (`server/application/game_session.py:38`)) |
| `asyncio.wait_for` | Todos los timeouts |
| `asyncio.wait(..., FIRST_COMPLETED)` | Esperar al primero de reader/writer (`ClientSession.run` (`server/adapters/client_session.py:122`)) |
| `asyncio.gather(return_exceptions=True)` | Recoger tareas canceladas sin que una excepción corte el resto |

**El orden `wait` → `clear` → leer** del `writer` es una pieza famosa de corrección: si se leyeran las casillas **antes** del `clear()`, un `set()` del tick entre ambos pasos se
perdería y el `writer` dormiría con datos pendientes (AGENTS.md §4.5).
**Qué buscar.** Documentación: *Synchronization Primitives*, *Queues*; "lost wakeup problem".

### 3.11 Cancelación de tareas y limpieza con `finally` [A]

**Qué es.** Cancelar una tarea (`task.cancel()`) lanza `CancelledError` dentro de ella en su próximo `await`. Por eso la limpieza va en `finally`, que se ejecuta **siempre**: pase lo que pase, el socket se cierra,
el cupo se libera y el tick se entera del `Disconnect`.

**Dónde está.** `ClientSession.run` (`server/adapters/client_session.py:122`) (su `finally`), `Listener._on_connect` (`server/adapters/listener.py:122`).
**Qué buscar.** "asyncio task cancellation", "CancelledError", "structured concurrency", "try finally resource cleanup".

### 3.12 El bucle de juego de **paso fijo** [A]

**Qué es.** Un *game loop* actualiza el mundo a intervalos fijos (aquí 1/30 s). Se programa el siguiente tick respecto al **anterior** (`next_at += dt`), no respecto a "ahora", para que los retrasos
no se acumulen; si un tick se pasa de su presupuesto, no se intenta "recuperar" (espiral de la muerte). Se usa un **reloj monotónico** (`loop.time()`), que no retrocede aunque cambie la hora del sistema.

**Dónde está.** `GameSession.run_tick_loop` (`server/application/game_session.py:233`); contador `tick_overruns` en `Metrics`.
**Qué buscar.** "Fix Your Timestep!" (Glenn Fiedler, *Gaffer on Games*), "fixed timestep game loop", "monotonic clock vs wall clock".

---

## 4. Resiliencia y sistema operativo [A]

### 4.1 Aislamiento de fallos [A]

**Qué es.** Un servidor no puede caer porque **un** cliente haga algo raro. Cada conexión tiene sus excepciones capturadas; el tick captura excepciones de la lógica; un observador roto no detiene la difusión.

**Dónde está.** `Listener._serve_client` (`server/adapters/listener.py:146`) (`except Exception`), `ClientSession.run` (`server/adapters/client_session.py:122`) (registra y no propaga), `GameSession._notify` (`server/application/game_session.py:223`), el `try/except` de `GameSession.run_tick_loop` (`server/application/game_session.py:233`).

### 4.2 Timeouts: los tres del proyecto [A]

| Timeout | Qué evita | Dónde |
| --- | --- | --- |
| Saludo (5 s, **total**, no se renueva con cada byte) | *Slowloris*: conexiones que gastan descriptores sin avanzar | `Listener._handshake_with_timeout` (`server/adapters/listener.py:166`), `ClientSession._read_timeout` (`server/adapters/client_session.py:172`) |
| Inactividad (30 s con ping cada 10 s) | Clientes muertos que TCP tardaría horas en notar | `ClientSession._ping_loop` (`server/adapters/client_session.py:232`), `ClientSession._on_read_timeout` (`server/adapters/client_session.py:181`) |
| Escritura (10 s) | Cliente "zombi" que no lee y retiene un descriptor | `ClientSession._drain` (`server/adapters/client_session.py:282`) |

**Qué buscar.** "slowloris", "TCP keepalive vs application heartbeat", "half-open connection".

### 4.3 Cierre ordenado y señales [A]

**Qué es.** `SIGTERM` es la petición educada de terminar (la que mandan `kill`, systemd, Railway); `SIGINT` es Ctrl+C; `SIGKILL` no se puede capturar. Un **manejador de señal no puede hacer `await`**: solo marca un
`Event` y la corrutina principal hace el trabajo. Cierre ordenado: dejar de aceptar → avisar a cada cliente (`close 1001`) → dar un plazo → cortar a los rezagados.

**Dónde está.** `serve_until_signal` (`server/main.py:103`) y `Listener.shutdown` (`server/adapters/listener.py:97`). Medido: 23 ms con 20 clientes, todos reciben `close 1001`.
**Qué buscar.** `man 7 signal`, "graceful shutdown", `loop.add_signal_handler`, "SIGTERM vs SIGKILL".

### 4.4 Logging síncrono y su riesgo [B]

El módulo `logging` escribe **de forma síncrona**: si el destino se pone lento (terminal, tubería llena, disco), **bloquea el event loop**. Pasó de verdad: bajo `EMFILE`, asyncio generó 83 768 errores
en 35 s. El proyecto lo mitiga con `RateLimitFilter.filter` (`server/log_filters.py:32`); la solución completa sería `logging.handlers.QueueHandler` con un hilo aparte (no hecho; está en §8 de la guía).
**Qué buscar.** "Python logging blocking asyncio", "QueueHandler QueueListener".

### 4.5 Herramientas de diagnóstico de Linux [A]

| Herramienta | Para qué | Ejemplo en el proyecto |
| --- | --- | --- |
| `ss -tlnp` | Sockets en escucha y qué proceso los tiene | Paso 1 del guion de la demo |
| `ss -ltn` | `Recv-Q` y `Send-Q` de un socket en `LISTEN` (cola de aceptación) | Explicar el backlog |
| `ss -tan` | Estados TCP de todas las conexiones (`TIME-WAIT`, `CLOSE-WAIT`, `FIN-WAIT-1`) | Verificar que no hay fugas |
| `ls /proc/<pid>/fd` | Descriptores abiertos de un proceso | `tools/leakcheck.py`, `tools/fd_watch.sh` |
| `/proc/net/tcp` | Tabla TCP del kernel (la que leen las herramientas) | `tools/procinfo.py` |
| `ulimit -n` | Tope de descriptores | Demo `ulimit -n 64` |
| `kill -TERM <pid>` | Pedir un cierre ordenado | Demo del paso 7 |
| `nstat`, `netstat -s` | Contadores del kernel (desbordes de la cola de aceptación) | Para **comprobar** la explicación del backlog |

**Qué buscar.** `man ss`, `man proc` (sección de `/proc/<pid>/fd` y `/proc/net/tcp`), `man kill`, "Linux ss command examples".


---

## 5. Juego en tiempo real [A/B]

### 5.1 Servidor autoritativo [A]

**Qué es.** El servidor es **la única fuente de verdad**: decide la física, las colisiones y las eliminaciones. El cliente solo envía **intenciones** ("quiero ir a la derecha", "estoy cargando"), nunca posiciones.
Así un cliente modificado no puede teletransportarse ni correr más: lo máximo que logra es mandar intenciones inválidas, que el codec rechaza.

**Dónde está.** `game_codec` (`server/adapters/codec.py:134`) (solo acepta intenciones validadas), `Disc.set_move` (`server/domain/disc.py:36`) (normaliza la dirección: `dx=1000` no corre más rápido), y el cliente (`syncIntent` (`client/client.js:354`)) envía solo direcciones.
**Qué buscar.** "authoritative server game architecture", "never trust the client", Gabriel Gambetta "Fast-Paced Multiplayer" (parte I).
**Te preguntarán.** "¿Qué impide hacer trampa?"

### 5.2 Tick, simulación y *snapshots* [A]

**Qué es.** El servidor avanza el mundo en **ticks** de duración fija (30 por segundo) y publica **instantáneas** (*snapshots*) del estado. Cada `state` se basta a sí mismo: no es un cambio ("el disco 7 se movió") sino el estado completo, así que perder uno no corrompe nada
(el siguiente lo corrige). Compárese con enviar *deltas*, que ahorra ancho de banda pero exige no perder ninguno.

**Dónde está.** `GameSession._snapshot` (`server/application/game_session.py:201`), `GameSession._publish_states` (`server/application/game_session.py:211`), `JsonFrameEncoder.encode_state` (`server/adapters/codec.py:160`).
**Qué buscar.** "snapshot interpolation", "state synchronization vs deterministic lockstep", "delta compression game networking".

### 5.3 Latencia, *jitter*, RTT y percentiles [B]

**Qué es.** **Latencia**: tiempo de ida. **RTT** (*round-trip time*): ida y vuelta (el proyecto lo mide con ping/pong). **Jitter**: variación de la latencia entre paquetes. Para un juego importa más el **p99** (el peor 1 %) que el promedio: los tirones los provoca la cola de la distribución.

**Dónde está.** `ClientSession._on_pong` (`server/adapters/client_session.py:210`) (mide), `tick_ms` y `rtt_ms` en `/metrics` (`Metrics.snapshot` (`server/metrics.py:46`)), columnas `rtt50`/`rttmx` de `tools/monitor.py`.
**Qué buscar.** "latency vs jitter vs RTT", "tail latency p99", "why percentiles not averages".

### 5.4 Interpolación de entidades (dibujar en el pasado) [B]

**Qué es.** Los estados llegan a 30 Hz con *jitter*; dibujarlos tal cual se ve a tirones. Se guarda un pequeño historial y se **dibuja ~100 ms en el pasado**, mezclando linealmente los dos estados que rodean ese instante (`lerp`). Se paga un retraso fijo a cambio de movimiento suave.

**Dónde está.** `pushSnapshot` (`client/client.js:109`), `interpolatedDiscs` (`client/client.js:145`), `lerpDiscs` (`client/client.js:133`) y `startsNewRound` (`client/client.js:125`) (los discos *saltan* a su punto de salida: no se mezclan).
**Qué buscar.** "entity interpolation", "Source Multiplayer Networking" (Valve, sección de interpolación), Gambetta "Entity Interpolation".

### 5.5 Lo que el proyecto **no** hace (y cómo decirlo) [B]

* **Predicción del cliente y reconciliación:** los juegos de acción mueven tu propio personaje al instante en tu pantalla y luego corrigen contra el servidor. Aquí **no**: tu propio disco también se ve ~100 ms + RTT después de pulsar. Es una simplificación asumida (el profe evalúa el servidor).
* **Compensación de lag** (*lag compensation*) y **compresión delta**: no se usan.
* **Extrapolación:** si no llega un estado, el cliente **mantiene** el último; no adivina hacia dónde iba.

Decirlo tú, con su motivo, suma: demuestra que conoces la técnica y que la descartaste a propósito.

### 5.6 Ancho de banda: por qué importa el tope de 8 [B]

Un estado pesa ~330 bytes con 8 discos (medido; 456 con 12) y se envía 30 veces por segundo **a cada jugador**: con N jugadores en una sala el tráfico crece como **N × N**. Por eso hay tope por sala, decimales limitados (`POSITION_DECIMALS`) y envío más lento (10 Hz) cuando nada se mueve.
**Medido:** 100 jugadores jugando a la vez ≈ 790 KB/s de salida. **Qué buscar.** "bandwidth cost of broadcast N squared", "area of interest management" (técnica avanzada, solo contexto).

---

## 6. Arquitectura, SOLID y patrones [A]

### 6.1 Arquitectura hexagonal (puertos y adaptadores) [A]

**Qué es.** El **núcleo** (dominio y casos de uso) no depende de la tecnología: define **puertos** (interfaces) y los **adaptadores** (red, JSON, WebSocket) los implementan. Las dependencias apuntan **hacia adentro**: el dominio no sabe que existe `asyncio` ni `socket`, así que se prueba sin red.

**Dónde está.** Capas `server/domain/` (sin red), `server/application/` (`SnapshotObserver` (`server/application/ports.py:52`), `SnapshotEncoder` (`server/application/ports.py:76`)), `server/adapters/` (todo lo de red) y `build` (`server/main.py:71`) (el único sitio que conecta). Una prueba comprueba que el dominio no importa red (`test_dominio_no_importa_red`).
**Qué buscar.** Alistair Cockburn "Hexagonal Architecture" (el artículo original), "ports and adapters", "clean architecture dependency rule".
**Te preguntarán.** "¿Por qué `GameSession` no sabe qué es un WebSocket?" "¿Qué pasaría si cambias JSON por binario?" (solo cambias `JsonFrameEncoder`).

### 6.2 Inyección de dependencias e inversión de dependencias [A]

**Qué es.** **Inversión de dependencias** (la D de SOLID): los módulos de alto nivel dependen de abstracciones, no de detalles. **Inyección**: en vez de que una clase cree lo que necesita, se lo **pasan por el constructor**. Aquí todo se arma en un solo lugar (`build` (`server/main.py:71`)) y `Config` se pasa por constructor, no es una variable global.

**Qué buscar.** "dependency inversion principle", "dependency injection constructor", "composition root" (así se llama `main.build`).

### 6.3 SOLID, con el ejemplo de tu código [A]

| Principio | Idea | Ejemplo concreto |
| --- | --- | --- |
| **S** — Responsabilidad única | Una clase, una razón para cambiar | `FrameReader` solo parte bytes; `Physics` solo mueve discos; `Hall` solo decide quién está en qué sala |
| **O** — Abierto/cerrado | Se extiende **sin modificar** | Una fase nueva = una clase `RoundState`; un mensaje nuevo = una entrada en el registro del codec; el tick no se toca |
| **L** — Sustitución de Liskov | Una subclase puede usarse donde se espera la base | Toda fase cumple "`on_tick` no lanza y devuelve una fase" (hay un fuzz de 5000 ticks) |
| **I** — Segregación de interfaces | Interfaces pequeñas | `SnapshotObserver` solo recibe bytes ya codificados; `SnapshotEncoder` solo codifica |
| **D** — Inversión de dependencias | Depender de abstracciones | `GameSession` depende de los puertos, no de `ClientSession` |

La rúbrica del Proyecto 1 habla de eliminar los bloques `if/switch` masivos con **polimorfismo y registros**. En tu código: el codec (`game_codec` (`server/adapters/codec.py:134`)), los opcodes (`ClientSession.__init__` (`server/adapters/client_session.py:39`)) y los manejadores del cliente (`handlers` (`client/client.js:164`)) son diccionarios, no `if/elif`.
**Qué buscar.** Robert C. Martin "SOLID principles", refactoring.guru (explicación con ejemplos de cada uno).

### 6.4 Los cinco patrones de diseño (GoF) del proyecto [A]

| Patrón | Qué problema resuelve | Dónde | Qué buscar |
| --- | --- | --- | --- |
| **State** | Cambiar de comportamiento según la fase sin un `if fase == ...` | `RoundState` (`server/domain/round_states.py:34`), `Room.step` (`server/domain/room.py:21`) | "State pattern GoF" |
| **Command** | Separar **cuándo llega** una orden de **cuándo se ejecuta**; base del único escritor | `Command` (`server/domain/commands.py:28`), `HallCommand` (`server/domain/commands.py:44`), `MembershipCommand` (`server/application/session_commands.py:28`) | "Command pattern" |
| **Observer** | Que el tick publique sin conocer a los receptores | `SnapshotObserver` (`server/application/ports.py:52`) ← `ClientSession` (`server/adapters/client_session.py:38`) | "Observer pattern" |
| **Strategy** | Cambiar un algoritmo (frecuencia de envío) sin tocar al que lo usa | `SendPolicy` (`server/application/send_policies.py:12`) | "Strategy pattern" |
| **Factory Method** (con registro) | Crear el objeto correcto (comando) según un dato (el tipo del mensaje) | `MessageCodec` (`server/adapters/codec.py:44`) | "Factory Method", "registry pattern" |

Para cada patrón debes poder decir: **el problema concreto**, **qué pasaría sin él** y **dónde está la línea**. La rúbrica pide "justificando el problema específico de arquitectura que resuelve cada uno".

### 6.5 Doble despacho [B]

**Qué es.** Elegir el código según el tipo de **dos** objetos sin `if isinstance`. Aquí: el tick llama a `command.run(hall)`; cada tipo de comando implementa `run` a su manera (`Command.run` (`server/domain/commands.py:35`) busca la sala del jugador; `HallCommand.run` (`server/domain/commands.py:53`) actúa sobre el vestíbulo). Es una variante simple del patrón **Visitor**.
**Qué buscar.** "double dispatch", "visitor pattern".

### 6.6 Diagrama de clases UML [B]

**Qué es.** La notación estándar para dibujar clases, herencia (`<|--`), implementación de interfaces, asociaciones. El diagrama de `docs/arquitectura.md` está en **Mermaid** (se ve en GitHub).
**Aviso honesto:** el documento de la rúbrica incluye dos proyectos; el **Proyecto Final 2** (el tuyo) **no** califica UML ni repositorio (eso es del Proyecto 1). Se entrega igual, y ayuda a explicar.
**Qué buscar.** "UML class diagram relationships", "Mermaid class diagram syntax".

---

## 7. Física y matemáticas del juego [B]

### 7.1 Vectores y normalización [B]

Un vector 2D `(x, y)` representa posición, velocidad o dirección. **Normalizar** = dividir por su longitud para obtener longitud 1 (solo dirección). El vector cero no se puede normalizar (dividir entre 0): el proyecto lo deja en cero (`Vec2.normalized` (`server/domain/vector.py:37`)). El **producto punto** mide cuánto se alinean dos vectores (`Vec2.dot` (`server/domain/vector.py:28`)).
**Qué buscar.** "vector normalization", "dot product meaning".

### 7.2 Choque elástico y conservación del momento [B]

**Qué es.** En un choque **elástico** se conservan el **momento** (`m·v`) y la **energía cinética**. En 1D, para dos discos de masas `ma`, `mb` que se acercan a lo largo de la normal `n`:
`va' = va − (2·mb/(ma+mb))·((va−vb)·n)·n` y `vb' = vb + (2·ma/(ma+mb))·((va−vb)·n)·n`. Las componentes **tangenciales** no cambian. El disco en embestida tiene **masa ×2**, y por eso "empuja más" sin reglas especiales.
**Dónde está.** `Physics._collide` (`server/domain/physics.py:104`) y `Physics.mass` (`server/domain/physics.py:93`). **Qué buscar.** "elastic collision 2D circles", "conservation of momentum", "collision response impulse".

### 7.3 *Tunneling* (atravesar objetos) [B]

**Qué es.** La colisión se detecta comparando posiciones **tick a tick**. Si un disco avanza en un tick **más que su radio**, puede pasar "a través" de otro sin que se detecte nunca el solapamiento. Regla del proyecto: `velocidad máxima × dt < radio`. Por eso `Config` **rechaza al arrancar** una configuración que lo permita, y la física recorta toda velocidad a `DASH_MAX_SPEED` (`Physics._cap_speed` (`server/domain/physics.py:89`)).
**Dónde está.** `Config.__post_init__` (`server/config.py:278`). **Qué buscar.** "tunneling collision detection", "continuous collision detection" (la alternativa más cara).

### 7.4 Determinismo y paso fijo [B]

Con paso fijo y los mismos datos de entrada, la simulación da **siempre el mismo resultado**. Eso permite probar la física con `pytest` sin sockets y reproducir un fallo. **Qué buscar.** "deterministic simulation fixed timestep".

### 7.5 Fricción exponencial independiente del tick [B]

La velocidad se multiplica por `exp(-k·dt)` en cada paso: es el decaimiento exponencial, **igual de rápido a cualquier frecuencia de tick** (a diferencia de `v *= 0.9`, que depende de cuántos ticks haya por segundo). Hay dos fricciones: al caminar frena rápido (control), y por encima de `MAX_SPEED` frena menos (para que el impulso llegue lejos).
**Dónde está.** `Physics.step` (`server/domain/physics.py:20`). **Qué buscar.** "frame-rate independent friction exponential decay".

---

## 8. Python y cliente web [B]

### 8.1 Python que usa el proyecto [B]

| Elemento | Para qué | Dónde |
| --- | --- | --- |
| `dataclass(frozen=True, slots=True)` | Clases de datos inmutables (y ligeras) | `Vec2` (`server/domain/vector.py:12`), `Config` (`server/config.py:16`) |
| `abc.ABC` / `@abstractmethod` | Interfaces y clases base abstractas | `SnapshotObserver` (`server/application/ports.py:52`), `RoundState` (`server/domain/round_states.py:34`) |
| `enum.IntEnum` | Constantes con nombre que también son enteros | `Opcode` (`server/adapters/ws_frames.py:27`), `CloseCode` (`server/adapters/ws_frames.py:36`) |
| `struct.pack` / `unpack_from` | Leer y escribir enteros en el orden de bytes de la red (`"!H"` = 16 bits *big-endian*) | `FrameReader.next_frame` (`server/adapters/ws_frames.py:97`) |
| `bytes` vs `bytearray` | Inmutable vs mutable (el búfer del reader es `bytearray`) | `FrameReader.feed` (`server/adapters/ws_frames.py:94`) |
| `int.from_bytes` | Truco del XOR de la máscara | `_unmask` (`server/adapters/ws_frames.py:162`) |
| `hashlib`, `base64` | SHA-1 y base64 del handshake | `accept_key` (`server/adapters/http_handshake.py:61`) |
| `logging` y `logging.Filter` | Registro y filtro anti-inundación | `RateLimitFilter` (`server/log_filters.py:17`) |
| Anotaciones de tipo (`int \| None`) | Documentan y ayudan a detectar errores | todo el código |
| `:=` (morsa) | Asignar y comprobar en una línea | `ClientSession._read_loop` (`server/adapters/client_session.py:149`) |

**Qué buscar.** Documentación de Python: *dataclasses*, *abc*, *struct* ("Byte Order, Size, and Alignment"), *enum*, *logging cookbook*. "network byte order big endian".

### 8.2 El cliente web [B]

| Concepto | Qué es | Dónde |
| --- | --- | --- |
| **WebSocket API** del navegador | `new WebSocket(url)`, eventos `open`, `message`, `close`; envía `JSON.stringify`; el navegador enmascara las tramas **solo** | `connect` (`client/client.js:273`) |
| **Canvas 2D** | Dibujar con código (`arc`, `fill`, `stroke`) sobre un lienzo | `draw` (`client/client.js:635`) |
| **`requestAnimationFrame`** | Llama a tu función al ritmo de la pantalla (~60 Hz) | `frame` (`client/client.js:688`) |
| **`KeyboardEvent.code`** | Tecla **física** (`KeyW`), independiente de la distribución del teclado | `MOVE_KEYS` (`client/client.js:311`) |
| **`performance.now()`** | Reloj monotónico del navegador | `pushSnapshot` (`client/client.js:109`) |
| **Ghosting del teclado** | Muchos teclados no registran ciertas combinaciones de 3 teclas | `CHARGE_CODES` (`client/client.js:319`) (por eso J y K) |
| **DOM seguro** | `textContent` en lugar de `innerHTML` para texto de otros usuarios (evita XSS) | `renderRooms` (`client/client.js:248`) |

**Qué buscar.** MDN: "WebSocket", "Canvas API", "window.requestAnimationFrame", "KeyboardEvent.code".

---

## 9. Medición, pruebas y carga [A/B]

### 9.1 Pruebas por capas [B]

| Capa | Qué comprueba | Ejemplo |
| --- | --- | --- |
| **Unitarias** del dominio | Reglas sin red | `tests/test_hall.py`, `tests/test_physics.py` |
| Con **dobles de prueba** (*fakes*) | El tick sin sockets | `FakeObserver`/`FakeEncoder` en `tests/test_game_session.py` |
| **Extremo a extremo** | Servidor real, clientes reales, puerto libre (`PORT=0`) | `tests/test_server_e2e.py` |
| **Fuzz** con semilla fija | Que nada lance excepciones con entradas al azar (reproducible) | `test_ningun_estado_lanza_excepciones_con_entradas_al_azar` |
| **Mutación manual** | Romper el código a propósito y comprobar que alguna prueba falla | bitácora, entradas 2 y 3 |

**Qué buscar.** "pytest fixtures parametrize", "test doubles fake vs mock", "mutation testing", "property-based fuzzing".

### 9.2 Pruebas de carga y qué medir [A]

**Qué es.** Demostrar con una herramienta que el servidor atiende muchos clientes a la vez y **qué le pasa bajo carga**. Lo que se mira: ticks por segundo (¿llega a 30?), **p50/p99 del tick** (¿cabe en 33 ms?), conexiones, descriptores, memoria, CPU, descartes y timeouts.
**Dónde está.** `tools/monitor.py` (en vivo), `tools/bots.py`, `tools/churn.py`, `tools/slow_client.py`, `tools/model_bench.py`.
**Resultados clave:** 100 jugadores en 13 salas jugando a la vez → tick p50 0,9 ms y p99 ~2 ms (de 33 ms); 10 443 conexiones en 15 s de `churn` sin fugas.
**Qué buscar.** "load testing methodology", "latency percentiles p50 p99 p999", "coordinated omission" (concepto avanzado).

### 9.3 La lección del benchmark: **audita la herramienta de medición** [A]

El primer benchmark del proyecto daba números falsos porque **el generador de clientes** (un solo proceso de Python) era el cuello de botella, no el servidor. Hay que comprobar que lo que se mide es lo que se cree medir.
Y otra: **PSS vs RSS** — al comparar memoria entre procesos, RSS cuenta N veces las páginas compartidas; **PSS** las reparte. **Qué buscar.** "benchmarking pitfalls load generator bottleneck", "PSS vs RSS memory".

---

## 10. La IA como par programador y tu bitácora [A]

El criterio 4 (20 puntos) pide una bitácora de *prompts* y que **identifiques errores** de concurrencia, código bloqueante o falta de sincronización, y los **refactorices**. Tu bitácora (`docs/bitacora-ia.md`) tiene 14 entradas; cada una **enseña un concepto**, y debes poder explicarla:

| # | El error | Concepto que enseña | Dónde estudiarlo |
| --- | --- | --- | --- |
| 1 | La validación de *tunneling* no cubría el peor caso | Física: velocidad máxima y masa ×2 | §7.3 |
| 2 | Una prueba que no detectaba el *tunneling* | Probar la prueba: mutación | §9.1 |
| 3 | Tope de cabecera HTTP que se saltaba si llegaba en un solo `read()` | Lecturas parciales y límites **antes** de reservar | §1.1, §2.5 |
| 4 | `start_server(sock=...)` pisaba el `listen(128)` | Backlog y qué hace `asyncio` por debajo | §1.4 |
| 5 | Timeouts "presentados como existentes" pero solo eran constantes | Una constante no es un comportamiento: **verifica antes de afirmar** | §4.2 |
| 6 | Identificar el proceso por su nombre mezclaba procesos | Identificar por **puerto/inodo**, no por nombre | §4.5 |
| 7 | `scenario_check` daba falsos FAIL (un estado por tick no está garantizado) | Buzón que reemplaza: no se asume un mensaje por tick | §3.8 |
| 8 | Falsa alarma: el logging síncrono bloqueó el event loop | `logging` es síncrono | §4.4 |
| 9 | La sala "arrancaba sola" con estado pegado | Estado oculto; reglas automáticas de "cuánto esperar" | §6 |
| 10 | La cola de turnos reordenaba un diccionario compartido | Un dato derivado se **calcula**, no se almacena | §3.7 |
| 11 | `abort()` dejaba el socket en `FIN-WAIT-1` | FIN vs RST, ventana cero | §1.6, §1.7 |
| 12 | Cifras falsas por el generador de carga | Auditar la herramienta de medición | §9.3 |
| 13 | El desempate por llegada hacía que los primeros jugaran siempre | Verificar la **propiedad** (equidad a largo plazo), no un caso | §9.1 |
| 14 | Se parchó dos veces una regla que no debía existir | Cuestionar el **diseño** cuando el mismo fallo reaparece | §6 |

**Cómo defender la bitácora.** Para cada entrada: (1) qué generó la IA, (2) por qué era un error (qué principio violaba), (3) cuál fue la corrección, (4) **cómo lo verificaste**. La rúbrica premia el análisis crítico "no superficial".
**Qué buscar.** "code review of AI generated code", "AI pair programming pitfalls". Lo más valioso es que **tú reproduzcas** al menos dos de estos errores (experimentos de §11).

---

## 11. Experimentos para entenderlo de verdad

Leer no basta: **rómpelo y míralo**. Casi todos usan herramientas que ya están en el repo (los experimentos 2 y 3 llevan su propio código corto). Hazlos en este orden.

1. **El socket en escucha.** Arranca el servidor (`.venv/bin/python -m server.main`) y en otra terminal: `ss -tlnp | grep 8080`. Observa `LISTEN`, `Recv-Q` (0) y `Send-Q` (128 = tu backlog) y el `pid`/`fd=3`.
2. **TCP no conserva mensajes.** Servidor mínimo que imprime cuánto trae cada `recv`:
   ```python
   import socket
   s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
   s.bind(("127.0.0.1", 9000)); s.listen()
   c, _ = s.accept()
   while data := c.recv(4096):
       print(len(data), data)
   ```
   Cliente (no ejecuté este experimento; es lo que predice la teoría): `python3 -c "import socket,time; c=socket.create_connection(('127.0.0.1',9000)); [c.send(b'hola') for _ in range(5)]; time.sleep(1)"`. Verás que los 5 envíos pueden llegar **juntos** en un solo `recv`.
3. **La cola de aceptación.** Servidor que hace `listen(1)` y **nunca** llama a `accept()`. Abre 6 conexiones desde otra terminal y mira `ss -ltn`: `Recv-Q` debería subir hasta ~`backlog` (en Linux, a veces `backlog + 1`) y las demás quedan esperando/reintentando. **No lo medí yo**: compruébalo y anota qué ves.
4. **`TIME_WAIT` y `SO_REUSEADDR`.** Con bots conectados (`.venv/bin/python tools/bots.py --count 20 --duration 60`) haz `kill -TERM` al servidor y mira `ss -tan | grep TIME-WAIT`. Reinicia **de inmediato**: arranca. Ahora comenta la línea de `SO_REUSEADDR` en `create_listen_socket` (`server/adapters/listener.py:25`) (en una rama) y repite: debería salir `Address already in use` (es lo que predice la teoría; no lo probé yo).
5. **Descriptores y fugas.** `ls /proc/$(ss -ltnpH 'sport = :8080' | grep -o 'pid=[0-9]*' | cut -d= -f2)/fd | wc -l` antes y después de `tools/churn.py --workers 20 --seconds 15`. Debe volver al valor base.
6. **EMFILE.** Arranca el servidor con `( ulimit -n 64; .venv/bin/python -m server.main )` y lanza 100 bots. Observa que sobrevive y que el log trae `(+N iguales suprimidos)`.
7. **Backpressure y cliente lento.** Sigue el paso 3 de `docs/guion-demo.md` (`tools/slow_client.py --keepalive --room N`) y mira `ovw/s` y `to` en `tools/monitor.py`.
8. **La condición de carrera.** `git checkout demo/race-bug`, arranca el servidor y corre `tools/race_demo.py 8080`. Mira los estados/s caer a 0. Vuelve con `git checkout main`.
9. **Los modelos de concurrencia.** `.venv/bin/python tools/model_bench.py --clients 300 --models asyncio,threads --seconds 8`. Compara CPU e hilos.
10. **El WebSocket en el navegador.** En el navegador, F12 → pestaña **Red/Network** → filtro **WS** → entra a una sala y mira los mensajes `rooms`, `roster`, `state` en vivo y el `101 Switching Protocols`.
11. **Señales.** Con clientes conectados: `kill -TERM <pid>` (los bots reciben `close 1001`) y repite con `kill -KILL <pid>` (el cliente ve el TCP cortado, sin cierre limpio).

---

## 12. Ruta de estudio (orden recomendado)

Cada bloque termina con un criterio: **puedes explicarlo sin mirar**.

| Bloque | Estudia | Haz | Listo cuando puedas explicar... |
| --- | --- | --- | --- |
| 1. Sockets y TCP | §1; Beej's Guide caps. 5-6; `man 7 tcp` | Experimentos 1-4 | Qué hace cada llamada del ciclo de vida de un socket; qué pasa si el backlog se llena; por qué `TIME_WAIT` |
| 2. HTTP y WebSocket | §2; RFC 6455 §1.3, §4, §5.2, §5.3, §5.5 | Experimento 10 | El handshake paso a paso; cómo se arma una trama; por qué se enmascara |
| 3. Concurrencia | §3; docs de asyncio (Event Loop, Tasks, Streams, Synchronization) | Experimentos 7-9 | Por qué asyncio y no hilos **con números**; qué es HOL blocking; qué es una región crítica sin hilos |
| 4. Resiliencia | §4 | Experimentos 5, 6, 11 | Los tres timeouts; cómo se evitan las fugas; cómo es el cierre ordenado |
| 5. Arquitectura | §6; refactoring.guru (los 5 patrones) | Lee `docs/arquitectura.md` | Cada patrón: problema, alternativa sin él, línea de código |
| 6. Tiempo real | §5, §7 | Mira `tools/monitor.py` con bots | Servidor autoritativo, tick, interpolación, qué se simplificó |
| 7. La bitácora | §10 | Reproduce 2 errores | Cada error: qué, por qué, corrección, verificación |
| 8. Ensayo | `docs/guion-demo.md` | Hazlo completo con OBS | Los 7 pasos y sus números |

---

## 13. Autoevaluación (preguntas y respuestas cortas)

Cubre la respuesta y contesta en voz alta; si dudas, vuelve al apartado indicado.

1. **¿Por qué `recv()` puede traer media trama?** TCP es un flujo de bytes sin límites de mensaje; el kernel entrega lo que haya. → §1.1
2. **¿Qué diferencia hay entre el socket de escucha y el de conexión?** El de escucha solo acepta; `accept()` devuelve un socket nuevo por cliente que sí transporta datos. → §1.2
3. **¿Qué hace `SO_REUSEADDR` y por qué lo necesitas?** Permite `bind` aunque haya conexiones en `TIME_WAIT`; sin él, reiniciar da *Address already in use*. → §1.3
4. **¿Qué es `TIME_WAIT` y quién lo tiene?** Estado del lado que cierra primero, ~60 s, para absorber paquetes retrasados. → §1.3
5. **¿Qué son la cola SYN y la de aceptación?** Conexiones a medio abrir y conexiones completas esperando `accept()`; la segunda mide `min(backlog, somaxconn)`. → §1.4
6. **¿Cómo ves el tamaño y la ocupación de la cola de aceptación?** `ss -ltn`: `Send-Q` = tamaño, `Recv-Q` = ocupación. → §1.4
7. **¿Qué es `EMFILE` y qué hace tu servidor?** Se acabaron los descriptores; `accept` falla; asyncio reintenta cada 1 s y el servidor sobrevive. → §1.5
8. **¿Qué indica `CLOSE_WAIT` acumulado?** Que el cliente cerró y el servidor no: fuga. → §1.6
9. **¿Por qué `abort()` dejaba `FIN-WAIT-1` y qué lo arregla?** El FIN viaja detrás de los datos pendientes; con ventana cero nunca sale; `SO_LINGER {1,0}` manda RST. → §1.6
10. **¿Cómo se completa el handshake de WebSocket?** `GET` con `Upgrade`, clave aleatoria; `101` con `base64(SHA-1(clave+GUID))`. → §2.1
11. **¿Por qué las tramas del cliente van enmascaradas?** Para que proxies no confundan el payload con HTTP; no es cifrado. → §2.3
12. **¿Cómo se codifica la longitud de una trama?** 7 bits; 126 → 2 bytes siguientes; 127 → 8 bytes. → §2.2
13. **¿Qué es el GIL y por qué importa aquí?** Un solo hilo ejecuta bytecode a la vez; los hilos no dan paralelismo de CPU y se estorban. → §3.3
14. **¿Por qué asyncio y no hilos?** Carga de E/S con conexiones largas y difusión: un hilo, 26 % de CPU frente a 165 % a 300 clientes. → §3.2
15. **¿Qué es multitarea cooperativa?** Las tareas ceden el control solo en un `await` que realmente se suspende. → §3.5
16. **¿Puede haber una condición de carrera sin hilos?** Sí: un `await` entre leer y modificar basta. → §3.6
17. **¿Por qué no hay cerrojos?** Un hilo y sin `await` en la región crítica; además hay un único escritor. → §3.6–3.7
18. **¿Qué es head-of-line blocking y cómo lo evitas?** Un consumidor lento frena a los demás; el tick nunca espera a un socket y deja bytes en un buzón de tamaño 1. → §3.8
19. **¿Qué hace `drain()`?** Se suspende si el búfer de escritura del transporte pasó la marca alta; solo lo usa el `writer` de cada cliente. → §3.9
20. **¿Por qué el `writer` hace `wait`, `clear` y **después** lee?** Si leyera antes del `clear`, un aviso entre ambos se perdería. → §3.10
21. **¿Por qué la limpieza va en `finally`?** Se ejecuta aunque la tarea se cancele o falle: socket, cupo y `Disconnect`. → §3.11
22. **¿Por qué el tick usa `next_at += dt`?** Para que los retrasos no se acumulen y no haya espiral de recuperación. → §3.12
23. **¿Qué timeouts tienes y qué evita cada uno?** Saludo 5 s (slowloris), inactividad 30 s con ping (clientes muertos), escritura 10 s (zombis). → §4.2
24. **¿Cómo es el apagado con SIGTERM?** El manejador marca un `Event`; se cierra el socket de escucha, `close 1001` a todos, 2 s de gracia, se corta a los rezagados. → §4.3
25. **¿Qué es un servidor autoritativo?** El servidor decide todo; el cliente solo manda intenciones. → §5.1
26. **¿Por qué dibujas 100 ms en el pasado?** Para tener siempre dos estados que mezclar y ocultar el *jitter*. → §5.4
27. **¿Qué no hace tu juego que sí hacen los de acción?** Predicción del cliente y compensación de lag. → §5.5
28. **¿Por qué hay un tope de 8 por sala?** El tráfico crece como N×N y la arena tiene espacio holgado para 8. → §5.6
29. **¿Qué es arquitectura hexagonal en tu proyecto?** Dominio sin red, puertos como interfaces, adaptadores para la red; `main.build` conecta. → §6.1
30. **Nombra los 5 patrones y su problema.** State (fases), Command (único escritor), Observer (difusión sin conocer receptores), Strategy (frecuencia de envío), Factory Method con registro (mensajes). → §6.4
31. **¿Qué es el *tunneling* y cómo lo evitas?** Un disco muy rápido atraviesa a otro entre ticks; `velocidad máxima × dt < radio`, validado al arrancar. → §7.3
32. **¿Qué muestran p50 y p99 del tick?** Lo típico y el peor 1 %: 0,9 ms y ~2 ms con 100 jugadores, frente a un presupuesto de 33 ms. → §9.2

---

## 14. Recursos: qué buscar (por título, sin enlaces inventados)

Busca cada uno **por su título**; no copio direcciones web porque no puedo garantizar que sigan vigentes. Casi todo está mejor en **inglés**.

**Estándares (RFC).** *RFC 6455 – The WebSocket Protocol* (el más importante; lee §1.3, §4, §5.2, §5.3, §5.5, §7.4.1). *RFC 9293 – Transmission Control Protocol* (estados y control de flujo). *RFC 9110* y *9112* (HTTP semántica y HTTP/1.1).

**Páginas de manual de Linux** (`man <nombre>` en tu terminal): `socket(7)`, `tcp(7)`, `ip(7)`, `listen(2)`, `bind(2)`, `accept(2)`, `setsockopt(2)`, `epoll(7)`, `signal(7)`, `getrlimit(2)`, `proc(5)`, `ss(8)`.

**Documentación de Python.** *asyncio* (Streams, Event Loop, Coroutines and Tasks, Synchronization Primitives, Queues, Developing with asyncio), módulos `socket` (y su *Socket Programming HOWTO*), `selectors`, `signal`, `struct`, `dataclasses`, `abc`, `logging` (y *Logging Cookbook*), `unittest`/`pytest`.

**Libros.** *Beej's Guide to Network Programming* (gratis, para sockets). *The Linux Programming Interface* (Michael Kerrisk; sockets, señales, epoll). *TCP/IP Illustrated, vol. 1* y *Unix Network Programming* (W. Richard Stevens; los clásicos). *Fluent Python* (Luciano Ramalho; capítulos de concurrencia y asyncio). *Concurrency with asyncio* (Matthew Fowler). *Design Patterns* (Gamma et al., "GoF").

**Artículos clásicos.** *The C10K problem* (Dan Kegel). *Fix Your Timestep!* y la serie de *Gaffer on Games* (Glenn Fiedler). *Fast-Paced Multiplayer* (Gabriel Gambetta, 4 partes). *Source Multiplayer Networking* (Valve, wiki de desarrolladores). *Hexagonal Architecture* (Alistair Cockburn). *Single Writer Principle* (Martin Thompson, *Mechanical Sympathy*). *How TCP backlog works in Linux* y *SYN packet handling in the wild* (artículos técnicos sobre las colas de conexión).

**Web.** MDN Web Docs: *WebSocket API*, *Canvas API*, *window.requestAnimationFrame*, *KeyboardEvent.code*. refactoring.guru: los 5 patrones y SOLID. OWASP: *Path Traversal*, *XSS*.

**Cómo buscar bien.** Usa el **término técnico exacto** en inglés y añade el contexto: `asyncio drain flow control`, `TCP zero window`, `listen backlog somaxconn`, `websocket masking why`, `SO_LINGER RST`, `epoll level triggered`. Desconfía de respuestas que no distingan **TCP** de **WebSocket** ni **hilos** de **corrutinas**; son errores muy comunes.

**Una advertencia final.** Este anexo explica los conceptos con mis palabras y con las cifras **medidas en tu proyecto**; para la defensa conviene que verifiques al menos los puntos de §1.4 (colas de conexión) y §3.3 (GIL) en las fuentes primarias, porque son los que más preguntas generan.
