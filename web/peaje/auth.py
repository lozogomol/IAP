"""Autenticación de IAP: exclusivamente con Google, contra la lista blanca de la tabla `usuarios`.

- Web: sesión de Django (sin django.contrib.auth users). Claves: usuario_id, nombre, email, rol.
- API móvil: JWT propio (HS256) en la cabecera `Authorization: Bearer <token>`.
"""
from datetime import datetime, timedelta, timezone
from functools import wraps
from urllib.parse import urlencode

import jwt
from django.conf import settings
from django.http import HttpResponseForbidden, HttpResponseRedirect
from drf_spectacular.extensions import OpenApiAuthenticationExtension
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from rest_framework import authentication, exceptions

from .models import Usuario

SESION_CLAVES = ("usuario_id", "nombre", "email", "rol")


# --- Google -----------------------------------------------------------------------------------

class TokenGoogleInvalido(Exception):
    pass


def verificar_token_google(credential):
    """Verifica el ID token de Google (firma, expiración, emisor y audience) y devuelve el email."""
    return datos_token_google(credential)[0]


def datos_token_google(credential):
    """Como verificar_token_google, pero devuelve (email, nombre). El nombre cae al usuario del email."""
    if not settings.GOOGLE_CLIENT_ID:
        raise TokenGoogleInvalido("GOOGLE_CLIENT_ID no está configurado")
    try:
        info = id_token.verify_oauth2_token(credential, google_requests.Request(), audience=settings.GOOGLE_CLIENT_ID)
    except ValueError as e:
        raise TokenGoogleInvalido(str(e)) from e
    email = (info.get("email") or "").strip().lower()
    if not email or not info.get("email_verified", False):
        raise TokenGoogleInvalido("La cuenta de Google no tiene un email verificado")
    nombre = (info.get("name") or "").strip()[:120] or email.split("@")[0]
    return email, nombre


def usuario_autorizado(email):
    """Usuario activo de la lista blanca con ese email, o None."""
    return Usuario.objects.filter(email_google__iexact=email, activo=True).first()


# --- Sesión web -------------------------------------------------------------------------------

def iniciar_sesion(request, usuario):
    request.session.cycle_key()  # evita fijación de sesión
    for clave in ("email_rechazado", "nombre_rechazado"):
        request.session.pop(clave, None)
    request.session.update({
        "usuario_id": usuario.id,
        "nombre": usuario.nombre,
        "email": usuario.email_google,
        "rol": usuario.rol,
    })


def cerrar_sesion(request):
    request.session.flush()


def usuario_actual(request):
    """Context processor: `usuario_sesion` en todas las plantillas (None si no hay sesión)."""
    if request.session.get("usuario_id"):
        return {"usuario_sesion": {k: request.session.get(k) for k in SESION_CLAVES}}
    return {"usuario_sesion": None}


def refrescar_sesion(request):
    """Revalida la sesión contra la BD: si el usuario fue desactivado la cierra; si cambió su rol lo actualiza.

    Devuelve el Usuario activo o None.
    """
    sesion = getattr(request, "session", None)
    uid = sesion.get("usuario_id") if sesion is not None else None
    if not uid:
        return None
    usuario = Usuario.objects.filter(pk=uid, activo=True).first()
    if usuario is None:
        cerrar_sesion(request)
        return None
    if request.session.get("rol") != usuario.rol or request.session.get("nombre") != usuario.nombre:
        request.session.update({"rol": usuario.rol, "nombre": usuario.nombre})
    return usuario


def login_requerido(vista):
    @wraps(vista)
    def envoltura(request, *args, **kwargs):
        if refrescar_sesion(request) is None:
            return HttpResponseRedirect(f"{settings.LOGIN_URL}?{urlencode({'next': request.get_full_path()})}")
        return vista(request, *args, **kwargs)
    return envoltura


def rol_requerido(*roles):
    """Uso: @rol_requerido("admin"). Implica login_requerido."""
    def decorador(vista):
        @wraps(vista)
        @login_requerido
        def envoltura(request, *args, **kwargs):
            if request.session.get("rol") not in roles:
                return HttpResponseForbidden("No tienes permisos para ver esta página.")
            return vista(request, *args, **kwargs)
        return envoltura
    return decorador


# --- JWT para la API --------------------------------------------------------------------------

def emitir_jwt(usuario):
    ahora = datetime.now(timezone.utc)
    payload = {
        "sub": str(usuario.id),
        "email": usuario.email_google,
        "rol": usuario.rol,
        "iat": ahora,
        "exp": ahora + timedelta(hours=settings.JWT_HORAS),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm="HS256")


class JWTAutenticacion(authentication.BaseAuthentication):
    """`Authorization: Bearer <jwt>`. request.user = Usuario (activo) de la lista blanca."""

    palabra = "Bearer"

    def authenticate(self, request):
        partes = authentication.get_authorization_header(request).split()
        if not partes or partes[0].lower() != self.palabra.lower().encode():
            return None
        if len(partes) != 2:
            raise exceptions.AuthenticationFailed("Cabecera Authorization mal formada.")
        try:
            payload = jwt.decode(partes[1].decode(), settings.JWT_SECRET, algorithms=["HS256"],
                                 options={"require": ["sub", "exp"]})
        except jwt.ExpiredSignatureError:
            raise exceptions.AuthenticationFailed("Token expirado.")
        except jwt.InvalidTokenError:
            raise exceptions.AuthenticationFailed("Token inválido.")
        usuario = Usuario.objects.filter(pk=payload["sub"], activo=True).first()
        if usuario is None:
            raise exceptions.AuthenticationFailed("Usuario no autorizado o desactivado.")
        usuario.is_authenticated = True  # lo que espera IsAuthenticated
        return usuario, payload

    def authenticate_header(self, request):
        return self.palabra  # hace que DRF responda 401 (no 403) sin token


class SesionAutenticacion(authentication.SessionAuthentication):
    """Sesión web de IAP (la del login con Google) para usar la API desde el navegador. Exige CSRF."""

    def authenticate(self, request):
        usuario = refrescar_sesion(request._request)
        if usuario is None:
            return None
        self.enforce_csrf(request)
        usuario.is_authenticated = True
        return usuario, None


class SesionAutenticacionEsquema(OpenApiAuthenticationExtension):
    target_class = "peaje.auth.SesionAutenticacion"
    name = "sesionWeb"

    def get_security_definition(self, auto_schema):
        return {"type": "apiKey", "in": "cookie", "name": settings.SESSION_COOKIE_NAME}


class JWTAutenticacionEsquema(OpenApiAuthenticationExtension):
    target_class = "peaje.auth.JWTAutenticacion"
    name = "jwtAuth"

    def get_security_definition(self, auto_schema):
        return {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}
