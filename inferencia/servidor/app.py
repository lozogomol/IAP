"""Servidor de inferencia de IAP (FastAPI). Hoy sirve M1 (placas); M2/M3 se sumarán aquí.

Contrato que consume la web Django (web/peaje/inferencia.py):

    GET  /salud     → estado del servidor y de los modelos (público, sin datos sensibles)
    POST /inferir   → multipart con el campo `imagen` (JPEG/PNG) y la cabecera X-API-Token
                      {"placas": [{"texto", "formato_bolivia", "confianza_deteccion", "confianza_lectura", "caja"}],
                       "categoria": null, "ejes": null, "milisegundos": 42}

`categoria` y `ejes` van en null hasta que existan M2 (segmentación) y M3 (keypoints): nunca se inventan.
Las confianzas van en 0..100 (la web las normaliza).

Variables de entorno:
    IAP_TOKEN      token compartido con la web (en Render: INFERENCE_TOKEN). Obligatorio para /inferir.
    IAP_MAX_MB     tamaño máximo de imagen (por defecto 6).
    IAP_UMBRAL     confianza mínima del detector, 0..1 (por defecto 0.4).

Ejecutar:  uvicorn app:app --host 0.0.0.0 --port 8000   (desde inferencia/servidor)
"""
import base64
import logging
import os
import secrets
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import cv2
import numpy as np
from fastapi import FastAPI, File, Header, HTTPException, Query, UploadFile

M1 = Path(__file__).resolve().parent.parent / "m1_placas"
sys.path.insert(0, str(M1))
import detector  # noqa: E402  (m1_placas/detector.py: YOLOv9-t ONNX compartido)

log = logging.getLogger("iap.inferencia")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

MAX_BYTES = int(float(os.environ.get("IAP_MAX_MB", "6")) * 1024 * 1024)
UMBRAL = float(os.environ.get("IAP_UMBRAL", "0.4"))
VERSION = "1.0.0"

_lector = None
_lock_carga = threading.Lock()
_inicio = time.time()
_contador = {"inferencias": 0, "errores": 0}


def lector():
    """OCR de fast-plate-ocr, cargado una sola vez con el modelo que viene en el repo (sin descargas)."""
    global _lector
    if _lector is None:
        with _lock_carga:
            if _lector is None:
                from fast_plate_ocr import LicensePlateRecognizer
                _lector = LicensePlateRecognizer(
                    onnx_model_path=detector.CARPETA / "cct_xs_v2_global.onnx",
                    plate_config_path=detector.CARPETA / "cct_xs_v2_global_plate_config.yaml",
                    device="cpu",
                )
    return _lector


@asynccontextmanager
async def precargar(_app):
    """Carga los modelos al arrancar para que la primera petición no pague el costo."""
    t0 = time.perf_counter()
    detector.sesion()
    lector()
    log.info("Modelos cargados en %d ms", (time.perf_counter() - t0) * 1000)
    if not os.environ.get("IAP_TOKEN"):
        log.warning("IAP_TOKEN no está configurado: /inferir responderá 503.")
    yield


app = FastAPI(title="IAP · Servidor de inferencia", version=VERSION, lifespan=precargar,
              description="M1: detección de placas (YOLOv9-t ONNX) + OCR (fast-plate-ocr).")


def _token_valido(recibido):
    esperado = os.environ.get("IAP_TOKEN", "")
    if not esperado:
        raise HTTPException(503, "El servidor no tiene IAP_TOKEN configurado.")
    if not recibido or not secrets.compare_digest(recibido.encode(), esperado.encode()):
        raise HTTPException(401, "Token inválido.")


def _decodificar(datos):
    if not datos:
        raise HTTPException(400, "La imagen está vacía.")
    if len(datos) > MAX_BYTES:
        raise HTTPException(413, "La imagen es demasiado grande.")
    imagen = cv2.imdecode(np.frombuffer(datos, np.uint8), cv2.IMREAD_COLOR)
    if imagen is None:
        raise HTTPException(400, "No se pudo leer la imagen (¿es JPEG o PNG?).")
    return imagen


def leer_placas(imagen):
    """Detecta y lee las placas. Devuelve (placas, cajas, textos) en el formato del contrato."""
    cajas = detector.detectar(imagen, umbral=UMBRAL)
    placas = []
    ocr = lector()
    for x1, y1, x2, y2, conf_det in cajas:
        if x2 - x1 < 8 or y2 - y1 < 4:
            continue
        recorte = cv2.cvtColor(imagen[y1:y2, x1:x2], cv2.COLOR_BGR2RGB)
        pred = ocr.run_one(recorte, return_confidence=True)
        texto = detector.limpiar(pred.plate or "")
        conf_lec = float(np.mean(pred.char_probs)) if pred.char_probs is not None else None
        placas.append({
            "texto": texto,
            "formato_bolivia": bool(detector.FORMATO_BOLIVIA.match(texto)),
            "confianza_deteccion": round(conf_det * 100, 1),
            "confianza_lectura": None if conf_lec is None else round(conf_lec * 100, 1),
            "caja": [x1, y1, x2, y2],
        })
    placas.sort(key=lambda p: p["confianza_lectura"] or 0, reverse=True)
    return placas


def _anotar(imagen, placas):
    for p in placas:
        x1, y1, x2, y2 = p["caja"]
        cv2.rectangle(imagen, (x1, y1), (x2, y2), (0, 200, 0), 3)
        cv2.putText(imagen, p["texto"] or "?", (x1, max(25, y1 - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.9,
                    (0, 200, 0), 2, cv2.LINE_AA)
    jpg = cv2.imencode(".jpg", imagen, [cv2.IMWRITE_JPEG_QUALITY, 80])[1]
    return "data:image/jpeg;base64," + base64.b64encode(jpg).decode()


@app.get("/salud")
def salud():
    return {
        "estado": "ok",
        "version": VERSION,
        "modelos": {
            "m1_placas": "cargado" if _lector is not None and detector._SESION is not None else "cargando",
            "m2_segmentacion": "pendiente",
            "m3_keypoints": "pendiente",
        },
        "token_configurado": bool(os.environ.get("IAP_TOKEN")),
        "inferencias": _contador["inferencias"],
        "errores": _contador["errores"],
        "activo_s": round(time.time() - _inicio),
    }


@app.post("/inferir")
def inferir(imagen: UploadFile = File(...),
            x_api_token: str | None = Header(default=None),
            anotada: bool = Query(False, description="Devuelve también la imagen con las cajas (más pesado).")):
    _token_valido(x_api_token)
    t0 = time.perf_counter()
    img = _decodificar(imagen.file.read(MAX_BYTES + 1))
    try:
        placas = leer_placas(img)
    except Exception:  # noqa: BLE001
        _contador["errores"] += 1
        log.exception("Fallo en la inferencia")
        raise HTTPException(500, "Error interno en la inferencia.")
    _contador["inferencias"] += 1
    ms = round((time.perf_counter() - t0) * 1000)
    log.info("inferir %dx%d → %s en %d ms", img.shape[1], img.shape[0], [p["texto"] for p in placas] or "-", ms)
    respuesta = {"placas": placas, "categoria": None, "ejes": None, "milisegundos": ms}
    if anotada:
        respuesta["imagen"] = _anotar(img, placas)
    return respuesta
