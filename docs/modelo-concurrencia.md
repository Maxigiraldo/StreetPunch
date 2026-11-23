# Por qué `asyncio` (modelo de concurrencia, con números medidos)

La rúbrica pide "justificar por qué es la solución óptima **para la carga elegida**".
Este documento define la carga, mide los cuatro modelos candidatos con ella
(`tools/model_bench.py`) y mide además el servidor real (`tools/monitor.py`).

## 1. La carga elegida

- Hasta **100 conexiones** (`MAX_CONNECTIONS`) repartidas en **salas de hasta 8 jugadores** (`MAX_ROOM_PLAYERS`); todos los de una sala juegan a 30 Hz (en espera, 10 Hz). En el peor caso hay ~13 salas jugando a la vez.
- Cada cliente recibe un estado de **~330 bytes** con 8 discos (medido; 456 con 12) **30 veces por segundo**. Casi todo es **E/S**: la física de 8 discos son 28 pares de colisión por tick y sala.
- **Estado compartido y pequeño, por sala**: cada sala es un mundo que solo ven sus jugadores. La difusión (un dato → 8 clientes) es el patrón dominante, y el trabajo crece con el número de salas, no con N×N global.
- Conexiones **largas** (una partida entera), no peticiones cortas.

## 2. Qué se midió y cómo

`tools/model_bench.py` levanta, uno a uno, un servidor mínimo de cada modelo con **TCP crudo** (sin WebSocket ni física, para comparar solo el modelo) que difunde 600 B a 30 Hz a N clientes (cota prudente: un estado real de StreetPunch mide 330 B con 8 discos y 456 B con 12, así que la prueba es algo más exigente que el juego). Los clientes corren en procesos aparte (hasta 8, para que el generador de carga no sea el cuello de botella; una primera versión con un solo proceso daba cifras falsas contra `asyncio` a 1000 clientes y se descartó). Se mide desde `/proc`: memoria **PSS** (reparte las páginas compartidas entre procesos; RSS las contaría N veces), hilos, procesos y CPU. Después se repite con **un cliente zombi** que conecta y no lee.

"Sanos" = reciben al menos el 90 % de lo esperado (27 Hz). Equipo: WSL2, 20 núcleos, loopback.

### 300 clientes

| Modelo | Hz que recibe cada cliente (mín.) | Memoria (PSS) | Hilos / procesos | CPU del servidor | Con un zombi |
| --- | --- | --- | --- | --- | --- |
| **asyncio** (1 hilo, `epoll`) | 28,4 | **18 MB** | 1 / 1 | **26 %** | sin cambio (28,4) |
| Hilo por cliente | 26,5 | 20 MB | 302 / 1 | **165 %** | sin cambio |
| ThreadPool de 16 (como `ThreadingTCPServer` acotado) | **0 para 284 de 300** | 16 MB | 17 / 1 | 5 % | igual: solo atiende a 16 |
| Proceso por cliente (`fork`) | 29,6 | **~220 a 260 MB** | ~300 / 301 | 36 a 46 % | sin cambio |
| Un hilo difunde con `sendall` bloqueante (**el error clásico**) | 28,0 | 15 MB | 2 / 1 | 6 % | **cae a 13,9 Hz para los 300** |

### 1000 clientes

| Modelo | Hz (mín.) | Memoria (PSS) | CPU | Nota |
| --- | --- | --- | --- | --- |
| **asyncio** | 27,2 | 25 MB | 87 % | un solo hilo ronda su techo (≈ 1 núcleo); con zombi 25,2 Hz |
| Hilo por cliente | 7,6 | 29 MB | 155 % | 780 a 1000 hilos peleando por el GIL |
| Proceso por cliente | 29,5 | ~200 MB | 55 % | funciona, pero por proceso (cota inferior, ver abajo) |
| Hilo difunde con `sendall` | 25,0 (sin zombi) | 15 MB | 17 % | con zombi: 10,6 Hz |

## 3. Qué demuestran los números

