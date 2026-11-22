import json
import logging
import time
import urllib.request

from django.conf import settings
from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .auth import (TokenGoogleInvalido, cerrar_sesion, datos_token_google, iniciar_sesion, login_requerido,
                   rol_requerido, usuario_autorizado)
from .models import Caseta, Usuario

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
        email, nombre = datos_token_google(credential)
    except TokenGoogleInvalido as e:
        log.warning("Token de Google rechazado: %s", e)
        return JsonResponse({"ok": False, "error": "Token de Google inválido."}, status=401)

    usuario = usuario_autorizado(email)
    if usuario is None:
        # Se guarda el email/nombre verificados por Google para poder "Solicitar acceso" sin confiar en el cliente.
        request.session["email_rechazado"] = email
        request.session["nombre_rechazado"] = nombre
        return JsonResponse({"ok": False, "error": "Cuenta no autorizada.", "redirect": "/acceso-denegado"},
                            status=403)

    iniciar_sesion(request, usuario)
    return JsonResponse({"ok": True, "redirect": _siguiente_seguro(request, datos.get("next"))})


def acceso_denegado(request):
    email = request.session.get("email_rechazado", "")
    existente = Usuario.objects.filter(email_google__iexact=email).first() if email else None
    return render(request, "peaje/acceso_denegado.html", {
        "email": email,
        "pendiente": bool(existente and existente.pendiente),
        "desactivado": bool(existente and not existente.pendiente and not existente.activo),
    }, status=403)


@require_POST
def solicitar_acceso(request):
    """Crea la solicitud (activo=False, rol operador) con el email/nombre que Google verificó en el login."""
    email = request.session.get("email_rechazado")
    if not email:
        messages.error(request, "Primero inicia sesión con Google para solicitar acceso.")
        return redirect("login")
    nombre = request.session.get("nombre_rechazado") or email.split("@")[0]
    existente = Usuario.objects.filter(email_google__iexact=email).first()
    if existente is None:
        Usuario.objects.create(email_google=email, nombre=nombre, rol="operador", activo=False, pendiente=True,
                               solicitado_at=timezone.now())
        messages.success(request, "Solicitud enviada. Un administrador debe aprobarla.")
    elif existente.pendiente:
        messages.info(request, "Ya tienes una solicitud pendiente. Espera a que un administrador la apruebe.")
    elif existente.activo:
        messages.info(request, "Tu cuenta ya está autorizada. Vuelve a iniciar sesión.")
    else:
        messages.warning(request, "Tu cuenta está desactivada o fue rechazada. Contacta a un administrador.")
    return redirect("acceso_denegado")


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
def caseta(request):
    return render(request, "peaje/caseta.html", {"casetas": Caseta.objects.all()})


@login_requerido
def consulta(request):
    return render(request, "peaje/consulta.html")


# --- Usuarios y solicitudes (solo admin) -----------------------------------------------------------

@rol_requerido("admin")
def usuarios(request):
    return render(request, "peaje/usuarios.html", {
        "pendientes": Usuario.objects.filter(pendiente=True).order_by("solicitado_at"),
        "usuarios": Usuario.objects.filter(pendiente=False),
        "roles": Usuario.ROLES,
    })


@require_POST
@rol_requerido("admin")
def usuario_accion(request, pk):
    usuario = get_object_or_404(Usuario, pk=pk)
    accion = request.POST.get("accion")
    propio = usuario.id == request.session.get("usuario_id")
    if accion == "aprobar" and usuario.pendiente:
        usuario.activo, usuario.pendiente = True, False
        usuario.save(update_fields=["activo", "pendiente"])
        messages.success(request, f"Acceso aprobado para {usuario.email_google}.")
    elif accion == "rechazar" and usuario.pendiente:
        # Queda inactivo y no pendiente: no puede volver a solicitar sin que un admin lo reactive.
        usuario.pendiente = False
        usuario.save(update_fields=["pendiente"])
        messages.info(request, f"Solicitud de {usuario.email_google} rechazada.")
    elif accion == "rol" and request.POST.get("rol") in dict(Usuario.ROLES) and not usuario.pendiente:
        if propio:
            messages.error(request, "No puedes cambiar tu propio rol.")
        else:
            usuario.rol = request.POST["rol"]
            usuario.save(update_fields=["rol"])
            messages.success(request, f"{usuario.nombre} ahora es {usuario.get_rol_display().lower()}.")
    elif accion in ("activar", "desactivar") and not usuario.pendiente:
        if propio:
            messages.error(request, "No puedes desactivarte a ti mismo.")
        else:
            usuario.activo = accion == "activar"
            usuario.save(update_fields=["activo"])
            messages.success(request, f"{usuario.nombre} {'activado' if usuario.activo else 'desactivado'}.")
    else:
        messages.error(request, "Acción no válida.")
    return redirect("usuarios")


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
