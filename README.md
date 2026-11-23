# StreetPunch

Juego multijugador en tiempo real (inspirado en StreetSmash): cada jugador es un
disco en una arena, se mueve, carga una embestida y empuja a los rivales fuera
del borde; gana el último en pie. **Lo que se evalúa es el servidor**:

- Servidor TCP concurrente en **Python 3.12+ con solo la librería estándar**.
  El socket se crea a mano (`bind`, `listen`, `SO_REUSEADDR`) y se entrega a
  `asyncio`; el **WebSocket (RFC 6455) está escrito a mano**, sin librerías.
- Arquitectura **hexagonal**, con un **único escritor** del estado (el tick) y
  sin cerrojos: ver [`docs/arquitectura.md`](docs/arquitectura.md).
- Cliente: HTML + JavaScript + `canvas`, sin framework ni paso de compilación.

Proyecto Final 2, Opción A, IS294 Arquitectura Cliente-Servidor (UTP).

## Cómo correrlo

```bash
python3 -m venv .venv && .venv/bin/pip install pytest      # solo para las pruebas
.venv/bin/python -m server.main                            # sirve en http://localhost:8080
```

Abre `http://localhost:8080` en varias pestañas. Al entrar verás el **vestíbulo**
con la lista de salas: **crea una** (eres su anfitrión) o **únete** a una abierta.
Cada sala admite hasta 8 jugadores y **todos juegan cada ronda**; con la sala
llena o jugando no se puede entrar. El anfitrión pulsa «Iniciar partida» cuando
están todos. Controles: flechas o WASD para moverse; Espacio (o J / K) mantenido
para cargar y soltado para embestir.

Con `Ctrl+C` o `kill -TERM <pid>` el servidor se apaga en orden (close 1001 a
cada cliente).

## Pruebas

```bash
.venv/bin/python -m pytest -q                   # unitarias y end-to-end contra un servidor real
.venv/bin/python tools/scenario_check.py        # escenarios de física contra el servidor corriendo
```

## Herramientas de carga y verificación (`tools/`)

| Herramienta | Para qué |
| --- | --- |
| `monitor.py` | Una línea por segundo: ticks/s, p50/p99 del tick, RTT, conexiones, salas, jugadores, descriptores, memoria, CPU, alertas. Lee `GET /metrics` y `/proc`. |
| `bots.py --count 50 [--room N] [--no-host]` | N jugadores simulados (responden los ping). Se unen a una sala abierta o crean la suya y la inician. `--room N`: solo a la tuya. |
| `slow_client.py [--mode trickle] [--keepalive] [--room N]` | Cliente que no lee: demuestra que no frena a los demás. |
| `churn.py` | Conexiones que se abren y cierran de todas las formas (limpia, brusca, sin hello, basura..., y crear/abandonar salas). |
| `leakcheck.py`, `fd_watch.sh` | Fugas de descriptores y `CLOSE_WAIT`. |
| `model_bench.py` | Compara asyncio, hilo por cliente, pool, proceso por cliente con la misma carga. |
| `scenario_check.py` | 11 comprobaciones de física/ronda contra el servidor real. |

## Documentación

| Archivo | Contenido |
| --- | --- |
| [`docs/diseno.md`](docs/diseno.md) | Diseño (fuente de verdad), requisitos y plan |
| [`docs/guia-del-codigo.md`](docs/guia-del-codigo.md) | **Recorrido de todo el código**: flujos de ejecución, cada clase y método, dónde está lo importante, límites honestos |
| [`docs/conceptos-y-estudio.md`](docs/conceptos-y-estudio.md) | **Anexo de estudio**: los conceptos que hay que dominar (redes, WebSocket, asyncio, patrones...), qué buscar, experimentos, autoevaluación |
| [`docs/arquitectura.md`](docs/arquitectura.md) | Estilo, diagrama de clases UML, patrones GoF y SOLID, qué tarea toca qué |
| [`docs/modelo-concurrencia.md`](docs/modelo-concurrencia.md) | Por qué asyncio, con números medidos |
| [`docs/protocolo.md`](docs/protocolo.md) | Mensajes, cierres, `/metrics`, plazos |
| [`docs/bitacora-ia.md`](docs/bitacora-ia.md) | Errores reales de la IA, con antes y después |
| [`docs/demo-race-bug.md`](docs/demo-race-bug.md) | La versión MALA a propósito (rama `demo/race-bug`) |
| [`docs/guion-demo.md`](docs/guion-demo.md) | Guion de la demo y preguntas de la defensa |

## Estructura

```
server/domain/       juego puro (sin red ni asyncio): Hall, Room, World, fases, física
server/application/  el tick (GameSession), puertos y políticas de envío
server/adapters/     listener, saludo HTTP, WebSocket, codec, ClientSession
server/main.py       único lugar que arma los objetos y registra las señales
server/config.py     todos los valores numéricos, cada uno con su porqué
client/              HTML + JS + canvas
tests/  tools/  docs/
```

Los agentes de IA trabajaron bajo las reglas de [`AGENTS.md`](AGENTS.md).