1. **ThreadPool acotado no sirve para conexiones largas.** Cada conexión de juego ocupa un worker durante toda la partida. Con un pool de 16, los 284 clientes restantes quedan **esperando en cola sin recibir nada** (0 Hz). Subir el pool es el modelo "hilo por cliente".
2. **Hilo por cliente funciona pero es caro y escala mal en CPython.** A 300 clientes gasta **6,4× más CPU** que `asyncio` (165 % frente a 26 %) para dar menos. A 1000 cae a 7,6 Hz: el GIL hace que los 1000 hilos compitan, y cada hilo reserva su pila. Con la rúbrica: "*funcional, pero consume recursos de manera desmedida bajo carga*" (nivel Satisfactorio).
3. **Proceso por cliente aísla pero cuesta memoria y no comparte estado.** ~0,8 MB por proceso incluso con copy-on-write (220 a 260 MB a 300 clientes, contra 18 MB). La medición es una **cota inferior**: cada hijo genera su propio estado; un juego real necesita que todos vean el **mismo mundo**, es decir IPC o memoria compartida, que añade latencia y complejidad.
4. **El error clásico se ve con datos.** Con un solo hilo difundiendo en fila, **un cliente que no lee frena a los 300** (28 → 13,9 Hz). Es el head-of-line blocking que la rúbrica pide evitar, y la demo `demo/race-bug` lo reproduce en el código real.
5. **`asyncio` con buzón por cliente es el más barato a esta carga** y no se frena con el zombi: el `drain` del zombi solo detiene a *su* tarea; el tick sigue y le pisa el estado viejo (`mailbox_overwrites`).

## 4. El servidor real (con WebSocket, física, ping, métricas, salas)

100 bots conectados (el tope) que forman **13 salas** y juegan **a la vez** (el peor caso: todas en `playing`, 30 Hz), midiendo con `tools/monitor.py`:

| Dato | Valor | Presupuesto |
| --- | --- | --- |
| Ticks por segundo | 29,9 | 30 |
| Duración del tick, p50 / p99 / máx | **0,9 / 2,0 / 2,7 ms** (picos con las 13 salas jugando) | 33 ms |
| Ticks pasados de presupuesto | 0 | — |
| Latencia RTT del cliente (ping → pong), p50 | 0,5 ms | — |
| Salida con las 100 jugando | ~790 KB/s | — |
| Memoria / CPU | 25 MB / 8 a 16 % | — |
| Descriptores | 107 (100 clientes + 7 base) | — |

El tick usa **~3 % de su presupuesto (p50), ~6 % en el p99 y 8 % en el máximo**, aun avanzando 13 partidas. Por eso **no** se mueve la física a otro hilo: sería complejidad (cerrojos, copias del estado) para ganar tiempo que sobra, y con GIL tampoco habría paralelismo real.

Antes de pasar a salas (una sola arena de 12 con cola) el mismo experimento daba p50 0,3 / p99 0,9 ms: las salas cuestan más CPU porque **juegan más a la vez** (100 discos activos en lugar de 12), pero cada tick sigue muy por debajo del presupuesto.

## 5. Decisión

**`asyncio` (un hilo, event loop sobre `epoll`) con un único escritor del estado**, porque:

- La carga es E/S de **conexiones largas con difusión**: exactamente para lo que un event loop gasta menos (1 hilo y 18 a 25 MB frente a cientos de hilos o procesos).
- Un único hilo hace que el **único escritor** sea una propiedad del modelo, no una promesa: no hay cerrojos que olvidar (la región crítica del tick no tiene `await`).
- Con **backpressure explícito** (buzón de tamaño 1, `SO_SNDBUF` de 64 KB, plazo de escritura) el cliente lento no frena a nadie, que es el requisito del profe.

## 6. Límites honestos (qué NO demuestra esto)

- **Es una microprueba en loopback**, con clientes y servidor en la misma máquina. Sirve para el orden de magnitud y el porqué, no como cifra absoluta. Por internet el RTT dominaría.
- **El techo de `asyncio` es un núcleo**: a ~1000 clientes × 30 Hz ronda el 90 % de CPU. Está 10× por encima del tope de diseño (100). Para pasar de ahí habría que repartir **salas** entre procesos (`SO_REUSEPORT`) con un mundo por proceso; no se implementó porque la carga no lo pide.
- **`fork` es cota inferior** de costo (sin IPC) y mide un ideal: aislamiento total sin compartir nada.
- Un hallazgo de la medición, útil para la defensa: `asyncio.start_server(sock=...)` vuelve a llamar a `listen()` con backlog **100** si no se le pasa `backlog=`. Con 1000 conexiones simultáneas y backlog 100, solo 580 de 1000 clientes llegaron a "sanos" en la primera pasada; con `backlog=1024`, 1000 de 1000. Es consistente con una cola de aceptación desbordada (SYN descartados y reintentos del cliente), pero **no lo comprobé con los contadores del kernel**. El servidor real ya lo hace bien (`server/adapters/listener.py:83`).

## 7. Cómo repetirlo

```bash
.venv/bin/python tools/model_bench.py --clients 300,1000 --seconds 8    # ~8 min; tabla de la sección 2
.venv/bin/python -m server.main                                          # y, aparte:
.venv/bin/python tools/bots.py --count 100 --duration 35 --host
.venv/bin/python tools/monitor.py                                        # sección 4
```
