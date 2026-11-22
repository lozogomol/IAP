"""Tests del modo caseta (votación, cobro, API), consulta y solicitudes de acceso."""
import json
import tempfile
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone

from . import cobro
from .auth import emitir_jwt
from .inferencia import InferenciaNoDisponible, interpretar
from .models import Alerta, Caseta, LecturaPlaca, Paso, Usuario, Vehiculo
from .tests import VERIFICAR, entrar

INFERIR = "peaje.api.inferir"


def _jpeg():
    return SimpleUploadedFile("frame.jpg", b"\xff\xd8\xff\xe0falso", content_type="image/jpeg")


def _lectura(placa, confianza=0.8, categoria="", ejes=None):
    return {"placa": placa, "texto_leido": placa, "confianza": confianza, "categoria": categoria, "ejes": ejes}


class InterpretarTests(TestCase):
    def test_normaliza_corrige_y_filtra(self):
        datos = {"placas": [
            {"texto": "2451-KTP", "confianza_lectura": 93.4},
            {"texto": "3876L8D", "confianza_lectura": 0.99},
            {"texto": "XX", "confianza_lectura": 99},
        ]}
        lecturas = interpretar(datos)
        self.assertEqual([x["placa"] for x in lecturas], ["3876LBD", "2451KTP"])
        self.assertEqual(lecturas[1]["confianza"], 0.934)
        self.assertEqual((lecturas[0]["categoria"], lecturas[0]["ejes"]), ("", None))

    def test_categoria_global_valida(self):
        lec = interpretar({"placas": [{"texto": "987PLB"}], "categoria": "camion", "ejes": 3})
        self.assertEqual((lec[0]["categoria"], lec[0]["ejes"]), ("camion", 3))
        lec = interpretar({"placas": [{"texto": "987PLB"}], "categoria": "nave", "ejes": 40})
        self.assertEqual((lec[0]["categoria"], lec[0]["ejes"]), ("", None))


class VotacionTests(TestCase):
    def setUp(self):
        self.caseta = Caseta.objects.first()

    def _leer(self, placa, confianza, hace_s=0):
        lec = LecturaPlaca.objects.create(caseta=self.caseta, placa=placa, confianza=confianza)
        LecturaPlaca.objects.filter(pk=lec.pk).update(created_at=timezone.now() - timedelta(seconds=hace_s))

    def test_una_lectura_baja_no_basta(self):
        self._leer("2451KTP", 0.7)
        self.assertIsNone(cobro.votar(self.caseta))

    def test_una_lectura_alta_basta(self):
        self._leer("2451KTP", 0.9)
        self.assertEqual(cobro.votar(self.caseta)["placa"], "2451KTP")

    def test_gana_la_mas_repetida(self):
        self._leer("2451KTB", 0.8)
        self._leer("2451KTP", 0.6)
        self._leer("2451KTP", 0.7)
        v = cobro.votar(self.caseta)
        self.assertEqual((v["placa"], v["votos"]), ("2451KTP", 2))

    def test_lecturas_viejas_no_cuentan(self):
        self._leer("2451KTP", 0.7, hace_s=10)
        self._leer("2451KTP", 0.7)
        self.assertIsNone(cobro.votar(self.caseta))

    def test_otra_caseta_no_cuenta(self):
        otra = Caseta.objects.exclude(pk=self.caseta.pk).first()
        LecturaPlaca.objects.create(caseta=otra, placa="2451KTP", confianza=0.7)
        self._leer("2451KTP", 0.7)
        self.assertIsNone(cobro.votar(self.caseta))


