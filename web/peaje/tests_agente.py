"""Tests del agente conversacional: herramientas, agente por reglas, Gemini (mockeado), API y página."""
import json
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from .auth import emitir_jwt
from .models import Paso, Usuario, Vehiculo, caseta_principal
from .servicios import agente
from .tests import entrar

INFERIR = "peaje.servicios.agente.inferir"
JPEG = b"\xff\xd8\xff\xe0foto-de-prueba"


def _lectura(placa, confianza=0.9):
    return {"placa": placa, "texto_leido": placa, "confianza": confianza, "categoria": "auto", "ejes": 2}


class Datos(TestCase):
    def setUp(self):
        caseta = caseta_principal()
        # 3876LBD: registrada con paso · 2451KTP: registrada sin pasos · 5555XYZ: pasó sin registro · 9999ZZZ: nada
        Paso.objects.create(placa_leida="3876LBD", placa_normalizada="3876LBD", caseta=caseta, estado="cobrado",
                            categoria_detectada="auto", ejes_detectados=2, monto=Decimal("4"),
                            imagen_url="/capturas/2026/11/paso_1.jpg")
        Paso.objects.create(placa_leida="5555XYZ", placa_normalizada="5555XYZ", caseta=caseta, estado="no_registrada",
                            categoria_detectada="camion", ejes_detectados=None, monto=Decimal("8"))
        Paso.objects.filter(placa_normalizada="2451KTP").delete()


@override_settings(GEMINI_API_KEY="")
class HerramientasTests(Datos):
    def test_cuatro_estados(self):
        con = agente.consultar_placa("3876LBD")
        self.assertEqual(con["estado"], "registrada_con_paso")
        up = con["ultimo_paso"]
        self.assertEqual((up["punto_de_cobro"], up["tipo"], up["ejes"], up["tarifa_bs"], up["resultado"]),
                         ("Peaje IAP", "Auto", "2", "4.00", "Cobrado"))
        self.assertRegex(up["fecha"], r"^\d{2}/\d{2}/\d{4}$")
        self.assertRegex(up["hora"], r"^\d{2}:\d{2}:\d{2}$")
        self.assertEqual(agente.consultar_placa("2451KTP")["estado"], "registrada_sin_paso")
        ident = agente.consultar_placa("5555XYZ")
        self.assertEqual((ident["estado"], ident["registrada"], ident["ultimo_paso"]["ejes"]),
                         ("identificada_no_registrada", False, "2 (estimado)"))
        self.assertEqual(agente.consultar_placa("9999ZZZ")["estado"], "no_existe")

    def test_normaliza(self):
        Vehiculo.objects.create(placa="1234ABC", categoria="auto")
        d = agente.consultar_placa("1234-abc")
        self.assertEqual((d["placa"], d["formato_valido"], d["estado"]), ("1234ABC", True, "registrada_sin_paso"))
        self.assertEqual(agente.consultar_placa(" 1234 abc ")["placa"], "1234ABC")

    def test_sugerencia_ocr(self):
        d = agente.consultar_placa("3876L8D")  # B leída como 8
        self.assertEqual((d["estado"], d["sugerencias"][0]), ("no_existe", "3876LBD"))
        d = agente.consultar_placa("S555XYZ")  # 5 leída como S: también sugiere placas vistas solo en pasos
        self.assertIn("5555XYZ", d["sugerencias"])

    def test_formato_invalido(self):
        d = agente.consultar_placa("AB12")
        self.assertEqual((d["estado"], d["formato_valido"]), ("no_existe", False))
        self.assertIn("formato", d["mensaje"])

    def test_historial(self):
        d = agente.historial_placa("3876lbd")
        self.assertEqual((d["total_pasos"], len(d["pasos"]), d["pasos"][0]["tarifa_bs"]), (1, 1, "4.00"))

    def test_imagen_sin_placa_legible(self):
        with mock.patch(INFERIR, return_value=([], 10, {})):
            d = agente.leer_placa_imagen(JPEG)
        self.assertEqual((d["estado"], d["placa"]), ("sin_lectura", None))
        self.assertIn("otra foto", d["mensaje"])

    def test_imagen_con_placa(self):
        with mock.patch(INFERIR, return_value=([_lectura("3876LBD")], 10, {})):
            d = agente.leer_placa_imagen(JPEG)
        self.assertEqual((d["estado"], d["placa"], d["lectura"]["confianza"]), ("registrada_con_paso", "3876LBD", 0.9))


