"""PWA instalable (y APK con PWABuilder / Trusted Web Activity): manifest, service worker, página sin conexión y
Digital Asset Links. Todo público: el navegador y Android los piden sin sesión."""
import json
import logging

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.templatetags.static import static
from django.views.decorators.cache import cache_control
from django.views.decorators.http import require_GET

log = logging.getLogger(__name__)

AZUL_NOCHE = "#050b1e"
VERSION_SW = "iap-v1"  # cambiarla invalida el caché de estáticos de todos los clientes


@require_GET
@cache_control(max_age=3600, public=True)
def manifest(request):
    datos = {
        "id": "/caseta",
        "name": "Peaje IAP",
        "short_name": "Peaje IAP",
        "description": "Sistema inteligente de control de peaje",
        "lang": "es",
        "start_url": "/caseta",
        "scope": "/",
        "display": "standalone",
        "orientation": "any",
        "background_color": AZUL_NOCHE,
        "theme_color": AZUL_NOCHE,
        "categories": ["business", "productivity"],
        "icons": [
            {"src": static("iconos/icono-192.png"), "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": static("iconos/icono-512.png"), "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": static("iconos/icono-512-maskable.png"), "sizes": "512x512", "type": "image/png",
             "purpose": "maskable"},
        ],
    }
    return HttpResponse(json.dumps(datos, ensure_ascii=False, indent=2), content_type="application/manifest+json")


@require_GET
def service_worker(request):
    """Servido desde la raíz para que su alcance sea todo el sitio. Sin caché HTTP: así siempre se usa el último."""
    respuesta = render(request, "peaje/service-worker.js", {
        "version": VERSION_SW,
        "precache": json.dumps([static("css/iap.css"), static("iconos/icono-192.png"), "/offline"]),
    }, content_type="application/javascript; charset=utf-8")
    respuesta["Service-Worker-Allowed"] = "/"
    respuesta["Cache-Control"] = "no-cache"
    return respuesta


@require_GET
def offline(request):
    """Página sin conexión: no extiende base.html para no guardar en caché datos del usuario ni del menú."""
    return render(request, "peaje/offline.html")


@require_GET
def assetlinks(request):
    """Digital Asset Links para la TWA: el contenido de ASSETLINKS_JSON (el que genera PWABuilder), o []."""
    texto = (getattr(settings, "ASSETLINKS_JSON", "") or "").strip()
    datos = []
    if texto:
        try:
            datos = json.loads(texto)
        except ValueError:
            log.warning("ASSETLINKS_JSON no es un JSON válido; se responde []")
    return JsonResponse(datos, safe=False)
