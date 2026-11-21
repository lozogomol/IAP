from decimal import Decimal

from django.test import TestCase

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
