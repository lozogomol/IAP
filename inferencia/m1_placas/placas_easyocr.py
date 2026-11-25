"""Lee placas con EasyOCR (entorno mamba).

Uso: python placas_easyocr.py ruta/imagen.jpg
La primera ejecución descarga los modelos de EasyOCR en inferencia/modelos/easyocr (no se sube).
"""
import json
import sys
import time
import warnings

warnings.filterwarnings("ignore")

import easyocr  # noqa: E402

from detector import config, detectar, leer_imagen, resultado

PERMITIDOS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def principal():
    if len(sys.argv) != 2:
        sys.exit("Uso: python placas_easyocr.py ruta/imagen.jpg")

    inicio = time.perf_counter()
    imagen = leer_imagen(sys.argv[1])
    cajas = detectar(imagen)
    lector = easyocr.Reader(["en"], gpu=False, verbose=False,
                            model_storage_directory=str(config.MODELOS / "easyocr"))

    textos, confianzas = [], []
    for x1, y1, x2, y2, _ in cajas:
        lecturas = lector.readtext(imagen[y1:y2, x1:x2], allowlist=PERMITIDOS)
        # Conserva solo el texto más alto: los caracteres de la placa, no las leyendas pequeñas.
        if lecturas:
            altura = lambda l: max(p[1] for p in l[0]) - min(p[1] for p in l[0])
            maxima = max(altura(l) for l in lecturas)
            lecturas = [l for l in lecturas if altura(l) >= maxima * 0.6]
        lecturas.sort(key=lambda l: l[0][0][0])
        textos.append("".join(texto for _, texto, _ in lecturas))
        confianzas.append(sum(c for _, _, c in lecturas) / len(lecturas) if lecturas else None)

    placas, anotada = resultado(imagen, cajas, textos, confianzas)
    print(json.dumps({
        "lector": "EasyOCR (red CRNN en PyTorch)",
        "placas": placas,
        "imagen": anotada,
        "python": sys.version.split()[0],
        "ejecutable": sys.executable,
        "libreria": f"EasyOCR {easyocr.__version__}",
        "milisegundos": round((time.perf_counter() - inicio) * 1000),
    }))


if __name__ == "__main__":
    principal()
