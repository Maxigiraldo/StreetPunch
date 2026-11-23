# Demo P8: la versión MALA (rama `demo/race-bug`)

Esta rama contiene **a propósito** el código que `AGENTS.md` prohíbe, para
mostrar con datos por qué las reglas existen. La versión buena es `main`.
Las pruebas de `pytest` de esta rama **no** pasan (el tick es `async` aquí);
no es un descuido.

## Qué cambia respecto de `main` (2 archivos)

| Dónde | Versión buena (`main`) | Versión mala (esta rama) | Regla violada |
| --- | --- | --- | --- |
| `server/application/game_session.py` `_publish` | El tick deja los bytes en el buzón de cada cliente (`on_snapshot`, sin await) | `await observer.send_now(data)` por cliente dentro del recorrido | §4.2 (nunca `await` sobre un socket en el tick) |
| `server/adapters/client_session.py` `send_now` | Solo la tarea `writer` escribe, y `drain` lleva plazo | El tick escribe y hace `drain()` sin plazo | §4.4 (un solo escritor por socket) |
| `server/adapters/client_session.py` `run` (finally) | `submit_membership(Disconnect)`: el tick lo ejecuta | `world.remove_player()` y `_observers.pop()` directos desde la tarea del cliente | §4.1 (único escritor del estado) |

## Cómo reproducirlo

```bash
# terminal 1: servidor de la rama mala
git checkout demo/race-bug && .venv/bin/python -m server.main
# terminal 2: monitor (columna tick/s)
.venv/bin/python tools/monitor.py
# terminal 3: la prueba (usa el puerto 8080 por defecto en el monitor; el script, el que le pases)
.venv/bin/python tools/race_demo.py 8080
# luego contar en el log del servidor:
#   grep -c "dictionary changed size" y grep -c "fallo dentro del tick"
```

## Qué se mide (2 corridas independientes, 60 s)

| | `main` (buena) | `demo/race-bug` (mala) |
| --- | --- | --- |
| Estados/s que recibe A | ~30 siempre | 30 → **0.0 durante ~10 s** → vuelve a 30 |
| `RuntimeError: dictionary changed size during iteration` | 0 | **1 por corrida** (probabilístico: necesita que el `drain` termine bien justo después de un borrado) |
| `fallo dentro del tick` (broadcast abortado a todos) | 0 | **~467 por corrida** (`ConnectionResetError` de un solo cliente aborta la difusión a los demás) |

Por qué el zombi solo congela ~10 s y no para siempre: su propia tarea
`writer` (que sí lleva plazo) aborta el socket a los `WRITE_TIMEOUT_S` y eso
libera el `drain` del tick. Sin ese plazo en `writer`, quedaría congelado
hasta el timeout de inactividad (30 s) o para siempre si el zombi mandara
algo cada tanto.

## Qué demuestra

1. **Head-of-line blocking:** un solo cliente que no lee frena el broadcast de
   todos. En `main`, el tick no espera a nadie: ese cliente pisa su buzón
   (`mailbox_overwrites`) y los demás siguen a 30 Hz.
2. **Carrera de datos sin hilos:** con `asyncio` no hacen falta hilos para una
   condición de carrera; basta un `await` entre leer y modificar un recurso
   compartido. El `await` del `drain` cede el control y otra tarea borra del
   diccionario que el `for` está recorriendo.
3. **Un cliente tumba el broadcast de todos:** la excepción de uno aborta el
   recorrido (en `main`, `_notify` aísla a cada observador, §4.9).
