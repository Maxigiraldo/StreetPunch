# Bitácora de IA — StreetPunch

Registro de cada error real encontrado en el código que generó la IA (Claude Code).
La meta son al menos 5 entradas, priorizando concurrencia y sockets
(ver `docs/diseno.md`, P11).

Categorías: **concurrencia**, **sockets**, **protocolo**, **SOLID**.

---

## Plantilla

### N. Título corto del error

- **Fecha y paso:** AAAA-MM-DD · P?
- **Prompt:**
  > Texto del prompt, o un resumen fiel.
- **Qué generó la IA:**
  ```python
  # fragmento original, con archivo:línea
  ```
- **Problema (categoría):** …
- **Corrección:**
  ```diff
  - línea mala
  + línea corregida
  ```
- **Por qué es un error:** qué principio o regla de `AGENTS.md` viola y qué pasaría en ejecución.
- **Cómo se verificó:** prueba `pytest`, comando o métrica que lo demuestra antes y después.

---

## Entradas

### 1. La validación de tunneling no cubría el peor caso

- **Fecha y paso:** 2026-11-21 · P1 (error) / P2 (detección y corrección)
- **Detectado por:** la propia IA al diseñar los choques de P2 (autorrevisión), no por Max.
- **Prompt:**
  > "si a todo, arranca" (P1: estructura, `config.py` con todos los valores comentados y la validación del tunneling).
- **Qué generó la IA:** en `server/config.py`, la validación usaba solo la velocidad de la embestida como máxima posible:
  ```python
  # Se usa la velocidad más alta posible (embestida a carga completa).
  max_step = max(self.MAX_SPEED, self.DASH_MAX_SPEED) * self.TICK_DT
  if max_step >= self.DISC_RADIUS: ...
  ```
- **Problema (categoría: física / validación de configuración):** un disco en embestida tiene masa ×2 (`DASH_MASS_FACTOR`). En un choque elástico 1D contra un disco quieto de masa 1, este sale a `2·m₁/(m₁+m₂)·v = 4/3 · 600 = 800 px/s`, o sea **26,7 px por tick, más que el radio de 24 px**. La validación pasaba, pero la garantía que prometía era falsa.
- **Corrección:** un tope global de velocidad aplicado antes de integrar la posición (`server/domain/physics.py`):
  ```diff
   for disc in discs:
       ...
       self._update_charge(disc, dt)
  +    self._cap_speed(disc)   # nunca más rápido que DASH_MAX_SPEED
       disc.pos = disc.pos + disc.vel * dt
  ```
  Y el comentario de `DASH_MAX_SPEED` en `config.py` ahora dice que es el tope global.
- **Por qué es un error:** la validación comprobaba el caso que se le ocurrió a quien la escribió, no el peor caso real. Una configuración "validada" podía producir desplazamientos mayores que el radio, y con eso la detección de choques por solapamiento deja de ser confiable.
- **Cómo se verificó:** `tests/test_physics.py::test_tope_global_tras_un_choque` pone un disco a 4/3 de `DASH_MAX_SPEED` y comprueba que avanza como máximo `DASH_MAX_SPEED·dt` (< radio). Al quitar `_cap_speed` a propósito, esa prueba falla.

### 2. Una prueba de tunneling que no detectaba el tunneling

- **Fecha y paso:** 2026-11-21 · P2
- **Detectado por:** la IA, al verificar con mutación (quitar el tope a propósito y ver qué prueba falla).
- **Prompt:**
  > "crea el venv e instala pytest, [...] y sigamos con p2"
- **Qué generó la IA:** dos versiones malas.
  1. Primera versión, corregida antes de ejecutarla: guardaba posiciones que nunca usaba y afirmaba `vel <= DASH_MAX_SPEED` **después** del paso, justo cuando el choque deja la velocidad por encima del tope (se recorta en el paso siguiente, antes de mover):
     ```python
     before = [d.pos for d in discs]
     physics.step(discs, DT)
     for d, p in zip(discs, before):
         assert d.vel.length() <= CFG.DASH_MAX_SPEED + 1e-9
     ```
  2. Segunda versión, `test_ningun_disco_atraviesa_a_otro`: una fila de discos y el orden en x no debe cambiar. **Pasa también sin el tope**, así que no protege lo que dice proteger.
