"""Formas de segmentación y keypoints para dibujar sobre el video del punto de cobro."""
from unittest import mock

from django.test import TestCase

from .inferencia import interpretar_vehiculo
from .models import Usuario, caseta_principal
from .tests import entrar
from .tests_caseta import INFERIR, SIN_VEHICULO, _jpeg, _lectura

FORMAS = {
    "vehiculo": {"categoria": "camion", "caja": [10, 20, 400, 300],
                 "poligono": [[10, 30], [390, 20], [400, 290], [15, 300]]},
    "llantas": [{"caja": [50, 250, 90, 300], "confianza": 88.0, "centro": [70, 275], "eje": 1,
                 "keypoints": [[70, 275, 0.95], [70, 298, 0.9], [70, 252, 0.9], [48, 275, 0.2], [92, 275, 0.9]]},
                {"caja": [300, 250, 340, 300], "confianza": 80.0, "centro": [320, 275], "eje": 2, "keypoints": None}],
    "nombres_keypoints": ["centro", "abajo", "arriba", "izquierda", "derecha"],
    "pasajeros": [{"caja": [100, 60, 140, 160], "poligonos": [[[100, 60], [140, 60], [140, 160], [100, 160]]]}],
    "placa": {"caja": [150, 220, 230, 245], "texto": "2451KTP"},
}


class InterpretarFormasTests(TestCase):
    def test_pasa_las_formas_validadas(self):
        f = interpretar_vehiculo({"formas": FORMAS})["formas"]
        self.assertEqual(f["vehiculo"]["poligono"], [[10, 30], [390, 20], [400, 290], [15, 300]])
        self.assertEqual(f["vehiculo"]["categoria"], "camion")
        self.assertEqual([l["eje"] for l in f["llantas"]], [1, 2])
        self.assertEqual(f["llantas"][0]["keypoints"][0], [70, 275, 0.95])
        self.assertIsNone(f["llantas"][1]["keypoints"])
        self.assertEqual(f["nombres_keypoints"][0], "centro")
        self.assertEqual(f["pasajeros"][0]["poligonos"][0][2], [140, 160])
        self.assertEqual(f["placa"], {"caja": [150, 220, 230, 245], "texto": "2451KTP"})

    def test_servidor_anterior_sin_formas(self):
        self.assertIsNone(interpretar_vehiculo({})["formas"])

    def test_datos_raros_no_rompen_y_se_acotan(self):
        raro = {"vehiculo": {"poligono": [[1, 2], ["x", 3], [4]]},           # menos de 3 puntos válidos
                "llantas": ["no", {"eje": "dos", "keypoints": [[1, 2]]}],     # keypoint sin confianza: se descarta
                "pasajeros": [{"poligonos": [[[i, i] for i in range(500)]]}] * 50,
                "placa": "no es un dict"}
        f = interpretar_vehiculo({"formas": raro})["formas"]
        self.assertIsNone(f["vehiculo"]["poligono"])
        self.assertEqual(f["llantas"], [{"caja": None, "centro": None, "eje": None, "keypoints": None}])
        self.assertEqual(len(f["pasajeros"]), 30)
        self.assertEqual(len(f["pasajeros"][0]["poligonos"][0]), 200)
        self.assertIsNone(f["placa"])


class ApiFormasTests(TestCase):
    def test_la_lectura_del_frame_trae_las_formas(self):
        entrar(self.client, Usuario.objects.get(email_google="lozogomol@gmail.com"))
        vehiculo = {**SIN_VEHICULO, "formas": interpretar_vehiculo({"formas": FORMAS})["formas"]}
        with mock.patch(INFERIR, return_value=([_lectura("2451KTP", 0.9)], 50, vehiculo)):
            d = self.client.post("/api/v1/pasos/frame", {"imagen": _jpeg(), "caseta_id": caseta_principal().id}).json()
        f = d["lectura"]["formas"]
        self.assertEqual((len(f["vehiculo"]["poligono"]), len(f["llantas"]), len(f["pasajeros"])), (4, 2, 1))
        with mock.patch(INFERIR, return_value=([], 50, SIN_VEHICULO)):  # servidor anterior: null, se dibujan cajas
            d = self.client.post("/api/v1/pasos/frame", {"imagen": _jpeg(), "caseta_id": caseta_principal().id}).json()
        self.assertIsNone(d["lectura"]["formas"])


class PaginaFormasTests(TestCase):
    def test_capas_colores_y_desvanecido(self):
        entrar(self.client, Usuario.objects.get(email_google="lozogomol@gmail.com"))
        r = self.client.get("/caseta")
        for capa in ("vehiculo", "llantas", "pasajeros", "placa"):
            self.assertContains(r, f'data-capa="{capa}" aria-pressed="true"')
        self.assertContains(r, 'RELLENO_VEHICULO = "rgba(30, 58, 138, 0.30)"')
        self.assertContains(r, 'AZUL_NOCHE = "#0a1430", AZUL_CLARO = "#9db4ff"')
        self.assertContains(r, 'ctx.lineWidth = 5; ctx.strokeStyle = "#ffffff"; ctx.stroke();')  # borde blanco
        self.assertContains(r, 'if (c === "pasajeros") e.alfa = 1;')    # siluetas opacas al instante
        self.assertContains(r, "SALIDA_MS = 600")                         # se desvanecen, no parpadean
        self.assertContains(r, "var escala = Math.max(w / e.ancho, h / e.alto);")  # encuadre cover
