"""Cliente del endpoint de inferencia (RunPod / Colab + ngrok).

Contrato: POST {INFERENCE_URL}/inferir, multipart con el campo `imagen` (JPEG) y la cabecera X-API-Token.
Respuesta esperada (los campos que falten se toleran):

    {"placas": [{"texto": "2451KTP", "confianza_lectura": 93.4, "confianza_deteccion": 88.0, "caja": [x1, y1, x2, y2],
                 "categoria": "auto", "confianza_categoria": 91.2, "caja_vehiculo": [x1, y1, x2, y2]}],
     "categoria": "auto", "confianza_categoria": 91.2, "caja_vehiculo": [x1, y1, x2, y2], "ejes": null}

`categoria` la da M2: es la clase del VEHÍCULO APUNTADO, ya en el español del sistema (auto, bus, camion, minibus,
moto). Cada placa trae el vehículo que la contiene, así que la clase que se muestra es la del vehículo de la placa
leída; el bloque global (vehículo de la placa leída por el servidor, o el más grande y centrado) solo se usa si la
placa no trae el suyo o no hay placa válida. Del vehículo principal llegan también `ejes` (M3, null si no se pudo
contar), `llantas_detectadas`, `pasajeros` (M4), `latencia_ms` por modelo, `cajas` y `evidencia` (el frame con las
personas cubiertas por su máscara; null si no había personas).
Las confianzas pueden venir en 0..1 o en 0..100: aquí se normalizan a 0..1.
"""
import base64
import binascii
import time

import requests
from django.conf import settings

from .models import CATEGORIAS
from .placas import corregir_ocr, es_formato_bolivia, normalizar_placa

CATEGORIAS_VALIDAS = {c for c, _ in CATEGORIAS}
TIMEOUT_S = 20


class InferenciaNoDisponible(Exception):
    pass


def _confianza(valor):
    try:
        v = float(valor)
    except (TypeError, ValueError):
        return None
    return round(v / 100 if v > 1 else v, 4)


def _categoria_ejes(datos):
    categoria = datos.get("categoria")
    categoria = categoria if categoria in CATEGORIAS_VALIDAS else ""
    try:
        ejes = int(datos.get("ejes")) if datos.get("ejes") is not None else None
    except (TypeError, ValueError):
        ejes = None
    if ejes is not None and not 2 <= ejes <= 9:
        ejes = None
    return categoria, ejes


def _caja(valor):
    """[x1, y1, x2, y2] en píxeles de la imagen enviada, o None."""
    if not isinstance(valor, (list, tuple)) or len(valor) != 4:
        return None
    try:
        return [round(float(v)) for v in valor]
    except (TypeError, ValueError):
        return None


def _vehiculo(datos):
    """Vehículo de un bloque (placa o global): clase tal cual la da el modelo, su confianza y su caja."""
    clase = datos.get("categoria")
    clase = clase.strip() if isinstance(clase, str) else ""
    return {
        "clase": clase,
        "confianza_clase": _confianza(datos.get("confianza_categoria")) if clase else None,
        "caja_vehiculo": _caja(datos.get("caja_vehiculo")),
    }


def _entero(valor, minimo=0, maximo=99):
    try:
        v = int(valor)
    except (TypeError, ValueError):
        return None
    return v if minimo <= v <= maximo else None


def _evidencia(valor):
    """Data URL JPEG de la evidencia (personas cubiertas) → bytes, o None."""
    if not isinstance(valor, str) or not valor.startswith("data:image/"):
        return None
    try:
        return base64.b64decode(valor.split(",", 1)[1], validate=True)
    except (IndexError, ValueError, binascii.Error):
        return None


MAX_PUNTOS = 200      # por polígono (el servidor manda ~60)
MAX_FORMAS = 30       # llantas, personas o partes de una persona


def _punto(valor, con_confianza=False):
    """[x, y] (o [x, y, conf]) numérico, o None."""
    if not isinstance(valor, (list, tuple)) or len(valor) < (3 if con_confianza else 2):
        return None
    try:
        p = [round(float(valor[0]), 1), round(float(valor[1]), 1)]
        if con_confianza:
            p.append(round(float(valor[2]), 3))
        return p
    except (TypeError, ValueError):
        return None


def _poligono(valor):
    puntos = [p for p in (_punto(v) for v in (valor or [])[:MAX_PUNTOS]) if p] if isinstance(valor, list) else []
    return puntos if len(puntos) >= 3 else None


