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
        self.assertIsNone(d["categoria"])  # M2/M3 aún no existen: nunca se inventa
        self.assertIsNone(d["ejes"])
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


if __name__ == "__main__":
    unittest.main()
