# Arquitectura de StreetPunch (la del código real)

Este documento describe lo que **hay** en `server/`, no lo planeado. Si el código
cambia, esto se actualiza con él.

## 1. Estilo: hexagonal (puertos y adaptadores) con un único escritor

```mermaid
flowchart LR
    subgraph adapters["server/adapters (red: lo que cambia)"]
        L[Listener<br/>bind/listen/accept, semáforo]
        H[HttpHandshake]
        C[ClientSession<br/>reader · writer · ping]
        F[FrameReader / FrameWriter]
        K[MessageCodec / JsonFrameEncoder]
    end
    subgraph app["server/application (casos de uso)"]
        G[GameSession<br/>EL TICK: único escritor]
        P[SnapshotObserver · SnapshotEncoder<br/>puertos]
        S[SendPolicy]
    end
    subgraph dom["server/domain (juego puro, sin red ni asyncio)"]
        HL[Hall<br/>vestíbulo y reglas de salas] --- RM[Room<br/>World + RoundState]
        RM --- PH[Physics]
        CM[Command / HallCommand]
    end
    L --> C
    C -->|comandos, put_nowait| G
    G -->|bytes en el buzón| C
    G --> HL
    G -.usa.-> P
    C -.implementa.-> P
    K -.implementa.-> P
    G --> S
```

**Regla de dependencias:** las flechas apuntan hacia adentro. `domain` no importa
`asyncio` ni `socket` (se prueba con `pytest` sin abrir un socket).
`application` conoce **interfaces** (`SnapshotObserver`, `SnapshotEncoder`), nunca
un WebSocket. `server/main.py` es el único lugar que construye y conecta los
objetos (`build()`), por inyección de dependencias en los constructores.

**Flujo de datos (un solo sentido de escritura):**

```
navegador ──trama──> reader ──Command──> cola acotada ──> TICK ──> Hall ──> Room (World + fase)
                                                            │
navegador <──trama── writer <── buzón (tamaño 1) <──bytes──┘   (estado de SU sala, 1 vez por tick)
```

El tick es el **único** que modifica el vestíbulo, las salas y la lista de
observadores. Conectarse, salir, crear/unirse/salir de una sala, iniciar,
moverse y cargar son comandos que el tick ejecuta; ninguna otra tarea toca esas
estructuras, así que no hace falta ningún `Lock`. Un solo tick avanza **todas**
las salas, una tras otra.

**Salas.** `Hall` (domain) sabe quién está conectado, qué salas hay y en cuál está
cada jugador, y aplica las reglas (tope de 8, solo se entra en espera, sala vacía
desaparece) sin ninguna red. Cada `Room` es un `World` propio más su fase.

## 2. Diagrama de clases

```mermaid
classDiagram
    direction LR

    class Listener {
        -Semaphore _slots
        -set _sessions
        +serve()
        +shutdown()
    }
    class HttpHandshake {
        +respond(req) bytes, bool
    }
    class ClientSession {
        -FrameReader _frames
        -MessageCodec _codec
        -bytes _mailbox
        +on_snapshot(data)
        +on_welcome(data)
        +on_roster(data)
        +run(leftover)
    }
    class SnapshotObserver {
        <<interface>>
        +on_snapshot(data)
        +on_welcome(data)
        +on_roster(data)
        +on_rooms(data)
        +on_notice(data)
    }
    class SnapshotEncoder {
        <<interface>>
        +encode_state(s) bytes
        +encode_welcome(w) bytes
        +encode_roster(entries, host) bytes
        +encode_rooms(rooms) bytes
        +encode_room(id) bytes
        +encode_error(code) bytes
    }
    class JsonFrameEncoder
    class MessageCodec {
        -dict registry
        +decode(payload, player_id) Command
    }
    class FrameReader {
        +feed(data)
        +next_frame() Frame
    }
    class FrameWriter {
        +text() +ping() +pong() +close()
    }

    class GameSession {
        -Hall hall
        -dict _observers
        -deque _membership
        -Queue _inputs
        +tick()
        +submit_input(id, cmd) bool
        +submit_membership(cmd)
        +run_tick_loop()
    }
    class SendPolicy {
        <<abstract>>
        +should_send(tick) bool
    }
    class EveryTick
    class EveryNthTick
    class MembershipCommand {
        <<abstract>>
        +execute(session)
    }
    class Connect
    class Disconnect

    class Command {
        <<abstract>>
        +execute(world)
        +run(hall)
    }
    class HallCommand {
        <<abstract>>
        +execute(hall)
        +run(hall)
    }
    class JoinCommand
    class LeaveCommand
    class MoveCommand
    class ChargeCommand
    class StartCommand
    class CreateRoomCommand
    class JoinRoomCommand
    class LeaveRoomCommand

    class Hall {
        +rooms
        +connect(id, name)
        +create_room(id)
        +join_room(id, room)
        +leave_room(id)
        +step()
        +summary() RoomInfo
    }
    class Room {
        +id
        +name
        +step()
    }
    class World {
        +players
        +discs
        +host() int
        +start_round()
    }
    class RoundState {
        <<abstract>>
        +on_tick(world) RoundState
    }
    class Waiting
    class Countdown
    class Playing
    class RoundOver
    class Physics {
        +step(discs, dt)
    }
    class Disc
    class Arena

    Listener --> HttpHandshake
    Listener ..> ClientSession : crea (session_factory)
    ClientSession ..|> SnapshotObserver
    ClientSession --> FrameReader
    ClientSession --> MessageCodec
    ClientSession --> GameSession : submit_*
    JsonFrameEncoder ..|> SnapshotEncoder
    MessageCodec ..> Command : fabrica

    GameSession --> SnapshotObserver
    GameSession --> SnapshotEncoder
    GameSession --> SendPolicy
    GameSession --> Hall
    GameSession --> MembershipCommand
    Hall --> Room : 1..N
    Room --> World
    Room --> RoundState
    EveryTick --|> SendPolicy
    EveryNthTick --|> SendPolicy
    Connect --|> MembershipCommand
    Disconnect --|> MembershipCommand

    JoinCommand --|> Command
    LeaveCommand --|> Command
    MoveCommand --|> Command
    ChargeCommand --|> Command
    StartCommand --|> Command
    CreateRoomCommand --|> HallCommand
    JoinRoomCommand --|> HallCommand
    LeaveRoomCommand --|> HallCommand
    Waiting --|> RoundState
    Countdown --|> RoundState
    Playing --|> RoundState
    RoundOver --|> RoundState
    Playing --> Physics
    World --> Disc
    World --> Arena
```