class RegistrarPasoTests(TestCase):
    def setUp(self):
        self.caseta = Caseta.objects.first()

    def test_cobra_con_categoria_del_registro(self):
        paso, dup, info = cobro.registrar_paso(self.caseta.id, "3876LBD")
        self.assertFalse(dup)
        self.assertEqual((paso.estado, paso.monto, paso.categoria_detectada, paso.ejes_detectados),
                         ("cobrado", Decimal("10"), "auto", 2))
        self.assertEqual(info["fuente_categoria"], "registro")
        self.assertEqual(Vehiculo.objects.get(placa="3876LBD").saldo, Decimal("240"))

    def test_usa_categoria_de_inferencia_si_llega(self):
        paso, _, info = cobro.registrar_paso(self.caseta.id, "3876LBD", categoria="camion", ejes=3)
        self.assertEqual((paso.monto, info["fuente_categoria"]), (Decimal("30"), "inferencia"))

    def test_saldo_insuficiente(self):
        paso, _, _ = cobro.registrar_paso(self.caseta.id, "1987FHK")
        self.assertEqual((paso.estado, paso.monto), ("saldo_insuficiente", Decimal("0")))
        self.assertTrue(Alerta.objects.filter(paso=paso, tipo="saldo_insuficiente").exists())

    def test_no_registrada_categoria_desconocida(self):
        paso, _, info = cobro.registrar_paso(self.caseta.id, "6999ZZZ")
        self.assertEqual((paso.estado, paso.monto, paso.categoria_detectada), ("no_registrada", Decimal("0"), ""))
        d = cobro.paso_a_dict(paso, info)
        self.assertEqual((d["categoria"], d["tarifa"], d["fuente_categoria"]), ("desconocida", None, "desconocida"))
        self.assertTrue(Alerta.objects.filter(paso=paso, tipo="placa_no_registrada").exists())

    def test_inactivo_no_se_cobra(self):
        paso, _, _ = cobro.registrar_paso(self.caseta.id, "654TRB")
        self.assertEqual((paso.estado, paso.monto), ("no_registrada", Decimal("0")))
        self.assertEqual(Vehiculo.objects.get(placa="654TRB").saldo, Decimal("40"))

    def test_antiduplicado_60s(self):
        p1, _, _ = cobro.registrar_paso(self.caseta.id, "3876LBD")
        p2, dup, _ = cobro.registrar_paso(self.caseta.id, "3876LBD")
        self.assertTrue(dup)
        self.assertEqual(p1.pk, p2.pk)
        self.assertEqual(Vehiculo.objects.get(placa="3876LBD").saldo, Decimal("240"))
        Paso.objects.filter(pk=p1.pk).update(created_at=timezone.now() - timedelta(seconds=61))
        _, dup, _ = cobro.registrar_paso(self.caseta.id, "3876LBD")
        self.assertFalse(dup)


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix="iap-test-"), CLOUDINARY_URL="", PUSHER_APP_ID="")
class ApiFrameTests(TestCase):
    def setUp(self):
        self.caseta = Caseta.objects.first()
        self.admin = Usuario.objects.get(email_google="lozogomol@gmail.com")
        self.client = self.client_class(enforce_csrf_checks=True)
        entrar(self.client, self.admin)
        self.client.get("/caseta")  # deja la cookie CSRF
        self.csrf = self.client.cookies["csrftoken"].value

    def _frame(self, lecturas):
        with mock.patch(INFERIR, return_value=(lecturas, 120)):
            return self.client.post("/api/v1/pasos/frame", {"imagen": _jpeg(), "caseta_id": self.caseta.id},
                                    HTTP_X_CSRFTOKEN=self.csrf)

    def test_sin_sesion_401(self):
        r = self.client_class().post("/api/v1/pasos/frame", {"caseta_id": self.caseta.id})
        self.assertEqual(r.status_code, 401)

    def test_sin_csrf_403(self):
        with mock.patch(INFERIR, return_value=([], 1)):
            r = self.client.post("/api/v1/pasos/frame", {"imagen": _jpeg(), "caseta_id": self.caseta.id})
        self.assertEqual(r.status_code, 403)

    def test_caseta_invalida(self):
        r = self.client.post("/api/v1/pasos/frame", {"imagen": _jpeg(), "caseta_id": "x"},
                             HTTP_X_CSRFTOKEN=self.csrf)
        self.assertEqual(r.status_code, 400)

    def test_sin_placa(self):
        self.assertEqual(self._frame([]).json()["resultado"], "sin_placa")

    def test_vota_y_registra_una_sola_vez(self):
        self.assertEqual(self._frame([_lectura("3876LBD", 0.7)]).json()["resultado"], "leyendo")
        d = self._frame([_lectura("3876LBD", 0.75)]).json()
        self.assertEqual(d["resultado"], "registrado")
        p = d["paso"]
        self.assertEqual((p["placa"], p["estado"], p["tarifa"], p["categoria"], p["ejes"]),
                         ("3876LBD", "cobrado", "10.00", "auto", 2))
        self.assertIn("latencia_ms", d)
        self.assertEqual(self._frame([_lectura("3876LBD", 0.9)]).json()["resultado"], "duplicado")
        self.assertEqual(Paso.objects.filter(placa_normalizada="3876LBD", caseta=self.caseta,
                                             usuario=self.admin).count(), 1)

    def test_inferencia_caida_502(self):
        with mock.patch(INFERIR, side_effect=InferenciaNoDisponible("timeout")):
            r = self.client.post("/api/v1/pasos/frame", {"imagen": _jpeg(), "caseta_id": self.caseta.id},
                                 HTTP_X_CSRFTOKEN=self.csrf)
        self.assertEqual(r.status_code, 502)

    def test_jwt_tambien_funciona(self):
        with mock.patch(INFERIR, return_value=([_lectura("2451KTP", 0.95)], 50)):
            r = self.client_class().post("/api/v1/pasos/frame", {"imagen": _jpeg(), "caseta_id": self.caseta.id},
                                         HTTP_AUTHORIZATION=f"Bearer {emitir_jwt(self.admin)}")
        self.assertEqual(r.json()["paso"]["estado"], "cobrado")


