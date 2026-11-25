"""Tiempo real con Pusher (canal privado) sin dependencias extra: el trigger se firma a mano (API REST v1.0).

Eventos en el canal `private-peaje`:
- `paso`: {"paso": {...}, "alerta": {...} | null, "kpis": {...}, "grafica": {...}}
- `pago`: {"paso": {...}, "alerta": {"id", "tipo", "nota"} | null, "kpis": {...}} (un paso pendiente se cobró en
  efectivo: al salir el vehículo, al cerrar el punto de cobro o al atender su alerta)
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
from django.core.cache import cache

log = logging.getLogger(__name__)
CANAL = "private-peaje"
VARIABLES = ("PUSHER_APP_ID", "PUSHER_KEY", "PUSHER_SECRET", "PUSHER_CLUSTER")
VIGENCIA_ESTADO = 600  # s: la verificación de credenciales se cachea 10 min (la página Estado la consulta un cron)


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


def faltantes():
    """Variables PUSHER_* vacías en el servidor."""
    return [v for v in VARIABLES if not getattr(settings, v, "")]


def _verificar_credenciales():
    """GET firmado a la API REST de Pusher (lista de canales): comprueba app_id, key, secret y cluster."""
    ruta = f"/apps/{settings.PUSHER_APP_ID}/channels"
    params = {"auth_key": settings.PUSHER_KEY, "auth_timestamp": str(int(time.time())), "auth_version": "1.0"}
    consulta = "&".join(f"{k}={params[k]}" for k in sorted(params))
    params["auth_signature"] = _firma(f"GET\n{ruta}\n{consulta}")
    try:
        r = requests.get(f"https://api-{settings.PUSHER_CLUSTER}.pusher.com{ruta}", params=params, timeout=4)
    except requests.RequestException as e:
        return {"modo": "sondeo", "motivo": f"no se pudo contactar a Pusher ({type(e).__name__})"}
    if r.status_code == 200:
        return {"modo": "pusher", "motivo": None}
    if r.status_code in (401, 403):
        return {"modo": "sondeo", "motivo": f"Pusher rechazó las credenciales ({r.status_code}): revisa PUSHER_*"}
    if r.status_code == 404:
        return {"modo": "sondeo", "motivo": "la app de Pusher no existe (404): revisa PUSHER_APP_ID y PUSHER_CLUSTER"}
    return {"modo": "sondeo", "motivo": f"Pusher respondió {r.status_code}"}


def estado():
    """{"modo": "pusher" | "sondeo", "motivo"} para la página Estado. Sin variables: sondeo cada 10 s (respaldo)."""
    falta = faltantes()
    if falta:
        return {"modo": "sondeo", "motivo": "faltan variables PUSHER_* en el servidor: " + ", ".join(falta)}
    clave = f"tiempo_real:estado:{settings.PUSHER_APP_ID}:{settings.PUSHER_KEY}:{settings.PUSHER_CLUSTER}"
    resultado = cache.get(clave)
    if resultado is None:
        resultado = _verificar_credenciales()
        cache.set(clave, resultado, VIGENCIA_ESTADO)
    return resultado


def config_pusher(request):
    """Context processor: clave pública y cluster para pusher-js (solo si está configurado)."""
    if not configurado():
        return {"pusher": None}
    return {"pusher": {"key": settings.PUSHER_KEY, "cluster": settings.PUSHER_CLUSTER, "canal": CANAL}}
