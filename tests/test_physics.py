"""Física: movimiento, embestida, choques y la garantía contra el tunneling."""

import itertools
import math

import pytest

from server.config import Config
from server.domain.arena import Arena
from server.domain.disc import Disc
from server.domain.physics import Physics
from server.domain.vector import Vec2

CFG = Config()
DT = CFG.TICK_DT


@pytest.fixture
def physics() -> Physics:
    return Physics(CFG)


def run(physics: Physics, discs: list[Disc], ticks: int) -> None:
    for _ in range(ticks):
        physics.step(discs, DT)


def momentum(physics: Physics, discs: list[Disc]) -> Vec2:
    total = Vec2()
    for d in discs:
        total = total + d.vel * physics.mass(d)
    return total


def kinetic(physics: Physics, discs: list[Disc]) -> float:
    return sum(0.5 * physics.mass(d) * d.vel.length() ** 2 for d in discs)


# ---------------- movimiento ----------------

def test_caminar_llega_a_la_velocidad_maxima_sin_pasarla(physics):
    d = Disc(1, Vec2())
    d.set_move(Vec2(1, 0))
    run(physics, [d], CFG.TICK_HZ)  # 1 s
    assert d.vel.length() == pytest.approx(CFG.MAX_SPEED)


def test_diagonal_no_es_mas_rapida(physics):
    # dx=1, dy=1 sin normalizar daría sqrt(2) veces más aceleración.
    d = Disc(1, Vec2())
    d.set_move(Vec2(1, 1))
    run(physics, [d], CFG.TICK_HZ)
    assert d.vel.length() == pytest.approx(CFG.MAX_SPEED)


def test_friccion_detiene_el_disco(physics):
    d = Disc(1, Vec2(), vel=Vec2(CFG.MAX_SPEED, 0))
    run(physics, [d], 2 * CFG.TICK_HZ)
    assert d.vel.length() < 1.0


def test_por_encima_de_max_speed_frena_con_la_friccion_de_deslizar(physics):
    """Una embestida o un golpe (velocidad > MAX_SPEED) pierde velocidad con
    SLIDE_FRICTION_PER_S, no con la de caminar: por eso llega más lejos."""
    d = Disc(1, Vec2(), vel=Vec2(CFG.DASH_MAX_SPEED, 0))
    physics.step([d], DT)
    assert d.vel.length() == pytest.approx(
        CFG.DASH_MAX_SPEED * math.exp(-CFG.SLIDE_FRICTION_PER_S * DT))


def test_a_velocidad_de_caminar_frena_con_la_friccion_normal(physics):
    d = Disc(1, Vec2(), vel=Vec2(CFG.MAX_SPEED, 0))
    physics.step([d], DT)
    assert d.vel.length() == pytest.approx(
        CFG.MAX_SPEED * math.exp(-CFG.FRICTION_PER_S * DT))


def test_la_embestida_llega_mas_lejos_que_con_una_sola_friccion():
    """Es el objetivo del cambio: con una fricción única (deslizar = caminar)
    la misma embestida recorre bastante menos."""
    import dataclasses

    def reach(cfg: Config) -> float:
        d = Disc(1, Vec2(), facing=Vec2(1, 0))
        ph = Physics(cfg)
        d.start_charge()
        for _ in range(cfg.TICK_HZ):
            ph.step([d], cfg.TICK_DT)
        d.release_charge()
        for _ in range(4 * cfg.TICK_HZ):
            ph.step([d], cfg.TICK_DT)
        return d.pos.x

    uniform = dataclasses.replace(CFG, SLIDE_FRICTION_PER_S=CFG.FRICTION_PER_S)
    assert reach(CFG) > 1.5 * reach(uniform)


# ---------------- inmóvil al cargar ----------------