class ConsultaTests(TestCase):
    def setUp(self):
        entrar(self.client, Usuario.objects.get(email_google="lozogomol@gmail.com"))

    def _c(self, placa):
        return self.client.get("/api/v1/vehiculos/consulta", {"placa": placa}).json()

    def test_cuatro_estados(self):
        self.assertEqual(self._c("3876-lbd")["estado"], "habilitada")
        self.assertEqual(self._c("1987FHK")["estado"], "saldo_insuficiente")
        self.assertEqual(self._c("654TRB")["estado"], "inactiva")
        d = self._c("3876L8D")
        self.assertEqual((d["estado"], d["sugerencias"]), ("no_registrada", ["3876LBD"]))

    def test_falta_placa(self):
        self.assertEqual(self.client.get("/api/v1/vehiculos/consulta").status_code, 400)

    def test_foto(self):
        with mock.patch(INFERIR, return_value=([_lectura("2451KTP", 0.9)], 80)):
            r = self.client.post("/api/v1/placas/leer", {"imagen": _jpeg()})
        self.assertEqual(r.json()["consulta"]["estado"], "habilitada")

    def test_paginas(self):
        self.assertContains(self.client.get("/consulta"), "Consulta de placa")
        r = self.client.get("/caseta")
        self.assertContains(r, "Modo caseta")
        self.assertContains(r, "Iniciar reconocimiento")
        self.assertContains(r, 'facingMode: { ideal: "environment" }')
        self.assertNotContains(r, "<select")  # sin selector de caseta


