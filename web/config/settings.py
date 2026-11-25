"""Configuración de Django. Todo lo sensible se lee de variables de entorno (.env en local, Environment en Render)."""
import os
import sys
from pathlib import Path

import dj_database_url
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR.parent / ".env")  # en local; en Render no existe y se usan sus variables

SECRET_KEY = os.environ.get("SECRET_KEY", "solo-para-desarrollo-cambiar")
DEBUG = os.environ.get("DEBUG", "False").lower() == "true"

ALLOWED_HOSTS = [h.strip() for h in os.environ.get("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()]
RENDER_HOST = os.environ.get("RENDER_EXTERNAL_HOSTNAME")  # Render la define sola
if RENDER_HOST:
    ALLOWED_HOSTS.append(RENDER_HOST)
CSRF_TRUSTED_ORIGINS = [f"https://{h}" for h in ALLOWED_HOSTS if h not in ("localhost", "127.0.0.1")]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "drf_spectacular",
    "peaje",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
        "peaje.auth.usuario_actual",
        "peaje.tiempo_real.config_pusher",
    ]},
}]
WSGI_APPLICATION = "config.wsgi.application"

# Base de datos: DATABASE_URL (Supabase o Render Postgres). Sin ella, SQLite solo para desarrollo.
DATABASES = {
    "default": dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        conn_max_age=600,
    )
}

LANGUAGE_CODE = "es"
TIME_ZONE = "America/La_Paz"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
TESTING = sys.argv[1:2] == ["test"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    # En los tests no hay collectstatic (ni manifest): se usa el almacenamiento simple.
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage" if TESTING
                    else "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True

# Endpoint de inferencia (RunPod o Colab): cambiar de proveedor = cambiar solo esta variable
INFERENCE_URL = os.environ.get("INFERENCE_URL", "")
INFERENCE_TOKEN = os.environ.get("INFERENCE_TOKEN", "")

# --- Inicio de sesión exclusivamente con Google (lista blanca en la tabla usuarios) ---
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
# Sin esto el popup de Google Identity Services no puede comunicarse con la página y el login falla.
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin-allow-popups"
LOGIN_URL = "/login"

# JWT propio para la app móvil (firmado con HS256)
JWT_SECRET = os.environ.get("JWT_SECRET", SECRET_KEY)
JWT_HORAS = int(os.environ.get("JWT_HORAS", "12"))

# Frames de la cámara (modo caseta) y fotos de consulta
DATA_UPLOAD_MAX_MEMORY_SIZE = 6 * 1024 * 1024
IMAGEN_MAX_BYTES = 5 * 1024 * 1024

REST_FRAMEWORK = {
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_AUTHENTICATION_CLASSES": ["peaje.auth.JWTAutenticacion", "peaje.auth.SesionAutenticacion"],
    # Todo exige autenticación; las rutas públicas usan @permission_classes([AllowAny]).
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "UNAUTHENTICATED_USER": None,
}
SPECTACULAR_SETTINGS = {
    "TITLE": "IAP - API del peaje",
    "DESCRIPTION": "API para la app móvil. Autenticación: Authorization: Bearer <JWT>.",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
}

# --- Tiempo real (Pusher). Sin estas variables el panel se actualiza consultando cada pocos segundos. ---
PUSHER_APP_ID = os.environ.get("PUSHER_APP_ID", "")
PUSHER_KEY = os.environ.get("PUSHER_KEY", "")
PUSHER_SECRET = os.environ.get("PUSHER_SECRET", "")
PUSHER_CLUSTER = os.environ.get("PUSHER_CLUSTER", "us2")

# --- Agente conversacional (Gemini). Sin clave responde el agente por reglas. ---
# En los tests la clave se vacía: nunca llaman a la API real (los tests que la necesitan la mockean).
GEMINI_API_KEY = "" if "test" in sys.argv else os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "")  # vacío = el "flash" por defecto de peaje/servicios/agente.py
# Si el principal está saturado o falla; uno o varios separados por comas (vacío = respaldo por defecto del agente).
GEMINI_MODEL_RESPALDO = os.environ.get("GEMINI_MODEL_RESPALDO", "")

# --- Capturas de los pasos: Cloudinary si CLOUDINARY_URL (cloudinary://key:secret@nube); si no, disco local. ---
CLOUDINARY_URL = os.environ.get("CLOUDINARY_URL", "")
MEDIA_ROOT = BASE_DIR / "media"

# --- Log en la consola (Render la muestra), también con DEBUG=False ---------------------------------------------
# django.request: los 500 con su traceback completo (sin esto, con DEBUG=False Django solo los manda por correo a
# ADMINS y en la consola no aparece nada). peaje: cada frame del modo caseta con su acción, y sus errores.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"simple": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"}},
    "handlers": {"consola": {"class": "logging.StreamHandler", "formatter": "simple"}},
    "loggers": {
        "django.request": {"handlers": ["consola"], "level": "ERROR", "propagate": False},
        # En los tests solo WARNING para no ensuciar la salida (assertLogs sigue funcionando).
        "peaje": {"handlers": ["consola"],
                  "level": "WARNING" if "test" in sys.argv else os.environ.get("IAP_LOG_NIVEL", "INFO"),
                  "propagate": False},
    },
}
