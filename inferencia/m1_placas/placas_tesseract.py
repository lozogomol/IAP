"""Lee placas con Tesseract (entorno conda).

Uso: python placas_tesseract.py ruta/imagen.jpg
"""
import json
import os
import sys
import time
from pathlib import Path

import cv2
import pytesseract

from detector import detectar, leer_imagen, resultado

# Laravel ejecuta este Python sin activar el entorno: se indica dónde está tesseract.
PREFIJO = Path(sys.prefix)
if os.name == "nt":
    pytesseract.pytesseract.tesseract_cmd = str(PREFIJO / "Library" / "bin" / "tesseract.exe")
    carpetas = [PREFIJO / "Library" / "share" / "tessdata", PREFIJO / "share" / "tessdata"]
else:
    pytesseract.pytesseract.tesseract_cmd = str(PREFIJO / "bin" / "tesseract")
    carpetas = [PREFIJO / "share" / "tessdata"]
for carpeta in carpetas:
    if carpeta.is_dir():
        os.environ["TESSDATA_PREFIX"] = str(carpeta)
        break

# psm 11 busca texto disperso: sirve para placas con varias líneas.
CONFIGURACION = "--psm 11 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def preparar(recorte):
    """Ampliación y binarización para ayudar a Tesseract.

    Se usa el canal de mayor valor de cada píxel: los caracteres negros quedan oscuros
    y los fondos de color (azul, amarillo, blanco) quedan claros.
    """
    brillo = recorte.max(axis=2)
    brillo = cv2.resize(brillo, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    brillo = cv2.bilateralFilter(brillo, 9, 75, 75)
    _, binaria = cv2.threshold(brillo, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return binaria


def caracteres_principales(datos):
    """Conserva solo el texto más alto: los caracteres de la placa, no las leyendas pequeñas."""
    palabras = [(datos["text"][i].strip(), datos["height"][i], float(datos["conf"][i]))
                for i in range(len(datos["text"])) if datos["text"][i].strip()]
    if not palabras:
        return "", None
    altura = max(alto for _, alto, _ in palabras)
    elegidas = [(texto, conf) for texto, alto, conf in palabras if alto >= altura * 0.6]
    confianzas = [conf for _, conf in elegidas if conf > 0]
    return "".join(texto for texto, _ in elegidas), (sum(confianzas) / len(confianzas) / 100 if confianzas else None)


def principal():
    if len(sys.argv) != 2:
        sys.exit("Uso: python placas_tesseract.py ruta/imagen.jpg")

    inicio = time.perf_counter()
    imagen = leer_imagen(sys.argv[1])
    cajas = detectar(imagen)

    textos, confianzas = [], []
    for x1, y1, x2, y2, _ in cajas:
        datos = pytesseract.image_to_data(preparar(imagen[y1:y2, x1:x2]), lang="eng", config=CONFIGURACION,
                                          output_type=pytesseract.Output.DICT)
        texto, confianza = caracteres_principales(datos)
        textos.append(texto)
        confianzas.append(confianza)

    placas, anotada = resultado(imagen, cajas, textos, confianzas)
    print(json.dumps({
        "lector": "Tesseract OCR",
        "placas": placas,
        "imagen": anotada,
        "python": sys.version.split()[0],
        "ejecutable": sys.executable,
        "libreria": f"Tesseract {pytesseract.get_tesseract_version()}",
        "milisegundos": round((time.perf_counter() - inicio) * 1000),
    }))


if __name__ == "__main__":
    principal()
