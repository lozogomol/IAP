"""Tests del modo caseta (votación, cobro, API), consulta y solicitudes de acceso."""
import json
import tempfile
from datetime import timedelta
from decimal import Decimal
from unittest import mock

import base64

import requests

from pathlib import Path

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone

from . import cobro, flujo
from .auth import emitir_jwt
from .inferencia import InferenciaNoDisponible, interpretar, interpretar_vehiculo
from .models import Alerta, Caseta, LecturaPlaca, Paso, PasoEnCurso, Tarifa, Usuario, Vehiculo
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
        v = interpretar_vehiculo(datos)
        self.assertEqual((v["clase"], v["confianza_clase"], v["caja_vehiculo"], v["cajas_placas"], v["ejes"],
                          v["pasajeros"], v["evidencia"]),
                         ("minibus", 0.881, [1, 2, 3, 4], [[5, 6, 7, 8]], None, None, None))

    def test_respuesta_completa_m1_a_m4(self):
        evidencia = b"\xff\xd8\xff\xe0cubierta"
        datos = {"placa": "2451KTP", "confianza": 93.4, "placas": [{"texto": "2451KTP", "confianza_lectura": 93.4}],
                 "categoria": "camion", "confianza_categoria": 90, "ejes": 3, "llantas_detectadas": 5,
                 "pasajeros": 2, "latencia_ms": {"total": 900, "m1": 40, "m2": 300, "m3": 200, "m4": 250},
                 "cajas": {"llantas": [{"caja": [1, 2, 3, 4], "centro": [2, 3]}], "personas": [[5, 5, 9, 9]]},
                 "detalle": {"motivo_ejes": "5 llantas del lado cercano en 3 grupo(s)"},
                 "evidencia": "data:image/jpeg;base64," + base64.b64encode(evidencia).decode()}
        v = interpretar_vehiculo(datos)
        self.assertEqual((v["ejes"], v["llantas_detectadas"], v["pasajeros"], v["cajas_llantas"], v["evidencia"]),
                         (3, 5, 2, [[1, 2, 3, 4]], evidencia))
        self.assertEqual(v["latencias"], {"m1": 40, "m2": 300, "m3": 200, "m4": 250})
        self.assertIn("3 grupo", v["detalle_ejes"])
        self.assertEqual(interpretar(datos)[0]["ejes"], 3)

    def test_datos_invalidos_no_se_inventan(self):
        v = interpretar_vehiculo({"ejes": 40, "pasajeros": -1, "evidencia": "no-es-imagen", "cajas": "x",
                                  "latencia_ms": None})
        self.assertEqual((v["ejes"], v["pasajeros"], v["evidencia"], v["cajas_llantas"]), (None, None, None, []))

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

    def test_ejes_y_pasajeros_mas_repetidos(self):
        for ejes, pasajeros in ((3, 1), (3, 2), (2, 2), (None, None)):
            LecturaPlaca.objects.create(caseta=self.caseta, placa="4471LCH", confianza=0.7,
                                        categoria="camion", ejes=ejes, pasajeros=pasajeros)
        v = cobro.votar(self.caseta)
        self.assertEqual((v["ejes"], v["pasajeros"]), (3, 2))

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
                         ("cobrado", Decimal("4"), "auto", 2))
        self.assertEqual(info["fuente_categoria"], "registro")
        self.assertEqual(Vehiculo.objects.get(placa="3876LBD").saldo, Decimal("246"))

    def test_usa_categoria_de_inferencia_si_llega(self):
        paso, _, info = cobro.registrar_paso(self.caseta.id, "3876LBD", categoria="camion", ejes=3)
        self.assertEqual((paso.monto, info["fuente_categoria"]), (Decimal("10"), "inferencia"))

    def test_tarifa_categoria_mas_ejes_detectados(self):
        # moto 2, auto 4, minibus 6, bus 8, camion 8 Bs con 2 ejes; +2 Bs por cada eje adicional
        for categoria, ejes, monto in (("moto", 2, 2), ("auto", 2, 4), ("minibus", 2, 6), ("bus", 2, 8),
                                       ("camion", 2, 8), ("camion", 3, 10), ("camion", 6, 16), ("bus", 4, 12),
                                       ("auto", 3, 6), ("minibus", 5, 12)):
            self.assertEqual(cobro._tarifa(categoria, ejes), Decimal(monto), (categoria, ejes))
        self.assertEqual(cobro._tarifa("camion", None), Decimal("8"))  # sin dato de M3: mínimo 2 ejes
        self.assertEqual(cobro._tarifa("camion", 1), Decimal("8"))
        self.assertIsNone(cobro._tarifa("", 2))

    def test_tarifa_sin_fila_usa_base_mas_2_por_eje(self):
        self.assertEqual(cobro.tarifa_de("camion", 9), (Decimal("22"), 9))  # no hay fila: 8 + 2·7
        self.assertEqual(cobro._tarifa("moto", 3), Decimal("4"))           # moto solo tiene 2 ejes en la tabla
        Tarifa.objects.filter(categoria="bus").delete()
        self.assertEqual(cobro._tarifa("bus", 3), Decimal("10"))            # sin filas: base fija + 2

    def test_tabla_de_tarifas(self):
        self.assertEqual(Tarifa.objects.count(), 21)
        self.assertEqual(list(Tarifa.objects.filter(categoria="auto").values_list("ejes", "monto")),
                         [(e, Decimal(4 + 2 * (e - 2))) for e in range(2, 7)])
        self.assertEqual(list(Tarifa.objects.filter(categoria="moto").values_list("ejes", "monto")), [(2, Decimal(2))])

    def test_guarda_ejes_y_pasajeros_y_cobra_por_ejes(self):
        Vehiculo.objects.filter(placa="3876LBD").update(saldo=Decimal("100"))
        paso, _, info = cobro.registrar_paso(self.caseta.id, "3876LBD", categoria="camion", ejes=4, pasajeros=2)
        self.assertEqual((paso.monto, paso.ejes_detectados, paso.pasajeros), (Decimal("12"), 4, 2))
        d = cobro.paso_a_dict(paso, info)
        self.assertEqual((d["ejes"], d["ejes_tarifa"], d["pasajeros"], d["tarifa"]), (4, 4, 2, "12.00"))

    def test_categoria_detectada_sin_ejes_usa_2_ejes(self):
        # 3876LBD está registrado como auto; M2 detecta minibus y M3 aún no existe → tarifa minibus/2 ejes
        paso, _, info = cobro.registrar_paso(self.caseta.id, "3876LBD", categoria="minibus")
        self.assertEqual((paso.estado, paso.monto, paso.categoria_detectada, paso.ejes_detectados),
                         ("cobrado", Decimal("6"), "minibus", None))
        self.assertEqual(info["fuente_categoria"], "inferencia")
        d = cobro.paso_a_dict(paso, info)
        self.assertEqual((d["categoria"], d["categoria_texto"], d["tarifa"], d["ejes"]),
                         ("minibus", "Minibús", "6.00", None))
        self.assertEqual(cobro.paso_a_dict(paso)["tarifa"], "6.00")  # también sin info

    def test_no_registrada_conserva_categoria_detectada(self):
        paso, _, info = cobro.registrar_paso(self.caseta.id, "6999ZZZ", categoria="moto")
        self.assertEqual((paso.estado, paso.monto, paso.categoria_detectada), ("pendiente_pago", Decimal("2"), "moto"))
        self.assertEqual(info["fuente_categoria"], "inferencia")

    def test_saldo_insuficiente(self):
        paso, _, _ = cobro.registrar_paso(self.caseta.id, "1987FHK")
        self.assertEqual((paso.estado, paso.monto), ("pendiente_pago", Decimal("4")))  # paga en efectivo
        self.assertTrue(Alerta.objects.filter(paso=paso, tipo="saldo_insuficiente").exists())

    def test_no_registrada_categoria_desconocida(self):
        paso, _, info = cobro.registrar_paso(self.caseta.id, "6999ZZZ")
        self.assertEqual((paso.estado, paso.monto, paso.categoria_detectada), ("pendiente_pago", Decimal("0"), ""))
        d = cobro.paso_a_dict(paso, info)
        self.assertEqual((d["categoria"], d["tarifa"], d["fuente_categoria"]), ("desconocida", None, "desconocida"))
        self.assertTrue(Alerta.objects.filter(paso=paso, tipo="placa_no_registrada").exists())

    def test_inactivo_no_se_cobra(self):
        paso, _, _ = cobro.registrar_paso(self.caseta.id, "654TRB")
        self.assertEqual((paso.estado, paso.monto, paso.forma_pago), ("pendiente_pago", Decimal("2"), ""))
        self.assertEqual(Vehiculo.objects.get(placa="654TRB").saldo, Decimal("40"))

    def test_antiduplicado_30s(self):
        p1, _, _ = cobro.registrar_paso(self.caseta.id, "3876LBD")
        p2, dup, _ = cobro.registrar_paso(self.caseta.id, "3876LBD")
        self.assertTrue(dup)
        self.assertEqual(p1.pk, p2.pk)
        self.assertEqual(Vehiculo.objects.get(placa="3876LBD").saldo, Decimal("246"))
        Paso.objects.filter(pk=p1.pk).update(created_at=timezone.now() - timedelta(seconds=31))
        _, dup, _ = cobro.registrar_paso(self.caseta.id, "3876LBD")
        self.assertFalse(dup)


