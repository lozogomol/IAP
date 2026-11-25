"""Genera los íconos PNG de la PWA (y del APK) a partir del ícono de peaje de la web (_icono.html).

Fondo azul noche (#050b1e) a sangre, sin esquinas redondeadas, y el ícono en blanco con trazos rectos.
Uso (necesita Pillow, solo para generar; la web no lo usa):

    web\\venv\\Scripts\\python.exe -m pip install Pillow
    web\\venv\\Scripts\\python.exe movil\\generar_iconos.py

Salida en web/static/iconos/: icono-192.png, icono-512.png, icono-512-maskable.png y apple-touch-icon.png (180).
"""
from pathlib import Path

from PIL import Image, ImageDraw

FONDO = (5, 11, 30)       # #050b1e, azul noche
TRAZO = (255, 255, 255)
SUPER = 4                 # supermuestreo para bordes suaves
DESTINO = Path(__file__).resolve().parent.parent / "web" / "static" / "iconos"

# Ícono de peaje en coordenadas del viewBox 0 0 48 48 (mismo dibujo que peaje/templates/peaje/_icono.html).
# (puntos, grosor, cerrado)
TRAZOS = [
    ([(6, 41), (6, 15), (18, 15), (18, 41)], 2.6, False),          # caseta
    ([(4, 15), (20, 15)], 2.4, False),                             # techo
    ([(9, 21), (15, 21), (15, 26), (9, 26)], 2.4, True),           # ventana
    ([(24, 38), (24, 33), (27, 28), (38, 28), (41, 33), (41, 38)], 2.4, True),  # vehículo
    ([(2, 41), (46, 41)], 2.6, False),                             # piso
]
BARRERA = ((18, 23), (44, 23), 3.2, (5, 3))                        # barrera punteada
RUEDAS = [(27, 36, 3, 3), (35, 36, 3, 3)]


def _segmento(d, a, b, ancho, ext_a, ext_b):
    """Trazo recto de a a b con extremos planos, extendido ext_* (para cerrar las esquinas sin redondear)."""
    (x1, y1), (x2, y2) = a, b
    largo = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
    ux, uy = (x2 - x1) / largo, (y2 - y1) / largo
    x1, y1, x2, y2 = x1 - ux * ext_a, y1 - uy * ext_a, x2 + ux * ext_b, y2 + uy * ext_b
    nx, ny = -uy * ancho / 2, ux * ancho / 2
    d.polygon([(x1 + nx, y1 + ny), (x2 + nx, y2 + ny), (x2 - nx, y2 - ny), (x1 - nx, y1 - ny)], fill=TRAZO)


def _desplazar(pts, dist):
    """Polígono convexo desplazado `dist` hacia afuera (negativo = hacia adentro), con esquinas en ángulo (inglete)."""
    area = sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]))
    signo = 1 if area > 0 else -1
    lineas = []
    for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]):
        largo = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
        nx, ny = signo * (y2 - y1) / largo * dist, -signo * (x2 - x1) / largo * dist
        lineas.append(((x1 + nx, y1 + ny), (x2 + nx, y2 + ny)))
    salida = []
    for (a1, a2), (b1, b2) in zip(lineas[-1:] + lineas[:-1], lineas):
        dxa, dya, dxb, dyb = a2[0] - a1[0], a2[1] - a1[1], b2[0] - b1[0], b2[1] - b1[1]
        t = ((b1[0] - a1[0]) * dyb - (b1[1] - a1[1]) * dxb) / (dxa * dyb - dya * dxb)
        salida.append((a1[0] + t * dxa, a1[1] + t * dya))
    return salida


def dibujar(lado, contenido):
    """Imagen cuadrada de `lado` px con el ícono ocupando la fracción `contenido` del ancho, centrado."""
    grande = lado * SUPER
    img = Image.new("RGB", (grande, grande), FONDO)
    d = ImageDraw.Draw(img)
    escala = grande * contenido / 44           # el dibujo ocupa x 2..46 e y ~13..42 del viewBox
    ox = (grande - 44 * escala) / 2 - 2 * escala
    oy = (grande - 29 * escala) / 2 - 13 * escala

    def p(x, y):
        return ox + x * escala, oy + y * escala

    for puntos, ancho, cerrado in TRAZOS:
        w = ancho * escala
        if cerrado:  # contorno cerrado: banda entre el polígono exterior y el interior (esquinas rectas)
            base = [p(x, y) for x, y in puntos]
            d.polygon(_desplazar(base, w / 2), fill=TRAZO)
            d.polygon(_desplazar(base, -w / 2), fill=FONDO)
            continue
        pts = [p(x, y) for x, y in puntos]
        for i in range(len(pts) - 1):
            interior_a = i > 0
            interior_b = i < len(pts) - 2
            _segmento(d, pts[i], pts[i + 1], w, w / 2 if interior_a else 0, w / 2 if interior_b else 0)
    (x1, y), (x2, _), ancho, (raya, hueco) = BARRERA
    x = x1
    while x < x2:
        _segmento(d, p(x, y), p(min(x + raya, x2), y), ancho * escala, 0, 0)
        x += raya + hueco
    for rx, ry, rw, rh in RUEDAS:
        d.rectangle([p(rx, ry), p(rx + rw, ry + rh)], fill=TRAZO)
    return img.resize((lado, lado), Image.LANCZOS)


def main():
    DESTINO.mkdir(parents=True, exist_ok=True)
    salidas = {
        "icono-192.png": (192, 0.70),
        "icono-512.png": (512, 0.70),
        # maskable: Android recorta a un círculo/forma; el dibujo queda dentro de la zona segura (círculo del 80 %)
        "icono-512-maskable.png": (512, 0.52),
        "apple-touch-icon.png": (180, 0.66),
    }
    for nombre, (lado, contenido) in salidas.items():
        dibujar(lado, contenido).save(DESTINO / nombre, optimize=True)
        print("generado", DESTINO / nombre)


if __name__ == "__main__":
    main()
