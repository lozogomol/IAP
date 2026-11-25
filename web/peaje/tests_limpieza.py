"""Comando limpiar_datos (dejar la app como nueva) e ícono neutro para pasos sin imagen."""
from decimal import Decimal
from io import StringIO

from django.core.management import CommandError, call_command
from django.test import TestCase

from . import cobro, evidencias
from .models import (Alerta, Caseta, EvidenciaImagen, LecturaPlaca, Paso, PasoEnCurso, Tarifa, Usuario, Vehiculo,
                     caseta_principal)
from .tests import entrar
from .tests_caseta import FRAME


class LimpiarDatosTests(TestCase):
    def setUp(self):
        caseta = caseta_principal()
        p1, _, _ = cobro.registrar_paso(caseta.id, "3876LBD", categoria="auto")    # cobrado
        p2, _, _ = cobro.registrar_paso(caseta.id, "7777XYZ", categoria="auto")    # pendiente + alerta
        evidencias.guardar_evidencia(p1, FRAME)
        evidencias.guardar_evidencia(p2, FRAME)
        PasoEnCurso.objects.create(caseta=caseta, placa="2451KTP", paso=p2)
        LecturaPlaca.objects.create(caseta=caseta, placa="2451KTP", confianza=0.9)
        self.conservados = (Usuario.objects.count(), Tarifa.objects.count(), Caseta.objects.count())

    def test_sin_confirmar_no_borra_nada(self):
        salida = StringIO()
        with self.assertRaisesMessage(CommandError, "--confirmar"):
            call_command("limpiar_datos", stdout=salida)
        self.assertIn("Antes:", salida.getvalue())
        self.assertEqual((Paso.objects.count(), EvidenciaImagen.objects.count(), Alerta.objects.count()), (2, 2, 1))
        self.assertTrue(Vehiculo.objects.exists())

    def test_confirmar_borra_datos_y_conserva_usuarios_tarifas_y_casetas(self):
        salida = StringIO()
        call_command("limpiar_datos", "--confirmar", stdout=salida)
        texto = salida.getvalue()
        self.assertIn("Antes:", texto)
        self.assertIn("Después:", texto)
        for modelo in (Paso, EvidenciaImagen, Alerta, Vehiculo, PasoEnCurso, LecturaPlaca):
            self.assertEqual(modelo.objects.count(), 0, modelo.__name__)
        self.assertEqual((Usuario.objects.count(), Tarifa.objects.count(), Caseta.objects.count()), self.conservados)
        self.assertGreater(Tarifa.objects.count(), 0)
        self.assertTrue(Usuario.objects.filter(email_google="lozogomol@gmail.com").exists())
        # la app sigue funcionando: se puede registrar un paso nuevo
        paso, _, _ = cobro.registrar_paso(caseta_principal().id, "1234ABC", categoria="auto")
        self.assertEqual((paso.estado, paso.monto), ("pendiente_pago", Decimal("4")))  # ya no hay vehículos


class IconoSinImagenTests(TestCase):
    def test_paso_sin_imagen_muestra_icono_y_las_rotas_se_reemplazan(self):
        entrar(self.client, Usuario.objects.get(email_google="lozogomol@gmail.com"))
        Paso.objects.create(placa_leida="A", placa_normalizada="1111AAA", caseta=caseta_principal(), estado="cobrado")
        r = self.client.get("/pasos")
        self.assertContains(r, '<span class="sin-captura" title="Sin imagen">', html=False)
        self.assertNotContains(r, "<img src=\"None\"")
        # si una evidencia no carga (borrada o inexistente), el ícono neutro reemplaza a la imagen rota
        self.assertContains(r, 'id="ico-sin-imagen"')
        self.assertContains(r, 'img.closest(".mini-btn, .captura-chat, .captura-grande, .captura-consulta")')
