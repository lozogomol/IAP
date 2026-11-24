"""Tests del servidor de inferencia. Ejecutar desde inferencia/servidor:  python -m unittest -v test_app

Usan los modelos reales del repo (no hay mocks del detector ni del OCR).
"""
import os
import unittest
from unittest import mock

import cv2
import numpy as np
from fastapi.testclient import TestClient

import app as servidor

TOKEN = "token-de-prueba"


def _jpeg(imagen):
    return cv2.imencode(".jpg", imagen)[1].tobytes()


def _escena_vacia():
    return np.full((480, 640, 3), 90, np.uint8)


class ServidorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = mock.patch.dict(os.environ, {"IAP_TOKEN": TOKEN})
        cls.env.start()
        cls.cm = TestClient(servidor.app)
        cls.cliente = cls.cm.__enter__()  # dispara el lifespan (carga de modelos)

    @classmethod
    def tearDownClass(cls):
        cls.cm.__exit__(None, None, None)
        cls.env.stop()

    def _inferir(self, datos, token=TOKEN, **params):
        headers = {"X-API-Token": token} if token is not None else {}
        return self.cliente.post("/inferir", files={"imagen": ("f.jpg", datos, "image/jpeg")},
                                 headers=headers, params=params)

    def test_salud(self):
        d = self.cliente.get("/salud").json()
        self.assertEqual(d["estado"], "ok")
        self.assertEqual(d["modelos"]["m1_placas"], "cargado")
        self.assertEqual(d["modelos"]["m2_segmentacion"], "cargado" if servidor.MODELO_M2.exists() else "pendiente")
        self.assertEqual(d["modelos"]["m3_keypoints"], "pendiente")
        self.assertTrue(d["token_configurado"])

    def test_sin_token_401(self):
        self.assertEqual(self._inferir(_jpeg(_escena_vacia()), token=None).status_code, 401)

    def test_token_incorrecto_401(self):
        self.assertEqual(self._inferir(_jpeg(_escena_vacia()), token="otro").status_code, 401)

    def test_sin_token_en_servidor_503(self):
        with mock.patch.dict(os.environ, {"IAP_TOKEN": ""}):
            self.assertEqual(self._inferir(_jpeg(_escena_vacia())).status_code, 503)

    def test_imagen_invalida_400(self):
        self.assertEqual(self._inferir(b"no es una imagen").status_code, 400)

    def test_imagen_muy_grande_413(self):
        with mock.patch.object(servidor, "MAX_BYTES", 100):
            self.assertEqual(self._inferir(_jpeg(_escena_vacia())).status_code, 413)

    def test_escena_sin_placas(self):
        r = self._inferir(_jpeg(_escena_vacia()))
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertEqual(d["placas"], [])
        self.assertIsNone(d["categoria"])  # sin vehículo no se inventa categoría
        self.assertIsNone(d["confianza_categoria"])
        self.assertIsNone(d["caja_vehiculo"])
        self.assertIsNone(d["ejes"])  # M3 pendiente
        self.assertIn("milisegundos", d)

    def test_anotada(self):
        d = self._inferir(_jpeg(_escena_vacia()), anotada="true").json()
        self.assertTrue(d["imagen"].startswith("data:image/jpeg;base64,"))

    def test_formato_de_cada_placa(self):
        """Con un detector simulado que devuelve una caja, el OCR real lee el recorte y arma el contrato."""
        img = np.full((300, 600, 3), 255, np.uint8)
        cv2.putText(img, "2451KTP", (40, 190), cv2.FONT_HERSHEY_SIMPLEX, 3, (0, 0, 0), 8, cv2.LINE_AA)
        with mock.patch.object(servidor.detector, "detectar", return_value=[(10, 60, 590, 240, 0.9)]):
            placas = self._inferir(_jpeg(img)).json()["placas"]
        self.assertEqual(len(placas), 1)
        p = placas[0]
        self.assertEqual(set(p), {"texto", "formato_bolivia", "confianza_deteccion", "confianza_lectura", "caja"})
        self.assertEqual(p["confianza_deteccion"], 90.0)
        self.assertEqual(p["caja"], [10, 60, 590, 240])
        self.assertTrue(0 <= p["confianza_lectura"] <= 100)
        self.assertEqual(p["formato_bolivia"], bool(servidor.detector.FORMATO_BOLIVIA.match(p["texto"])))


    @unittest.skipUnless(servidor.MODELO_M2.exists(), "falta inferencia/modelos/m2_seg.pt")
    def test_m2_categoria_con_foto_real(self):
        """bus.jpg (viene con ultralytics): M2 debe devolver una categoría válida con su caja."""
        from pathlib import Path

        import ultralytics
        foto = Path(ultralytics.__file__).parent / "assets" / "bus.jpg"
        d = self._inferir(foto.read_bytes(), anotada="true").json()
        self.assertEqual(d["categoria"], "bus")
        self.assertTrue(0 < d["confianza_categoria"] <= 100)
        self.assertEqual(len(d["caja_vehiculo"]), 4)
        self.assertIsNone(d["ejes"])
        self.assertTrue(d["imagen"].startswith("data:image/jpeg;base64,"))


class ElegirVehiculoTests(unittest.TestCase):
    """Regla del vehículo principal (sin modelo: detecciones simuladas)."""

    def _v(self, categoria, caja, area, mascara=None):
        return {"categoria": categoria, "confianza": 0.9, "caja": caja, "area": area, "mascara": mascara}

    def test_sin_vehiculos(self):
        self.assertIsNone(servidor.elegir_vehiculo([], []))

    def test_sin_placa_gana_mayor_area(self):
        vs = [self._v("auto", [0, 0, 10, 10], 50), self._v("bus", [20, 20, 90, 90], 3000)]
        self.assertEqual(servidor.elegir_vehiculo(vs, [])["categoria"], "bus")

    def test_gana_el_que_contiene_el_centro_de_la_placa(self):
        vs = [self._v("bus", [0, 0, 300, 300], 50000), self._v("moto", [400, 100, 500, 300], 4000)]
        placas = [{"caja": [430, 250, 470, 270]}]
        self.assertEqual(servidor.elegir_vehiculo(vs, placas)["categoria"], "moto")

    def test_placa_fuera_de_todos_usa_mayor_area(self):
        vs = [self._v("auto", [0, 0, 100, 100], 5000), self._v("camion", [200, 0, 400, 200], 20000)]
        self.assertEqual(servidor.elegir_vehiculo(vs, [{"caja": [600, 600, 620, 610]}])["categoria"], "camion")

    def test_superpuestos_prefiere_la_mascara(self):
        m = np.zeros((300, 300), bool)
        m[200:260, 100:200] = True  # la máscara del auto cubre la placa
        vs = [self._v("bus", [0, 0, 300, 300], 60000, np.zeros((300, 300), bool)),
              self._v("auto", [80, 150, 220, 280], 6000, m)]
        self.assertEqual(servidor.elegir_vehiculo(vs, [{"caja": [130, 220, 170, 240]}])["categoria"], "auto")


if __name__ == "__main__":
    unittest.main()