- **Problema (categoría: pruebas / física):** para que un disco atraviese a otro, el avance relativo por tick debe superar ~2 diámetros (el disco tiene que saltarse toda la zona de solapamiento), no un radio. Con 800 px/s no se llega. La regla "avance < radio" del diseño es conservadora, y la prueba no llega a la zona de peligro.
- **Corrección:** la garantía la protege una prueba directa del tope (`test_tope_global_tras_un_choque`, ver entrada 1). `test_ningun_disco_atraviesa_a_otro` se conserva solo como regresión de escenario, y su debilidad queda documentada aquí.
- **Por qué es un error:** una prueba verde que no falla cuando se rompe lo que protege da falsa confianza. Viola el propósito de §7 de `AGENTS.md` ("qué demuestra cada una").
- **Cómo se verificó:** con mutación. Con `_cap_speed` reemplazado por `pass`, solo falla `test_tope_global_tras_un_choque`; `test_ningun_disco_atraviesa_a_otro` sigue en verde.

### 3. El tope de la cabecera HTTP se saltaba si llegaba completa en un solo `read()`

- **Fecha y paso:** 2026-11-21 · P3
- **Detectado por:** la IA. El defecto del código lo vio al releerlo; el de la prueba, con mutación.
- **Prompt:**
  > "Todo lo que consideres que valga la pena que vaya en la bitacora ponlo en la bitacora, los test pasaron vamos con p3"
- **Qué generó la IA:** primera versión de `read_head` en `server/adapters/http_handshake.py`:
  ```python
  while True:
      end = buf.find(_HEAD_END)
      if end != -1:
          return bytes(buf[:end]), bytes(buf[end + 4:])   # ← sin mirar el tamaño
      if len(buf) > max_bytes:
          raise HttpError(HTTPStatus.REQUEST_HEADER_FIELDS_TOO_LARGE)
      data = await reader.read(chunk)
  ```
  Y una prueba que leía de a 1 byte:
  ```python
  asyncio.run(feed_in_chunks(huge, len(huge)))   # "todo en un solo read"... pero read_head usaba chunk=1
  ```
- **Problema (categoría: sockets):** el tope solo se revisaba cuando *todavía no* aparecía `\r\n\r\n`. Si un `read()` traía de una vez una cabecera de 12 KB completa, se aceptaba aunque el límite fuera de 8 KB. Además, la prueba decía "todo en un solo read", pero el servidor leía de a 1 byte. Por eso el búfer pasaba el tope antes de ver el fin, y la prueba nunca entraba al camino defectuoso.
- **Corrección:**
  ```diff
   end = buf.find(_HEAD_END)
  +if (end if end != -1 else len(buf)) > max_bytes:
  +    raise HttpError(HTTPStatus.REQUEST_HEADER_FIELDS_TOO_LARGE)
   if end != -1:
       return bytes(buf[:end]), bytes(buf[end + len(_HEAD_END):])
  -if len(buf) > max_bytes:
  -    raise HttpError(HTTPStatus.REQUEST_HEADER_FIELDS_TOO_LARGE)
  ```
  La prueba ahora se parametriza con `read_chunk` en `[1, len(HUGE)]`, uno por cada camino.
- **Por qué es un error:** es la misma falsa suposición que castiga la rúbrica (criterio 2), pero al revés: el código suponía cómo iban a llegar los bytes. TCP no garantiza nada sobre el tamaño de cada `read()`, así que un límite de seguridad tiene que valer para cualquier partición del flujo.
- **Cómo se verificó:** con mutación. Volví a poner la condición vieja (`if end == -1 and len(buf) > max_bytes`). Con la prueba vieja, todo seguía en verde. Con la nueva, falla `test_cabecera_demasiado_grande_431[8225]`, el caso de un solo `read()`.

### 4. `start_server(sock=...)` pisaba el `listen(128)` hecho a mano

- **Fecha y paso:** 2026-11-21 · P4
- **Detectado por:** la IA, al revisar `ss -tlnp` con el servidor real corriendo.
- **Prompt:**
  > "Confirmo" (arrancar P4: listener, sesiones y tick).
- **Qué generó la IA:** en `server/adapters/listener.py`:
  ```python
  sock.listen(backlog)          # en create_listen_socket, con LISTEN_BACKLOG = 128
  ...
  server = await asyncio.start_server(self._on_connect, sock=self._sock)
  ```
- **Problema (categoría: sockets):** `start_server` (o sea `loop.create_server`) tiene su propio parámetro `backlog`, que por defecto vale 100, y **vuelve a llamar a `sock.listen(100)`** sobre el socket que le entregamos. El `listen(128)` manual quedaba sobrescrito sin ningún error ni aviso.
  ```
  $ ss -tlnp | grep 8080
  LISTEN 0      100         127.0.0.1:8080  ...     ← Send-Q de un socket LISTEN = backlog
  ```
- **Corrección:**
  ```diff
  -server = await asyncio.start_server(self._on_connect, sock=self._sock)
  +server = await asyncio.start_server(self._on_connect, sock=self._sock,
  +                                    backlog=self._cfg.LISTEN_BACKLOG)
  ```
