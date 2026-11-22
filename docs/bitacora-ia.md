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
