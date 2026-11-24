"""Servidor de inferencia de IAP (FastAPI). Sirve M1 (placas) y M2 (tipo de vehículo); M3 se sumará aquí.

Contrato que consume la web Django (web/peaje/inferencia.py):

    GET  /salud     → estado del servidor y de los modelos (público, sin datos sensibles)
    POST /inferir   → multipart con el campo `imagen` (JPEG/PNG) y la cabecera X-API-Token
                      {"placas": [{"texto", "formato_bolivia", "confianza_deteccion", "confianza_lectura", "caja"}],
                       "categoria": "auto", "confianza_categoria": 91.2, "caja_vehiculo": [x1, y1, x2, y2],
                       "ejes": null, "milisegundos": 42}

M2 (YOLO11s-seg, inferencia/modelos/m2_seg.pt) elige el vehículo principal: el que contiene el centro de la placa
leída por M1; si no hay placa (o ningún vehículo la contiene), el de mayor área de máscara. Sin vehículos (o sin el
.pt) `categoria`, `confianza_categoria` y `caja_vehiculo` van en null. `ejes` va en null hasta que exista M3
(keypoints): nunca se inventa. Las confianzas van en 0..100 (la web las normaliza).

Variables de entorno:
    IAP_TOKEN      token compartido con la web (en Render: INFERENCE_TOKEN). Obligatorio para /inferir.
    IAP_MAX_MB     tamaño máximo de imagen (por defecto 6).
    IAP_UMBRAL     confianza mínima del detector, 0..1 (por defecto 0.4).
    IAP_DISPOSITIVO  dispositivo de M2: "cpu" o "0" (GPU). Por defecto GPU si hay CUDA.

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

RAIZ = Path(__file__).resolve().parent.parent
M1 = RAIZ / "m1_placas"
MODELO_M2 = RAIZ / "modelos" / "m2_seg.pt"
sys.path.insert(0, str(M1))
import detector  # noqa: E402  (m1_placas/detector.py: YOLOv9-t ONNX compartido)

log = logging.getLogger("iap.inferencia")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

MAX_BYTES = int(float(os.environ.get("IAP_MAX_MB", "6")) * 1024 * 1024)
UMBRAL = float(os.environ.get("IAP_UMBRAL", "0.4"))
VERSION = "1.1.0"
M2_CONF = 0.35
M2_IMGSZ = 640
# Nombres de clase del modelo → categorías de la web (models.CATEGORIAS)
CATEGORIAS_M2 = {"moto": "moto", "auto": "auto", "minibus": "minibus", "camion": "camion", "bus": "bus"}

_lector = None
_m2 = None
_m2_estado = "pendiente"  # pendiente (no hay .pt) | cargando | cargado | error
_m2_dispositivo = "cpu"
_lock_m2 = threading.Lock()  # los modelos de ultralytics no son seguros entre hilos
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


def cargar_m2():
    """Carga M2 una sola vez (GPU si hay CUDA, si no CPU). Si no existe el .pt, M2 queda 'pendiente'."""
    global _m2, _m2_estado, _m2_dispositivo
    if _m2 is not None or not MODELO_M2.exists():
        return _m2
    _m2_estado = "cargando"
    try:
        import torch
        from ultralytics import YOLO
        _m2_dispositivo = os.environ.get("IAP_DISPOSITIVO") or ("0" if torch.cuda.is_available() else "cpu")
        modelo = YOLO(str(MODELO_M2), task="segment")
        # Calentamiento: la primera predicción inicializa capas y memoria
        modelo.predict(np.zeros((M2_IMGSZ, M2_IMGSZ, 3), np.uint8), imgsz=M2_IMGSZ, conf=M2_CONF,
                       device=_m2_dispositivo, verbose=False)
        _m2, _m2_estado = modelo, "cargado"
        log.info("M2 cargado en %s (clases: %s)", _m2_dispositivo, list(modelo.names.values()))
    except Exception:  # noqa: BLE001  (el servidor sigue sirviendo M1)
        _m2_estado = "error"
        log.exception("No se pudo cargar M2 (%s)", MODELO_M2)
    return _m2


@asynccontextmanager
async def precargar(_app):
    """Carga los modelos al arrancar para que la primera petición no pague el costo."""
    t0 = time.perf_counter()
    detector.sesion()
    lector()
    cargar_m2()
    log.info("Modelos cargados en %d ms", (time.perf_counter() - t0) * 1000)
    if not os.environ.get("IAP_TOKEN"):
        log.warning("IAP_TOKEN no está configurado: /inferir responderá 503.")
    yield


app = FastAPI(title="IAP · Servidor de inferencia", version=VERSION, lifespan=precargar,
              description="M1: detección de placas (YOLOv9-t ONNX) + OCR (fast-plate-ocr). "
                          "M2: tipo de vehículo (YOLO11s-seg).")


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


def detectar_vehiculos(imagen):
    """M2 → lista de {"categoria", "confianza", "caja", "area", "mascara"}; la máscara (bool) y la caja están en
    coordenadas de la imagen original (retina_masks)."""
    if _m2 is None:
        return []
    with _lock_m2:
        r = _m2.predict(imagen, conf=M2_CONF, imgsz=M2_IMGSZ, device=_m2_dispositivo, retina_masks=True,
                        verbose=False)[0]
    if r.boxes is None or len(r.boxes) == 0:
        return []
    cajas = r.boxes.xyxy.cpu().numpy()
    clases = r.boxes.cls.cpu().numpy().astype(int)
    confs = r.boxes.conf.cpu().numpy()
    mascaras = (r.masks.data.cpu().numpy() > 0.5) if r.masks is not None else [None] * len(cajas)
    vehiculos = []
    for i, (caja, clase, conf) in enumerate(zip(cajas, clases, confs)):
        categoria = CATEGORIAS_M2.get(r.names[clase])
        if categoria is None:
            continue
        x1, y1, x2, y2 = (int(round(float(v))) for v in caja)
        vehiculos.append({
            "categoria": categoria,
            "confianza": float(conf),
            "caja": [x1, y1, x2, y2],
            "area": int(mascaras[i].sum()) if mascaras[i] is not None else (x2 - x1) * (y2 - y1),
            "mascara": mascaras[i],
        })
    return vehiculos


def _caja_contiene(vehiculo, punto):
    x1, y1, x2, y2 = vehiculo["caja"]
    return x1 <= punto[0] <= x2 and y1 <= punto[1] <= y2


def _mascara_contiene(vehiculo, punto):
    m = vehiculo.get("mascara")
    if m is None:
        return False
    x, y = int(punto[0]), int(punto[1])
    return 0 <= y < m.shape[0] and 0 <= x < m.shape[1] and bool(m[y, x])


def elegir_vehiculo(vehiculos, placas):
    """Vehículo principal: el que contiene el centro de la placa mejor leída por M1; si no hay placa o ningún
    vehículo la contiene, el de mayor área de máscara. Si varios contienen el centro, gana el que lo tiene dentro
    de la máscara y, a igualdad, el de mayor área."""
    if not vehiculos:
        return None
    if placas:
        x1, y1, x2, y2 = placas[0]["caja"]
        centro = ((x1 + x2) / 2, (y1 + y2) / 2)
        candidatos = [v for v in vehiculos if _caja_contiene(v, centro)]
        if candidatos:
            return max(candidatos, key=lambda v: (_mascara_contiene(v, centro), v["area"]))
    return max(vehiculos, key=lambda v: v["area"])


def _anotar(imagen, placas, vehiculo=None):
    if vehiculo is not None:
        color = (40, 40, 200)
        m = vehiculo.get("mascara")
        if m is not None and m.shape == imagen.shape[:2]:
            imagen[m] = (imagen[m] * 0.6 + np.array(color) * 0.4).astype(np.uint8)
            contornos, _ = cv2.findContours(m.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            cv2.drawContours(imagen, contornos, -1, color, 2, cv2.LINE_AA)
        x1, y1, x2, y2 = vehiculo["caja"]
        cv2.rectangle(imagen, (x1, y1), (x2, y2), color, 2)
        etiqueta = f"{vehiculo['categoria']} {vehiculo['confianza'] * 100:.0f}%"
        (ancho, alto), _ = cv2.getTextSize(etiqueta, cv2.FONT_HERSHEY_SIMPLEX, 0.8, 2)
        y_txt = max(alto + 8, y1)
        cv2.rectangle(imagen, (x1, y_txt - alto - 8), (x1 + ancho + 8, y_txt), color, -1)
        cv2.putText(imagen, etiqueta, (x1 + 4, y_txt - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2,
                    cv2.LINE_AA)
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
            "m2_segmentacion": _m2_estado,
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
        vehiculo = elegir_vehiculo(detectar_vehiculos(img), placas)
    except Exception:  # noqa: BLE001
        _contador["errores"] += 1
        log.exception("Fallo en la inferencia")
        raise HTTPException(500, "Error interno en la inferencia.")
    _contador["inferencias"] += 1
    ms = round((time.perf_counter() - t0) * 1000)
    log.info("inferir %dx%d → %s · %s en %d ms", img.shape[1], img.shape[0],
             [p["texto"] for p in placas] or "-", vehiculo["categoria"] if vehiculo else "-", ms)
    respuesta = {
        "placas": placas,
        "categoria": vehiculo["categoria"] if vehiculo else None,
        "confianza_categoria": round(vehiculo["confianza"] * 100, 1) if vehiculo else None,
        "caja_vehiculo": vehiculo["caja"] if vehiculo else None,
        "ejes": None,  # M3 pendiente: nunca se inventa
        "milisegundos": ms,
    }
    if anotada:
        respuesta["imagen"] = _anotar(img, placas, vehiculo)
    return respuesta
