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