@override_settings(GEMINI_API_KEY="")
class AgenteReglasTests(Datos):
    """Sin clave: agente por reglas con las mismas herramientas y respuestas en plantilla."""

    def test_cuatro_estados_por_texto(self):
        casos = {
            "¿Pasó la placa 3876LBD?": ("registrada_con_paso", "está registrada. Último paso:"),
            "y la 2451KTP?": ("registrada_sin_paso", "no tiene pasos"),
            "placa 5555 XYZ": ("identificada_no_registrada", "posible evasión"),
            "consulta 9999ZZZ": ("no_existe", "No hay registros de la placa 9999ZZZ"),
        }
        for texto, (estado, frase) in casos.items():
            r = agente.responder(texto)
            self.assertEqual((r["estado"], r["motor"]), (estado, "reglas"), texto)
            self.assertIn(frase, r["respuesta"], texto)
        r = agente.responder("¿Pasó la placa 3876LBD?")
        self.assertIn("tarifa 4.00 Bs (Cobrado)", r["respuesta"])
        self.assertIn("en Peaje IAP", r["respuesta"])

    def test_normalizacion_en_el_texto(self):
        Vehiculo.objects.create(placa="1234ABC", categoria="auto")
        r = agente.responder("¿pasó la 1234-abc?")
        self.assertEqual((r["placa"], r["estado"]), ("1234ABC", "registrada_sin_paso"))

    def test_sugerencia_ocr_en_la_respuesta(self):
        r = agente.responder("¿Pasó la placa 3876L8D?")
        self.assertEqual((r["estado"], r["sugerencias"]), ("no_existe", ["3876LBD"]))
        self.assertIn("¿Quisiste decir 3876LBD?", r["respuesta"])

    def test_historial_por_texto(self):
        r = agente.responder("historial de 3876LBD")
        self.assertIn("Historial (1 pasos", r["respuesta"])

    def test_sin_placa_responde_ayuda(self):
        r = agente.responder("hola")
        self.assertEqual((r["estado"], r["placa"]), ("ayuda", None))
        self.assertIn("1234ABC", r["respuesta"])

    def test_imagen_sin_placa(self):
        with mock.patch(INFERIR, return_value=([], 10, {})):
            r = agente.responder("", imagen=JPEG)
        self.assertEqual((r["estado"], r["motor"]), ("sin_lectura", "reglas"))
        self.assertIn("No encontré una placa legible", r["respuesta"])


def _gemini_falso(llamadas):
    """Cliente google-genai falso: ejecuta las herramientas que pide el 'modelo' y devuelve un texto."""

    def generate_content(model, contents, config):
        herramientas = {f.__name__: f for f in config.tools}
        resultado = None
        for nombre, args in llamadas:
            resultado = herramientas[nombre](**args)
        return SimpleNamespace(text=f"Respuesta del modelo: {resultado['estado']}" if resultado else "")

    return mock.Mock(return_value=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content)))


