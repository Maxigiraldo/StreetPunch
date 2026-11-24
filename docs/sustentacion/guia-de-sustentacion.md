# Guía de la sustentación en vivo

Orden sugerido para mostrar el proyecto **en vivo, en `localhost`**, y respuestas cortas a las
preguntas probables. Todo se explica y se ejecuta en el momento. Los números entre
paréntesis son los que salieron al medir (en otro equipo variarán un poco).

Terminales sugeridas: **T1** servidor, **T2** monitor, **T3** herramientas, más el navegador.

## 0. Antes de empezar (checklist)

- [ ] Nada escuchando en 8080: `ss -tlnp | grep 8080` no debe mostrar nada.
- [ ] `cd` al repo, `.venv` creado, rama `main` limpia (`git status`).
- [ ] `.venv/bin/python -m pytest -q` en verde (~22 s). Si algo falla, **no sigas**: arréglalo antes.
- [ ] Navegador con una pestaña en `http://localhost:8080` (se usa en el paso 2).
- [ ] Todo corre en `localhost`; no depende de internet.

## 1. Arrancar el servidor y mirar el socket
```bash
# T1
.venv/bin/python -m server.main
# T3
ss -tlnp | grep 8080
```

Salida esperada: `LISTEN 0 128 127.0.0.1:8080 ... users:(("python",pid=...,fd=3))`.

**Qué decir:**
- El socket lo creo yo a mano (`server/adapters/listener.py:25`): `SO_REUSEADDR`, `bind`, `listen(128)`, no bloqueante, y se lo entrego a `asyncio.start_server(sock=...)`.
- En `ss` de un socket en LISTEN, **Send-Q = backlog** (128, la cola de aceptación que pedí) y **Recv-Q = cuántas conexiones ya completaron el saludo y esperan `accept()`** (0 ahora).
- `fd=3`: los descriptores 0, 1 y 2 son stdin/stdout/stderr; el socket de escucha es el primero libre.

## 2. Salas: pestañas del navegador + bots
```bash
# T2
.venv/bin/python tools/monitor.py
```

1. Abre 2 pestañas y entra con distinto nombre. Verás el **vestíbulo**: la lista de salas (vacía) y el botón «Crear sala».
2. En la pestaña 1 pulsa **Crear sala** (eres el anfitrión; el estado dice «Estás en la sala N»). En la pestaña 2 aparece la sala en la lista: **Unirse**. En la 1 pulsa **Iniciar partida**.
3. Juega unos segundos: flechas, mantener Espacio y soltar para embestir.
4. Lanza bots **sin** `--room`: forman sus propias salas.

```bash
# T3
.venv/bin/python tools/bots.py --count 50 --duration 60
```

**Qué mostrar en el monitor:** `salas` (~7), `juega` (50), `lobby` (0), `tick/s` ≈ 30 y el tick a `p50/p99` ≈ 0,5 / 1,5 ms frente a un presupuesto de 33 ms.
**Qué decir:** cada sala es una partida independiente (su propio `World` y su propia fase) y **un solo tick las avanza todas**: sigue habiendo un único escritor. Una sala llena (8) o jugando no deja entrar: ya no hay cola ni espectadores, y todos juegan cada ronda. Con 100 bots hay ~13 salas jugando a la vez y el tick sigue en p99 ≈ 2 ms.
**Para que los bots se unan a TU sala:** crea la sala en el navegador, mira su número y lanza `bots.py --count 7 --room N`.

## 3. Cliente lento: no frena a nadie
Un zombi solo llena el búfer si **está dentro de una sala que juega** (en el vestíbulo o en la sala de espera casi no recibe datos). Orden:

1. Con el servidor **limpio** (reinícialo), en el navegador **crea una sala** (eres el anfitrión) y anota su número.
2. En T3 lanza el zombi, que se une a esa sala **antes** de que empiece la ronda:

```bash
.venv/bin/python tools/slow_client.py --keepalive --seconds 60 --room N
```