- **Por qué es un error:** la cola de aceptación (conexiones que ya completaron el saludo TCP y esperan su `accept()`) era más chica de lo configurado y justificado. En una ráfaga de 100+ bots conectando a la vez, el kernel descartaría SYN antes de lo previsto. Peor aún, el valor de `config.py` mentía: decía 128 y el kernel usaba 100. Es la clase de detalle que esconde un framework, y justo la razón por la que el proyecto crea el socket a mano.
- **Cómo se verificó:** con `ss -tlnp` antes (Send-Q = 100) y después (Send-Q = 128), con el servidor real (`.venv/bin/python -m server.main`).

### 5. Se presentaron como existentes unos timeouts que solo eran constantes en `config.py`

- **Fecha y paso:** 2026-11-22 · verificación de P1–P4 (corregido en P6)
- **Detectado por:** la IA, al comprobarlo con clientes silenciosos antes de dárselo a Max como "funciona".
- **Prompt:**
  > "quiero que me indiques todo lo que tengo que hacer yo para probar lo que llevamos, y que es lo que tengo que esperar que suceda en cada paso"
- **Qué generó la IA:** en la guía de pruebas afirmó que el servidor cortaba al cliente que no habla (saludo de 5 s, ping cada 10 s, inactividad de 30 s). En `server/config.py` existían `HANDSHAKE_TIMEOUT_S`, `PING_INTERVAL_S` e `IDLE_TIMEOUT_S`, pero **ningún código los usaba**: solo `WRITE_TIMEOUT_S` se leía (en `Listener._close`). `ClientSession._on_pong` decía `return True   # P6`.
- **Problema (categoría: sockets):** una constante de configuración no es un comportamiento. Un cliente que abre el TCP y calla ocupaba un descriptor y un cupo del semáforo para siempre (ataque slowloris). Se comprobó abriendo conexiones mudas: seguían `ESTAB` pasados 100 s.
- **Corrección (P6):**
  ```python
  # server/adapters/listener.py: plazo TOTAL del saludo HTTP
  return await asyncio.wait_for(self._handshake.handle(reader, writer), self._cfg.HANDSHAKE_TIMEOUT_S)
  # server/adapters/client_session.py: plazo total antes del hello, inactividad después
  data = await asyncio.wait_for(self._reader.read(n), self._read_timeout())
  ```
  más un ping cada `PING_INTERVAL_S` que deja el frame en una casilla (el writer sigue siendo el único que escribe) y `drain()` con `WRITE_TIMEOUT_S`.
- **Por qué es un error:** se le dijo a Max que algo estaba hecho sin haberlo comprobado. Es el mismo defecto que la bitácora busca (afirmar un comportamiento de red que no existe), y justo el que la rúbrica castiga: sin plazos, un cliente roto se queda con el recurso para siempre.
- **Cómo se verificó:** `tests/test_server_e2e.py`: `test_saludo_http_que_no_llega_se_cierra_por_timeout`, `test_slowloris_un_byte_cada_tanto_no_renueva_el_plazo`, `test_websocket_sin_hello_a_tiempo_error_timeout_y_1008`, `test_cliente_que_no_responde_los_pings_se_cierra_por_inactividad` y `test_cliente_que_responde_pong_sigue_conectado_y_se_mide_el_rtt`; `tests/test_client_session_write.py` para el plazo de escritura.

### 6. Identificar el proceso del servidor por su nombre mezclaba procesos distintos

- **Fecha y paso:** 2026-11-22 · P4 (comando de prueba) y P7 (`tools/monitor.py`)
- **Detectado por:** Max (primer caso) y la IA (segundo caso).
- **Prompt:**
  > Max: "ls /proc/$PID/fd me dio 55" (al contar los descriptores del servidor con el `PID` que la IA le indicó obtener con `pgrep -f "server.main"`).
- **Qué generó la IA:**
  ```bash
  PID=$(pgrep -f "server.main")      # comando que se le dio a Max
  ```
  y después, en `tools/monitor.py`, una `find_server_pid()` que devolvía el primer proceso cuyo `cmdline` contenía `server.main`.
- **Problema (categoría: sockets):** `pgrep -f` devolvió dos PIDs (el servidor y un proceso del servidor de VS Code que también contenía esa cadena), así que el 55 no era del servidor. En el monitor, con dos servidores corriendo (8080 y 8081), mostraba los **descriptores y la memoria del 8080 junto a las métricas del 8081**: números plausibles y falsos.
- **Corrección:**
  ```diff
  -if b"server.main" in args: return int(entry)
  +# dueño del socket en LISTEN: /proc/net/tcp da el inode, /proc/<pid>/fd lo enlaza al proceso
  +if os.readlink(f"/proc/{entry}/fd/{fd}") == f"socket:[{inode}]": return int(entry)
  ```
  Para Max se corrigió el comando a `pgrep -f "python -m server.main"`, y `tools/leakcheck.py` compara el `argv` exacto.
