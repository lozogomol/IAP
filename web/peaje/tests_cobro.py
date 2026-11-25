"""Caseta real, si pasó pagó: cobro con saldo, pendiente de pago y cobro automático en efectivo."""
import importlib
import tempfile
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.apps import apps as django_apps
from django.test import TestCase, override_settings
from django.utils import timezone

from . import cobro, flujo, panel, tiempo_real
from .models import Alerta, Paso, Usuario, Vehiculo, caseta_principal
from .servicios import agente
from .tests import entrar
from .tests_caseta import INFERIR, SIN_VEHICULO, _jpeg, _lectura

PUSHER = dict(PUSHER_APP_ID="123", PUSHER_KEY="clave", PUSHER_SECRET="secreto", PUSHER_CLUSTER="us2")


class Base(TestCase):
    def setUp(self):
        self.caseta = caseta_principal()
        self.t0 = timezone.now()
        Vehiculo.objects.filter(placa="3876LBD").update(saldo=Decimal("100"))   # auto, saldo suficiente
        Vehiculo.objects.filter(placa="1987FHK").update(saldo=Decimal("1"))     # auto, saldo insuficiente

    def _f(self, segundos, placa=None, categoria="auto", ejes=2):
        lecturas = [_lectura(placa, 0.9, categoria=categoria, ejes=ejes)] if placa else []
        vehiculo = {**SIN_VEHICULO, "clase": categoria, "caja_vehiculo": [0, 0, 9, 9], "ejes": ejes}
        return flujo.procesar_frame(self.caseta, lecturas, vehiculo if placa else SIN_VEHICULO, b"\xff\xd8frame",
                                    ahora=self.t0 + timedelta(seconds=segundos))

    def _pasar(self, t, placa, categoria="auto", ejes=2):
        """Un vehículo: dos frames con tipo y ejes → se cobra en el segundo."""
        r1 = self._f(t, placa, categoria, ejes)
        r2 = self._f(t + 0.5, placa, categoria, ejes)
        self.assertEqual(r2["accion"], "registrado")
        return r1, r2["paso"]


class RegistroTests(Base):
    def test_registrado_con_saldo_cobrado_con_saldo(self):
        _, paso = self._pasar(0, "3876LBD")
        self.assertEqual((paso.estado, paso.forma_pago, paso.monto, paso.resultado_texto),
                         ("cobrado", "saldo", Decimal("4"), "Cobrado con saldo"))
        self.assertEqual(Vehiculo.objects.get(placa="3876LBD").saldo, Decimal("96"))
        self.assertFalse(Alerta.objects.filter(paso=paso).exists())

    def test_no_registrada_y_saldo_insuficiente_quedan_pendientes_con_alerta(self):
        _, p1 = self._pasar(0, "7777XYZ")
        _, p2 = self._pasar(2, "1987FHK")
        p1.refresh_from_db()
        self.assertEqual((p2.estado, p2.forma_pago, p2.monto, p2.resultado_texto),
                         ("pendiente_pago", "", Decimal("4"), "Pendiente de pago (saldo insuficiente)"))
        self.assertEqual(Alerta.objects.get(paso=p2).tipo, "saldo_insuficiente")
        self.assertEqual(Alerta.objects.get(paso=p1).tipo, "placa_no_registrada")  # la alerta se genera siempre
        self.assertEqual(Vehiculo.objects.get(placa="1987FHK").saldo, Decimal("1"))  # no se descuenta el saldo