3. Pulsa «Iniciar partida» (hace falta otro jugador: abre una segunda pestaña y únete). Mira en el monitor `ovw/s` (estados pisados) y `to` (timeouts).

**Resultado medido** (`tools/race_demo.py`, 60 s, 2 jugadores + 1 zombi vivo en la misma sala): los demás siguieron a **~30 estados/s todo el tiempo**; el servidor contó `mailbox_overwrites = 299` y cortó al zombi (`write_timeouts = 1`) a los 10 s.
**Qué decir:** el buzón es de **tamaño 1**: si el cliente no lee, el tick le pisa el estado viejo y sigue. `drain()` solo detiene la tarea `writer` de *ese* cliente. A los 10 s (`WRITE_TIMEOUT_S`) se le aborta con RST (`abort_with_reset`, `server/adapters/net_util.py`) porque `transport.abort()` mandaría un FIN que nunca sale con la ventana en cero y el socket quedaría en `FIN-WAIT-1`. El búfer de envío del kernel se limitó a 64 KB (`SEND_BUFFER_BYTES`): el valor por defecto (~2,6 MB por conexión) tardaba más de 10 minutos en notar al zombi.

## 4. Churn y descriptores: nada se fuga
```bash
# T3
.venv/bin/python tools/churn.py --workers 20 --seconds 30
.venv/bin/python tools/leakcheck.py --clients 50
# opcional, en otra terminal: tools/fd_watch.sh 8080
```

**Qué mostrar:** `fd` vuelve a su valor base (7), `ESTAB` baja a 0 y `CLOSE_WAIT` queda en **0**. `churn.py` abre y cierra de todas las formas: limpia, brusca (RST), sin `hello`, JSON basura, trama sin máscara, HTTP, y **crear y abandonar salas** (también cerrando de golpe dentro de una sala). Medido: **10 443 conexiones en 15 s** sin ninguna fuga.
**Qué decir:** cada conexión libera su socket y su cupo del semáforo en un `finally` (`server/adapters/listener.py`), sea cual sea el camino de salida. `CLOSE_WAIT` acumulado sería la firma de una fuga: el cliente cerró y el servidor no.

## 5. El código MALO contra el bueno
El mismo escenario, sobre la rama que rompe tres reglas a propósito (`docs/demo-race-bug.md`):

```bash
# Bueno (main)
.venv/bin/python -m server.main            # T1
.venv/bin/python tools/race_demo.py 8080   # T3  -> ~30 estados/s siempre
# parar T1 con Ctrl+C

git checkout demo/race-bug                         # etiqueta demo-race-bug-v2
.venv/bin/python -m server.main 2> /tmp/malo.log   # T1 (el log va a stderr)
.venv/bin/python tools/race_demo.py 8080           # T3  -> 0 estados/s ~10 s
grep -c "fallo dentro del tick" /tmp/malo.log      # ~221
grep -c "dictionary changed size" /tmp/malo.log    # 0 o 1: es probabilístico
git checkout main
```

**Resultado medido (2 corridas):** bueno 30 Hz y 0 errores. Malo: **0 estados/s durante ~9 s**, **221** `fallo dentro del tick` y **1** `RuntimeError: dictionary changed size during iteration` por corrida (el error de diccionario es probabilístico; la congelación sí es fiable).
**Qué decir:** el error clásico es `await writer.drain()` por cliente dentro del recorrido de difusión: un cliente que no lee congela a todos (head-of-line blocking), y como `await` cede el control, otra tarea borra del diccionario que el `for` está recorriendo. Con `asyncio` no hacen falta hilos para tener una condición de carrera: basta un `await` entre leer y modificar. Mi regla es **único escritor**: nadie toca el mundo salvo el tick; entrar y salir son comandos.

## 6. `ulimit -n 64`: EMFILE
```bash
# T1 (reinicia el servidor con un tope de 64 descriptores)
( ulimit -n 64; .venv/bin/python -m server.main )
# T3
.venv/bin/python tools/bots.py --count 100 --duration 15 --ramp 0.01
```

