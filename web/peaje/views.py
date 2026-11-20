import json
import time
import urllib.request

from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import render


def inicio(request):
    return render(request, "peaje/inicio.html")


def estado(request):
    """Indica si el endpoint de inferencia responde (página 'Estado' que pide la tarea)."""
    url = settings.INFERENCE_URL.rstrip("/")
    if not url:
        return JsonResponse({"web": "ok", "inferencia": "sin configurar (falta INFERENCE_URL)"})
    t0 = time.perf_counter()
    try:
        req = urllib.request.Request(url + "/salud", headers={"X-API-Token": settings.INFERENCE_TOKEN})
        with urllib.request.urlopen(req, timeout=5) as r:
            datos = json.loads(r.read() or b"{}")
        return JsonResponse({"web": "ok", "inferencia": "ok", "latencia_ms": round((time.perf_counter() - t0) * 1000),
                             "detalle": datos})
    except Exception as e:  # noqa: BLE001
        return JsonResponse({"web": "ok", "inferencia": "caida", "error": str(e)}, status=503)
