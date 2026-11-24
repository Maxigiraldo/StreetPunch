"""Archivos estáticos del cliente: que el servidor los registre con el tipo correcto."""

from server.config import Config
from server.main import CLIENT_DIR, load_static

WOFF2_MAGIC = b"wOF2"   # los primeros 4 bytes de cualquier WOFF2 (firma del formato)


def test_la_fuente_orbitron_se_sirve_como_woff2() -> None:
    # Sin el tipo registrado en CONTENT_TYPES, load_static la saltaría y el navegador
    # caería a la fuente de respaldo sin avisar: por eso se comprueba aquí.
    files = load_static(CLIENT_DIR)
    font = files["/orbitron.woff2"]
    assert font.content_type == "font/woff2"
    assert font.content[:4] == WOFF2_MAGIC


def test_el_cliente_neon_se_sirve_completo() -> None:
    files = load_static(CLIENT_DIR)
    assert files["/style.css"].content_type.startswith("text/css")
    assert files["/neon.js"].content_type.startswith("text/javascript")
    assert files["/client.js"].content_type.startswith("text/javascript")
    assert files["/"] is files["/index.html"]


def test_el_html_pide_los_archivos_que_el_servidor_sirve() -> None:
    # Si index.html referenciara un archivo que no está en la lista blanca, daría 404 en el navegador.
    files = load_static(CLIENT_DIR)
    html = files["/index.html"].content.decode()
    for path in ("/style.css", "/neon.js", "/client.js", "/orbitron.woff2"):
        assert path in html
        assert path in files


def test_hay_ocho_colores_de_jugador_distintos_y_validos() -> None:
    colors = Config().PLAYER_COLORS
    assert len(colors) == Config().MAX_ROOM_PLAYERS   # uno por plaza de la sala
    assert len(set(colors)) == len(colors)
    assert all(len(c) == 7 and c.startswith("#") and int(c[1:], 16) >= 0 for c in colors)


def test_los_mp3_se_sirven_como_audio_mpeg() -> None:
    # Sin ".mp3" en CONTENT_TYPES, load_static los saltaría y el navegador recibiría 404 sin que nada falle.
    files = load_static(CLIENT_DIR)
    mp3s = [path for path in files if path.endswith(".mp3")]
    assert mp3s, "no hay ningún mp3 registrado"
    for path in mp3s:
        assert files[path].content_type == "audio/mpeg"
        # Un MP3 empieza con una etiqueta ID3 o con un fotograma (11 bits a 1: 0xFF y 0xE?).
        head = files[path].content[:3]
        assert head == b"ID3" or (head[0] == 0xFF and head[1] & 0xE0 == 0xE0), path


def test_audio_js_solo_pide_archivos_que_existen() -> None:
    # Las rutas de audio viven en client/audio.js; si una apunta a un archivo que no está en la
    # lista blanca, el navegador recibiría 404 y esa pista quedaría muda sin avisar.
    import re

    files = load_static(CLIENT_DIR)
    urls = re.findall(r'url:\s*"(/[^"]+)"', files["/audio.js"].content.decode())
    assert len(urls) >= 7    # 2 músicas + 5 efectos
    for url in urls:
        assert url in files, url
    assert "/audio.js" in files["/index.html"].content.decode()