def pasar_vehiculo(client, caseta, lecturas, vehiculo=None, **extra):
    """Un vehículo completo por la API: dos frames con su placa (bloquea y vota tipo/ejes) y, si aún faltan datos,
    pasados los 6 s un frame vacío (se completa con respaldos y cobra). Devuelve (primera respuesta, la del cobro)."""
    url = "/api/v1/pasos/frame"
    with mock.patch(INFERIR, return_value=(lecturas, 50, vehiculo or SIN_VEHICULO)):
        primero = client.post(url, {"imagen": _jpeg(), "caseta_id": caseta.id}, **extra)
        segundo = client.post(url, {"imagen": _jpeg(), "caseta_id": caseta.id}, **extra)
    if segundo.json().get("accion") == "registrado":
        return primero, segundo
    PasoEnCurso.objects.filter(caseta=caseta).update(bloqueada_at=timezone.now() - timedelta(seconds=7))
    with mock.patch(INFERIR, return_value=([], 50, SIN_VEHICULO)):
        return primero, client.post(url, {"imagen": _jpeg(), "caseta_id": caseta.id}, **extra)


class FlujoTests(TestCase):
    """Máquina de estados del punto de cobro: un vehículo = un cobro, con resultado fijo por placa."""

    def setUp(self):
        self.caseta = Caseta.objects.first()
        self.t0 = timezone.now()

    def _f(self, segundos, lecturas=(), vehiculo=None):
        return flujo.procesar_frame(self.caseta, list(lecturas), vehiculo or SIN_VEHICULO, b"\xff\xd8frame",
                                    ahora=self.t0 + timedelta(seconds=segundos))

    def _con(self, clase="", ejes=None, pasajeros=None):
        return {**SIN_VEHICULO, "clase": clase, "caja_vehiculo": [0, 0, 100, 100], "ejes": ejes, "pasajeros": pasajeros}

    def _completo(self, t, placa, categoria="auto", ejes=2):
        """Dos frames con tipo y ejes: el paso se completa y cobra en el segundo."""
        self._f(t, [_lectura(placa, 0.9, categoria=categoria, ejes=ejes)], self._con(categoria, ejes))
        return self._f(t + 0.5, [_lectura(placa, 0.9, categoria=categoria, ejes=ejes)], self._con(categoria, ejes))

    def test_placa_bloqueada_no_cambia(self):
        r = self._f(0, [_lectura("3876LBD", 0.9, categoria="auto")], self._con("auto"))
        self.assertEqual((r["accion"], r["flujo"]["placa"], r["flujo"]["placa_bloqueada"]), ("en_curso", "3876LBD", True))
        for t in (0.5, 1, 1.5):  # otra lectura (incluso más confiable) no cambia la placa del paso
            r = self._f(t, [_lectura("2451KTP", 0.99, categoria="moto")], self._con("moto"))
            self.assertEqual(r["flujo"]["placa"], "3876LBD")
        r = self._f(6, [])
        self.assertEqual((r["accion"], r["paso"].placa_normalizada), ("registrado", "3876LBD"))
        self.assertFalse(Paso.objects.filter(placa_normalizada="2451KTP").exists())

    def test_votacion_antes_de_bloquear(self):
        self.assertEqual(self._f(0, [_lectura("3876LBD", 0.6)])["accion"], "esperando_confirmacion")
        r = self._f(0.5, [_lectura("3876LBD", 0.6)])
        self.assertEqual((r["accion"], r["flujo"]["placa"]), ("en_curso", "3876LBD"))

    def test_tipo_por_mayoria_y_se_bloquea(self):
        self._f(0, [_lectura("5630KRT", 0.9, categoria="minibus")], self._con("minibus"))
        self.assertFalse(self._f(0.5, [_lectura("5630KRT", 0.8, categoria="auto")], self._con("auto"))["flujo"]["tipo_listo"])
        r = self._f(1, [_lectura("5630KRT", 0.8, categoria="minibus")], self._con("minibus"))
        self.assertEqual((r["flujo"]["tipo"], r["flujo"]["tipo_listo"], r["flujo"]["etapa"]), ("minibus", True, "ejes"))
        for t in (1.5, 2, 2.5):  # ya bloqueado: votos posteriores no lo cambian
            self._f(t, [_lectura("5630KRT", 0.8, categoria="auto")], self._con("auto"))
        paso = self._f(6, [])["paso"]
        self.assertEqual((paso.categoria_detectada, paso.monto), ("minibus", Decimal("6")))

    def test_con_tipo_y_ejes_cobra_al_instante(self):
        self._f(0, [_lectura("4720MDX", 0.9, categoria="camion", ejes=4)], self._con("camion", 4))
        r = self._f(0.5, [_lectura("4720MDX", 0.9, categoria="camion", ejes=3)], self._con("camion", 3))
        self.assertEqual(r["accion"], "en_curso")  # tipo listo, ejes empatados
        r = self._f(1, [_lectura("4720MDX", 0.9, categoria="camion", ejes=4)], self._con("camion", 4))
        self.assertEqual((r["accion"], r["motivo"]), ("registrado", "completo"))
        self.assertEqual((r["paso"].ejes_detectados, r["paso"].monto, r["paso"].estado), (4, Decimal("12"), "cobrado"))
        self.assertEqual(r["evidencia"], b"\xff\xd8frame")  # mejor frame de la placa

    def test_pasajeros_maximo_estable(self):
        self._f(0, [_lectura("4471LCH", 0.9, categoria="bus")], self._con("bus", None, 1))
        for t, n in ((0.5, 3), (1, 3), (1.5, 2), (1.8, 5)):  # 5 se vio una sola vez: no es estable
            self._f(t, [], self._con("bus", None, n))
        self.assertEqual(self._f(6, [])["paso"].pasajeros, 3)

    def test_misma_placa_no_genera_segundo_paso(self):
        r = self._completo(0, "3876LBD")
        self.assertEqual(r["accion"], "registrado")
        paso = r["paso"]
        for i in range(60):  # 9 minutos con la misma placa en cuadro (y frames sin placa en medio)
            t = 1 + i * 9
            r = self._f(t, [_lectura("3876LBD", 0.95, categoria="camion", ejes=5)], self._con("camion", 5))
            self.assertEqual((r["accion"], r["paso"].pk), ("fijo", paso.pk))
            self.assertEqual(self._f(t + 2, [], self._con("auto", 2))["accion"], "fijo")
        self.assertEqual(Paso.objects.filter(placa_normalizada="3876LBD").count(), 1)
        self.assertEqual(Vehiculo.objects.get(placa="3876LBD").saldo, Decimal("246"))  # un solo cobro
        self.assertEqual(flujo.estado_de(self.caseta)["etapa"], "registrado")

    def test_lectura_suelta_de_otra_placa_no_cambia_la_pantalla(self):
        paso = self._completo(0, "3876LBD")["paso"]
        r = self._f(2, [_lectura("2451KTP", 0.6)])
        self.assertEqual((r["accion"], r["paso"].pk, r["candidata"]), ("fijo", paso.pk, "2451KTP"))

    def test_placa_distinta_inicia_nuevo_paso(self):
        p1 = self._completo(0, "3876LBD")["paso"]
        r = self._f(2, [_lectura("2451KTP", 0.9)], self._con())
        self.assertEqual((r["accion"], r["flujo"]["placa"], r["flujo"]["etapa"]), ("en_curso", "2451KTP", "tipo"))
        self.assertIsNone(PasoEnCurso.objects.get(caseta=self.caseta).paso)  # la tarjeta se limpia
        p2 = self._completo(2.5, "2451KTP", "bus", 2)["paso"]
        self.assertNotEqual(p1.pk, p2.pk)
        # después de otra placa distinta, la primera vuelve a cobrarse aunque no pasaron 10 min
        p3 = self._completo(10, "3876LBD")["paso"]
        self.assertEqual(p3.placa_normalizada, "3876LBD")
        self.assertEqual(Paso.objects.filter(placa_normalizada="3876LBD").count(), 2)

    def test_sin_tipo_ni_ejes_se_completa_con_respaldos_a_los_6s(self):
        # No registrada y M2 nunca vio el vehículo: "auto" con 2 ejes estimados.
        self._f(0, [_lectura("6999ZZZ", 0.9)])
        self.assertEqual(self._f(5.9, [])["accion"], "en_curso")
        r = self._f(6, [])
        self.assertEqual((r["accion"], r["motivo"]), ("registrado", "tiempo"))
        p = r["paso"]
        self.assertEqual((p.categoria_detectada, p.ejes_detectados, p.ejes_texto, p.monto, p.estado),
                         ("auto", None, "2 (estimado)", Decimal("4"), "pendiente_pago"))
        self.assertEqual(r["info"]["fuente_categoria"], "respaldo")
        self.assertEqual(self._f(7, [])["accion"], "fijo")  # una sola vez

    def test_sin_tipo_usa_el_registro_y_luego_la_clase_mas_vista(self):
        # Registrado (bus) y sin mayoría de tipo: categoría del registro.
        self._f(0, [_lectura("4471LCH", 0.9, categoria="moto")], self._con("moto"))
        r = self._f(6, [])
        self.assertEqual((r["paso"].categoria_detectada, r["info"]["fuente_categoria"], r["paso"].monto),
                         ("bus", "registro", Decimal("8")))
        # No registrado: la clase de M2 más vista, aunque no llegó a mayoría.
        self._f(10, [_lectura("7777XYZ", 0.9, categoria="camion", ejes=3)], self._con("camion", 3))
        r = self._f(16, [])
        self.assertEqual((r["paso"].categoria_detectada, r["paso"].ejes_detectados, r["paso"].monto),
                         ("camion", 3, Decimal("10")))

    def test_misma_placa_vuelve_a_cobrarse_tras_10_min_sin_verla(self):
        self._completo(0, "3876LBD")
        self.assertEqual(self._f(9 * 60, [_lectura("3876LBD", 0.9)])["accion"], "fijo")  # visto a los 9 min
        for t in (12 * 60, 18 * 60):  # sin verla: la pantalla sigue fija
            self.assertEqual(self._f(t, [])["accion"], "fijo")
        r = self._f(9 * 60 + 10 * 60 + 1, [_lectura("3876LBD", 0.9, categoria="auto")], self._con("auto"))
        self.assertEqual((r["accion"], r["flujo"]["placa"]), ("en_curso", "3876LBD"))
        self.assertEqual(self._f(9 * 60 + 10 * 60 + 8, [])["accion"], "registrado")
        self.assertEqual(Paso.objects.filter(placa_normalizada="3876LBD").count(), 2)

    def test_alertas_no_registrada_y_saldo_insuficiente(self):
        p1 = self._completo(0, "9999ZZZ")["paso"]
        p2 = self._completo(2, "1987FHK")["paso"]
        p1.refresh_from_db()  # al confirmarse la placa de 1987FHK, el paso de 9999ZZZ se cobró en efectivo
        self.assertEqual((p1.estado, p1.forma_pago, p2.estado), ("cobrado", "efectivo", "pendiente_pago"))
        self.assertTrue(Alerta.objects.get(paso=p1).atendida)
        self.assertTrue(Alerta.objects.filter(paso=p1, tipo="placa_no_registrada").exists())
        self.assertTrue(Alerta.objects.filter(paso=p2, tipo="saldo_insuficiente").exists())

    def test_camara_detenida_completa_el_pendiente(self):
        self._f(0, [_lectura("3876LBD", 0.9)], self._con())
        r = self._f(20, [_lectura("2451KTP", 0.9)], self._con())  # vuelve a encender con otro vehículo
        self.assertEqual((r["accion"], r["paso"].placa_normalizada), ("registrado", "3876LBD"))


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

    def _pasar(self, lecturas, vehiculo=None):
        return pasar_vehiculo(self.client, self.caseta, lecturas, vehiculo, HTTP_X_CSRFTOKEN=self.csrf)

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
        self.assertEqual((d["flujo"]["etapa"], d["flujo"]["placa_bloqueada"]), ("placa", False))
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

    def test_registra_ejes_pasajeros_y_evidencia_cubierta(self):
        """El paso guarda ejes y pasajeros, cobra por ejes y la captura es la evidencia con personas cubiertas."""
        evidencia = b"\xff\xd8\xff\xe0personas-cubiertas"
        vehiculo = {**SIN_VEHICULO, "clase": "camion", "ejes": 3, "pasajeros": 1, "llantas_detectadas": 4,
                    "cajas_llantas": [[1, 2, 3, 4]], "latencias": {"m1": 1, "m2": 2, "m3": 3, "m4": 4},
                    "evidencia": evidencia}
        primero, ultimo = self._pasar([_lectura("3876LBD", 0.9, categoria="camion", ejes=3)], vehiculo)
        lec = primero.json()["lectura"]
        self.assertEqual(primero.json()["accion"], "en_curso")
        self.assertEqual((lec["ejes"], lec["pasajeros"], lec["llantas_detectadas"], lec["cajas_llantas"]),
                         (3, 1, 4, [[1, 2, 3, 4]]))
        self.assertEqual(lec["latencia_modelos_ms"], {"m1": 1, "m2": 2, "m3": 3, "m4": 4})
        d = ultimo.json()
        self.assertEqual((d["accion"], d["motivo"], d["mensaje"]), ("registrado", "completo", "Tarifa: 10.00 Bs · Cobrado con saldo"))
        p = d["paso"]
        self.assertEqual((p["categoria"], p["ejes"], p["pasajeros"], p["tarifa"]), ("camion", 3, 1, "10.00"))
        self.assertEqual(base64.b64decode(d["captura"].split(",", 1)[1]), evidencia)
        paso = Paso.objects.get(pk=p["id"])
        self.assertEqual((paso.ejes_detectados, paso.pasajeros), (3, 1))
        with open(Path(settings.MEDIA_ROOT) / paso.imagen_url.removeprefix("/"), "rb") as f:
            self.assertEqual(f.read(), evidencia)  # nunca el frame original con rostros

    def test_sin_personas_guarda_el_frame(self):
        d = self._pasar([_lectura("3876LBD", 0.9)])[1].json()
        paso = Paso.objects.get(pk=d["paso"]["id"])
        with open(Path(settings.MEDIA_ROOT) / paso.imagen_url.removeprefix("/"), "rb") as f:
            self.assertEqual(f.read(), b"\xff\xd8\xff\xe0falso")

    def test_votacion_bloqueo_y_cobro_automatico(self):
        d = self._frame([_lectura("3876LBD", 0.7, caja=[1, 2, 3, 4])]).json()
        self.assertEqual((d["accion"], d["mensaje"], d["votos"], d["votos_necesarios"], d["paso"]),
                         ("esperando_confirmacion", "Leyendo 3876LBD (confirmando 1/2)", 1, 2, None))
        self.assertEqual((d["lectura"]["placa"], d["lectura"]["confianza"], d["lectura"]["caja_placa"]),
                         ("3876LBD", 0.7, [1, 2, 3, 4]))
        d = self._frame([_lectura("3876LBD", 0.75)]).json()
        self.assertEqual((d["accion"], d["flujo"]["placa"], d["flujo"]["placa_bloqueada"], d["flujo"]["etapa"]),
                         ("en_curso", "3876LBD", True, "tipo"))
        self.assertEqual(d["mensaje"], "Placa 3876LBD · detectando tipo")
        self.assertFalse(Paso.objects.filter(placa_normalizada="3876LBD").exists())  # aún faltan tipo y ejes
        PasoEnCurso.objects.filter(caseta=self.caseta).update(bloqueada_at=timezone.now() - timedelta(seconds=7))
        d = self._frame([]).json()
        self.assertEqual((d["accion"], d["mensaje"]), ("registrado", "Tarifa: 4.00 Bs · Cobrado con saldo"))
        p = d["paso"]
        self.assertEqual((p["placa"], p["estado"], p["tarifa"], p["categoria"], p["ejes"], p["ejes_texto"],
                          p["fuente_categoria"]), ("3876LBD", "cobrado", "4.00", "auto", None, "2 (estimado)", "registro"))
        self.assertEqual(d["flujo"]["etapa"], "registrado")

    def test_una_lectura_confiable_bloquea_al_instante(self):
        d = self._frame([_lectura("3876LBD", 0.80)]).json()
        self.assertEqual((d["accion"], d["flujo"]["placa_bloqueada"]), ("en_curso", True))

    def test_alerta_en_el_mensaje(self):
        d = self._pasar([_lectura("9999ZZZ", 0.9)])[1].json()
        self.assertEqual(d["mensaje"], "Tarifa: 4.00 Bs · Pendiente de pago (no registrada)")  # sin tipo: "auto"

    def test_misma_placa_queda_fija_y_no_cobra_otra_vez(self):
        registro = self._pasar([_lectura("3876LBD", 0.9)])[1].json()
        for _ in range(20):  # muchos frames de la misma placa (y alguno sin placa): mismo resultado, sin otro paso
            d = self._frame([_lectura("3876LBD", 0.95)]).json()
            self.assertEqual((d["accion"], d["paso"]["id"], d["mensaje"]),
                             ("fijo", registro["paso"]["id"], "Tarifa: 4.00 Bs · Cobrado con saldo"))
            self.assertEqual(d["flujo"]["etapa"], "registrado")
        self.assertEqual(self._frame([]).json()["accion"], "fijo")
        self.assertEqual(Paso.objects.filter(placa_normalizada="3876LBD", caseta=self.caseta).count(), 1)
        self.assertEqual(Vehiculo.objects.get(placa="3876LBD").saldo, Decimal("246"))

    def test_misma_placa_se_cobra_otra_vez_tras_10_min_sin_verla(self):
        self._pasar([_lectura("3876LBD", 0.9)])
        PasoEnCurso.objects.update(ultimo_visto_at=timezone.now() - timedelta(minutes=10, seconds=1))
        LecturaPlaca.objects.all().delete()
        self.assertEqual(self._pasar([_lectura("3876LBD", 0.9)])[1].json()["accion"], "registrado")
        self.assertEqual(Paso.objects.filter(placa_normalizada="3876LBD").count(), 2)

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
        d = self._frame([_lectura("3876LBD", 0.75, categoria="camion")]).json()
        self.assertEqual((d["flujo"]["tipo"], d["flujo"]["tipo_listo"]), ("camion", True))
        PasoEnCurso.objects.filter(caseta=self.caseta).update(bloqueada_at=timezone.now() - timedelta(seconds=7))
        p = self._frame([]).json()["paso"]
        self.assertEqual((p["categoria"], p["categoria_texto"], p["ejes"], p["tarifa"], p["fuente_categoria"]),
                         ("camion", "Camión", None, "8.00", "inferencia"))

    def test_inferencia_caida_error(self):
        with self.assertLogs("peaje.caseta", level="INFO") as logs:
            r = self._post_error(InferenciaNoDisponible("timeout"))
        self.assertEqual(r.status_code, 502)
        d = r.json()
        self.assertEqual((d["accion"], d["lectura"], d["paso"]), ("error", None, None))
        self.assertIn("Sin conexión con la inferencia", d["mensaje"])
        self.assertIn("accion=error", logs.output[0])

    def test_jwt_tambien_funciona(self):
        cliente = self.client_class()
        _, r = pasar_vehiculo(cliente, self.caseta, [_lectura("2451KTP", 0.95)],
                              HTTP_AUTHORIZATION=f"Bearer {emitir_jwt(self.admin)}")
        self.assertEqual(r.json()["paso"]["estado"], "cobrado")