- **Por qué es un error:** el nombre de un comando no identifica a quién escucha en un puerto; lo que sí lo identifica es el socket. Una herramienta de medición que mezcla fuentes es peor que no tener herramienta, porque inspira confianza.
- **Cómo se verificó:** con el servidor real en el 8080 y uno de pruebas en el 8081, el monitor con `--port 8081` mostró el PID del 8081 (distinto del 8080). Con un puerto sin servidor termina con "Nadie escucha en el puerto 8099".

### 7. `scenario_check` daba falsos FAIL por suponer que llegaba un estado por tick y que la posición era exacta

- **Fecha y paso:** 2026-11-22 · P4/P5 (herramientas de verificación)
- **Detectado por:** la IA, al ver resultados imposibles en su propia herramienta.
- **Prompt:**
  > "Pues las pruebas pasaron, asi que comitea y sigamos" (tras pedir una verificación automática en vez de leer logs a ojo).
- **Qué generó la IA:** primero `peak_speed` dividía la distancia entre dos estados por **un** `dt`, y luego comparaba contra `MAX_SPEED` sin tolerancia:
  ```python
  speed = hypot(x1 - x0, y1 - y0) / dt        # asume que estados consecutivos son ticks consecutivos
  assert speed <= cfg.MAX_SPEED
  ```
- **Problema (categoría: protocolo):** el protocolo dice que el buzón de tamaño 1 **reemplaza** estados (`docs/protocolo.md` §4), así que el cliente puede ver saltos de varios ticks. Resultado: "pico 1584 px/s" con `MAX_SPEED = 170`. Segundo falso FAIL: "171 vs 170", porque las posiciones viajan redondeadas a `POSITION_DECIMALS` = 0,1 px y eso mete ±3 px/s a 30 Hz.
- **Corrección:**
  ```diff
  -speed = hypot(dx, dy) / dt
  +speed = hypot(dx, dy) / ((tick1 - tick0) * dt)      # ticks reales entre estados
  +assert speed <= cfg.MAX_SPEED + rounding_tolerance(cfg)   # 10**-POSITION_DECIMALS / TICK_DT
  ```
- **Por qué es un error:** otra vez se asumió cómo llegan los datos (un mensaje por tick), la misma falsa suposición del criterio 2 de la rúbrica, pero dentro de la herramienta de pruebas. Un falso FAIL hace perder confianza en la herramienta y esconde los fallos verdaderos.
- **Cómo se verificó:** la velocidad real se calculó directo sobre el dominio (170,000000 px/s) y `scenario_check` volvió a dar todo PASS (11 PASS, 0 FAIL).

### 8. Una falsa alarma de "el servidor no se recupera" en la prueba de EMFILE: el logging síncrono bloqueó el event loop

- **Fecha y paso:** 2026-11-22 · P6 (prueba con `ulimit -n 30`)
- **Detectado por:** la IA, depurando su propia prueba.
- **Prompt:**
  > "una vez verifiques que todo funciona ve con p6" (incluye comprobar que `accept` resiste EMFILE).
- **Qué generó la IA:** un script de prueba que lanzaba el servidor con `stdout=subprocess.PIPE` y solo leía la salida al final.
- **Problema (categoría: concurrencia):** bajo EMFILE, asyncio escribe un error con traceback por cada intento de `accept` (~2000 líneas en pocos segundos). El pipe (64 KB) se llenó y el `write()` **síncrono** del módulo `logging` bloqueó el hilo del event loop: nada avanzaba (CPU 0,08 s, 23 conexiones sin atender, el tick congelado) hasta que el script leyó. Se interpretó primero como un fallo del servidor ("no se recupera en 30 s").
- **Corrección:** en la prueba, enviar la salida a un archivo. Con eso: 80 conexiones con solo 30 descriptores, el servidor sigue vivo y vuelve a contestar `/metrics` a los 2,5 s de soltarlas.
- **Por qué es un error:** el error fue de la prueba, pero revela un riesgo real del servidor que viola la regla "nada bloqueante en el event loop" (AGENTS.md §4.8): `logging` con un destino lento (terminal, pipe, disco) frena el tick y a todos los clientes. El diseño actual no lo evita; la mitigación estándar es un `QueueHandler` con un hilo aparte, y queda como riesgo documentado.
- **Cómo se verificó:** `/proc/<pid>/fd` y `ss -tan` durante la prueba; `faulthandler` y la lista de tareas mostraron las conexiones sin atender; con la salida a un archivo el tiempo de recuperación fue de 2,5 s y la salida del proceso 0.