def _formas(valor):
    """Formas para dibujar en el punto de cobro (validadas y acotadas), o None si el servidor no las manda:
    vehículo (polígono de M2), llantas (keypoints y eje de M3), pasajeros (polígonos de M4) y placa (caja de M1)."""
    if not isinstance(valor, dict):
        return None
    v = valor.get("vehiculo") if isinstance(valor.get("vehiculo"), dict) else None
    llantas = []
    for l in (valor.get("llantas") or [])[:MAX_FORMAS]:
        if not isinstance(l, dict):
            continue
        kps = l.get("keypoints") if isinstance(l.get("keypoints"), list) else []
        llantas.append({"caja": _caja(l.get("caja")), "centro": _punto(l.get("centro")),
                        "eje": _entero(l.get("eje"), minimo=1, maximo=20),
                        "keypoints": [k for k in (_punto(p, con_confianza=True) for p in kps[:10]) if k] or None})
    pasajeros = []
    for p in (valor.get("pasajeros") or [])[:MAX_FORMAS]:
        if isinstance(p, dict):
            poligonos = [pg for pg in (_poligono(x) for x in (p.get("poligonos") or [])[:MAX_FORMAS]) if pg]
            pasajeros.append({"caja": _caja(p.get("caja")), "poligonos": poligonos})
    placa = valor.get("placa") if isinstance(valor.get("placa"), dict) else None
    nombres = valor.get("nombres_keypoints")
    return {
        "vehiculo": {"categoria": str(v.get("categoria") or "")[:20], "caja": _caja(v.get("caja")),
                     "poligono": _poligono(v.get("poligono"))} if v else None,
        "llantas": llantas,
        "nombres_keypoints": [str(n)[:20] for n in nombres[:10]] if isinstance(nombres, list) else [],
        "pasajeros": pasajeros,
        "placa": {"caja": _caja(placa.get("caja")), "texto": str(placa.get("texto") or "")[:20]} if placa else None,
    }


def interpretar_vehiculo(datos):
    """Datos del frame que no dependen de que la placa sea válida: vehículo principal (M2), ejes (M3), pasajeros
    (M4), cajas, latencias por modelo y la evidencia con las personas cubiertas."""
    placas = [p for p in datos.get("placas") or [] if isinstance(p, dict)]
    cajas = datos.get("cajas") if isinstance(datos.get("cajas"), dict) else {}
    llantas = [l for l in cajas.get("llantas") or [] if isinstance(l, dict)]
    lat = datos.get("latencia_ms") if isinstance(datos.get("latencia_ms"), dict) else {}
    _, ejes = _categoria_ejes(datos)
    return {
        **_vehiculo(datos),
        "cajas_placas": [c for c in (_caja(p.get("caja")) for p in placas) if c],
        "cajas_llantas": [c for c in (_caja(l.get("caja")) for l in llantas) if c],
        "ejes": ejes,
        "llantas_detectadas": _entero(datos.get("llantas_detectadas")),
        "pasajeros": _entero(datos.get("pasajeros")),
        "latencias": {k: _entero(lat.get(k), maximo=10 ** 6) for k in ("m1", "m2", "m3", "m4")},
        "detalle_ejes": str((datos.get("detalle") or {}).get("motivo_ejes") or "")[:200]
        if isinstance(datos.get("detalle"), dict) else "",
        "evidencia": _evidencia(datos.get("evidencia")),
        "formas": _formas(datos.get("formas")),
    }


def interpretar(datos):
    """Respuesta cruda → lista de lecturas válidas (formato boliviano), de mayor a menor confianza.

    Cada lectura: {"placa", "texto_leido", "confianza", "categoria", "ejes", "caja", "clase", "confianza_clase",
    "caja_vehiculo"}. `clase` es la del vehículo que contiene ESA placa (nombre del modelo, para mostrar);
    `categoria` es esa misma clase si tiene tarifa en el peaje (models.CATEGORIAS), o "" si no.
    """
    _, ejes_global = _categoria_ejes(datos)
    vehiculo_global = _vehiculo(datos)
    lecturas = []
    for p in datos.get("placas") or []:
        if not isinstance(p, dict):
            continue
        texto = p.get("texto") or p.get("placa") or ""
        placa = corregir_ocr(normalizar_placa(texto))
        if not es_formato_bolivia(placa):
            continue
        _, ejes = _categoria_ejes(p)
        vehiculo = _vehiculo(p) if p.get("categoria") or p.get("caja_vehiculo") else vehiculo_global
        lecturas.append({
            "placa": placa,
            "texto_leido": texto,
            "confianza": _confianza(p.get("confianza_lectura", p.get("confianza"))),
            "categoria": vehiculo["clase"] if vehiculo["clase"] in CATEGORIAS_VALIDAS else "",
            "ejes": ejes if ejes is not None else ejes_global,
            "caja": _caja(p.get("caja")),
            **vehiculo,
        })
    lecturas.sort(key=lambda l: l["confianza"] or 0, reverse=True)
    return lecturas


def inferir(imagen_bytes, nombre="frame.jpg"):
    """Envía la imagen al endpoint. Devuelve (lecturas, ms_inferencia, vehiculo). Lanza InferenciaNoDisponible.

    `vehiculo`: ver interpretar_vehiculo (categoría M2, su confianza y las cajas para dibujar en el video).
    """
    url = settings.INFERENCE_URL.rstrip("/")
    if not url:
        raise InferenciaNoDisponible("INFERENCE_URL no está configurado.")
    t0 = time.perf_counter()
    try:
        r = requests.post(
            url + "/inferir",
            files={"imagen": (nombre, imagen_bytes, "image/jpeg")},
            headers={"X-API-Token": settings.INFERENCE_TOKEN, "ngrok-skip-browser-warning": "1"},
            timeout=TIMEOUT_S,
        )
        r.raise_for_status()
        datos = r.json()
    except requests.Timeout as e:
        raise InferenciaNoDisponible(f"la inferencia no respondió en {TIMEOUT_S} s (timeout)") from e
    except (requests.RequestException, ValueError) as e:
        raise InferenciaNoDisponible(str(e)) from e
    if not isinstance(datos, dict):
        raise InferenciaNoDisponible("Respuesta de inferencia con formato inesperado.")
    return interpretar(datos), round((time.perf_counter() - t0) * 1000), interpretar_vehiculo(datos)