@override_settings(GEMINI_API_KEY="clave-de-prueba", GEMINI_MODEL="")
class AgenteGeminiTests(Datos):
    def test_usa_las_herramientas_y_devuelve_sus_datos(self):
        with mock.patch("google.genai.Client", _gemini_falso([("consultar_placa", {"placa": "3876-lbd"})])) as cliente:
            r = agente.responder("¿Pasó la placa 3876-lbd?")
        self.assertEqual(cliente.call_args.kwargs["api_key"], "clave-de-prueba")
        self.assertEqual(cliente.call_args.kwargs["http_options"].timeout, agente.TIMEOUT_MS)
        self.assertEqual((r["motor"], r["estado"], r["placa"]), ("gemini", "registrada_con_paso", "3876LBD"))
        self.assertEqual(r["respuesta"], "Respuesta del modelo: registrada_con_paso")
        self.assertEqual(r["datos"]["ultimo_paso"]["tarifa_bs"], "4.00")

    def test_imagen_con_gemini(self):
        with mock.patch("google.genai.Client", _gemini_falso([("leer_placa_imagen", {})])), \
                mock.patch(INFERIR, return_value=([_lectura("5555XYZ")], 10, {})):
            r = agente.responder("", imagen=JPEG)
        self.assertEqual((r["motor"], r["estado"]), ("gemini", "identificada_no_registrada"))

    def test_si_gemini_falla_responde_el_agente_por_reglas(self):
        with mock.patch("google.genai.Client", side_effect=RuntimeError("API caída")), \
                self.assertLogs("peaje.agente", level="ERROR"):
            r = agente.responder("¿Pasó la placa 3876LBD?")
        self.assertEqual((r["motor"], r["estado"]), ("reglas", "registrada_con_paso"))
        self.assertTrue(r["respuesta"])

    def test_modelo_flash_por_defecto_y_configurable(self):
        self.assertEqual(agente.modelo(), agente.MODELO_POR_DEFECTO)
        self.assertIn("flash", agente.MODELO_POR_DEFECTO)
        with override_settings(GEMINI_MODEL="otro-modelo"):
            self.assertEqual(agente.modelo(), "otro-modelo")


@override_settings(GEMINI_API_KEY="")
class ApiChatTests(Datos):
    URL = "/api/v1/agente/chat"

    def setUp(self):
        super().setUp()
        self.admin = Usuario.objects.get(email_google="lozogomol@gmail.com")

    def test_requiere_autenticacion(self):
        self.assertEqual(self.client.post(self.URL, {"mensaje": "x"}, content_type="application/json").status_code, 401)

    def test_json_con_sesion(self):
        entrar(self.client, self.admin)
        r = self.client.post(self.URL, {"mensaje": "¿Pasó la placa 3876LBD?",
                                        "historial": [{"rol": "usuario", "texto": "hola"}]},
                             content_type="application/json")
        d = r.json()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(set(d), {"respuesta", "estado", "placa", "sugerencias", "datos", "motor"})
        self.assertEqual((d["estado"], d["placa"]), ("registrada_con_paso", "3876LBD"))
        self.assertEqual(d["datos"]["ultimo_paso"]["imagen_url"], "/capturas/2026/11/paso_1.jpg")

    def test_multipart_con_imagen_y_jwt(self):
        with mock.patch(INFERIR, return_value=([], 10, {})):
            r = self.client.post(self.URL, {"mensaje": "", "historial": json.dumps([]),
                                            "imagen": SimpleUploadedFile("p.jpg", JPEG, content_type="image/jpeg")},
                                 HTTP_AUTHORIZATION=f"Bearer {emitir_jwt(self.admin)}")
        self.assertEqual((r.status_code, r.json()["estado"]), (200, "sin_lectura"))

    def test_sin_mensaje_ni_imagen_400(self):
        entrar(self.client, self.admin)
        self.assertEqual(self.client.post(self.URL, {}, content_type="application/json").status_code, 400)

    def test_en_swagger(self):
        from drf_spectacular.generators import SchemaGenerator
        operacion = SchemaGenerator().get_schema(public=True)["paths"]["/api/v1/agente/chat"]["post"]
        self.assertIn("multipart/form-data", operacion["requestBody"]["content"])
        self.assertIn("application/json", operacion["requestBody"]["content"])


@override_settings(GEMINI_API_KEY="")
class PaginaAsistenteTests(TestCase):
    def test_solo_autenticados(self):
        self.assertEqual(self.client.get("/chat").status_code, 302)

    def test_pagina(self):
        entrar(self.client, Usuario.objects.get(email_google="lozogomol@gmail.com"))
        r = self.client.get("/chat")
        self.assertContains(r, '<textarea id="mensaje"')
        self.assertContains(r, 'capture="environment"')
        self.assertContains(r, "¿Pasó la placa 1234ABC?")
        self.assertContains(r, "envía una foto de una placa")
        self.assertContains(r, 'id="escribiendo"')
        self.assertNotContains(r, "Agente conversacional no disponible")
