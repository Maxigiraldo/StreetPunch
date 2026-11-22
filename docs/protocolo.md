# Protocolo de StreetPunch — versión 1

El servidor es la **autoridad**: decide la física, las colisiones y las eliminaciones.
El cliente solo envía **intenciones** (hacia dónde quiere moverse y si carga la
embestida). El servidor nunca acepta posiciones del cliente.

Los límites numéricos que se citan aquí viven en `server/config.py`.

## 1. Transporte

| Capa | Qué |
| --- | --- |
| TCP | Socket `SOCK_STREAM` creado a mano, `HOST:PORT` de `config.py` |
| HTTP | `GET /` entrega `client/index.html` y los `.js`; `GET /ws` con `Upgrade: websocket` abre el WebSocket |
| WebSocket | RFC 6455. Solo se usan tramas de texto (`0x1`), close (`0x8`), ping (`0x9`) y pong (`0xA`) |
| Mensajes | Una trama de texto = un objeto JSON en UTF-8 con un campo `t` (tipo) |

### Respuestas HTTP del saludo

| Situación | Respuesta |
| --- | --- |
| `GET /` o un archivo del cliente | 200, el archivo ya cargado en memoria, y se cierra la conexión |
| `GET /ws` válido (§4.2.1 del RFC) | 101 con `Sec-WebSocket-Accept = base64(SHA-1(clave + GUID))` |
| Ruta que no está en la lista blanca | 404 |
| Método distinto de `GET` | 405 |
| Falta `Host`, `Upgrade` o `Connection: upgrade`, o la clave no son 16 bytes en base64 | 400 |
| `Sec-WebSocket-Version` ≠ 13 | 426 con `Sec-WebSocket-Version: 13` |
| Cabecera > `MAX_HTTP_HEADER_BYTES` (8 KB) | 431 |

Si en el mismo `read()` llegan bytes después de la cabecera (la primera trama), no se pierden: pasan al `FrameReader`.

### Reglas de framing

- Las tramas del cliente **deben** venir enmascaradas. Si una no trae máscara, se cierra con **1002**.
- Las longitudes de 7, 16 y 64 bits se aceptan, pero una trama mayor que `MAX_FRAME_BYTES` (4 KB) se rechaza **antes** de reservar memoria y se cierra con **1009**.
- No hay fragmentación: una trama de datos sin `FIN` o con opcode `0x0` es una violación del protocolo y se cierra con **1002**.
- Las tramas binarias (`0x2`) no se usan. Se cierra con **1003**.
- El servidor envía un ping cada `PING_INTERVAL_S` (10 s). Si pasan `IDLE_TIMEOUT_S` (30 s) sin recibir nada, cierra la conexión.
- El servidor contesta cada ping con un pong que lleva el mismo payload.
- Si una trama de texto no es UTF-8 válido, se cierra con **1007** (lo detecta el codec).

## 2. Ciclo de vida de una conexión

```
cliente                                servidor
  | --- TCP connect ----------------------> |  accept + cupo del semáforo
  | --- GET /ws (Upgrade) ----------------> |  timeout de saludo: 5 s
  | <-- 101 Switching Protocols ----------- |
  | --- {"t":"hello","name":"Ana"} -------> |  JoinCommand -> cola de entradas
  | <-- {"t":"welcome",...} --------------- |  lo genera el tick (único escritor)
  | <-- {"t":"state",...} x 30 Hz -------- |  10 Hz si es espectador
  | --- {"t":"input",...} / charge ------> |
  | --- close ----------------------------> |  LeaveCommand; se libera el cupo
```

`hello` debe ser el **primer** mensaje. Cualquier otro antes de él se responde con `error` y se cierra con 1008.

## 3. Mensajes cliente → servidor

| `t` | Campos | Ejemplo | Validación |
| --- | --- | --- | --- |
| `hello` | `name`: string | `{"t":"hello","name":"Ana"}` | Solo una vez. Tras quitar espacios, entre 1 y `MAX_NAME_LEN` (16) caracteres. Si no, `bad_name` |
| `input` | `dx`, `dy`: entero en {-1, 0, 1}; `seq`: entero ≥ 0 (opcional) | `{"t":"input","dx":1,"dy":-1,"seq":42}` | Fuera de rango → `bad_message` |
| `charge` | `on`: booleano | `{"t":"charge","on":true}` | `true` empieza a cargar y `false` suelta la embestida. Si no es booleano → `bad_message` |

