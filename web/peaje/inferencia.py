"""Cliente del endpoint de inferencia (RunPod / Colab + ngrok).

Contrato: POST {INFERENCE_URL}/inferir, multipart con el campo `imagen` (JPEG) y la cabecera X-API-Token.
Respuesta esperada (los campos que falten se toleran):

    {"placas": [{"texto": "2451KTP", "confianza_lectura": 93.4, "confianza_deteccion": 88.0}],
     "categoria": "auto", "ejes": 2}

`categoria` y `ejes` llegarán cuando existan M2/M3; también se aceptan dentro de cada placa.
Las confianzas pueden venir en 0..1 o en 0..100: aquí se normalizan a 0..1.
"""
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


def interpretar(datos):
    """Respuesta cruda → lista de lecturas válidas (formato boliviano), de mayor a menor confianza.

    Cada lectura: {"placa", "texto_leido", "confianza", "categoria", "ejes"}.
    """
    categoria_global, ejes_global = _categoria_ejes(datos)
    lecturas = []
    for p in datos.get("placas") or []:
        if not isinstance(p, dict):
            continue
        texto = p.get("texto") or p.get("placa") or ""
        placa = corregir_ocr(normalizar_placa(texto))
        if not es_formato_bolivia(placa):
            continue
        categoria, ejes = _categoria_ejes(p)
        lecturas.append({
            "placa": placa,
            "texto_leido": texto,
            "confianza": _confianza(p.get("confianza_lectura", p.get("confianza"))),
            "categoria": categoria or categoria_global,
            "ejes": ejes if ejes is not None else ejes_global,
        })
    lecturas.sort(key=lambda l: l["confianza"] or 0, reverse=True)
    return lecturas


def inferir(imagen_bytes, nombre="frame.jpg"):
    """Envía la imagen al endpoint. Devuelve (lecturas, ms_inferencia). Lanza InferenciaNoDisponible."""
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
    except (requests.RequestException, ValueError) as e:
        raise InferenciaNoDisponible(str(e)) from e
    if not isinstance(datos, dict):
        raise InferenciaNoDisponible("Respuesta de inferencia con formato inesperado.")
    return interpretar(datos), round((time.perf_counter() - t0) * 1000)