## 3. Patrones GoF: qué problema de arquitectura resuelve cada uno

| Patrón | Dónde | Problema concreto que resuelve | Qué pasaría sin él |
| --- | --- | --- | --- |
| **State** | `RoundState` y `Waiting`/`Countdown`/`Playing`/`RoundOver` (`server/domain/round_states.py`) | El tick no pregunta "¿en qué fase estoy?": llama a `state.on_tick(world)` y recibe la siguiente fase. Cada fase conoce solo su regla (la sala espera al anfitrión, la cuenta cancela si alguien se va...). | Un `if phase == ...` largo dentro del tick que crece con cada fase (viola OCP). |
| **Command** | `Command`, `HallCommand` y sus hijas, `MembershipCommand` (`server/domain/commands.py`, `server/application/session_commands.py`) | Separa **cuándo llega** una orden (en la tarea `reader`, en cualquier momento) de **cuándo se ejecuta** (dentro del tick). Es la base del único escritor: el reader crea el comando y lo encola; solo el tick lo ejecuta. Con `run(hall)` (doble despacho) el tick no distingue una orden de partida de una de sala. | Cada reader modificaría el mundo directamente: carreras de datos y la necesidad de cerrojos. |
| **Observer** | `SnapshotObserver` (puerto) y `ClientSession` (observador) | El tick publica estado sin saber quién lo recibe ni cómo se envía; los clientes se suscriben y se dan de baja. | `GameSession` conocería sockets (rompería la arquitectura hexagonal). |
| **Strategy** | `SendPolicy` con `EveryTick` / `EveryNthTick` | Decide a qué frecuencia se envía el estado de una sala según su **fase** (30 Hz jugando, 10 Hz en espera/cuenta/resultados) sin tocar el tick. | `if fase == "playing"` dentro del bucle de difusión. |
| **Factory Method (por registro)** | `MessageCodec` con el registro `{tipo: fábrica}` (`server/adapters/codec.py`) | Convierte un mensaje JSON en un `Command`. Un mensaje nuevo (`start`, `create`, `join`, `leave`) es **una entrada nueva en el registro**, no otro `elif`. | Un `if/elif` por tipo de mensaje en el reader. |

Otras decisiones que no son GoF y se defienden igual: **buzón de tamaño 1** por
cliente (backpressure: pisa el estado viejo), **semáforo** (tope de 100
conexiones), **inyección de dependencias** en `main.build()`.

## 4. SOLID: dónde se ve

- **S**: `FrameReader` solo parte bytes en tramas; `HttpHandshake` solo el saludo; `Physics` solo mueve discos; `Metrics` solo cuenta; `Hall` solo decide quién está en qué sala; `World` solo una partida.
- **O**: agregar un tipo de mensaje, una fase o una política de envío es **agregar una clase o una entrada** de registro; el tick no se toca.
- **L**: toda `RoundState` cumple el contrato "`on_tick` no lanza y devuelve un `RoundState`" (hay una prueba de fuzz con 5000 ticks al azar, `tests/test_round_states.py`); todo `Command` es seguro aunque el jugador ya se haya ido.
- **I**: los puertos son pequeños (`SnapshotObserver` solo recibe bytes ya codificados; `SnapshotEncoder` solo codifica).
- **D**: `GameSession` depende de `SnapshotObserver`/`SnapshotEncoder` (abstracciones); `main.build()` inyecta las concretas.

## 5. Concurrencia: qué tarea toca qué

| Tarea | Lee | Escribe | Nunca |
| --- | --- | --- | --- |
| `tick` (una) | todo | **vestíbulo, salas**, observadores, buzones | `await` sobre un socket |
| `reader` (una por cliente) | su socket | la cola de entradas (`put_nowait`) y las casillas de control | el vestíbulo ni las salas |
| `writer` (una por cliente) | su buzón y casillas (estado, roster, rooms, avisos) | **su** socket (única tarea que lo hace) | el vestíbulo ni las salas |
| `ping` (una por cliente) | — | la casilla de ping | el socket directamente |
| `Listener.serve` | — | semáforo, registros de sesiones | el mundo |

Región crítica del tick: no tiene ningún `await` entre leer y modificar el mundo,
por eso una sola tarea puede modificarlo sin cerrojos (en un solo hilo con
`asyncio`, otra tarea solo se intercala en un `await`).

## 6. Comparación con la versión mala

`git checkout demo/race-bug` (etiqueta `demo-race-bug-v1`) rompe tres de estas
reglas a propósito; ver `docs/demo-race-bug.md` para qué se mide.