class ConsultaTests(TestCase):
    def setUp(self):
        entrar(self.client, Usuario.objects.get(email_google="lozogomol@gmail.com"))

    def _c(self, placa):
        return self.client.get("/api/v1/vehiculos/consulta", {"placa": placa}).json()

    def test_ultimo_paso_con_categoria_ejes_y_pasajeros(self):
        cobro.registrar_paso(Caseta.objects.first().id, "3876LBD", categoria="auto", ejes=2, pasajeros=3)
        u = self._c("3876LBD")["ultimo_paso"]
        self.assertEqual((u["categoria"], u["categoria_texto"], u["ejes"], u["pasajeros"]), ("auto", "Auto", 2, 3))

    def test_historial_de_pasos(self):
        caseta = Caseta.objects.first()
        cobro.registrar_paso(caseta.id, "3876LBD", categoria="camion", ejes=3, pasajeros=2)
        cobro.registrar_paso(caseta.id, "2451KTP", categoria="moto")
        r = self.client.get("/pasos")
        self.assertContains(r, "<h1>Historial de pasos</h1>", html=False)
        self.assertContains(r, '<td data-label="Ejes" class="num">3</td>', html=False)
        self.assertContains(r, '<td data-label="Pasajeros" class="num">2</td>', html=False)
        self.assertContains(r, '<td data-label="Pasajeros" class="num">–</td>', html=False)  # sin dato
        self.assertContains(r, "Camión")
        r = self.client.get("/pasos?q=2451")
        self.assertContains(r, "2451KTP")
        self.assertNotContains(r, "3876LBD")
        r = self.client.get("/pasos?fecha=" + timezone.localdate().isoformat())
        self.assertContains(r, "2451KTP")
        r = self.client.get("/pasos?fecha=2000-01-01")
        self.assertContains(r, "Sin pasos registrados")
        self.assertEqual(self.client.get("/pasos?fecha=2026-13-45").status_code, 200)

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
        self.assertContains(self.client.get("/consulta"), "<h1>Consulta de placas</h1>", html=False)
        r = self.client.get("/caseta")
        self.assertContains(r, "<h1>Punto de cobro</h1>", html=False)
        self.assertContains(r, "Iniciar")
        self.assertNotContains(r, "Reanudar")  # cámara continua: sin autostop ni reanudar
        self.assertNotContains(r, "DURACION_S")
        self.assertContains(r, "INTERVALO = { captura: 500, espera: 1000, fijo: 2000 }")
        self.assertContains(r, "Peticiones (sesión)")
        self.assertContains(r, 'facingMode = { ideal: "environment" }')
        self.assertContains(r, "aspectRatio: { ideal: 9 / 16 }")   # vertical
        self.assertContains(r, "aspectRatio: { ideal: 16 / 9 }")   # horizontal
        self.assertContains(r, 'screen.orientation.addEventListener("change"')
        self.assertContains(r, 'id="zoom"')
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


