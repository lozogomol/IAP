"""Lógica pura del servidor de inferencia (sin modelos): vehículo principal, ejes, pasajeros y privacidad.

Todo trabaja con diccionarios y arreglos de numpy en coordenadas de la imagen completa, así se prueba sin cargar
ningún modelo (test_app.py).
"""
from statistics import mean, median

import numpy as np

import config

# Orden de los 5 puntos de M3 (kpt_shape [5, 3]), verificado sobre fotos con el modelo real.
KPT_CENTRO, KPT_ABAJO, KPT_ARRIBA, KPT_IZQ, KPT_DER = range(5)


# --- Vehículo principal ------------------------------------------------------------------------------

def _caja_contiene(caja, punto):
    x1, y1, x2, y2 = caja
    return x1 <= punto[0] <= x2 and y1 <= punto[1] <= y2


def _mascara_contiene(vehiculo, punto):
    m = vehiculo.get("mascara")
    if m is None:
        return False
    x, y = int(punto[0]), int(punto[1])
    return 0 <= y < m.shape[0] and 0 <= x < m.shape[1] and bool(m[y, x])


def _centralidad(caja, forma):
    """1 en el centro de la imagen, ~0 en una esquina."""
    if not forma:
        return 1.0
    alto, ancho = forma[:2]
    x1, y1, x2, y2 = caja
    dx = ((x1 + x2) / 2 - ancho / 2) / (ancho / 2)
    dy = ((y1 + y2) / 2 - alto / 2) / (alto / 2)
    return max(0.05, 1 - (dx * dx + dy * dy) ** 0.5 / 2 ** 0.5)


def mas_grande_y_centrado(vehiculos, forma=None, conf_min=config.M2_CONF_SIN_PLACA):
    """Sin placa de referencia: mayor área ponderada por cercanía al centro, solo con confianza >= conf_min."""
    candidatos = [v for v in vehiculos if v["confianza"] >= conf_min]
    return max(candidatos, key=lambda v: v["area"] * _centralidad(v["caja"], forma)) if candidatos else None


def placa_leida(placas):
    """La placa de referencia: formato boliviano primero, luego mayor confianza de lectura."""
    if not placas:
        return None
    return max(placas, key=lambda p: (bool(p.get("formato_bolivia")), p.get("confianza_lectura") or 0))


def vehiculo_de_placa(vehiculos, placa, forma=None):
    """El vehículo que contiene el centro de la placa (si varios: el que lo tiene en la máscara y luego el de mayor
    área). Si ninguno la contiene, el más grande y centrado con confianza >= 0.40."""
    if not vehiculos:
        return None
    x1, y1, x2, y2 = placa["caja"]
    centro = ((x1 + x2) / 2, (y1 + y2) / 2)
    candidatos = [v for v in vehiculos if _caja_contiene(v["caja"], centro)]
    if candidatos:
        return max(candidatos, key=lambda v: (_mascara_contiene(v, centro), v["area"]))
    return mas_grande_y_centrado(vehiculos, forma)


def elegir_vehiculo(vehiculos, placas, forma=None):
    """Vehículo principal: el que contiene el centro de la placa leída; sin placa, el más grande y centrado."""
    if not vehiculos:
        return None
    placa = placa_leida(placas)
    return vehiculo_de_placa(vehiculos, placa, forma) if placa else mas_grande_y_centrado(vehiculos, forma)


def recorte(caja, forma, margen=config.M3_MARGEN_RECORTE):
    """Caja del vehículo con margen, recortada a la imagen: (x1, y1, x2, y2) enteros."""
    alto, ancho = forma[:2]
    x1, y1, x2, y2 = caja
    mx, my = (x2 - x1) * margen, (y2 - y1) * margen
    return (max(0, int(x1 - mx)), max(0, int(y1 - my)), min(ancho, int(x2 + mx)), min(alto, int(y2 + my)))


# --- M3: llantas → ejes ------------------------------------------------------------------------------

def llanta(caja, conf, puntos=None, kpt_conf=config.M3_KPT_CONF):
    """Normaliza una detección de M3 a {caja, confianza, centro, alto, ancho, puntos}.

    Se adapta al kpt_shape real: 5 puntos (centro, abajo, arriba, izquierda, derecha), 1 punto (centro) o ninguno
    (solo caja). Un punto con confianza < kpt_conf no se usa y se cae a la caja.
    """
    x1, y1, x2, y2 = (float(v) for v in caja)
    pts = None if puntos is None else np.asarray(puntos, dtype=float).reshape(len(puntos), -1)

    def punto(i):
        if pts is None or i >= len(pts):
            return None
        p = pts[i]
        if len(p) >= 3 and p[2] < kpt_conf:
            return None
        return float(p[0]), float(p[1])

    centro = punto(KPT_CENTRO) or ((x1 + x2) / 2, (y1 + y2) / 2)
    abajo, arriba, izq, der = punto(KPT_ABAJO), punto(KPT_ARRIBA), punto(KPT_IZQ), punto(KPT_DER)
    alto = abs(abajo[1] - arriba[1]) if abajo and arriba else y2 - y1
    ancho = abs(der[0] - izq[0]) if izq and der else x2 - x1
    return {
        "caja": [int(round(x1)), int(round(y1)), int(round(x2)), int(round(y2))],
        "confianza": float(conf),
        "centro": [round(centro[0], 1), round(centro[1], 1)],
        "alto": max(1.0, alto),
        "ancho": max(1.0, ancho),
        "puntos": None if pts is None else [[round(float(p[0]), 1), round(float(p[1]), 1)] for p in pts],
    }


