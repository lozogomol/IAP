"""Diagnóstico del entorno IAP: plataforma, intérprete, entorno virtual y versiones de librerías.

Uso (desde cualquier carpeta):  python scripts/diagnostico.py
Imprime el resumen y lo guarda en diagnostico_<plataforma>.json (carpeta actual).
Si una librería no está instalada lo indica, sin fallar.
"""
import importlib
import json
import os
import platform
import sys
from datetime import datetime


def detectar_plataforma():
    if "google.colab" in sys.modules or os.environ.get("COLAB_RELEASE_TAG") or os.path.isdir("/content/sample_data"):
        return "colab"
    if os.environ.get("RENDER") or os.environ.get("RENDER_SERVICE_ID"):
        return "render"
    if os.environ.get("RUNPOD_POD_ID") or os.environ.get("RUNPOD_API_KEY"):
        return "runpod"
    return "local"


def entorno_virtual():
    """(activo, tipo, nombre): venv/virtualenv o conda."""
    if sys.prefix != getattr(sys, "base_prefix", sys.prefix) or os.environ.get("VIRTUAL_ENV"):
        return True, "venv", os.environ.get("VIRTUAL_ENV", sys.prefix)
    conda = os.environ.get("CONDA_DEFAULT_ENV")
    if conda and conda != "base":
        return True, "conda", conda
    if conda == "base":
        return False, "conda (base)", conda
    return False, None, None


def version(modulo, atributo="__version__"):
    try:
        m = importlib.import_module(modulo)
        return str(getattr(m, atributo, "instalado (sin versión)"))
    except Exception:  # noqa: BLE001  (no instalada o falla al importar)
        return None


def info_torch():
    try:
        import torch
    except Exception:  # noqa: BLE001
        return {"torch": None, "cuda_disponible": None, "cuda_version": None, "gpu": None}
    cuda = bool(torch.cuda.is_available())
    return {
        "torch": torch.__version__,
        "cuda_disponible": cuda,
        "cuda_version": torch.version.cuda,
        "gpu": torch.cuda.get_device_name(0) if cuda else None,
    }


def main():
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:  # noqa: BLE001
        pass
    plataforma = detectar_plataforma()
    activo, tipo, nombre = entorno_virtual()
    datos = {
        "fecha": datetime.now().isoformat(timespec="seconds"),
        "plataforma": plataforma,
        "sistema": f"{platform.system()} {platform.release()} ({platform.machine()})",
        "python": platform.python_version(),
        "interprete": sys.executable,
        "entorno_virtual_activo": activo,
        "entorno_virtual_tipo": tipo,
        "entorno_virtual": nombre,
        **info_torch(),
        "ultralytics": version("ultralytics"),
        "onnxruntime": version("onnxruntime"),
        "opencv": version("cv2"),
        "numpy": version("numpy"),
        "django": version("django"),
        "fastapi": version("fastapi"),
    }

    print("=== Diagnóstico IAP ===")
    print(f"Servicio/plataforma: {plataforma}")
    print(f"Sistema: {datos['sistema']}")
    print(f"Python: {datos['python']}  ({datos['interprete']})")
    print(f"Entorno virtual activo: {activo}" + (f"  [{tipo}: {nombre}]" if tipo else ""))
    etiquetas = [
        ("torch", "torch"), ("cuda_disponible", "CUDA disponible"), ("cuda_version", "Versión CUDA"),
        ("gpu", "GPU"), ("ultralytics", "ultralytics"), ("onnxruntime", "onnxruntime"),
        ("opencv", "opencv"), ("numpy", "numpy"), ("django", "django"), ("fastapi", "fastapi"),
    ]
    for clave, etiqueta in etiquetas:
        valor = datos[clave]
        if valor is None:
            valor = "no instalado" if clave in ("torch", "ultralytics", "onnxruntime", "opencv",
                                                "numpy", "django", "fastapi") else "-"
        print(f"{etiqueta}: {valor}")

    archivo = f"diagnostico_{plataforma}.json"
    with open(archivo, "w", encoding="utf-8") as f:
        json.dump(datos, f, ensure_ascii=False, indent=2)
    print(f"Guardado en {os.path.abspath(archivo)}")


if __name__ == "__main__":
    main()