class TarifaSiempreTests(TestCase):
    """La tarifa se calcula y se guarda en el paso en los 3 estados; la recaudación suma solo lo cobrado."""

    def setUp(self):
        self.caseta = Caseta.objects.first()

    def test_tarifas_exactas_en_la_base(self):
        # Base de 2 ejes: moto 2, auto 4, minibus 6, bus 8, camion 8 Bs; +2 Bs por cada eje sobre 2.
        esperadas = {("moto", 2): 2, ("auto", 2): 4, ("minibus", 2): 6, ("bus", 2): 8, ("camion", 2): 8}
        for categoria, base in (("auto", 4), ("minibus", 6), ("bus", 8), ("camion", 8)):
            for ejes in range(3, 7):
                esperadas[(categoria, ejes)] = base + 2 * (ejes - 2)
        filas = {(t.categoria, t.ejes): t.monto for t in Tarifa.objects.all()}
        self.assertEqual(filas, {k: Decimal(v) for k, v in esperadas.items()})
        for (categoria, ejes), monto in esperadas.items():
            self.assertEqual(cobro.tarifa_de(categoria, ejes), (Decimal(monto), ejes))
        self.assertEqual(cobro.tarifa_de("camion", 7), (Decimal("18"), 7))   # sin fila: base + 2 por eje
        self.assertEqual(cobro.tarifa_de("auto", None), (Decimal("4"), 2))   # sin ejes detectados: 2

    def test_monto_guardado_en_los_3_estados(self):
        Vehiculo.objects.filter(placa="3876LBD").update(saldo=Decimal("100"))
        Vehiculo.objects.filter(placa="1987FHK").update(saldo=Decimal("1"))
        casos = (("3876LBD", "cobrado", "Cobrado con saldo", Decimal("90")),
                 ("1987FHK", "pendiente_pago", "Pendiente de pago (saldo insuficiente)", Decimal("1")),
                 ("7777XYZ", "pendiente_pago", "Pendiente de pago (no registrada)", None))
        for placa, estado, texto, saldo in casos:
            paso, _, info = cobro.registrar_paso(self.caseta.id, placa, categoria="camion", ejes=3)
            self.assertEqual((paso.estado, paso.monto), (estado, Decimal("10")), placa)
            d = cobro.paso_a_dict(paso, info)
            self.assertEqual((d["tarifa"], d["resultado_texto"], d["cobrado"]), ("10.00", texto, estado == "cobrado"))
            self.assertEqual(d["monto_cobrado"], "10.00" if estado == "cobrado" else "0.00")
            if saldo is not None:
                self.assertEqual(Vehiculo.objects.get(placa=placa).saldo, saldo)

    def test_dos_ejes_detectados_muestra_monto_aunque_no_este_registrada(self):
        paso, _, info = cobro.registrar_paso(self.caseta.id, "5555ABC", categoria="auto", ejes=2)
        d = cobro.paso_a_dict(paso, info)
        self.assertEqual((paso.estado, d["tarifa"], d["ejes_texto"]), ("pendiente_pago", "4.00", "2"))

    def test_sin_ejes_detectados_es_2_estimado(self):
        paso, _, info = cobro.registrar_paso(self.caseta.id, "5555ABC", categoria="bus")
        d = cobro.paso_a_dict(paso, info)
        self.assertEqual((paso.ejes_detectados, d["ejes_tarifa"], d["ejes_texto"], d["tarifa"]),
                         (None, 2, "2 (estimado)", "8.00"))

    def test_recaudacion_solo_de_cobrados(self):
        from . import panel
        Paso.objects.all().delete()
        Vehiculo.objects.filter(placa="3876LBD").update(saldo=Decimal("100"))
        Vehiculo.objects.filter(placa="1987FHK").update(saldo=Decimal("0"))
        cobro.registrar_paso(self.caseta.id, "3876LBD", categoria="bus", ejes=3)       # cobrado 10
        cobro.registrar_paso(self.caseta.id, "1987FHK", categoria="camion", ejes=4)    # saldo insuficiente 12
        cobro.registrar_paso(self.caseta.id, "7777XYZ", categoria="minibus", ejes=2)   # no registrada 6
        self.assertEqual(sorted(Paso.objects.values_list("monto", flat=True)),
                         [Decimal("6"), Decimal("10"), Decimal("12")])
        self.assertEqual(panel.kpis()["recaudacion_hoy"], "10.00")

    def test_pantallas_muestran_tarifa_y_resultado(self):
        entrar(self.client, Usuario.objects.create(email_google="op@test.bo", nombre="Op", rol="admin"))
        cobro.registrar_paso(self.caseta.id, "7777XYZ", categoria="auto")
        r = self.client.get("/pasos")
        self.assertContains(r, "Tarifa: 4,00 Bs")  # formato local (es)
        self.assertContains(r, "Pendiente de pago (no registrada)")
        self.assertContains(r, "2 (estimado)")
        self.assertEqual(self.client.get("/").status_code, 200)  # el panel carga con la nueva fila
        c = cobro.consultar_placa("7777XYZ")["ultimo_paso"]
        self.assertEqual((c["tarifa"], c["resultado_texto"], c["ejes_texto"]),
                         ("4.00", "Pendiente de pago (no registrada)", "2 (estimado)"))


