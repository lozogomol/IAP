"""PWA / APK (TWA): manifest, service worker, página sin conexión, assetlinks e íconos. Todo sin login."""
import json
import struct
from pathlib import Path

from django.conf import settings
from django.test import TestCase, override_settings

from .models import Usuario
from .tests import entrar

ICONOS = Path(settings.BASE_DIR) / "static" / "iconos"


def _tamano_png(ruta):
    with open(ruta, "rb") as f:
        cabecera = f.read(24)
    assert cabecera[:8] == b"\x89PNG\r\n\x1a\n", ruta
    return struct.unpack(">II", cabecera[16:24])


class ManifestTests(TestCase):
    def test_manifest_sin_login(self):
        r = self.client.get("/manifest.webmanifest")
        self.assertEqual((r.status_code, r["Content-Type"]), (200, "application/manifest+json"))
        m = json.loads(r.content)
        self.assertEqual((m["name"], m["short_name"], m["description"]),
                         ("Peaje IAP", "Peaje IAP", "Sistema inteligente de control de peaje"))
        self.assertEqual((m["start_url"], m["scope"], m["display"], m["orientation"]),
                         ("/caseta", "/", "standalone", "any"))
        self.assertEqual((m["background_color"], m["theme_color"]), ("#050b1e", "#050b1e"))
        iconos = {(i["sizes"], i["purpose"]): i for i in m["icons"]}
        self.assertEqual(set(iconos), {("192x192", "any"), ("512x512", "any"), ("512x512", "maskable")})
        for icono in m["icons"]:
            self.assertEqual(icono["type"], "image/png")
            self.assertTrue(icono["src"].startswith("/static/iconos/"), icono["src"])

    def test_iconos_png_con_su_tamano(self):
        for nombre, tamano in (("icono-192.png", (192, 192)), ("icono-512.png", (512, 512)),
                               ("icono-512-maskable.png", (512, 512)), ("apple-touch-icon.png", (180, 180))):
            self.assertEqual(_tamano_png(ICONOS / nombre), tamano, nombre)


class ServiceWorkerTests(TestCase):
    def test_desde_la_raiz_sin_login(self):
        r = self.client.get("/service-worker.js")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r["Content-Type"].startswith("application/javascript"))
        self.assertEqual(r["Service-Worker-Allowed"], "/")
        self.assertEqual(r["Cache-Control"], "no-cache")

    def test_solo_estaticos_y_nunca_api_login_ni_paginas(self):
        js = self.client.get("/service-worker.js").content.decode()
        for regla in (r"/^\/api\//", r"/^\/auth\//", r"/^\/login/", r"/^\/logout/"):
            self.assertIn(regla, js)
        self.assertIn('url.pathname.indexOf("/static/") === 0', js)   # solo /static/ (y fuentes) al caché
        self.assertIn("function redPrimero", js)                       # network first
        self.assertIn('if (pedido.method !== "GET") return;', js)      # POST nunca pasa por el caché
        self.assertIn('caches.match("/offline")', js)
        precache = json.loads(js.split("var PRECACHE = ", 1)[1].split(";", 1)[0])
        self.assertIn("/offline", precache)
        self.assertTrue(all(p.startswith("/static/") or p == "/offline" for p in precache))

    def test_pagina_sin_conexion(self):
        r = self.client.get("/offline")
        self.assertContains(r, "Sin conexión. Revisa tu internet.")
        self.assertNotContains(r, "usuario-menu")  # no incluye datos del usuario ni el menú


class AssetLinksTests(TestCase):
    @override_settings(ASSETLINKS_JSON="")
    def test_vacio_devuelve_lista_vacia(self):
        r = self.client.get("/.well-known/assetlinks.json")
        self.assertEqual((r.status_code, r["Content-Type"], json.loads(r.content)), (200, "application/json", []))

    def test_devuelve_el_contenido_de_la_variable(self):
        datos = [{"relation": ["delegate_permission/common.handle_all_urls"],
                  "target": {"namespace": "android_app", "package_name": "com.iap.peaje",
                             "sha256_cert_fingerprints": ["AB:CD"]}}]
        with override_settings(ASSETLINKS_JSON=json.dumps(datos)):
            r = self.client.get("/.well-known/assetlinks.json")
        self.assertEqual((r.status_code, r["Content-Type"], json.loads(r.content)), (200, "application/json", datos))

    @override_settings(ASSETLINKS_JSON="{no es json")
    def test_json_invalido_devuelve_lista_vacia(self):
        with self.assertLogs("peaje.pwa", level="WARNING"):
            r = self.client.get("/.well-known/assetlinks.json")
        self.assertEqual(json.loads(r.content), [])


class BaseHtmlTests(TestCase):
    def test_etiquetas_pwa_y_registro_del_service_worker(self):
        for cliente_logueado in (False, True):
            if cliente_logueado:
                entrar(self.client, Usuario.objects.get(email_google="lozogomol@gmail.com"))
            r = self.client.get("/caseta" if cliente_logueado else "/login")
            self.assertContains(r, '<link rel="manifest" href="/manifest.webmanifest">', html=False)
            self.assertContains(r, '<meta name="theme-color" content="#050b1e">', html=False)
            self.assertContains(r, 'rel="apple-touch-icon"')
            self.assertContains(r, 'name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"')
            self.assertContains(r, 'navigator.serviceWorker.register("/service-worker.js", { scope: "/" })')

    def test_login_con_google_identity_services(self):
        r = self.client.get("/login")
        self.assertContains(r, "https://accounts.google.com/gsi/client")  # botón GIS, no WebView

    def test_camara_pide_permiso_tambien_dentro_de_la_app(self):
        entrar(self.client, Usuario.objects.get(email_google="lozogomol@gmail.com"))
        r = self.client.get("/caseta")
        self.assertContains(r, "navigator.mediaDevices.getUserMedia")
        self.assertContains(r, "Ajustes del teléfono > Apps > Peaje IAP (o Chrome) > Permisos > Cámara")