def contar_ejes(caja_vehiculo, llantas):
    """Ejes del vehículo principal a partir de sus llantas (lógica del notebook del ingeniero).

    1. Llantas del vehículo: centro dentro de su caja en horizontal y en su parte baja.
    2. Vista frontal/posterior (llantas de canto: ancho/alto mediano bajo) → ejes = None.
    3. Lado cercano: llantas con altura visible >= 75 % de la más alta (las del lado lejano se ven más chicas).
    4. Ordenadas en x, dos llantas a menos de 0.6 diámetros en horizontal son un mismo eje (rueda doble).
    5. Mínimo 2 ejes.
    Devuelve (ejes | None, llantas_del_vehiculo, detalle).
    """
    x1, y1, x2, y2 = caja_vehiculo
    corte = y1 + config.M3_PARTE_BAJA * (y2 - y1)
    propias = [l for l in llantas if x1 <= l["centro"][0] <= x2 and l["centro"][1] >= corte]
    detalle = {"llantas_vehiculo": len(propias), "llantas_lado_cercano": 0, "vista": None, "motivo_ejes": ""}
    if not propias:
        detalle["motivo_ejes"] = "No se detectaron llantas en la parte baja del vehículo."
        return None, propias, detalle

    aspecto = median(l["ancho"] / l["alto"] for l in propias)
    if aspecto < config.M3_VISTA_FRONTAL:
        detalle.update(vista="frontal_posterior",
                       motivo_ejes="Vista frontal o posterior: los ejes no se pueden contar.")
        return None, propias, detalle
    detalle["vista"] = "lateral"

    alto_max = max(l["alto"] for l in propias)
    cercanas = sorted((l for l in propias if l["alto"] >= config.M3_LADO_CERCANO * alto_max),
                      key=lambda l: l["centro"][0])
    grupos = [[cercanas[0]]]
    for anterior, actual in zip(cercanas, cercanas[1:]):
        diametro = mean((anterior["alto"], actual["alto"]))
        if actual["centro"][0] - anterior["centro"][0] < config.M3_FUSION_DIAMETROS * diametro:
            grupos[-1].append(actual)
        else:
            grupos.append([actual])
    ejes = max(config.M3_EJES_MIN, len(grupos))
    detalle.update(llantas_lado_cercano=len(cercanas), grupos_llantas=len(grupos),
                   motivo_ejes=f"{len(cercanas)} llantas del lado cercano en {len(grupos)} grupo(s)"
                               + (f"; mínimo {config.M3_EJES_MIN} ejes" if len(grupos) < config.M3_EJES_MIN else ""))
    return ejes, propias, detalle


# --- M4: pasajeros y privacidad ---------------------------------------------------------------------

def fraccion_dentro(persona, caja):
    """Fracción de la persona (máscara si hay, si no caja) que cae dentro de la caja del vehículo."""
    x1, y1, x2, y2 = caja
    m, (ox, oy) = persona.get("mascara"), persona.get("origen", (0, 0))
    if m is not None and m.any():
        ys, xs = np.nonzero(m)
        xs, ys = xs + ox, ys + oy
        return float(((xs >= x1) & (xs <= x2) & (ys >= y1) & (ys <= y2)).mean())
    px1, py1, px2, py2 = persona["caja"]
    area = max(1, (px2 - px1) * (py2 - py1))
    ix = max(0, min(px2, x2) - max(px1, x1))
    iy = max(0, min(py2, y2) - max(py1, y1))
    return ix * iy / area


def contar_pasajeros(caja_vehiculo, personas):
    """Pasajeros = personas cuya máscara (o caja) cae dentro de la caja del vehículo principal."""
    return sum(1 for p in personas if fraccion_dentro(p, caja_vehiculo) >= config.M4_DENTRO)


def cubrir_personas(imagen, personas, color=config.COLOR_PRIVACIDAD):
    """PRIVACIDAD: pinta cada persona con un color sólido (máscara; sin máscara, su caja). Modifica `imagen`.

    No hay reconocimiento facial ni recortes de rostros: solo se tapa a la persona entera.
    """
    alto, ancho = imagen.shape[:2]
    for p in personas:
        m, (ox, oy) = p.get("mascara"), p.get("origen", (0, 0))
        if m is not None and m.any():
            mh, mw = m.shape[:2]
            h, w = min(mh, alto - oy), min(mw, ancho - ox)
            region = imagen[oy:oy + h, ox:ox + w]
            region[m[:h, :w]] = color
        else:
            x1, y1, x2, y2 = (int(v) for v in p["caja"])
            imagen[max(0, y1):y2, max(0, x1):x2] = color
    return imagen
