"""Tiempo real: Pusher cuando está configurado y funciona; sondeo de 10 s solo como respaldo; diagnóstico en Estado."""
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings

from . import panel, tiempo_real
from .models import Usuario
from .tests import entrar

PUSHER = dict(PUSHER_APP_ID="123", PUSHER_KEY="clave", PUSHER_SECRET="secreto", PUSHER_CLUSTER="us2")
SIN_PUSHER = dict(PUSHER_APP_ID="", PUSHER_KEY="", PUSHER_SECRET="", PUSHER_CLUSTER="us2")


class EstadoTiempoRealTests(TestCase):
    def setUp(self):
        cache.clear()

    def _estado(self, formato=""):
        with override_settings(INFERENCE_URL=""):
            return self.client.get("/estado" + formato)

    @override_settings(**SIN_PUSHER)
    def test_sin_variables_respaldo_por_sondeo_con_motivo(self):
        r = self._estado()
        self.assertContains(r, "Tiempo real: respaldo por sondeo (faltan variables PUSHER_* en el servidor: "
                               "PUSHER_APP_ID, PUSHER_KEY, PUSHER_SECRET)")
        d = self._estado("?format=json").json()["tiempo_real"]
        self.assertEqual(d["modo"], "sondeo")

    @override_settings(**PUSHER)
    def test_con_credenciales_validas_pusher_conectado(self):
        with mock.patch("peaje.tiempo_real.requests.get", return_value=mock.Mock(status_code=200)) as get:
            r = self._estado()
            self._estado()
        self.assertContains(r, "Tiempo real: Pusher conectado")
        get.assert_called_once()  # cacheado 10 min
        url, params = get.call_args.args[0], get.call_args.kwargs["params"]
        self.assertEqual(url, "https://api-us2.pusher.com/apps/123/channels")
        firma = tiempo_real._firma(f"GET\n/apps/123/channels\nauth_key=clave&auth_timestamp={params['auth_timestamp']}"
                                   "&auth_version=1.0")
        self.assertEqual(params["auth_signature"], firma)

    @override_settings(**PUSHER)
    def test_credenciales_rechazadas(self):
        with mock.patch("peaje.tiempo_real.requests.get", return_value=mock.Mock(status_code=401)):
            self.assertContains(self._estado(), "Tiempo real: respaldo por sondeo (Pusher rechazó las credenciales (401)")

    @override_settings(**PUSHER)
    def test_sin_red_a_pusher(self):
        import requests
        with mock.patch("peaje.tiempo_real.requests.get", side_effect=requests.ConnectionError()):
            self.assertContains(self._estado(), "respaldo por sondeo (no se pudo contactar a Pusher (ConnectionError))")


class ClienteTiempoRealTests(TestCase):
    def setUp(self):
        entrar(self.client, Usuario.objects.get(email_google="lozogomol@gmail.com"))

    @override_settings(**PUSHER)
    def test_con_pusher_carga_pusher_js_y_el_sondeo_es_solo_respaldo(self):
        r = self.client.get("/")
        self.assertContains(r, "https://js.pusher.com/8.2.0/pusher.min.js")
        self.assertContains(r, 'canal.bind("pusher:subscription_succeeded", function () { modo(true); });')
        self.assertContains(r, 'canal.bind("pusher:subscription_error", function () { modo(false); });')
        self.assertNotContains(r, "IAP.tiempoReal = true;")  # ya no se declara "en vivo" antes de conectar
        self.assertContains(r, "modoTiempoReal(!!(window.IAP && IAP.tiempoReal));")
        self.assertContains(r, 'document.addEventListener("iap:tiempo-real"')

    @override_settings(**SIN_PUSHER)
    def test_sin_pusher_no_carga_pusher_js_y_sondea(self):
        r = self.client.get("/")
        self.assertNotContains(r, "pusher.min.js")
        self.assertContains(r, "sondeo = setInterval(consultar, POLL_MS);")


class EmisionDeEventosTests(TestCase):
    @override_settings(**PUSHER)
    def test_el_servidor_emite_eventos_por_pusher(self):
        with mock.patch.object(tiempo_real, "en_segundo_plano") as fondo:
            tiempo_real.notificar("kpis", {"kpis": panel.kpis()})
        funcion, evento, datos = fondo.call_args.args
        self.assertEqual((funcion, evento), (tiempo_real._enviar, "kpis"))

    @override_settings(**SIN_PUSHER)
    def test_sin_pusher_no_emite(self):
        with mock.patch.object(tiempo_real, "en_segundo_plano") as fondo:
            tiempo_real.notificar("kpis", {"kpis": {}})
        fondo.assert_not_called()