**Resultado medido:** el proceso llegó a **64 descriptores** y el servidor **siguió vivo**; de 100 bots conectaron **57** (los demás no pudieron: no había descriptores; los bots crearon 8 salas) y nadie recibió un error de servidor. En el log salió `OSError: [Errno 24] Too many open files` y líneas como `(+2942 iguales suprimidos)` y `(+8319 iguales suprimidos)`: **11 261 líneas** que no llegaron al disco. **Al soltar las conexiones, los descriptores volvieron a 7 y las salas a 0** (medido).
**Qué decir:** cuando `accept()` falla por EMFILE, `asyncio` ya reintenta cada 1 s en vez de morir, pero escribe un traceback por intento: miles. Como `logging` es **síncrono**, eso puede frenar el tick; por eso `RateLimitFilter` (`server/log_filters.py`) deja pasar una línea cada 5 s y cuenta las suprimidas. Es el riesgo conocido de que `logging` no sea asíncrono (un `QueueHandler` con hilo aparte sería la mitigación completa, no está hecha). 

## 7. `kill -TERM` y reinicio inmediato
```bash
# T1: servidor corriendo con bots conectados (tools/bots.py --count 20 --duration 60)
kill -TERM $(ss -ltnpH 'sport = :8080' | grep -o 'pid=[0-9]*' | cut -d= -f2)
ss -tan | grep :8080 | awk '{print $1}' | sort | uniq -c
.venv/bin/python -m server.main            # reinicio INMEDIATO
```

**Resultado medido:** el servidor termina en **23 ms**; los 20 bots reciben `close 1001` (going away); quedan ~40 sockets en `TIME-WAIT`; el reinicio inmediato arranca **sin** `Address already in use`.
**Qué decir:** el manejador de señal solo marca un `Event` (no puede hacer `await`); la corrutina principal manda close 1001 a cada cliente, da un plazo (`SHUTDOWN_GRACE_S`) y aborta a los rezagados. `TIME-WAIT` lo guarda el lado que cierra primero (aquí el servidor) 2×MSL ≈ 60 s para no confundir paquetes viejos con una conexión nueva; **`SO_REUSEADDR` permite hacer `bind` aunque haya sockets en `TIME-WAIT`**, por eso el reinicio funciona.

## 8. Cierre
Mostrar `docs/modelo-concurrencia.md` (tabla: asyncio 26 % de CPU frente a 165 % de hilos a 300 clientes; y el servidor real: 100 jugadores en 13 salas, tick p99 ≈ 2 ms) y `docs/arquitectura.md` (UML). Frase: "el estilo es hexagonal con único escritor; el modelo, asyncio, porque la carga es E/S de conexiones largas con difusión, y lo medí".

---

# Preguntas probables de la defensa (con respuesta corta)

**¿Qué son la cola SYN y la cola de aceptación?** Dos colas por socket en LISTEN. La **SYN** guarda conexiones a medio abrir (llegó el SYN, se envió el SYN-ACK, falta el ACK; la acota `tcp_max_syn_backlog` y, si se llena, entran las SYN cookies). La de **aceptación** guarda las ya completas que esperan `accept()`; su tamaño es `min(backlog, net.core.somaxconn)` (aquí `min(128, 4096) = 128`). Si se llena, el kernel descarta el último ACK / el SYN y el cliente **reintenta a 1 s, 3 s, 7 s** (esa es la teoría del kernel). En mi medición, con el `listen(100)` que `asyncio` aplica por defecto y 1000 conexiones simultáneas, solo 580 de 1000 clientes llegaron a ≥ 27 Hz; con `backlog=1024`, 1000 de 1000. Es consistente con SYN descartados, pero **no lo comprobé con los contadores del kernel** (`nstat -az TcpExtListenOverflows`). Se ve con `ss -ltn` (Recv-Q = ocupación, Send-Q = tamaño).