def test_cargando_no_acelera_pero_si_apunta(physics):
    """Mientras se carga, las teclas solo cambian hacia dónde apunta."""
    d = Disc(1, Vec2(), facing=Vec2(1, 0))
    d.start_charge()
    d.set_move(Vec2(0, 1))            # pulsa "abajo" con la carga activa
    run(physics, [d], CFG.TICK_HZ // 2)
    assert d.pos.length() == 0.0       # no se movió
    assert d.vel.length() == 0.0
    assert d.facing == Vec2(0, 1)      # pero ahora apunta hacia abajo


def test_la_embestida_sale_hacia_donde_se_apunto(physics):
    d = Disc(1, Vec2(), facing=Vec2(1, 0))
    d.start_charge()
    d.set_move(Vec2(0, 1))
    run(physics, [d], CFG.TICK_HZ // 2)
    d.set_move(Vec2())                 # suelta las flechas: facing se conserva
    d.release_charge()
    physics.step([d], DT)
    assert d.vel.x == pytest.approx(0.0, abs=1e-9)
    assert d.vel.y > 0.0


def test_al_soltar_la_carga_vuelve_a_caminar(physics):
    d = Disc(1, Vec2())
    d.start_charge()
    d.set_move(Vec2(1, 0))
    run(physics, [d], 5)
    assert d.vel.length() == 0.0       # cargando: quieto
    d.release_charge()
    physics.step([d], DT)              # la suelta lanza la embestida
    d.vel = Vec2()                     # se descarta el impulso: aquí solo importa caminar
    run(physics, [d], 5)
    assert d.vel.length() > 0.0        # ya no carga (en enfriamiento): vuelve a acelerar


def test_en_enfriamiento_mantener_la_tecla_de_carga_no_inmoviliza(physics):
    """Sin carga real (enfriamiento) no hay motivo para dejar al disco clavado."""
    d = Disc(1, Vec2())
    d.cooldown = CFG.DASH_COOLDOWN_S
    d.start_charge()
    d.set_move(Vec2(1, 0))
    run(physics, [d], 5)
    assert d.vel.length() > 0.0
    assert d.charge_time == 0.0


def test_un_golpe_si_mueve_a_un_disco_que_esta_cargando(physics):
    """Estar inmóvil por cargar no lo hace inamovible: la colisión sí lo empuja."""
    victim = Disc(2, Vec2(2 * CFG.DISC_RADIUS - 2, 0))
    victim.start_charge()
    hitter = Disc(1, Vec2(0, 0), vel=Vec2(CFG.MAX_SPEED, 0))
    run(physics, [hitter, victim], 3)
    assert victim.vel.x > 0.0


def test_slide_friction_mayor_que_la_normal_es_invalida():
    import dataclasses

    with pytest.raises(ValueError, match="SLIDE_FRICTION_PER_S"):
        dataclasses.replace(CFG, SLIDE_FRICTION_PER_S=CFG.FRICTION_PER_S + 1)


# ---------------- embestida ----------------

def charge_and_release(physics: Physics, d: Disc, ticks: int) -> None:
    d.start_charge()
    run(physics, [d], ticks)
    d.release_charge()
    physics.step([d], DT)


def test_embestida_con_carga_completa(physics):
    d = Disc(1, Vec2(), facing=Vec2(0, 1))
    charge_and_release(physics, d, 2 * CFG.TICK_HZ)  # carga de sobra: se topa en 1 s
    assert d.vel.length() == pytest.approx(CFG.DASH_MAX_SPEED)
    assert d.vel.normalized().y == pytest.approx(1.0)  # sale hacia donde mira
    assert d.dashing
    assert d.cooldown == pytest.approx(CFG.DASH_COOLDOWN_S)


def test_embestida_proporcional_a_la_carga(physics):
    d = Disc(1, Vec2())
    half = CFG.TICK_HZ // 2
    charge_and_release(physics, d, half)
    expected = CFG.DASH_MAX_SPEED * (half * DT) / CFG.CHARGE_MAX_S
    assert d.vel.length() == pytest.approx(expected)


def test_en_enfriamiento_no_carga(physics):
    d = Disc(1, Vec2())
    charge_and_release(physics, d, CFG.TICK_HZ)
    cooldown_before = d.cooldown
    d.start_charge()
    physics.step([d], DT)
    assert d.charge_time == 0.0
    d.release_charge()
    physics.step([d], DT)
    # Soltar sin carga no reinicia el enfriamiento: solo siguió bajando.
    assert d.cooldown < cooldown_before


def test_embestida_termina_cuando_frena(physics):
    d = Disc(1, Vec2())
    charge_and_release(physics, d, CFG.TICK_HZ)
    run(physics, [d], CFG.TICK_HZ)
    assert not d.dashing
    assert physics.mass(d) == CFG.DISC_MASS


# ---------------- choques ----------------

def head_on(v_a: float, v_b: float) -> tuple[Disc, Disc]:
    """Dos discos ya solapados por 2 px, acercándose."""
    gap = 2 * CFG.DISC_RADIUS - 2
    return (Disc(1, Vec2(0, 0), vel=Vec2(v_a, 0)),
            Disc(2, Vec2(gap, 0), vel=Vec2(v_b, 0)))


def test_choque_igual_masa_intercambia_velocidades(physics):
    a, b = head_on(200, -100)
    physics.resolve_collisions([a, b])
    assert a.vel.x == pytest.approx(-100)
    assert b.vel.x == pytest.approx(200)


@pytest.mark.parametrize("a_dashing", [False, True])
def test_choque_conserva_momento_y_energia(physics, a_dashing):
    a, b = head_on(300, -50)
    a.vel = Vec2(300, 40)   # con componente tangencial
    a.dashing = a_dashing
    before_p, before_e = momentum(physics, [a, b]), kinetic(physics, [a, b])
    physics.resolve_collisions([a, b])
    after_p = momentum(physics, [a, b])
    assert after_p.x == pytest.approx(before_p.x)
    assert after_p.y == pytest.approx(before_p.y)
    assert kinetic(physics, [a, b]) == pytest.approx(before_e)


def test_disco_en_embestida_empuja_mas(physics):
    normal_a, normal_b = head_on(300, 0)
    physics.resolve_collisions([normal_a, normal_b])
    dash_a, dash_b = head_on(300, 0)
    dash_a.dashing = True
    physics.resolve_collisions([dash_a, dash_b])
    assert dash_b.vel.x > normal_b.vel.x
    # Elástico 1D con masas 2 y 1: vb' = 2*m_a/(m_a+m_b) * va = 4/3 * va
    assert dash_b.vel.x == pytest.approx(300 * 2 * CFG.DASH_MASS_FACTOR / (CFG.DASH_MASS_FACTOR + 1))


def test_choque_separa_discos_solapados(physics):
    a, b = head_on(0, 0)
    physics.resolve_collisions([a, b])
    assert (b.pos - a.pos).length() == pytest.approx(2 * CFG.DISC_RADIUS)


def test_discos_que_se_alejan_no_rebotan(physics):
    a, b = head_on(-100, 100)
    physics.resolve_collisions([a, b])
    assert a.vel.x == -100 and b.vel.x == 100


def test_centros_identicos_no_dividen_entre_cero(physics):
    a, b = Disc(1, Vec2(5, 5)), Disc(2, Vec2(5, 5))
    physics.resolve_collisions([a, b])
    assert (b.pos - a.pos).length() == pytest.approx(2 * CFG.DISC_RADIUS)


# ---------------- tunneling ----------------

def test_tope_global_tras_un_choque(physics):
    """Un choque contra un disco en embestida deja al otro a 4/3 de
    DASH_MAX_SPEED (800 px/s = 26,7 px por tick > radio). El tope de
    Physics lo recorta ANTES de mover: avanza como máximo DASH_MAX_SPEED*dt."""
    d = Disc(1, Vec2(), vel=Vec2(CFG.DASH_MAX_SPEED * 4 / 3, 0))
    physics.step([d], DT)
    assert d.pos.length() <= CFG.DASH_MAX_SPEED * DT + 1e-9
    assert d.pos.length() < CFG.DISC_RADIUS


def test_ningun_disco_atraviesa_a_otro(physics):
    """Escenario peor: embestida completa contra una fila de discos que
    vienen de frente, en una línea. Si hubiera tunneling, algún disco
    terminaría al otro lado de su vecino: el orden sobre el eje x cambiaría."""
    dasher = Disc(1, Vec2(-200, 0), facing=Vec2(1, 0))
    dasher.start_charge()
    run(physics, [dasher], CFG.TICK_HZ)
    dasher.release_charge()
    others = [Disc(i, Vec2(-200 + 60 * i, 0), vel=Vec2(-CFG.MAX_SPEED, 0)) for i in range(2, 6)]
    discs = [dasher, *others]
    for _ in range(CFG.TICK_HZ):
        physics.step(discs, DT)
        xs = [d.pos.x for d in discs]
        assert xs == sorted(xs), "un disco atravesó a otro"


# ---------------- arena ----------------

def test_arena_contiene_por_el_centro():
    arena = Arena(CFG.ARENA_RADIUS)
    assert arena.contains(Disc(1, Vec2(CFG.ARENA_RADIUS, 0)))
    assert not arena.contains(Disc(1, Vec2(CFG.ARENA_RADIUS + 0.1, 0)))


def test_puntos_de_aparicion_dentro_y_separados():
    arena = Arena(CFG.ARENA_RADIUS)
    points = arena.spawn_points(CFG.MAX_ACTIVE_PLAYERS, CFG.SPAWN_RING_FRACTION)
    assert len(points) == CFG.MAX_ACTIVE_PLAYERS
    for p, q in itertools.combinations(points, 2):
        assert (p - q).length() > 2 * CFG.DISC_RADIUS
    assert all(p.length() + CFG.DISC_RADIUS < CFG.ARENA_RADIUS for p in points)
    assert math.isclose(points[0].length(), CFG.ARENA_RADIUS * CFG.SPAWN_RING_FRACTION)


def test_un_toque_corto_no_embiste_ni_gasta_enfriamiento(physics):
    """Visto jugando: un toque de 0,1 s daba una embestida más lenta que caminar
    y encima bloqueaba la carga 1,5 s."""
    d = Disc(1, Vec2(0, 0))
    charge_and_release(physics, d, round((CFG.MIN_CHARGE_S / CFG.TICK_DT) / 2))
    assert d.vel.length() == 0 and d.cooldown == 0.0 and not d.dashing


def test_con_la_carga_minima_si_embiste(physics):
    d = Disc(1, Vec2(0, 0))
    charge_and_release(physics, d, round(CFG.MIN_CHARGE_S / CFG.TICK_DT) + 1)
    assert d.vel.length() > 0 and d.cooldown > 0.0
