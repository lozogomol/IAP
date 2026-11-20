"""Lee placas con fast-plate-ocr (entorno venv).

Uso: python placas_fastocr.py ruta/imagen.jpg
"""
import json
import sys
import time

import cv2
import fast_plate_ocr
from fast_plate_ocr import LicensePlateRecognizer

from detector import CARPETA, detectar, leer_imagen, resultado


def principal():
    if len(sys.argv) != 2:
        sys.exit("Uso: python placas_fastocr.py ruta/imagen.jpg")

    inicio = time.perf_counter()
    imagen = leer_imagen(sys.argv[1])
    cajas = detectar(imagen)
    lector = LicensePlateRecognizer(
        onnx_model_path=CARPETA / "cct_xs_v2_global.onnx",
        plate_config_path=CARPETA / "cct_xs_v2_global_plate_config.yaml",
    )

    textos, confianzas = [], []
    for x1, y1, x2, y2, _ in cajas:
        recorte = cv2.cvtColor(imagen[y1:y2, x1:x2], cv2.COLOR_BGR2RGB)
        prediccion = lector.run_one(recorte, return_confidence=True)
        textos.append(prediccion.plate)
        confianzas.append(float(prediccion.char_probs.mean()) if prediccion.char_probs is not None else None)

    placas, anotada = resultado(imagen, cajas, textos, confianzas)
    print(json.dumps({
        "lector": "fast-plate-ocr (red CCT en ONNX)",
        "placas": placas,
        "imagen": anotada,
        "python": sys.version.split()[0],
        "ejecutable": sys.executable,
        "libreria": f"fast-plate-ocr {fast_plate_ocr.__version__}" if hasattr(fast_plate_ocr, "__version__") else "fast-plate-ocr",
        "milisegundos": round((time.perf_counter() - inicio) * 1000),
    }))


if __name__ == "__main__":
    principal()
