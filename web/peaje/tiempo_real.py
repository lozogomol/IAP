"""Tiempo real con Pusher (canal privado) sin dependencias extra: el trigger se firma a mano (API REST v1.0).

Eventos en el canal `private-peaje`:
- `paso`: {"paso": {...}, "alerta": {...} | null, "kpis": {...}, "grafica": {...}}
- `pago`: {"paso": {...}, "alerta": {"id", "tipo", "nota"} | null, "kpis": {...}} (un paso pendiente se cobró en
  efectivo: al pasar el siguiente vehículo o al atender su alerta)
- `kpis`: {"kpis": {...}}
"""
import hashlib
import hmac
import json
import logging
import threading
import time

import requests
from django.conf import settings

log = logging.getLogger(__name__)
CANAL = "private-peaje"


def configurado():
    return bool(settings.PUSHER_APP_ID and settings.PUSHER_KEY and settings.PUSHER_SECRET)


def _firma(texto):
    return hmac.new(settings.PUSHER_SECRET.encode(), texto.encode(), hashlib.sha256).hexdigest()


def firmar_canal(socket_id, canal):
    """Respuesta de autorización de Pusher para un canal privado."""
    return {"auth": f"{settings.PUSHER_KEY}:{_firma(f'{socket_id}:{canal}')}"}


def _enviar(evento, datos):
    cuerpo = json.dumps({"name": evento, "channels": [CANAL], "data": json.dumps(datos, ensure_ascii=False)})
    ruta = f"/apps/{settings.PUSHER_APP_ID}/events"
    params = {
        "auth_key": settings.PUSHER_KEY,
        "auth_timestamp": str(int(time.time())),
        "auth_version": "1.0",
        "body_md5": hashlib.md5(cuerpo.encode()).hexdigest(),
    }
    consulta = "&".join(f"{k}={params[k]}" for k in sorted(params))
    params["auth_signature"] = _firma(f"POST\n{ruta}\n{consulta}")
    try:
        r = requests.post(f"https://api-{settings.PUSHER_CLUSTER}.pusher.com{ruta}", params=params, data=cuerpo,
                          headers={"Content-Type": "application/json"}, timeout=5)
        r.raise_for_status()
    except requests.RequestException as e:
        log.warning("No se pudo notificar a Pusher (%s): %s", evento, e)


def en_segundo_plano(funcion, *args):
    """Ejecuta trabajo de red sin demorar la respuesta (aislado para poder reemplazarlo en los tests)."""
    threading.Thread(target=funcion, args=args, daemon=True).start()


def notificar(evento, datos):
    if configurado():
        en_segundo_plano(_enviar, evento, datos)


def config_pusher(request):
    """Context processor: clave pública y cluster para pusher-js (solo si está configurado)."""
    if not configurado():
        return {"pusher": None}
    return {"pusher": {"key": settings.PUSHER_KEY, "cluster": settings.PUSHER_CLUSTER, "canal": CANAL}}
