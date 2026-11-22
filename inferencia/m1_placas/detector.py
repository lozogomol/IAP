"""Detector de placas compartido por los tres entornos (YOLOv9-t en ONNX).

Cada script de lectura importa este archivo, así los tres usan las mismas cajas.
"""
import base64
import re
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

CARPETA = Path(__file__).parent / "modelo"
MODELO = CARPETA / "yolo-v9-t-384-license-plates-end2end.onnx"
LADO = 384
FORMATO_BOLIVIA = re.compile(r"^\d{3,4}[A-Z]{3}$")
_SESION = None


def sesion():
    """Sesión ONNX del detector, creada una sola vez (crearla en cada imagen cuesta cientos de ms)."""
    global _SESION
    if _SESION is None:
        _SESION = ort.InferenceSession(str(MODELO), providers=["CPUExecutionProvider"])
    return _SESION


def leer_imagen(ruta):
    imagen = cv2.imread(str(ruta))
    if imagen is None:
        raise SystemExit("No se pudo leer la imagen.")
    return imagen


def detectar(imagen, umbral=0.4):
    """Devuelve una lista de (x1, y1, x2, y2, confianza) en coordenadas de la imagen original."""
    alto, ancho = imagen.shape[:2]
    escala = min(LADO / alto, LADO / ancho)
    nuevo_ancho, nuevo_alto = round(ancho * escala), round(alto * escala)
    margen_x, margen_y = (LADO - nuevo_ancho) / 2, (LADO - nuevo_alto) / 2
    lienzo = cv2.resize(imagen, (nuevo_ancho, nuevo_alto), interpolation=cv2.INTER_LINEAR)
    lienzo = cv2.copyMakeBorder(
        lienzo, round(margen_y - 0.1), round(margen_y + 0.1), round(margen_x - 0.1), round(margen_x + 0.1),
        cv2.BORDER_CONSTANT, value=(114, 114, 114),
    )
    entrada = (lienzo[:, :, ::-1].transpose(2, 0, 1) / 255.0).astype(np.float32)[None]

    s = sesion()
    salida = s.run(None, {s.get_inputs()[0].name: entrada})[0]

    cajas = []
    for _, x1, y1, x2, y2, _, confianza in salida:
        if confianza < umbral:
            continue
        x1, x2 = [(v - margen_x) / escala for v in (x1, x2)]
        y1, y2 = [(v - margen_y) / escala for v in (y1, y2)]
        cajas.append((
            int(max(0, x1)), int(max(0, y1)), int(min(ancho, x2)), int(min(alto, y2)), float(confianza),
        ))
    return cajas


def limpiar(texto):
    return re.sub(r"[^A-Z0-9]", "", texto.upper())


def resultado(imagen, cajas, textos, confianzas_lectura):
    """Dibuja las cajas y arma la lista de placas para el JSON."""
    placas = []
    for (x1, y1, x2, y2, confianza), texto, lectura in zip(cajas, textos, confianzas_lectura):
        texto = limpiar(texto)
        cv2.rectangle(imagen, (x1, y1), (x2, y2), (0, 200, 0), 3)
        cv2.putText(imagen, texto or "?", (x1, max(25, y1 - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 200, 0), 2, cv2.LINE_AA)
        placas.append({
            "texto": texto,
            "formato_bolivia": bool(FORMATO_BOLIVIA.match(texto)),
            "confianza_deteccion": round(confianza * 100, 1),
            "confianza_lectura": None if lectura is None else round(float(lectura) * 100, 1),
        })
    anotada = base64.b64encode(cv2.imencode(".jpg", imagen, [cv2.IMWRITE_JPEG_QUALITY, 85])[1]).decode()
    return placas, "data:image/jpeg;base64," + anotada
