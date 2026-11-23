"""Configuración de StreetPunch: el único lugar con valores numéricos.

Cada valor lleva su porqué. Para cambiar uno no se toca código, pero sí hay que
anotar la razón del cambio (ver tabla "Valores configurables" en docs/diseno.md).

Es una dataclass congelada (frozen): nadie puede modificarla en caliente. Así,
aunque varias tareas la lean a la vez, no hay estado compartido mutable.
`main.py` crea una instancia y se la pasa por constructor a quien la necesite
(inversión de dependencias): ningún módulo la importa como variable global.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    # ------------------------------------------------------------------
    # Red y socket
    # ------------------------------------------------------------------

    # Solo localhost para el plan A: la demo no depende de la red de nadie.
    # El plan B (Railway) usará "0.0.0.0" y el PORT del entorno (paso P9).
    HOST: str = "127.0.0.1"

    # Puerto sin privilegios (>1024): no requiere root.
    PORT: int = 8080

    # Tamaño pedido para la cola de aceptación de listen(). Ahí esperan las
    # conexiones que ya completaron el saludo TCP (SYN, SYN-ACK, ACK) hasta
    # que el servidor llama a accept(). 128 aguanta 100 bots conectando a la
    # vez. El kernel lo recorta a net.core.somaxconn solo si lo supera; en
    # este equipo somaxconn = 4096 (medido en P0), así que se usa 128 tal cual.
    LISTEN_BACKLOG: int = 128

    # Tope propio de conexiones simultáneas (asyncio.Semaphore). Caben ~13 salas
    # de 8 jugadores (MAX_ROOM_PLAYERS) con margen. El límite del sistema operativo
    # (ulimit -n) es otro tema: este tope existe para que la memoria y el
    # ancho de banda sigan acotados.
    MAX_CONNECTIONS: int = 100

    # Búfer de envío del kernel por conexión (SO_SNDBUF). Linux lo AUTOAJUSTA
    # hasta ~2,6 MB (medido con `ss -tm`: tb=2626560): un cliente que no lee
    # tardaría más de 10 minutos en llenarlo y recién entonces actuaría nuestro
    # backpressure (buzón de 1 + WRITE_TIMEOUT_S); con 100 clientes serían hasta
    # ~260 MB de memoria del kernel retenidos por conexiones zombi. Con 64 KB
    # (Linux lo duplica) caben ~3 s de estado de un jugador (~20 KB/s con 8-12
    # discos), de sobra para absorber un tropiezo de la red, y un cliente
    # que no lee se detecta en decenas de segundos y no en minutos. Si el
    # juego envía más datos por segundo, subirlo.
    SEND_BUFFER_BYTES: int = 65536

    # Con el servidor lleno aún se contesta el saludo para decir "full" (1013),
    # pero cada contestación ocupa un descriptor hasta HANDSHAKE_TIMEOUT_S. Sin
    # tope, una avalancha de conexiones podría gastar todos los descriptores
    # solo en rechazar. Pasado este número, se cierra directo sin explicar.
    MAX_REJECTING_HANDSHAKES: int = 16

    # Tamaño máximo de una trama WebSocket. Los mensajes del juego pesan
    # ~50-300 B. Se revisa ANTES de reservar memoria: si no, un cliente que
    # anuncie 2^63 bytes nos haría intentar reservar exabytes.
    MAX_FRAME_BYTES: int = 4096

    # Tope de la cabecera HTTP del saludo. Un navegador manda ~0,5-2 KB
    # (más si hay cookies de localhost); 8 KB es el valor por defecto de
    # servidores como nginx. Sin tope, un cliente podría mandar cabeceras
    # infinitas y llenarnos la memoria sin terminar nunca el saludo.
    MAX_HTTP_HEADER_BYTES: int = 8192

    # Cuántos bytes pedir por cada read() del socket. Es un MÁXIMO, no una
    # promesa: TCP puede entregar menos (media trama) o varias tramas
    # juntas. 4 KB cubre una trama máxima de una sola vez casi siempre.
    READ_CHUNK_BYTES: int = 4096

    # ------------------------------------------------------------------
    # Tick y difusión
    # ------------------------------------------------------------------

    # Frecuencia de simulación y de envío a jugadores. A 30 Hz son 33 ms por
    # tick: fluido con interpolación de ~100 ms en el cliente.
    TICK_HZ: int = 30

    # Frecuencia del estado mientras la sala NO está jugando (espera, cuenta
    # atrás, resultados): nada se mueve, así que 10 Hz ahorra 2/3 del tráfico.
    # Debe dividir a TICK_HZ (se valida abajo) porque EveryNthTick envía uno
    # de cada N ticks.
    QUIET_PHASE_HZ: int = 10

    # Meta medida del percentil 99 del tick: 5 ms es el 15 % de los 33 ms
    # disponibles. Es una meta para las métricas, no una regla que se imponga.
    TICK_P99_TARGET_MS: float = 5.0

    # ------------------------------------------------------------------
    # Backpressure y límites por cliente
    # ------------------------------------------------------------------

    # Estados que caben en el buzón de salida de cada cliente. Con 1, el
    # estado nuevo reemplaza al viejo: a un cliente lento se le saltan
    # estados en vez de acumularle memoria. Subir a 2 solo si hay tirones
    # (cada estado extra suma 33 ms de retraso).
    MAILBOX_SIZE: int = 1

    # Capacidad de la cola de entradas compartida (lectores -> tick). Al
    # estar acotada, la memoria no crece sin límite; si se llena, se
    # descarta la entrada y se cuenta (put_nowait, nunca await put()).
    INPUT_QUEUE_SIZE: int = 1024

    # Entradas que el tick acepta de un mismo cliente por tick. Un humano no
    # cambia de dirección más de 4 veces en 33 ms; lo que pase de ahí es
    # spam y se descarta.
    MAX_INPUTS_PER_CLIENT_PER_TICK: int = 4

    # Longitud máxima del nombre. Cabe sobre el disco en el canvas.
    MAX_NAME_LEN: int = 16

    # Decimales al codificar el estado. Una décima de píxel es invisible y
    # cada dígito de más son bytes x discos x 30 Hz x clientes.
    POSITION_DECIMALS: int = 1
    CHARGE_DECIMALS: int = 2

    # Cuántas duraciones de tick se guardan para calcular p50/p99: 300 ticks
    # son los últimos 10 s a 30 Hz, una ventana estable pero reciente.
    METRICS_TICK_WINDOW: int = 300

    # Un error idéntico y repetitivo (p. ej. "sin descriptores" en el accept) se
    # registra como mucho una vez cada tantos segundos. 5 s deja ver que sigue
    # pasando sin inundar el log.
    LOG_REPEAT_INTERVAL_S: float = 5.0

    # ------------------------------------------------------------------
    # Timeouts y apagado
    # ------------------------------------------------------------------

    # Un navegador completa el saludo HTTP y envía "hello" en milisegundos.
    # Tardar más de 5 s indica un ataque slowloris o un cliente roto. El plazo
    # se aplica dos veces: 5 s para el saludo HTTP y 5 s más para el hello.
    # Es un plazo TOTAL (no se renueva con cada byte), para que mandar un byte
    # cada 4 s no mantenga la conexión viva para siempre.
    HANDSHAKE_TIMEOUT_S: float = 5.0

    # TCP puede tardar horas en notar que el otro extremo murió (keepalive
    # por defecto ~2 h). Por eso mandamos un ping cada 10 s y cerramos si
    # pasan 30 s sin recibir nada (3 pings perdidos).
    PING_INTERVAL_S: float = 10.0
    IDLE_TIMEOUT_S: float = 30.0

    # Si drain() no termina en 10 s, el cliente no está leyendo (zombi): se
    # cierra para liberar su cupo y su descriptor.
    WRITE_TIMEOUT_S: float = 10.0

    # Tiempo que se espera tras enviar close 1001 en SIGTERM antes de cerrar
    # los sockets a la fuerza.
    SHUTDOWN_GRACE_S: float = 2.0

    # ------------------------------------------------------------------
    # Reglas de la ronda
    # ------------------------------------------------------------------

    # Tope de jugadores POR SALA. Todos los de la sala juegan cada ronda: no hay
    # cola ni espectadores (con una sola arena y cola, la gente esperaba "volando"
    # y la rotación se sentía injusta). Con la sala llena no se puede entrar: se
    # elige otra o se crea una. 8 discos caben holgados en la arena (300 px de
    # radio) y el tráfico de cada sala crece como N x N.
    MAX_ROOM_PLAYERS: int = 8

    # Tope de salas a la vez: acota la memoria y el trabajo del tick (cada sala
    # corre su propia física). 25 salas de 4 ya usan las 100 conexiones; es un
    # límite de seguridad, no uno que se alcance jugando.
    MAX_ROOMS: int = 25

    # Con uno solo no hay partida.
    MIN_PLAYERS_TO_START: int = 2


    # Duración de la cuenta regresiva y de la pantalla de resultados.
    COUNTDOWN_S: float = 3.0
    RESULTS_S: float = 3.0

    # Embestida: se carga hasta 1 s y luego queda en enfriamiento 1,5 s.
    # Valores de jugabilidad; se ajustan jugando (P5).
    CHARGE_MAX_S: float = 1.0
    # Carga mínima para que soltar lance la embestida. Un toque de 0,1 s daba
    # 600 x 0,1 = 60 px/s: MÁS LENTO que caminar (170) y encima gastaba el
    # enfriamiento de 1,5 s (visto jugando: "no deja cargar mientras me
    # muevo"). Por debajo de esto soltar se ignora, sin enfriamiento. 0,2 s
    # = 120 px/s, aún suave, pero ya no es un toque accidental; 0 lo desactiva.
    MIN_CHARGE_S: float = 0.2
    DASH_COOLDOWN_S: float = 1.5

    # ------------------------------------------------------------------
    # Física (PROVISIONAL: se fija el domingo en P2 y se ajusta jugando en P5)
    # Unidades: píxeles del mundo y segundos.
    # ------------------------------------------------------------------

    # Radio de la arena circular. Con un canvas de ~700 px deja margen.
    ARENA_RADIUS: float = 300.0

    # Radio de cada disco. 8 discos (el tope de una sala) ocupan ~5 % del área de
    # la arena: hay espacio para moverse.
    DISC_RADIUS: float = 24.0

    # Margen de caída: un disco solo se elimina cuando su CENTRO pasa del borde
    # por esta cantidad de radios de disco. Con 0 se eliminaba al cruzar el
    # borde con la mitad del disco aún sobre la arena y se sentía injusto
    # ("se pierde y todavía hay mucho del personaje dentro"). Con 1.0 el disco
    # ya está ENTERO fuera del borde cuando cae: se ve la caída. Más de ~2
    # haría que alguien colgando casi sin apoyo siga "vivo" demasiado tiempo.
    ELIMINATION_MARGIN_DISCS: float = 1.0

    # Aceleración al mantener una dirección. Con MAX_SPEED de 170 se llega
    # a la velocidad máxima en ~0,2 s: la respuesta se siente inmediata.
    # Se bajó de 1200 a 850 junto con MAX_SPEED para conservar esos 0,2 s.
    ACCELERATION: float = 850.0

    # Velocidad máxima caminando (sin embestida). Se bajó de 240 a 170 (P5,
    # tras jugar): caminar es solo posicionarse y la embestida es lo que
    # decide la partida, así que no conviene que sea fácil perseguir al rival.
    MAX_SPEED: float = 170.0

    # Fricción al CAMINAR: fracción de velocidad que se pierde por segundo
    # (decaimiento exponencial). Con 4, sin tocar teclas el disco se detiene
    # en ~1 s: se controla con precisión.
    FRICTION_PER_S: float = 4.0

    # Fricción cuando el disco va MÁS RÁPIDO que MAX_SPEED (embestida o golpe
    # recibido). Es menor para que los impulsos lleguen lejos sin que caminar
    # se sienta como hielo. Medido con Physics (embestida completa): con 4
    # (igual que caminar) la embestida alcanzaba ~140 px y un rival golpeado
    # se deslizaba ~115 px; con 2 alcanza ~250 px y el rival se desliza
    # ~270 px, y caminar sigue deteniéndose en ~0,9 s. Debe ser <=
    # FRICTION_PER_S (se valida abajo).
    SLIDE_FRICTION_PER_S: float = 2.0

    # Velocidad de la embestida con carga completa; es proporcional a la
    # carga. También es el TOPE GLOBAL de velocidad: un choque elástico contra
    # un disco en embestida (masa x2) puede lanzar al otro hasta a 4/3 de esta
    # velocidad. Sin tope, eso rompería la garantía contra el tunneling.
    # Physics recorta toda velocidad a este valor antes de mover.
    DASH_MAX_SPEED: float = 600.0

    # "Un disco en embestida empuja más" (RF-09): durante la embestida su
    # masa efectiva se multiplica por este factor. Así el choque sigue
    # siendo elástico y conserva el momento: no hace falta una regla
    # especial que lo rompa.
    DASH_MASS_FACTOR: float = 2.0

    # Masa normal de un disco. Es la unidad: lo que importa es la razón con
    # DASH_MASS_FACTOR, no el valor absoluto.
    DISC_MASS: float = 1.0

    # Los discos aparecen repartidos en un círculo al 60 % del radio de la
    # arena: lejos del borde (nadie cae al empezar) y lejos del centro (no
    # nacen encimados). Con 8 discos quedan separados ~140 px entre centros
    # (2 x 0,6 x 300 x sen(22,5°)), casi el triple del diámetro.
    SPAWN_RING_FRACTION: float = 0.6

    # Colores de los discos: 12, elegidos para distinguirse sobre fondo oscuro. El
    # color sale de `id % 12` (ver world.color_for): es el mismo en el vestíbulo y
    # en cualquier sala, pero dos jugadores cuyos ids difieran en múltiplo de 12
    # comparten color (límite conocido; el nombre sobre el disco los distingue).
    PLAYER_COLORS: tuple[str, ...] = (
        "#e4572e", "#29b6f6", "#ffc914", "#76b041", "#c060a1", "#17bebb",
        "#ff8c42", "#8e7dbe", "#f25f5c", "#2ec4b6", "#d4e157", "#f7a1c4",
    )

    # ------------------------------------------------------------------
    # Valores derivados (no se configuran: salen de los de arriba)
    # ------------------------------------------------------------------

    @property
    def TICK_DT(self) -> float:
        """Duración de un tick en segundos (paso fijo de la simulación)."""
        return 1.0 / self.TICK_HZ

    @property
    def QUIET_EVERY_N_TICKS(self) -> int:
        """Cada cuántos ticks se envía el estado de una sala que no juega (EveryNthTick)."""
        return self.TICK_HZ // self.QUIET_PHASE_HZ

    # ------------------------------------------------------------------
    # Validación: una configuración incoherente debe fallar al arrancar,
    # no a mitad de una partida.
    # ------------------------------------------------------------------

    def __post_init__(self) -> None:
        errors = []

        # Tunneling: la física detecta choques comparando posiciones tick a
        # tick. Si un disco avanza en un tick más que su radio, puede pasar
        # "a través" de otro disco sin que nunca se detecte el solapamiento.
        # Se usa la velocidad más alta posible: DASH_MAX_SPEED, que Physics
        # impone como tope global incluso después de un choque.
        max_step = max(self.MAX_SPEED, self.DASH_MAX_SPEED) * self.TICK_DT
        if max_step >= self.DISC_RADIUS:
            errors.append(
                f"tunneling: desplazamiento máximo por tick {max_step:.1f} px "
                f">= radio del disco {self.DISC_RADIUS} px; baja DASH_MAX_SPEED "
                f"o sube TICK_HZ"
            )

        # Si deslizar frenara MÁS que caminar, un golpe duraría menos que un
        # paso: lo contrario de lo que se busca.
        if self.SLIDE_FRICTION_PER_S > self.FRICTION_PER_S:
            errors.append("SLIDE_FRICTION_PER_S no puede superar FRICTION_PER_S")

        if self.TICK_HZ % self.QUIET_PHASE_HZ != 0:
            errors.append("QUIET_PHASE_HZ debe dividir exactamente a TICK_HZ")

        if not 2 <= self.MIN_PLAYERS_TO_START <= self.MAX_ROOM_PLAYERS:
            errors.append("MIN_PLAYERS_TO_START debe estar entre 2 y MAX_ROOM_PLAYERS")

        if self.MAX_ROOM_PLAYERS > self.MAX_CONNECTIONS:
            errors.append("MAX_ROOM_PLAYERS no puede superar MAX_CONNECTIONS")

        if self.MAX_ROOMS < 1:
            errors.append("MAX_ROOMS debe ser al menos 1")

        # Con inactividad <= intervalo de ping, un cliente sano se cerraría
        # antes de tener oportunidad de responder.
        if self.IDLE_TIMEOUT_S <= self.PING_INTERVAL_S:
            errors.append("IDLE_TIMEOUT_S debe ser mayor que PING_INTERVAL_S")

        # El disco tiene que caber holgado dentro de la arena.
        if self.DISC_RADIUS * 4 > self.ARENA_RADIUS:
            errors.append("DISC_RADIUS es demasiado grande para ARENA_RADIUS")

        if not 0 <= self.MIN_CHARGE_S < self.CHARGE_MAX_S:
            errors.append("MIN_CHARGE_S debe estar entre 0 y CHARGE_MAX_S")

        if self.ELIMINATION_MARGIN_DISCS < 0:
            errors.append("ELIMINATION_MARGIN_DISCS no puede ser negativo")

        if self.MAILBOX_SIZE < 1 or self.INPUT_QUEUE_SIZE < 1:
            errors.append("MAILBOX_SIZE e INPUT_QUEUE_SIZE deben ser >= 1")

        if errors:
            raise ValueError("Config inválida:\n  - " + "\n  - ".join(errors))