**¿Por qué `backlog=` en `start_server`?** `asyncio.start_server(sock=...)` vuelve a llamar a `listen()` con 100 si no se le pasa, y pisaría el `listen(128)` mío (`listener.py:83`).

**¿Cómo funciona `epoll` bajo `asyncio`?** El event loop usa `selectors.DefaultSelector` = `EpollSelector` en Linux. Registra cada socket no bloqueante; `epoll_wait` duerme hasta que alguno esté listo (o venza un temporizador como el del tick) y el loop ejecuta los callbacks listos. Un solo hilo atiende a todos: no hay un hilo dormido por cliente.

**¿Y el GIL?** Solo un hilo ejecuta bytecode a la vez, así que los hilos no dan paralelismo de CPU. El I/O sí libera el GIL, pero con muchos hilos se pelean por él: lo medí (165 % de CPU y 7,6 Hz a 1000 clientes). Con `asyncio` hay un hilo: el GIL no estorba.

**¿Por qué no mover la física a otro hilo?** El tick completo, con 13 salas jugando a la vez (100 jugadores), cuesta 0,9 ms (p50) y ~2 ms (p99) de 33 ms. Otro hilo añadiría cerrojos y copias del estado, y por el GIL no habría paralelismo real. Si hiciera falta, sería un **proceso** por sala.

**¿Qué son las lecturas parciales?** TCP es un flujo de bytes, no de mensajes: un `read()` puede traer media trama o varias. `FrameReader` (`server/adapters/ws_frames.py`) acumula y entrega tramas completas; se prueba con tramas partidas en pedazos de 1 byte, varias juntas, longitudes 126 y 127, sin máscara y por encima del máximo.

**¿`TIME_WAIT` y `SO_REUSEADDR`?** Ver paso 7. `SO_REUSEPORT` es otra cosa: permite que **varios** procesos escuchen el mismo puerto (reparto de carga); no se usa.

**¿Por qué un buzón de tamaño 1 y no una cola?** En tiempo real solo importa el último estado; una cola crecería sin límite con un cliente lento (memoria) y entregaría estados viejos. Pisar es el backpressure: se cuenta en `mailbox_overwrites`.

**¿Qué pasa si un cliente manda un mensaje enorme?** Se comprueba `MAX_FRAME_BYTES` **antes** de reservar memoria; si lo supera, close 1009.

**¿Dónde hay una región crítica?** El tick completo: ningún `await` entre leer y modificar el mundo, así que no hace falta `Lock`. El tope de conexiones usa `asyncio.Semaphore` (cupo adquirido y liberado en `finally`).

**¿Qué errores corrigió a la IA?** Ver `docs/bitacora-ia.md`. Los más defendibles: la **sala que arrancaba sola con estado pegado** (9), la **cola de turnos injusta** (10 y 13, que acabó en rediseñar con salas), las **cifras falsas de la herramienta de pruebas** (7, 8 y 12), y el `transport.abort()` que dejaba el socket en `FIN-WAIT-1` (11).

**¿Por qué salas y no una arena con cola de turnos?** Lo probamos primero: una arena de 12 con cola. Se sentía injusto (el anfitrión jugaba casi siempre y mucha gente esperaba sin turno), y la regla de rotación **falló dos veces** (entradas 10 y 13 de la bitácora). Con salas de 8 y todos jugando cada ronda no hay cola: la regla injusta desaparece en vez de arreglarse. Costó más CPU (más discos activos a la vez), pero medido sigue en ~3 % del presupuesto del tick.

**¿Sigue habiendo un solo escritor con varias salas?** Sí. El tick es una sola tarea y avanza todas las salas en el mismo hilo; crear, unirse y salir de una sala son comandos que el tick ejecuta (`CreateRoomCommand`, `JoinRoomCommand`, `LeaveRoomCommand`), por la **misma cola acotada** que los movimientos, así que están sujetos al mismo límite por cliente y por tick.
