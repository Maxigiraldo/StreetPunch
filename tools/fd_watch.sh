#!/usr/bin/env bash
# Vigila los descriptores y las conexiones del servidor, una línea por segundo.
#
# Uso:  tools/fd_watch.sh [puerto]        (por defecto 8080; Ctrl+C para terminar)
#
# Qué muestra:
#   fd          descriptores abiertos por el proceso (ls /proc/<pid>/fd).
#   limite      el tope del proceso (ulimit -n): si fd se le acerca, el servidor
#               deja de poder aceptar conexiones (EMFILE).
#   ESTAB       conexiones establecidas del lado del servidor.
#   CLOSE_WAIT  el cliente cerró y el servidor NO: si no baja a 0, hay una fuga.
#   TIME_WAIT   normal tras cerrar muchas conexiones; se va solo en ~60 s.
# Lo que se espera: tras una carga, fd vuelve a su valor base y CLOSE_WAIT a 0.
set -u
PORT="${1:-8080}"

# El proceso dueño del socket en LISTEN (no se busca por nombre: ver bitácora, entrada 6).
PID="$(ss -ltnpH "sport = :${PORT}" 2>/dev/null | grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2)"
if [ -z "$PID" ]; then
  echo "Nadie escucha en el puerto ${PORT}: ¿está corriendo el servidor?" >&2
  exit 1
fi
LIMIT="$(awk '/Max open files/ {print $4}' "/proc/${PID}/limits")"
echo "Servidor pid ${PID}, puerto ${PORT}, límite de descriptores ${LIMIT}"
printf '%-8s %5s %7s %6s %11s %10s\n' hora fd limite ESTAB CLOSE_WAIT TIME_WAIT

while kill -0 "$PID" 2>/dev/null; do
  FDS="$(ls "/proc/${PID}/fd" 2>/dev/null | wc -l)"
  # Solo el lado del servidor: puerto LOCAL = el del juego.
  STATES="$(ss -tanH "sport = :${PORT}")"
  ESTAB="$(grep -c '^ESTAB' <<<"$STATES")"
  CW="$(grep -c '^CLOSE-WAIT' <<<"$STATES")"
  TW="$(grep -c '^TIME-WAIT' <<<"$STATES")"
  printf '%-8s %5s %7s %6s %11s %10s\n' "$(date +%H:%M:%S)" "$FDS" "$LIMIT" "$ESTAB" "$CW" "$TW"
  sleep 1
done
echo "El servidor (pid ${PID}) terminó."
