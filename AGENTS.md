# AGENTS.md — Reglas para los agentes de código

Proyecto: **StreetPunch** (inspirado en StreetSmash; el repositorio se llama `streetsmash`), servidor concurrente de juego multijugador en tiempo real.
Materia: IS294 Arquitectura Cliente-Servidor (UTP), Proyecto Final 2, Opción A.
Estudiante responsable: Max. El agente es un **par programador**, no el autor.

El documento de diseño (fases 1 a 4) es la fuente de verdad. Si una instrucción
contradice el diseño, **detente y pregunta** antes de escribir código.

---

## 1. Cómo debes trabajar

1. **Explica antes y después.** Antes de escribir código, di en 3-5 líneas qué vas a
   hacer y por qué. Después, explica lo que hiciste.
2. **Cita archivo y línea.** Cada explicación referencia `archivo.py:línea` (por
   ejemplo `server/adapters/ws_frames.py:42`) para que Max pueda ubicarla.
3. **Explica los conceptos.** Cuando uses algo de redes o concurrencia (backlog,
   `epoll`, `drain`, enmascarado de WebSocket, `SO_REUSEADDR`, `TIME_WAIT`, región
   crítica, backpressure...), explica el concepto en lenguaje claro, no solo el código.
4. **Comentarios en español**, que expliquen el *porqué*, no que repitan el código.
5. **Pasos pequeños.** Un paso del plan a la vez. Nada de generar módulos que no se
   pidieron.
6. **Sé honesto con tus dudas.** Si no estás seguro de un detalle del RFC 6455 o de
   `asyncio`, dilo. Nunca inventes un comportamiento.
7. **Señala tus propios riesgos.** Al terminar cada paso, lista lo que Max debería
   revisar con más cuidado (posibles condiciones de carrera, recursos sin liberar,
   casos borde).

## 2. Restricciones técnicas (no negociables)

- **Lenguaje:** Python 3.12+. **Solo librería estándar** en el servidor
  (`socket`, `asyncio`, `hashlib`, `base64`, `struct`, `json`, `signal`, `logging`).
- **Prohibido** cualquier librería o framework que esconda el socket o el framing:
  `websockets`, `aiohttp`, Socket.IO, FastAPI, Django, Flask, Tornado, etc.
  `asyncio.start_server(host, port)` a secas también está prohibido: el socket se crea
  a mano y se entrega con `asyncio.start_server(sock=...)`.
- **Socket:** `socket.socket(AF_INET, SOCK_STREAM)`, `SO_REUSEADDR`, `bind`,
  `listen(config.LISTEN_BACKLOG)`, `setblocking(False)`.
- **Cliente:** HTML + JavaScript sin framework + `canvas`. Sin paso de compilación.
- **Todos los valores numéricos** viven en `server/config.py`, cada uno con un
  comentario que explique por qué tiene ese valor. Nada de números mágicos en el código.

## 3. Arquitectura (hexagonal) — respétala

- `server/domain/`: lógica pura del juego. **No importa `asyncio`, `socket` ni nada de
  red.** Debe poder probarse con `pytest` sin abrir sockets.
- `server/application/`: `GameSession` (el tick), puertos (interfaces) y políticas de
  envío. Conoce interfaces, **nunca** un WebSocket concreto.
- `server/adapters/`: listener, saludo HTTP, framing WebSocket, codec y `ClientSession`.
- `server/main.py`: el **único** lugar que construye y conecta los objetos y registra
  las señales.
- Las dependencias apuntan **hacia adentro**. Si necesitas que el dominio "sepa" algo
  de la red, estás equivocando el diseño: detente y pregunta.

## 4. Reglas de concurrencia (aquí se juega la nota)

1. **Único escritor del estado.** Solo `tick_loop` modifica el estado del juego, el
   registro de jugadores y la lista de observadores. Entrar, salir, suscribirse y darse
   de baja son **comandos** (`JoinCommand`, `LeaveCommand`) que se ejecutan dentro del
   tick. Ninguna otra tarea toca esas estructuras.
2. **Nunca `await` sobre un socket dentro del tick.** El tick deja el estado en el
   buzón de cada cliente y sigue. Un `await writer.drain()` en el bucle de difusión es
   un **error** (head-of-line blocking).
3. **Cola de entradas:** `put_nowait`. Si está llena, se descarta la entrada y se
   incrementa un contador. **Nunca** `await queue.put()`.
