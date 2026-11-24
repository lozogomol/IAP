"""Tests del modo caseta (votación, cobro, API), consulta y solicitudes de acceso."""
import json
import tempfile
from datetime import timedelta
from decimal import Decimal
from unittest import mock

import requests

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone

from . import cobro
from .auth import emitir_jwt
from .inferencia import InferenciaNoDisponible, interpretar, interpretar_vehiculo
from .models import Alerta, Caseta, LecturaPlaca, Paso, Usuario, Vehiculo
from .tests import VERIFICAR, entrar

INFERIR = "peaje.api.inferir"


def _jpeg():
    return SimpleUploadedFile("frame.jpg", b"\xff\xd8\xff\xe0falso", content_type="image/jpeg")


def _lectura(placa, confianza=0.7, categoria="", ejes=None, caja=None, clase=None, confianza_clase=None,
             caja_vehiculo=None):
    return {"placa": placa, "texto_leido": placa, "confianza": confianza, "categoria": categoria, "ejes": ejes,
            "caja": caja, "clase": categoria if clase is None else clase, "confianza_clase": confianza_clase,
            "caja_vehiculo": caja_vehiculo}


SIN_VEHICULO = {"clase": "", "confianza_clase": None, "caja_vehiculo": None, "cajas_placas": []}


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

    def test_respuesta_con_m2_sin_m3(self):
        datos = {"placas": [{"texto": "987PLB", "confianza_lectura": 90, "caja": [5, 6, 7.4, 8]}, {"texto": "XX"}],
                 "categoria": "minibus", "confianza_categoria": 88.1, "caja_vehiculo": [1, 2, 3, 4], "ejes": None}
        lec = interpretar(datos)
        self.assertEqual((lec[0]["categoria"], lec[0]["ejes"], lec[0]["caja"]), ("minibus", None, [5, 6, 7, 8]))
        self.assertEqual(interpretar_vehiculo(datos), {"clase": "minibus", "confianza_clase": 0.881,
                                                       "caja_vehiculo": [1, 2, 3, 4], "cajas_placas": [[5, 6, 7, 8]]})

    def test_cada_placa_usa_su_propio_vehiculo(self):
        """La clase es la del vehículo que contiene la placa leída, no la global ni la de mayor porcentaje."""
        datos = {"placas": [
            {"texto": "2451KTP", "confianza_lectura": 95, "categoria": "moto", "confianza_categoria": 52,
             "caja_vehiculo": [400, 100, 500, 300]},
            {"texto": "3876LBD", "confianza_lectura": 80, "categoria": "bus", "confianza_categoria": 97,
             "caja_vehiculo": [0, 0, 300, 300]},
        ], "categoria": "moto", "confianza_categoria": 52, "caja_vehiculo": [400, 100, 500, 300]}
        lec = interpretar(datos)
        self.assertEqual([(x["placa"], x["clase"], x["categoria"], x["confianza_clase"], x["caja_vehiculo"])
                          for x in lec],
                         [("2451KTP", "moto", "moto", 0.52, [400, 100, 500, 300]),
                          ("3876LBD", "bus", "bus", 0.97, [0, 0, 300, 300])])

    def test_clase_del_modelo_se_muestra_tal_cual(self):
        """Una clase sin tarifa se muestra igual (sin lista fija), pero no se usa para cobrar."""
        lec = interpretar({"placas": [{"texto": "987PLB", "categoria": "tractor", "confianza_categoria": 70}]})
        self.assertEqual((lec[0]["clase"], lec[0]["categoria"]), ("tractor", ""))