class CobroEnEfectivoTests(Base):
    def test_pendiente_pasa_a_cobrado_en_efectivo_al_pasar_el_siguiente(self):
        _, pendiente = self._pasar(0, "7777XYZ")
        self.assertEqual((pendiente.estado, pendiente.resultado_texto),
                         ("pendiente_pago", "Pendiente de pago (no registrada)"))
        # la misma placa (o ningún vehículo) no resuelve nada
        self.assertEqual(self._f(2, "7777XYZ")["accion"], "fijo")
        self._f(3)
        pendiente.refresh_from_db()
        self.assertEqual(pendiente.estado, "pendiente_pago")
        # se confirma la placa del siguiente vehículo → el anterior pagó en efectivo
        r = self._f(5, "3876LBD")
        self.assertEqual(r["resuelto"].pk, pendiente.pk)
        pendiente.refresh_from_db()
        self.assertEqual((pendiente.estado, pendiente.forma_pago, pendiente.resultado_texto, pendiente.monto),
                         ("cobrado", "efectivo", "Cobrado en efectivo", Decimal("4")))
        alerta = Alerta.objects.get(paso=pendiente)
        self.assertEqual((alerta.atendida, alerta.nota), (True, cobro.NOTA_SIGUIENTE))
        self.assertEqual(alerta.atendida_at, self.t0 + timedelta(seconds=5))

    def test_saldo_insuficiente_paga_en_efectivo_sin_tocar_el_saldo(self):
        _, pendiente = self._pasar(0, "1987FHK")
        self._pasar(2, "3876LBD")
        pendiente.refresh_from_db()
        self.assertEqual((pendiente.estado, pendiente.forma_pago), ("cobrado", "efectivo"))
        self.assertEqual(Vehiculo.objects.get(placa="1987FHK").saldo, Decimal("1"))

    def test_un_cobrado_con_saldo_no_cambia_al_pasar_el_siguiente(self):
        _, p1 = self._pasar(0, "3876LBD")
        r = self._f(2, "7777XYZ")
        self.assertNotIn("resuelto", r)
        p1.refresh_from_db()
        self.assertEqual((p1.estado, p1.forma_pago), ("cobrado", "saldo"))

    def test_cobrar_en_efectivo_solo_una_vez(self):
        _, pendiente = self._pasar(0, "7777XYZ")
        self.assertTrue(cobro.cobrar_en_efectivo(pendiente, cobro.NOTA_SIGUIENTE))
        self.assertFalse(cobro.cobrar_en_efectivo(pendiente, cobro.NOTA_OPERADOR))
        self.assertEqual(Alerta.objects.get(paso=pendiente).nota, cobro.NOTA_SIGUIENTE)


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix="iap-test-"), CLOUDINARY_URL="", **PUSHER)
class ApiYTiempoRealTests(Base):
    def setUp(self):
        super().setUp()
        self.client = self.client_class()
        entrar(self.client, Usuario.objects.get(email_google="lozogomol@gmail.com"))

    def _frame(self, placa):
        with mock.patch(INFERIR, return_value=([_lectura(placa, 0.9, categoria="auto", ejes=2)], 50,
                                               {**SIN_VEHICULO, "clase": "auto", "ejes": 2})):
            return self.client.post("/api/v1/pasos/frame", {"imagen": _jpeg(), "caseta_id": self.caseta.id}).json()

    def test_el_siguiente_vehiculo_emite_pago_por_pusher_y_resuelve_la_tarjeta_anterior(self):
        with mock.patch.object(tiempo_real, "en_segundo_plano"), mock.patch.object(tiempo_real, "notificar"):
            self._frame("7777XYZ")
            d = self._frame("7777XYZ")
        self.assertEqual((d["accion"], d["paso"]["estado"], d["paso"]["resultado_texto"]),
                         ("registrado", "pendiente_pago", "Pendiente de pago (no registrada)"))
        self.assertEqual(d["mensaje"], "Tarifa: 4.00 Bs · Pendiente de pago (no registrada)")
        with mock.patch.object(tiempo_real, "notificar") as notificar:
            d = self._frame("3876LBD")
        self.assertEqual((d["accion"], d["anterior"]["placa"], d["anterior"]["resultado_texto"],
                          d["anterior"]["forma_pago"]), ("en_curso", "7777XYZ", "Cobrado en efectivo", "efectivo"))
        evento, datos = notificar.call_args.args
        self.assertEqual((evento, datos["paso"]["placa"], datos["paso"]["forma_pago"], datos["alerta"]["nota"]),
                         ("pago", "7777XYZ", "efectivo", cobro.NOTA_SIGUIENTE))
        self.assertEqual(datos["kpis"]["recaudacion_efectivo"], "4.00")


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix="iap-test-"), CLOUDINARY_URL="", PUSHER_APP_ID="")
class AlertaAtendidaTests(Base):
    def test_marcar_alerta_atendida_cobra_en_efectivo(self):
        _, pendiente = self._pasar(0, "1987FHK")
        alerta = Alerta.objects.get(paso=pendiente)
        entrar(self.client, Usuario.objects.get(email_google="lozogomol@gmail.com"))
        with mock.patch.object(tiempo_real, "notificar") as notificar:
            r = self.client.post(f"/alertas/{alerta.id}/atender")
        self.assertEqual(r.status_code, 302)
        pendiente.refresh_from_db()
        alerta.refresh_from_db()
        self.assertEqual((pendiente.estado, pendiente.forma_pago), ("cobrado", "efectivo"))
        self.assertEqual((alerta.atendida, alerta.nota), (True, cobro.NOTA_OPERADOR))
        self.assertIsNotNone(alerta.atendida_at)
        self.assertEqual(Vehiculo.objects.get(placa="1987FHK").saldo, Decimal("1"))
        self.assertEqual(notificar.call_args.args[0], "pago")
        # la lista de atendidas muestra cómo se resolvió
        self.assertContains(self.client.get("/alertas?filtro=atendidas"), "Cobrado por el operador")
        # atenderla otra vez no cambia nada
        self.assertFalse(cobro.atender_alerta(alerta))

    def test_alerta_resuelta_por_el_siguiente_vehiculo_muestra_su_nota(self):
        self._pasar(0, "7777XYZ")
        self._pasar(2, "3876LBD")
        entrar(self.client, Usuario.objects.get(email_google="lozogomol@gmail.com"))
        r = self.client.get("/alertas?filtro=atendidas")
        self.assertContains(r, "7777XYZ")
        self.assertContains(r, "Cobrado en efectivo al pasar el siguiente vehículo")
        self.assertContains(self.client.get("/alertas"), "No hay alertas pendientes")