JPEG_REAL = Path(settings.BASE_DIR).parent / "docs" / "metricas" / "m2" / "labels.jpg"


@override_settings(DEBUG=False, MEDIA_ROOT=tempfile.mkdtemp(prefix="iap-test-"), CLOUDINARY_URL="", PUSHER_APP_ID="")
class FrameProduccionTests(TestCase):
    """Reproduce el 500 de producción: DEBUG=False, JPEG real, sesión web + CSRF y el flujo completo por la API."""

    def setUp(self):
        self.caseta = Caseta.objects.get(nombre="Peaje IAP")  # la caseta del punto de cobro (migración 0004)
        self.client = self.client_class(enforce_csrf_checks=True)
        entrar(self.client, Usuario.objects.get(email_google="lozogomol@gmail.com"))
        self.client.get("/caseta")
        self.csrf = self.client.cookies["csrftoken"].value
        self.jpeg = JPEG_REAL.read_bytes()
        self.assertEqual(self.jpeg[:2], b"\xff\xd8")

    def _frame(self, placa=None, categoria="", ejes=None, pasajeros=None, confianza=0.9):
        lecturas = [_lectura(placa, confianza, categoria=categoria, ejes=ejes)] if placa else []
        vehiculo = {**SIN_VEHICULO, "clase": categoria, "caja_vehiculo": [0, 0, 10, 10] if categoria else None,
                    "ejes": ejes, "pasajeros": pasajeros}
        with mock.patch(INFERIR, return_value=(lecturas, 80, vehiculo)):
            r = self.client.post("/api/v1/pasos/frame",
                                 {"imagen": SimpleUploadedFile("frame.jpg", self.jpeg, content_type="image/jpeg"),
                                  "caseta_id": self.caseta.id}, HTTP_X_CSRFTOKEN=self.csrf)
        self.assertEqual(r.status_code, 200, r.content[:300])
        self.assertEqual(r["Content-Type"], "application/json")
        return r.json()

    def test_flujo_completo_sin_500(self):
        self.assertEqual(Tarifa.objects.count(), 21)
        # placa nueva con tipo, ejes y pasajeros → se completa y cobra en el segundo frame
        self.assertEqual(self._frame("3876LBD", "camion", 3, 2)["accion"], "en_curso")
        d = self._frame("3876LBD", "camion", 3, 2)
        self.assertEqual((d["accion"], d["paso"]["tarifa"], d["paso"]["pasajeros"]), ("registrado", "10.00", 2))
        # misma placa y frames sin placa → resultado fijo
        self.assertEqual(self._frame("3876LBD", "camion", 3, 2)["accion"], "fijo")
        self.assertEqual(self._frame()["accion"], "fijo")
        # placa distinta sin tipo ni ejes → en curso; a los 6 s se completa con los respaldos
        self.assertEqual(self._frame("6999ZZZ")["accion"], "en_curso")
        PasoEnCurso.objects.filter(caseta=self.caseta).update(bloqueada_at=timezone.now() - timedelta(seconds=7))
        d = self._frame()
        self.assertEqual((d["accion"], d["paso"]["categoria"], d["paso"]["ejes_texto"], d["paso"]["estado"]),
                         ("registrado", "auto", "2 (estimado)", "pendiente_pago"))
        self.assertEqual(self._frame()["accion"], "fijo")
        self.assertEqual(Paso.objects.count(), 2)

    def test_la_fila_de_la_caseta_se_bloquea_sin_outer_join_en_postgresql(self):
        """La causa del 500: SELECT … LEFT OUTER JOIN … FOR UPDATE (PostgreSQL lo rechaza; SQLite lo ignora)."""
        from django.db.backends.postgresql.base import DatabaseWrapper
        pg = DatabaseWrapper({**settings.DATABASES["default"], "ENGINE": "django.db.backends.postgresql"})
        pg.get_autocommit = lambda: False  # compila el SQL sin conectarse
        sql = flujo.fila_bloqueada().filter(caseta=self.caseta).query.get_compiler(connection=pg).as_sql()[0]
        self.assertIn("FOR UPDATE", sql)
        self.assertNotIn("JOIN", sql)

    def test_error_inesperado_responde_json_y_queda_en_el_log(self):
        with mock.patch("peaje.api.flujo.procesar_frame", side_effect=RuntimeError("falla de prueba")), \
                mock.patch(INFERIR, return_value=([], 80, SIN_VEHICULO)), \
                self.assertLogs("peaje.caseta", level="ERROR") as logs:
            r = self.client.post("/api/v1/pasos/frame",
                                 {"imagen": SimpleUploadedFile("frame.jpg", self.jpeg, content_type="image/jpeg"),
                                  "caseta_id": self.caseta.id}, HTTP_X_CSRFTOKEN=self.csrf)
        self.assertEqual(r.status_code, 500)
        self.assertEqual((r.json()["accion"], r.json()["mensaje"]),
                         ("error", "Error interno del servidor al procesar el frame."))
        self.assertIn("RuntimeError: falla de prueba", logs.output[0])  # con traceback

    def test_logging_de_errores_en_consola(self):
        loggers = settings.LOGGING["loggers"]
        self.assertEqual((loggers["django.request"]["level"], loggers["django.request"]["handlers"]),
                         ("ERROR", ["consola"]))
        self.assertEqual(settings.LOGGING["handlers"]["consola"]["class"], "logging.StreamHandler")
        self.assertIn("consola", loggers["peaje"]["handlers"])