class VotacionTests(TestCase):
    def setUp(self):
        self.caseta = Caseta.objects.first()

    def _leer(self, placa, confianza, hace_s=0, categoria="", ejes=None):
        lec = LecturaPlaca.objects.create(caseta=self.caseta, placa=placa, confianza=confianza,
                                          categoria=categoria, ejes=ejes)
        LecturaPlaca.objects.filter(pk=lec.pk).update(created_at=timezone.now() - timedelta(seconds=hace_s))

    def test_una_lectura_baja_no_basta(self):
        self._leer("2451KTP", 0.7)
        self.assertIsNone(cobro.votar(self.caseta))

    def test_una_lectura_alta_basta(self):
        self._leer("2451KTP", 0.80)
        self.assertEqual(cobro.votar(self.caseta)["placa"], "2451KTP")

    def test_dos_lecturas_en_4s(self):
        self._leer("2451KTP", 0.6, hace_s=3.5)
        self._leer("2451KTP", 0.6)
        self.assertEqual(cobro.votar(self.caseta)["votos"], 2)

    def test_gana_la_mas_repetida(self):
        self._leer("2451KTB", 0.8)
        self._leer("2451KTP", 0.6)
        self._leer("2451KTP", 0.7)
        v = cobro.votar(self.caseta)
        self.assertEqual((v["placa"], v["votos"]), ("2451KTP", 2))

    def test_categoria_mas_repetida_sin_ejes(self):
        self._leer("2451KTP", 0.7, categoria="auto")
        self._leer("2451KTP", 0.7, categoria="minibus")
        self._leer("2451KTP", 0.7, categoria="minibus")
        self._leer("2451KTP", 0.7)
        v = cobro.votar(self.caseta)
        self.assertEqual((v["categoria"], v["ejes"]), ("minibus", None))

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

    def test_categoria_detectada_sin_ejes_usa_2_ejes(self):
        # 3876LBD está registrado como auto; M2 detecta minibus y M3 aún no existe → tarifa minibus/2 ejes
        paso, _, info = cobro.registrar_paso(self.caseta.id, "3876LBD", categoria="minibus")
        self.assertEqual((paso.estado, paso.monto, paso.categoria_detectada, paso.ejes_detectados),
                         ("cobrado", Decimal("15"), "minibus", None))
        self.assertEqual(info["fuente_categoria"], "inferencia")
        d = cobro.paso_a_dict(paso, info)
        self.assertEqual((d["categoria"], d["categoria_texto"], d["tarifa"], d["ejes"]),
                         ("minibus", "Minibús", "15.00", None))
        self.assertEqual(cobro.paso_a_dict(paso)["tarifa"], "15.00")  # también sin info

    def test_no_registrada_conserva_categoria_detectada(self):
        paso, _, info = cobro.registrar_paso(self.caseta.id, "6999ZZZ", categoria="moto")
        self.assertEqual((paso.estado, paso.monto, paso.categoria_detectada), ("no_registrada", Decimal("0"), "moto"))
        self.assertEqual(info["fuente_categoria"], "inferencia")

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

    def test_antiduplicado_30s(self):
        p1, _, _ = cobro.registrar_paso(self.caseta.id, "3876LBD")
        p2, dup, _ = cobro.registrar_paso(self.caseta.id, "3876LBD")
        self.assertTrue(dup)
        self.assertEqual(p1.pk, p2.pk)
        self.assertEqual(Vehiculo.objects.get(placa="3876LBD").saldo, Decimal("240"))
        Paso.objects.filter(pk=p1.pk).update(created_at=timezone.now() - timedelta(seconds=31))
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

    def _frame(self, lecturas, vehiculo=None):
        with mock.patch(INFERIR, return_value=(lecturas, 120, vehiculo or SIN_VEHICULO)):
            return self.client.post("/api/v1/pasos/frame", {"imagen": _jpeg(), "caseta_id": self.caseta.id},
                                    HTTP_X_CSRFTOKEN=self.csrf)

    def test_sin_sesion_401(self):
        r = self.client_class().post("/api/v1/pasos/frame", {"caseta_id": self.caseta.id})
        self.assertEqual(r.status_code, 401)

    def test_sin_csrf_403(self):
        with mock.patch(INFERIR, return_value=([], 1, SIN_VEHICULO)):
            r = self.client.post("/api/v1/pasos/frame", {"imagen": _jpeg(), "caseta_id": self.caseta.id})
        self.assertEqual(r.status_code, 403)

    def test_caseta_invalida(self):
        r = self.client.post("/api/v1/pasos/frame", {"imagen": _jpeg(), "caseta_id": "x"},
                             HTTP_X_CSRFTOKEN=self.csrf)
        self.assertEqual(r.status_code, 400)

    def _post_error(self, efecto):
        with mock.patch(INFERIR, side_effect=efecto):
            return self.client.post("/api/v1/pasos/frame", {"imagen": _jpeg(), "caseta_id": self.caseta.id},
                                    HTTP_X_CSRFTOKEN=self.csrf)

    def test_sin_placa_devuelve_vehiculo_y_cajas(self):
        vehiculo = {"clase": "camion", "confianza_clase": 0.91, "caja_vehiculo": [10, 20, 300, 400],
                    "cajas_placas": [[50, 60, 90, 80]]}
        d = self._frame([], vehiculo).json()
        self.assertEqual((d["accion"], d["mensaje"], d["paso"]), ("sin_placa", "Buscando placa…", None))
        lec = d["lectura"]
        self.assertEqual((lec["placa"], lec["categoria"], lec["confianza_categoria"], lec["caja_vehiculo"],
                          lec["cajas_placas"]), (None, "camion", 0.91, [10, 20, 300, 400], [[50, 60, 90, 80]]))
        self.assertIn("latencia_ms", lec)

    def test_tipo_es_el_del_vehiculo_de_la_placa_leida(self):
        global_ = {"clase": "bus", "confianza_clase": 0.98, "caja_vehiculo": [0, 0, 300, 300], "cajas_placas": []}
        lec = _lectura("3876LBD", 0.7, categoria="auto", confianza_clase=0.55, caja_vehiculo=[400, 100, 600, 300])
        d = self._frame([lec], global_).json()["lectura"]
        self.assertEqual((d["categoria"], d["confianza_categoria"], d["caja_vehiculo"]),
                         ("auto", 0.55, [400, 100, 600, 300]))

    def test_esperando_confirmacion_y_registrado(self):
        d = self._frame([_lectura("3876LBD", 0.7, caja=[1, 2, 3, 4])]).json()
        self.assertEqual((d["accion"], d["mensaje"], d["votos"], d["votos_necesarios"], d["paso"]),
                         ("esperando_confirmacion", "Leyendo 3876LBD (confirmando 1/2)", 1, 2, None))
        self.assertEqual((d["lectura"]["placa"], d["lectura"]["confianza"], d["lectura"]["caja_placa"]),
                         ("3876LBD", 0.7, [1, 2, 3, 4]))
        self.assertFalse(Paso.objects.filter(placa_normalizada="3876LBD").exists())
        d = self._frame([_lectura("3876LBD", 0.75)]).json()
        self.assertEqual((d["accion"], d["mensaje"]), ("registrado", "Registrado: cobrado"))
        p = d["paso"]
        self.assertEqual((p["placa"], p["estado"], p["tarifa"], p["categoria"], p["ejes"]),
                         ("3876LBD", "cobrado", "10.00", "auto", 2))
        self.assertIn("latencia_ms", d["lectura"])

    def test_una_lectura_confiable_registra_al_instante(self):
        self.assertEqual(self._frame([_lectura("3876LBD", 0.80)]).json()["accion"], "registrado")

    def test_duplicado_no_cobra_y_dice_hace_cuanto(self):
        self._frame([_lectura("3876LBD", 0.9)])
        p = Paso.objects.get(placa_normalizada="3876LBD", caseta=self.caseta)
        Paso.objects.filter(pk=p.pk).update(created_at=timezone.now() - timedelta(seconds=25))
        d = self._frame([_lectura("3876LBD", 0.9)]).json()
        self.assertEqual((d["accion"], d["segundos_desde_ultimo_paso"], d["mensaje"], d["paso"]["id"]),
                         ("duplicado", 25, "Ya registrado hace 25 s", p.pk))
        # una lectura floja de la misma placa también se reconoce como ya registrada
        LecturaPlaca.objects.all().delete()
        self.assertEqual(self._frame([_lectura("3876LBD", 0.5)]).json()["accion"], "duplicado")
        self.assertEqual(Paso.objects.filter(placa_normalizada="3876LBD", caseta=self.caseta,
                                             usuario=self.admin).count(), 1)
        self.assertEqual(Vehiculo.objects.get(placa="3876LBD").saldo, Decimal("240"))

    def test_registra_de_nuevo_pasados_30s(self):
        self._frame([_lectura("3876LBD", 0.9)])
        Paso.objects.update(created_at=timezone.now() - timedelta(seconds=31))
        LecturaPlaca.objects.all().delete()
        self.assertEqual(self._frame([_lectura("3876LBD", 0.9)]).json()["accion"], "registrado")

    def test_cada_frame_queda_en_el_log(self):
        with self.assertLogs("peaje.caseta", level="INFO") as logs:
            self._frame([])
            self._frame([_lectura("3876LBD", 0.7)])
        self.assertIn("accion=sin_placa", logs.output[0])
        self.assertIn("accion=esperando_confirmacion placa=3876LBD", logs.output[1])

    def test_fallo_inesperado_tambien_es_error(self):
        with self.assertLogs("peaje.caseta", level="ERROR"):
            r = self._post_error(KeyError("placas"))
        self.assertEqual((r.status_code, r.json()["accion"]), (502, "error"))

    def test_timeout_real_se_traduce(self):
        with override_settings(INFERENCE_URL="http://inferencia.invalid"), \
                mock.patch("peaje.inferencia.requests.post", side_effect=requests.Timeout()):
            r = self.client.post("/api/v1/pasos/frame", {"imagen": _jpeg(), "caseta_id": self.caseta.id},
                                 HTTP_X_CSRFTOKEN=self.csrf)
        self.assertEqual((r.status_code, r.json()["accion"]), (502, "error"))
        self.assertIn("timeout", r.json()["mensaje"])

    def test_registra_con_categoria_detectada(self):
        self._frame([_lectura("3876LBD", 0.7, categoria="camion")])
        p = self._frame([_lectura("3876LBD", 0.75, categoria="camion")]).json()["paso"]
        self.assertEqual((p["categoria"], p["categoria_texto"], p["ejes"], p["tarifa"], p["fuente_categoria"]),
                         ("camion", "Camión", None, "20.00", "inferencia"))

    def test_inferencia_caida_error(self):
        with self.assertLogs("peaje.caseta", level="INFO") as logs:
            r = self._post_error(InferenciaNoDisponible("timeout"))
        self.assertEqual(r.status_code, 502)
        d = r.json()
        self.assertEqual((d["accion"], d["lectura"], d["paso"]), ("error", None, None))
        self.assertIn("Sin conexión con la inferencia", d["mensaje"])
        self.assertIn("accion=error", logs.output[0])

    def test_jwt_tambien_funciona(self):
        with mock.patch(INFERIR, return_value=([_lectura("2451KTP", 0.95)], 50, SIN_VEHICULO)):
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
        with mock.patch(INFERIR, return_value=([_lectura("2451KTP", 0.9)], 80, SIN_VEHICULO)):
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