4. **Un solo escritor por socket.** Solo la tarea `writer` de cada cliente escribe y
   hace `drain`. El `reader` deja los pong y el close en la `control_queue`; el
   `writer` atiende primero la `control_queue`, luego el `welcome` pendiente y al final el buzón. Cada trama se escribe en **una sola**
   llamada a `write()`.
5. **Orden del buzón:** el `writer` hace `await event.wait()`, luego `event.clear()`,
   y **después** lee el buzón. Leer antes de `clear()` puede perder un aviso.
6. **Estado inmutable:** el tick codifica el estado a `bytes` **una vez** por tick y
   entrega ese mismo objeto a todos los buzones.
7. **Recursos siempre liberados:** cada conexión cierra su socket y libera su cupo del
   `asyncio.Semaphore` en un `finally`, en cualquier camino de salida.
8. **Nada bloqueante en el event loop:** prohibido `time.sleep`, E/S de archivos
   síncrona en caliente, `socket.recv` bloqueante, cálculos largos.
9. **Ninguna excepción de un cliente puede tumbar el servidor.** Se captura, se
   registra y se cierra solo esa conexión.
10. Al escribir una sección crítica, **indica explícitamente** si hay algún `await`
    entre leer y modificar un recurso compartido, y por qué es seguro.

## 5. Protocolo y framing

- WebSocket según **RFC 6455** en lo que usamos: tramas de texto, ping/pong, close;
  longitudes de 7, 16 y 64 bits; máscara **obligatoria** en tramas del cliente (si no
  viene, cerrar con 1002).
- **`FrameReader` incremental:** una lectura puede traer media trama o varias tramas.
  Nunca asumas que `read()` devuelve un mensaje completo.
- Comprueba el tamaño máximo (`config.MAX_FRAME_BYTES`) **antes** de reservar memoria.
- Mensajes del juego: los definidos en `docs/protocolo.md`. Un mensaje desconocido o
  malformado → `{"t":"error",...}` y cierre 1008. El servidor **nunca** acepta
  posiciones del cliente.
- Decodificación por **registro** `{tipo: fábrica}`, no con `if/elif` por tipo.

## 6. SOLID y patrones

- Patrones del diseño: State (`RoundState`), Command, Observer, Strategy (`SendPolicy`),
  Factory Method con registro (`MessageCodec`). **No agregues patrones que no resuelvan
  un problema concreto.**
- Clases pequeñas y con una responsabilidad. Si una clase empieza a hacer dos cosas,
  avisa antes de seguir.
- Sin `if/elif` largos sobre tipos o fases: usa polimorfismo o registros.

## 7. Pruebas

- Cada paso del dominio y del framing trae sus pruebas `pytest`.
- Framing: tramas partidas en pedazos de 1 byte, varias tramas en un mismo bloque,
  longitudes 126 y 127, trama sin máscara, trama por encima del máximo.
- Explica cómo correr las pruebas y qué demuestra cada una.

## 8. Bitácora de IA

Max registra en `docs/bitacora-ia.md` cada error real que encuentre en tu código.
Si Max te señala un error, **no lo corrijas en silencio**: explica por qué era un
error, qué principio violaba y cómo lo verifica la corrección.

Formato de cada entrada:
- Fecha y paso
- Prompt
- Qué generó la IA
- Problema (categoría: concurrencia, sockets, protocolo o SOLID)
- Corrección (diff o fragmento)
- Por qué es un error
- Cómo se verificó

## 9. Lista de auditoría (Max la revisa en cada paso)

- [ ] ¿Hay alguna llamada bloqueante dentro del event loop?
- [ ] ¿Hay un `await` sobre un socket dentro del tick o del bucle de difusión?
- [ ] ¿Alguna tarea distinta del tick modifica el estado, el registro o los observadores?
- [ ] ¿Hay un `await` entre leer y modificar un recurso compartido?
- [ ] ¿Dos tareas escriben en el mismo socket?
- [ ] ¿Se asume que `read()` trae un mensaje completo?
- [ ] ¿Todos los caminos de salida cierran el socket y liberan el semáforo?
- [ ] ¿Una excepción de un cliente puede tumbar el servidor?
- [ ] ¿Hay números mágicos fuera de `config.py`?
- [ ] ¿El dominio importa algo de red?
- [ ] ¿Hay un `if/elif` que debería ser polimorfismo o registro?