class RecaudacionTests(Base):
    def test_recaudacion_suma_saldo_y_efectivo_con_desglose(self):
        Paso.objects.all().delete()
        self._pasar(0, "3876LBD")                    # 4 con saldo
        self._pasar(2, "7777XYZ", "camion", 3)       # 10 pendiente → efectivo al pasar el siguiente
        self._pasar(4, "1987FHK")                    # 4 pendiente (aún nadie pasó después)
        k = panel.kpis()
        self.assertEqual((k["recaudacion_hoy"], k["recaudacion_saldo"], k["recaudacion_efectivo"]),
                         ("14.00", "4.00", "10.00"))
        self.assertEqual(Paso.objects.filter(estado="pendiente_pago").count(), 1)
        entrar(self.client, Usuario.objects.get(email_google="lozogomol@gmail.com"))
        r = self.client.get("/")
        self.assertContains(r, 'id="k-saldo">4.00<')
        self.assertContains(r, 'id="k-efectivo">10.00<')
        # el historial muestra estado y forma de pago
        r = self.client.get("/pasos")
        self.assertContains(r, '<td data-label="Forma de pago">Efectivo</td>', html=False)
        self.assertContains(r, '<td data-label="Forma de pago">Saldo</td>', html=False)
        self.assertContains(r, "Pendiente de pago (saldo insuficiente)")


@override_settings(GEMINI_API_KEY="")
class ConsultaYAsistenteTests(Base):
    def test_no_registrada_pagada_en_efectivo_sigue_identificada_no_registrada(self):
        self._pasar(0, "7777XYZ")
        self._pasar(2, "3876LBD")
        d = agente.consultar_placa("7777XYZ")
        self.assertEqual((d["estado"], d["ultimo_paso"]["cobrado"], d["ultimo_paso"]["forma_pago"],
                          d["ultimo_paso"]["resultado"]),
                         ("identificada_no_registrada", True, "Efectivo", "Cobrado en efectivo"))
        r = agente.responder("¿Pasó la placa 7777XYZ?")
        self.assertIn("NO está registrada (posible evasión)", r["respuesta"])
        self.assertIn("(Cobrado en efectivo)", r["respuesta"])
        c = cobro.consultar_placa("7777XYZ")
        self.assertEqual((c["estado"], c["ultimo_paso"]["estado"], c["ultimo_paso"]["forma_pago_texto"]),
                         ("no_registrada", "cobrado", "Efectivo"))

    def test_resumen_dia_con_desglose_y_pendientes(self):
        Paso.objects.all().delete()
        self._pasar(0, "3876LBD")
        self._pasar(2, "7777XYZ")
        self._pasar(4, "1987FHK")
        d = agente.resumen_dia("hoy")
        self.assertEqual((d["recaudado_bs"], d["recaudado_saldo_bs"], d["recaudado_efectivo_bs"],
                          d["pendientes_de_pago"], d["no_cobrados"]["saldo_insuficiente"]),
                         ("8.00", "4.00", "4.00", 1, 1))
        self.assertIn("(saldo 4.00 Bs, efectivo 4.00 Bs)", agente.plantilla(d))


class MigracionTests(TestCase):
    def test_adapta_los_pasos_existentes(self):
        migracion = importlib.import_module("peaje.migrations.0011_cobro_en_efectivo")
        caseta = caseta_principal()
        cobrado = Paso.objects.create(placa_leida="A", placa_normalizada="3876LBD", caseta=caseta, estado="cobrado")
        viejo = Paso.objects.create(placa_leida="B", placa_normalizada="7777XYZ", caseta=caseta,
                                    estado="cobrado", forma_pago="")
        Paso.objects.filter(pk=viejo.pk).update(estado="no_registrada")
        migracion.adaptar_pasos(django_apps, None)
        cobrado.refresh_from_db()
        viejo.refresh_from_db()
        self.assertEqual((cobrado.forma_pago, viejo.estado, viejo.forma_pago), ("saldo", "pendiente_pago", ""))


class PantallaCasetaTests(TestCase):
    def test_tarjeta_del_vehiculo_anterior(self):
        entrar(self.client, Usuario.objects.get(email_google="lozogomol@gmail.com"))
        r = self.client.get("/caseta")
        self.assertContains(r, 'id="anterior"')
        self.assertContains(r, 'p.estado === "pendiente_pago" ? "ambar"')
