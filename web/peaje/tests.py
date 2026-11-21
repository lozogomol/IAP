import json
from decimal import Decimal
from unittest import mock

import jwt
from django.conf import settings
from django.http import HttpResponse
from django.test import RequestFactory, TestCase, override_settings
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory

from .auth import emitir_jwt, rol_requerido

from .models import Alerta, Caseta, Paso, Tarifa, Usuario, Vehiculo
from .placas import corregir_ocr, distancia_edicion, es_formato_bolivia, normalizar_placa, sugerencias


class NormalizarTests(TestCase):
    def test_quita_simbolos_y_mayusculas(self):
        self.assertEqual(normalizar_placa(" 2451-ktp "), "2451KTP")
        self.assertEqual(normalizar_placa("987 p.l.b"), "987PLB")
        self.assertEqual(normalizar_placa(None), "")


class FormatoTests(TestCase):
    def test_validas(self):
        self.assertTrue(es_formato_bolivia("2451KTP"))
        self.assertTrue(es_formato_bolivia("987PLB"))

    def test_invalidas(self):
        for p in ("24KTP", "12345ABC", "2451KT", "ABC1234", "2451-KTP", ""):
            self.assertFalse(es_formato_bolivia(p), p)


class CorregirOcrTests(TestCase):
    def test_cuatro_digitos(self):
        self.assertEqual(corregir_ocr("Z4S1KTP"), "2451KTP")
        self.assertEqual(corregir_ocr("3876L8D"), "3876LBD")

    def test_tres_digitos(self):
        self.assertEqual(corregir_ocr("9B7PL8"), "987PLB")
        self.assertEqual(corregir_ocr("OI2G5Z"), "012GSZ")

    def test_longitud_desconocida_no_cambia(self):
        self.assertEqual(corregir_ocr("O1"), "O1")


class SugerenciasTests(TestCase):
    existentes = ["2451KTP", "3876LBD", "987PLB", "4012MNS"]

    def test_distancia(self):
        self.assertEqual(distancia_edicion("2451KTP", "2451KTB"), 1)
        self.assertEqual(distancia_edicion("abc", "abc"), 0)

    def test_por_correccion_ocr(self):
        self.assertEqual(sugerencias("3876-L8D", self.existentes), ["3876LBD"])

    def test_por_distancia_uno(self):
        self.assertEqual(sugerencias("2451KTR", self.existentes), ["2451KTP"])
        self.assertEqual(sugerencias("451KTP", self.existentes), ["2451KTP"])

    def test_sin_coincidencias(self):
        self.assertEqual(sugerencias("6120PQR", self.existentes), [])

    def test_no_sugiere_la_misma(self):
        self.assertEqual(sugerencias("2451KTP", self.existentes), [])


class DatosInicialesTests(TestCase):
    """La migración de datos se aplica también en la BD de tests."""

    def test_cargados(self):
        self.assertEqual(Vehiculo.objects.count(), 20)
        self.assertEqual(Tarifa.objects.count(), 10)
        self.assertEqual(Caseta.objects.count(), 3)
        self.assertTrue(Usuario.objects.filter(email_google="lozogomol@gmail.com", rol="admin").exists())
        self.assertEqual(Tarifa.objects.get(categoria="camion", ejes=6).monto, Decimal("60"))
        self.assertEqual(Paso.objects.filter(estado="no_registrada").count(), 2)
        self.assertEqual(Alerta.objects.filter(tipo="placa_no_registrada").count(), 2)

    def test_placas_validas(self):
        for v in Vehiculo.objects.all():
            self.assertTrue(es_formato_bolivia(v.placa), v.placa)

    def test_tarifa_para_cada_vehiculo(self):
        for v in Vehiculo.objects.all():
            self.assertTrue(Tarifa.objects.filter(categoria=v.categoria, ejes=v.ejes).exists(), v.placa)

    def test_hay_registrados_sin_paso(self):
        con_paso = set(Paso.objects.values_list("placa_normalizada", flat=True))
        sin_paso = Vehiculo.objects.exclude(placa__in=con_paso)
        self.assertGreaterEqual(sin_paso.count(), 5)


VERIFICAR = "peaje.auth.id_token.verify_oauth2_token"