- Los campos desconocidos se **ignoran**. Si un cliente manda `x`/`y`, el servidor ni los lee.
- Solo se aplican `MAX_INPUTS_PER_CLIENT_PER_TICK` (4) entradas por cliente por tick. Las demás se descartan en silencio y se cuentan en las métricas, sin error ni cierre.
- Si la cola de entradas compartida está llena, la entrada también se descarta y se cuenta.

## 4. Mensajes servidor → cliente

### `welcome` (una sola vez, dirigido a un cliente)

```json
{"t":"welcome","id":7,"color":"#e4572e","role":"player","arena":{"radius":300.0,"disc_radius":24.0}}
```

| Campo | Significado |
| --- | --- |
| `id` | Identificador del cliente durante la sesión |
| `color` | Color asignado a su disco |
| `role` | `"player"` o `"spectator"` en el momento de entrar |
| `arena` | Geometría para dibujar |

Llega siempre **antes** del primer `state`. El tick genera el `welcome` en el mismo tick en que ejecuta el `JoinCommand`, y el `writer` lo envía antes que el contenido del buzón.

### `state` (cada tick, el mismo para todos)

```json
{"t":"state","tick":1234,"phase":"playing","ms":null,"winner":null,
 "scores":{"7":3,"9":1},"d":[[7,312.5,140.2,0.6,1],[9,100.0,80.3,0.0,0]]}
```

| Campo | Significado |
| --- | --- |
| `tick` | Número de tick, creciente. Sirve para descartar estados viejos y para interpolar |
| `phase` | `"waiting"`, `"countdown"`, `"playing"` o `"round_over"` |
| `ms` | Milisegundos que le quedan a la fase actual; `null` si la fase no tiene plazo (`waiting`, `playing`) |
| `winner` | Id del ganador durante `round_over`; `null` en las demás fases o si nadie ganó |
| `scores` | Rondas ganadas por id (las claves son strings porque JSON solo admite claves string) |
| `d` | Discos de la ronda: `[id, x, y, carga 0..1, vivo 1/0]`. Un disco eliminado queda con `vivo = 0` hasta el fin de la ronda |

**Cada `state` se basta a sí mismo.** No existen mensajes separados de cambio de fase ni de fin de ronda. El cliente compara `phase` con la del estado anterior para detectar los cambios. Por eso el servidor puede descartar estados sin miedo: el buzón de tamaño 1 reemplaza el viejo, y el siguiente trae toda la información.

**Rol actual:** el cliente juega si su `id` aparece en `d`. Si no aparece, es espectador (está en cola o la ronda empezó sin él).

### `error` (antes de un cierre por regla)

```json
{"t":"error","code":"bad_name"}
```

| `code` | Cuándo | Cierre |
| --- | --- | --- |
| `bad_name` | Nombre vacío, demasiado largo o que no es string | 1008 |
| `bad_message` | JSON inválido, `t` desconocido o campos fuera de rango | 1008 |
| `not_hello` | Primer mensaje distinto de `hello`, o un segundo `hello` | 1008 |
| `full` | Se alcanzó `MAX_CONNECTIONS` | 1013 |

## 5. Códigos de cierre

| Código | Significado en StreetPunch |
| --- | --- |
| 1000 | Cierre normal |
| 1001 | El servidor se apaga (SIGTERM/SIGINT) |
| 1002 | Violación del framing (trama sin máscara, fragmentación) |
| 1003 | Trama de un tipo que no se acepta (binaria) |
| 1007 | Texto que no es UTF-8 válido |
| 1008 | Mensaje que viola el protocolo del juego (va precedido de `error`) |
| 1009 | Trama mayor que `MAX_FRAME_BYTES` |
| 1013 | Servidor lleno |

## 6. Pendiente

- **`seq` en `input`.** El diseño dice que `seq` "mide latencia", pero para eso el servidor tendría que devolverle a cada cliente su último `seq` procesado. Eso no cabe en `state`, porque `state` es un solo `bytes` para todos. Hay que decidirlo antes de P4: o se quita `seq`, o la latencia se mide con el RTT de ping/pong.
