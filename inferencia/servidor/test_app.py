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

    def test_usa_la_placa_leida_no_la_primera(self):
        """La placa de referencia es la de formato boliviano con mayor confianza de lectura."""
        vs = [self._v("bus", [0, 0, 300, 300], 50000), self._v("moto", [400, 100, 500, 300], 4000)]
        placas = [{"caja": [100, 200, 140, 220], "formato_bolivia": False, "confianza_lectura": 99},
                  {"caja": [430, 250, 470, 270], "formato_bolivia": True, "confianza_lectura": 70}]
        self.assertEqual(servidor.elegir_vehiculo(vs, placas)["categoria"], "moto")

    def test_no_gana_la_de_mayor_porcentaje(self):
        """El vehículo apuntado manda aunque otra detección tenga más confianza."""
        vs = [{**self._v("bus", [0, 0, 300, 300], 50000), "confianza": 0.99},
              {**self._v("auto", [400, 100, 600, 300], 30000), "confianza": 0.51}]
        placas = [{"caja": [480, 250, 520, 270], "formato_bolivia": True, "confianza_lectura": 90}]
        self.assertEqual(servidor.elegir_vehiculo(vs, placas, (480, 640))["categoria"], "auto")

    def test_sin_placa_gana_el_grande_y_centrado(self):
        forma = (480, 640)
        esquina = self._v("camion", [0, 0, 200, 160], 32000)      # más grande pero en la esquina
        centro = self._v("minibus", [220, 140, 420, 340], 30000)  # centrado
        self.assertEqual(servidor.elegir_vehiculo([esquina, centro], [], forma)["categoria"], "minibus")
        self.assertEqual(servidor.mas_grande_y_centrado([esquina, centro])["categoria"], "camion")  # sin forma

    def test_placa_fuera_de_todos_usa_grande_y_centrado(self):
        forma = (480, 640)
        vs = [self._v("camion", [0, 0, 200, 160], 32000), self._v("auto", [220, 140, 420, 340], 30000)]
        placa = {"caja": [600, 440, 630, 470], "formato_bolivia": True, "confianza_lectura": 90}
        self.assertEqual(servidor.vehiculo_de_placa(vs, placa, forma)["categoria"], "auto")


class _T:
    """Tensor simulado de ultralytics (.cpu().numpy())."""

    def __init__(self, a):
        self.a = np.asarray(a)

    def cpu(self):
        return self

    def numpy(self):
        return self.a


class ClasesDelModeloTests(unittest.TestCase):
    """La clase es exactamente modelo.names (Roboflow), sin lista fija ni reordenamiento."""

    def _resultado(self, names, clases, confs, cajas):
        from types import SimpleNamespace
        return SimpleNamespace(boxes=_Cajas(cajas, clases, confs), masks=None, names=names)

    def test_usa_modelo_names_tal_cual(self):
        names = {0: "auto", 1: "bus", 2: "camion", 3: "minibus", 4: "moto"}
        r = self._resultado(names, [4, 2, 0], [0.9, 0.8, 0.7],
                            [[0, 0, 10, 10], [10, 10, 30, 30], [40, 40, 50, 50]])
        modelo = mock.Mock(predict=mock.Mock(return_value=[r]))
        with mock.patch.object(servidor, "_m2", modelo):
            vs = servidor.detectar_vehiculos(np.zeros((60, 60, 3), np.uint8))
        self.assertEqual([v["categoria"] for v in vs], ["moto", "camion", "auto"])

    def test_clase_nueva_pasa_sin_filtrar(self):
        r = self._resultado({0: "tractor"}, [0], [0.6], [[0, 0, 10, 10]])
        with mock.patch.object(servidor, "_m2", mock.Mock(predict=mock.Mock(return_value=[r]))):
            self.assertEqual(servidor.detectar_vehiculos(np.zeros((20, 20, 3), np.uint8))[0]["categoria"], "tractor")


class _Cajas:
    """r.boxes simulado."""

    def __init__(self, cajas, clases, confs):
        self.xyxy, self.cls, self.conf = _T(cajas), _T(clases), _T(confs)

    def __len__(self):
        return len(self.cls.a)


class VehiculoPorPlacaTests(unittest.TestCase):
    """/inferir: cada placa lleva SU vehículo y el global es el de la placa leída."""

    @classmethod
    def setUpClass(cls):
        cls.env = mock.patch.dict(os.environ, {"IAP_TOKEN": TOKEN})
        cls.env.start()
        cls.cliente = TestClient(servidor.app)  # sin lifespan: los modelos se simulan

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()

    def test_cada_placa_con_su_vehiculo(self):
        placas = [{"texto": "XX1", "formato_bolivia": False, "confianza_deteccion": 90.0, "confianza_lectura": 95.0,
                   "caja": [100, 200, 140, 220]},
                  {"texto": "2451KTP", "formato_bolivia": True, "confianza_deteccion": 80.0,
                   "confianza_lectura": 85.0, "caja": [430, 250, 470, 270]}]
        vs = [{"categoria": "bus", "confianza": 0.97, "caja": [0, 0, 300, 300], "area": 50000, "mascara": None},
              {"categoria": "moto", "confianza": 0.55, "caja": [400, 100, 500, 300], "area": 4000, "mascara": None}]
        with mock.patch.object(servidor, "leer_placas", return_value=placas), \
                mock.patch.object(servidor, "detectar_vehiculos", return_value=vs):
            d = self.cliente.post("/inferir", files={"imagen": ("f.jpg", _jpeg(_escena_vacia()), "image/jpeg")},
                                  headers={"X-API-Token": TOKEN}).json()
        self.assertEqual((d["categoria"], d["confianza_categoria"], d["caja_vehiculo"]),
                         ("moto", 55.0, [400, 100, 500, 300]))
        self.assertEqual([(p["texto"], p["categoria"]) for p in d["placas"]], [("XX1", "bus"), ("2451KTP", "moto")])


if __name__ == "__main__":
    unittest.main()