@override_settings(GOOGLE_CLIENT_ID="cliente-test.apps.googleusercontent.com")
class LoginGoogleWebTests(TestCase):
    def setUp(self):
        self.admin = Usuario.objects.get(email_google="lozogomol@gmail.com")
        self.client = self.client_class(enforce_csrf_checks=True)

    def _post(self, credential="tok", **extra):
        self.client.get("/login")  # deja la cookie CSRF
        token = self.client.cookies["csrftoken"].value
        return self.client.post("/auth/google/web", json.dumps({"credential": credential, **extra}),
                                content_type="application/json", HTTP_X_CSRFTOKEN=token)

    def test_login_page_tiene_boton_google(self):
        r = self.client.get("/login")
        self.assertContains(r, "accounts.google.com/gsi/client")
        self.assertContains(r, "cliente-test.apps.googleusercontent.com")

    def test_coop_permite_popups(self):
        r = self.client.get("/login")
        self.assertEqual(r["Cross-Origin-Opener-Policy"], "same-origin-allow-popups")

    def test_sin_csrf_rechaza(self):
        r = self.client.post("/auth/google/web", "{}", content_type="application/json")
        self.assertEqual(r.status_code, 403)

    def test_email_autorizado_abre_sesion(self):
        with mock.patch(VERIFICAR, return_value={"email": "LozoGomol@gmail.com", "email_verified": True}) as v:
            r = self._post(next="/pasos")
        self.assertEqual(v.call_args.kwargs["audience"], "cliente-test.apps.googleusercontent.com")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"ok": True, "redirect": "/pasos"})
        s = self.client.session
        self.assertEqual((s["usuario_id"], s["email"], s["rol"]), (self.admin.id, self.admin.email_google, "admin"))
        self.assertContains(self.client.get("/"), self.admin.nombre)

    def test_next_externo_se_ignora(self):
        with mock.patch(VERIFICAR, return_value={"email": "lozogomol@gmail.com", "email_verified": True}):
            r = self._post(next="https://malo.com/")
        self.assertEqual(r.json()["redirect"], "/")

    def test_email_no_autorizado_403(self):
        with mock.patch(VERIFICAR, return_value={"email": "intruso@gmail.com", "email_verified": True}):
            r = self._post()
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["redirect"], "/acceso-denegado")
        self.assertNotIn("usuario_id", self.client.session)
        r = self.client.get("/acceso-denegado")
        self.assertContains(r, "Acceso denegado: tu cuenta de Google no está autorizada en IAP", status_code=403)
        self.assertContains(r, "intruso@gmail.com", status_code=403)

    def test_usuario_inactivo_403(self):
        Usuario.objects.filter(pk=self.admin.pk).update(activo=False)
        with mock.patch(VERIFICAR, return_value={"email": "lozogomol@gmail.com", "email_verified": True}):
            self.assertEqual(self._post().status_code, 403)

    def test_token_invalido_401(self):
        with mock.patch(VERIFICAR, side_effect=ValueError("firma inválida")):
            self.assertEqual(self._post().status_code, 401)

    def test_email_no_verificado_401(self):
        with mock.patch(VERIFICAR, return_value={"email": "lozogomol@gmail.com", "email_verified": False}):
            self.assertEqual(self._post().status_code, 401)

    def test_logout_limpia_sesion(self):
        with mock.patch(VERIFICAR, return_value={"email": "lozogomol@gmail.com", "email_verified": True}):
            self._post()
        self.assertRedirects(self.client.get("/logout"), "/login", fetch_redirect_response=False)
        self.assertNotIn("usuario_id", self.client.session)


class PaginasProtegidasTests(TestCase):
    def test_sin_sesion_redirige_a_login(self):
        for ruta in ("/", "/vehiculos", "/pasos", "/alertas", "/chat"):
            r = self.client.get(ruta)
            self.assertEqual(r.status_code, 302, ruta)
            self.assertTrue(r["Location"].startswith("/login?next="), ruta)

    def test_con_sesion_muestra_barra(self):
        s = self.client.session
        s.update({"usuario_id": 1, "nombre": "Ana Operadora", "email": "a@x.com", "rol": "operador"})
        s.save()
        r = self.client.get("/vehiculos")
        for texto in ("Panel", "Vehículos", "Pasos", "Alertas", "Chat", "Estado", "Ana Operadora", "operador", "Salir"):
            self.assertContains(r, texto)

    def test_rol_requerido(self):
        vista = rol_requerido("admin")(lambda request: HttpResponse("ok"))
        rf = RequestFactory()
        for rol, esperado in (("admin", 200), ("operador", 403)):
            req = rf.get("/x")
            req.session = {"usuario_id": 1, "rol": rol}
            self.assertEqual(vista(req).status_code, esperado, rol)
        req = rf.get("/x")
        req.session = {}
        self.assertEqual(vista(req).status_code, 302)


@override_settings(INFERENCE_URL="")
class EstadoTests(TestCase):
    def test_publico_html(self):
        r = self.client.get("/estado")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Estado del sistema")

    def test_json(self):
        r = self.client.get("/estado?format=json")
        self.assertEqual(r.json(), {"web": "ok", "inferencia": "sin configurar (falta INFERENCE_URL)"})


@api_view(["GET"])
def _vista_api(request):
    return Response({"email": request.user.email_google})


class JWTTests(TestCase):
    def setUp(self):
        self.admin = Usuario.objects.get(email_google="lozogomol@gmail.com")
        self.rf = APIRequestFactory()

    def _get(self, auth=None):
        req = self.rf.get("/api/x", HTTP_AUTHORIZATION=auth) if auth else self.rf.get("/api/x")
        return _vista_api(req)

    def test_sin_token_401(self):
        self.assertEqual(self._get().status_code, 401)

    def test_token_valido(self):
        r = self._get(f"Bearer {emitir_jwt(self.admin)}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["email"], self.admin.email_google)

    def test_token_otra_clave_401(self):
        malo = jwt.encode({"sub": str(self.admin.id), "exp": 9999999999}, "otra", algorithm="HS256")
        self.assertEqual(self._get(f"Bearer {malo}").status_code, 401)

    def test_token_expirado_401(self):
        viejo = jwt.encode({"sub": str(self.admin.id), "exp": 1}, settings.JWT_SECRET, algorithm="HS256")
        self.assertEqual(self._get(f"Bearer {viejo}").status_code, 401)

    def test_usuario_desactivado_401(self):
        token = emitir_jwt(self.admin)
        Usuario.objects.filter(pk=self.admin.pk).update(activo=False)
        self.assertEqual(self._get(f"Bearer {token}").status_code, 401)

    def test_schema_openapi(self):
        r = self.client.get("/api/schema/")
        self.assertEqual(r.status_code, 200)