@override_settings(GOOGLE_CLIENT_ID="cliente-test.apps.googleusercontent.com")
class SolicitudAccesoTests(TestCase):
    def _rechazado(self, email="nuevo@gmail.com", nombre="Nuevo Usuario"):
        self.client.get("/login")
        token = self.client.cookies["csrftoken"].value
        with mock.patch(VERIFICAR, return_value={"email": email, "email_verified": True, "name": nombre}):
            r = self.client.post("/auth/google/web", json.dumps({"credential": "t"}),
                                 content_type="application/json", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(r.status_code, 403)

    def test_solicitar_crea_pendiente(self):
        self._rechazado()
        self.assertContains(self.client.get("/acceso-denegado"), "Solicitar acceso</button>", status_code=403)
        self.client.post("/acceso-denegado/solicitar")
        u = Usuario.objects.get(email_google="nuevo@gmail.com")
        self.assertEqual((u.nombre, u.rol, u.activo, u.pendiente), ("Nuevo Usuario", "operador", False, True))
        r = self.client.get("/acceso-denegado")
        self.assertContains(r, "Solicitud enviada", status_code=403)
        self.assertContains(r, "Tu solicitud de acceso está pendiente", status_code=403)
        self.assertNotContains(r, "Solicitar acceso</button>", status_code=403)
        self.client.post("/acceso-denegado/solicitar")  # repetir no duplica y avisa
        self.assertContains(self.client.get("/acceso-denegado"), "Ya tienes una solicitud pendiente",
                            status_code=403)
        self.assertEqual(Usuario.objects.filter(email_google="nuevo@gmail.com").count(), 1)

    def test_pendiente_no_puede_entrar(self):
        self._rechazado()
        self.client.post("/acceso-denegado/solicitar")
        self._rechazado()  # sigue en 403

    def test_sin_login_google_no_crea(self):
        self.client.post("/acceso-denegado/solicitar")
        self.assertFalse(Usuario.objects.filter(pendiente=True).exists())


class UsuariosAdminTests(TestCase):
    def setUp(self):
        self.admin = Usuario.objects.get(email_google="lozogomol@gmail.com")
        self.pend = Usuario.objects.create(email_google="p@x.com", nombre="Pepe", activo=False, pendiente=True,
                                           solicitado_at=timezone.now())
        self.oper = Usuario.objects.create(email_google="o@x.com", nombre="Olga", rol="operador")

    def test_solo_admin(self):
        entrar(self.client, self.oper)
        self.assertEqual(self.client.get("/usuarios").status_code, 403)
        r = self.client.post(f"/usuarios/{self.oper.id}", {"accion": "rol", "rol": "admin"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(Usuario.objects.get(pk=self.oper.pk).rol, "operador")

    def test_lista_pendientes_y_aprobar(self):
        entrar(self.client, self.admin)
        r = self.client.get("/usuarios")
        self.assertContains(r, "Solicitudes pendientes")
        self.assertContains(r, "p@x.com")
        self.client.post(f"/usuarios/{self.pend.id}", {"accion": "aprobar"})
        u = Usuario.objects.get(pk=self.pend.pk)
        self.assertEqual((u.activo, u.pendiente), (True, False))

    def test_rechazar(self):
        entrar(self.client, self.admin)
        self.client.post(f"/usuarios/{self.pend.id}", {"accion": "rechazar"})
        u = Usuario.objects.get(pk=self.pend.pk)
        self.assertEqual((u.activo, u.pendiente), (False, False))

    def test_cambiar_rol_y_no_el_propio(self):
        entrar(self.client, self.admin)
        self.client.post(f"/usuarios/{self.oper.id}", {"accion": "rol", "rol": "admin"})
        self.assertEqual(Usuario.objects.get(pk=self.oper.pk).rol, "admin")
        self.client.post(f"/usuarios/{self.admin.id}", {"accion": "rol", "rol": "operador"})
        self.client.post(f"/usuarios/{self.admin.id}", {"accion": "desactivar"})
        a = Usuario.objects.get(pk=self.admin.pk)
        self.assertEqual((a.rol, a.activo), ("admin", True))

    def test_rol_cambiado_se_refleja_en_la_sesion(self):
        entrar(self.client, self.oper)
        Usuario.objects.filter(pk=self.oper.pk).update(rol="admin")
        self.assertEqual(self.client.get("/usuarios").status_code, 200)
