import json
import logging
import time
import urllib.request

from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .auth import (TokenGoogleInvalido, cerrar_sesion, iniciar_sesion, login_requerido, usuario_autorizado,
                   verificar_token_google)

log = logging.getLogger(__name__)


# --- Inicio de sesión ---------------------------------------------------------------------------

def _siguiente_seguro(request, url):
    if url and url_has_allowed_host_and_scheme(url, allowed_hosts={request.get_host()},
                                               require_https=request.is_secure()):
        return url
    return "/"


def login(request):
    if request.session.get("usuario_id"):
        return redirect(_siguiente_seguro(request, request.GET.get("next")))
    return render(request, "peaje/login.html", {
        "google_client_id": settings.GOOGLE_CLIENT_ID,
        "next": _siguiente_seguro(request, request.GET.get("next")),
    })


@require_POST
def auth_google_web(request):
    """Recibe el ID token de Google (fetch POST con CSRF) y abre la sesión si el email está en la lista blanca."""
    try:
        datos = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        datos = {}
    credential = datos.get("credential")
    if not credential:
        return JsonResponse({"ok": False, "error": "Falta el credential de Google."}, status=400)
    try:
        email = verificar_token_google(credential)
    except TokenGoogleInvalido as e:
        log.warning("Token de Google rechazado: %s", e)
        return JsonResponse({"ok": False, "error": "Token de Google inválido."}, status=401)

    usuario = usuario_autorizado(email)
    if usuario is None:
        request.session["email_rechazado"] = email
        return JsonResponse({"ok": False, "error": "Cuenta no autorizada.", "redirect": "/acceso-denegado"},
                            status=403)

    iniciar_sesion(request, usuario)
    return JsonResponse({"ok": True, "redirect": _siguiente_seguro(request, datos.get("next"))})


def acceso_denegado(request):
    email = request.session.pop("email_rechazado", "")
    return render(request, "peaje/acceso_denegado.html", {"email": email}, status=403)


def logout(request):
    cerrar_sesion(request)
    return redirect("login")


# --- Páginas protegidas (placeholders) ----------------------------------------------------------

def _placeholder(request, titulo, texto):
    return render(request, "peaje/placeholder.html", {"titulo": titulo, "texto": texto})


@login_requerido
def panel(request):
    return _placeholder(request, "Panel", "Aquí irá el panel en tiempo real de los pasos por las casetas.")


@login_requerido
def vehiculos(request):
    return _placeholder(request, "Vehículos", "Aquí irá el listado y la gestión de vehículos registrados.")


@login_requerido
def pasos(request):
    return _placeholder(request, "Pasos", "Aquí irá el historial de pasos por el peaje.")


@login_requerido
def alertas(request):
    return _placeholder(request, "Alertas", "Aquí irán las alertas de placas no registradas y saldo insuficiente.")


@login_requerido
def chat(request):
    return _placeholder(request, "Chat", "Aquí irá el agente conversacional.")


# --- Estado (público) ---------------------------------------------------------------------------

def _estado_inferencia():
    """(datos, status_http) del endpoint de inferencia."""
    url = settings.INFERENCE_URL.rstrip("/")
    if not url:
        return {"web": "ok", "inferencia": "sin configurar (falta INFERENCE_URL)"}, 200
    t0 = time.perf_counter()
    try:
        req = urllib.request.Request(url + "/salud", headers={"X-API-Token": settings.INFERENCE_TOKEN})
        with urllib.request.urlopen(req, timeout=5) as r:
            detalle = json.loads(r.read() or b"{}")
        return {"web": "ok", "inferencia": "ok", "latencia_ms": round((time.perf_counter() - t0) * 1000),
                "detalle": detalle}, 200
    except Exception as e:  # noqa: BLE001
        return {"web": "ok", "inferencia": "caida", "error": str(e)}, 503


def estado(request):
    """Página 'Estado'. Con ?format=json devuelve el JSON que consulta el cron."""
    datos, status = _estado_inferencia()
    if request.GET.get("format") == "json":
        return JsonResponse(datos, status=status)
    return render(request, "peaje/estado.html", {
        "datos": datos,
        "detalle_json": json.dumps(datos.get("detalle"), ensure_ascii=False, indent=2) if "detalle" in datos else "",
    }, status=status)
