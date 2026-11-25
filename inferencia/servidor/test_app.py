"""Tests del servidor de inferencia. Ejecutar desde inferencia/servidor:  python -m unittest -v test_app

ServidorTests usa los modelos reales del repo (rutas de config.py). El resto prueba la lógica con detecciones
simuladas (sin modelos).
"""
import base64
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import cv2
import numpy as np
from fastapi.testclient import TestClient

import app as servidor
import config
import logica

TOKEN = "token-de-prueba"


def _jpeg(imagen):
    return cv2.imencode(".jpg", imagen)[1].tobytes()


def _escena_vacia():
    return np.full((480, 640, 3), 90, np.uint8)


def _foto_bus():
    import ultralytics
    return (Path(ultralytics.__file__).parent / "assets" / "bus.jpg").read_bytes()


def _decodificar_data_url(url):
    datos = base64.b64decode(url.split(",", 1)[1])
    return cv2.imdecode(np.frombuffer(datos, np.uint8), cv2.IMREAD_COLOR)


class ConfigTests(unittest.TestCase):
    def test_rutas_uniformes_y_existentes(self):
        for ruta in (config.M1_DETECTOR, config.M1_OCR, config.M1_OCR_CONFIG, config.M2_VEHICULOS,
                     config.M3_LLANTAS, config.M4_PASAJEROS):
            self.assertTrue(ruta.name.startswith("iap_m"), ruta.name)
            self.assertEqual(ruta.parent, config.MODELOS)
            self.assertTrue(ruta.exists(), ruta)
        self.assertTrue((config.ARCHIVO / "iap_m2_vehiculos_seg_v1_propio.pt").exists())

    def test_detector_m1_usa_config(self):
        self.assertEqual(servidor.detector.MODELO, config.M1_DETECTOR)


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

    def test_salud_lista_los_4_modelos(self):
        d = self.cliente.get("/salud").json()
        self.assertEqual(d["estado"], "ok")
        self.assertEqual(d["modelos"], {"m1_placas": "cargado", "m2_vehiculos": "cargado",
                                        "m3_llantas": "cargado", "m4_pasajeros": "cargado"})
        det = d["detalle_modelos"]
        self.assertEqual((det["m2_vehiculos"]["tarea"], det["m2_vehiculos"]["clases"]),
                         ("segment", ["auto", "bus", "camion", "minibus", "moto"]))
        self.assertEqual((det["m3_llantas"]["tarea"], det["m3_llantas"]["kpt_shape"]), ("pose", [5, 3]))
        self.assertEqual(det["m4_pasajeros"]["archivo"], "iap_m4_pasajeros_seg.pt")
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
        with mock.patch.object(config, "MAX_BYTES", 100):
            self.assertEqual(self._inferir(_jpeg(_escena_vacia())).status_code, 413)

    def test_escena_vacia_no_inventa(self):
        d = self._inferir(_jpeg(_escena_vacia())).json()
        self.assertEqual((d["placa"], d["categoria"], d["ejes"], d["pasajeros"], d["llantas_detectadas"]),
                         (None, None, None, None, 0))
        self.assertEqual(d["placas"], [])
        self.assertIsNone(d["evidencia"])
        self.assertEqual(set(d["latencia_ms"]), {"total", "m1", "m2", "m3", "m4"})
        self.assertEqual(set(d["cajas"]), {"placa", "vehiculo", "placas", "llantas", "personas"})
        self.assertIn("motivo_ejes", d["detalle"])
        self.assertEqual(d["formas"], {"vehiculo": None, "llantas": [], "pasajeros": [], "placa": None,
                                       "nombres_keypoints": ["centro", "abajo", "arriba", "izquierda", "derecha"]})

    def test_formato_de_cada_placa(self):
        """Con un detector simulado que devuelve una caja, el OCR real lee el recorte y arma el contrato."""
        img = np.full((300, 600, 3), 255, np.uint8)
        cv2.putText(img, "2451KTP", (40, 190), cv2.FONT_HERSHEY_SIMPLEX, 3, (0, 0, 0), 8, cv2.LINE_AA)
        with mock.patch.object(servidor.detector, "detectar", return_value=[(10, 60, 590, 240, 0.9)]):
            d = self._inferir(_jpeg(img)).json()
        p = d["placas"][0]
        self.assertTrue({"texto", "formato_bolivia", "confianza_deteccion", "confianza_lectura", "caja"} <= set(p))
        self.assertEqual((p["confianza_deteccion"], p["caja"]), (90.0, [10, 60, 590, 240]))
        if p["formato_bolivia"]:  # la placa de la respuesta solo sale con formato boliviano
            self.assertEqual((d["placa"], d["cajas"]["placa"]), (p["texto"], p["caja"]))
        else:
            self.assertIsNone(d["placa"])

    def test_foto_real_los_4_modelos(self):
        """bus.jpg (viene con ultralytics): bus de 2 ejes con personas delante."""
        d = self._inferir(_foto_bus(), anotada="true").json()
        self.assertEqual(d["categoria"], "bus")
        self.assertEqual(d["detalle"]["clase_modelo"], "bus")
        self.assertTrue(0 < d["confianza_categoria"] <= 100)
        self.assertEqual(d["cajas"]["vehiculo"], d["caja_vehiculo"])
        self.assertIn(d["ejes"], (None, 2, 3))
        self.assertGreaterEqual(d["llantas_detectadas"], 1)
        self.assertIsInstance(d["pasajeros"], int)
        self.assertTrue(all(isinstance(d["latencia_ms"][k], int) for k in ("total", "m1", "m2", "m3", "m4")))
        self.assertTrue(d["imagen"].startswith("data:image/jpeg;base64,"))
        # formas para el punto de cobro: polígono del vehículo (M2), keypoints de llantas (M3), siluetas (M4)
        f = d["formas"]
        alto, ancho = cv2.imdecode(np.frombuffer(_foto_bus(), np.uint8), cv2.IMREAD_COLOR).shape[:2]
        pol = f["vehiculo"]["poligono"]
        self.assertEqual((f["vehiculo"]["categoria"], f["vehiculo"]["caja"]), ("bus", d["caja_vehiculo"]))
        self.assertTrue(3 <= len(pol) <= logica.MAX_PUNTOS_POLIGONO)
        self.assertTrue(all(0 <= x < ancho and 0 <= y < alto for x, y in pol))
        self.assertEqual(len(f["llantas"]), d["llantas_detectadas"])
        for l in f["llantas"]:
            self.assertTrue(l["keypoints"] is None or all(len(k) == 3 for k in l["keypoints"]))
            self.assertIn(l["eje"], (None, 1, 2, 3))
        self.assertEqual(len(f["pasajeros"]), len(d["cajas"]["personas"]))
        for persona in f["pasajeros"]:  # solo números: contornos y caja, nunca imagen ni datos de rostro
            self.assertEqual(set(persona), {"caja", "poligonos"})
            self.assertTrue(all(isinstance(v, int) for pg in persona["poligonos"] for pt in pg for v in pt))
        # privacidad: hay personas → la evidencia viene con ellas cubiertas por color sólido
        self.assertTrue(d["cajas"]["personas"])
        evidencia = _decodificar_data_url(d["evidencia"])
        x1, y1, x2, y2 = d["cajas"]["personas"][0]
        centro = evidencia[(y1 + y2) // 2 - 5:(y1 + y2) // 2 + 5, (x1 + x2) // 2 - 5:(x1 + x2) // 2 + 5]
        self.assertTrue(np.allclose(centro.mean(axis=(0, 1)), config.COLOR_PRIVACIDAD, atol=12))


class _T:
    """Tensor simulado de ultralytics (.cpu().numpy())."""

    def __init__(self, a):
        self.a = np.asarray(a, dtype=float)

    def cpu(self):
        return self

    def numpy(self):
        return self.a


class _Cajas:
    def __init__(self, cajas, clases, confs):
        self.xyxy, self.cls, self.conf = _T(cajas), _T(clases), _T(confs)

    def __len__(self):
        return len(self.cls.a)


class VehiculoTests(unittest.TestCase):
    """Vehículo principal (detecciones simuladas)."""

    def _v(self, categoria, caja, area, conf=0.9, mascara=None):
        return {"categoria": categoria, "confianza": conf, "caja": caja, "area": area, "mascara": mascara}

    def test_sin_vehiculos(self):
        self.assertIsNone(logica.elegir_vehiculo([], []))

    def test_gana_el_que_contiene_el_centro_de_la_placa(self):
        vs = [self._v("bus", [0, 0, 300, 300], 50000, 0.99), self._v("moto", [400, 100, 500, 300], 4000, 0.3)]
        placas = [{"caja": [430, 250, 470, 270], "formato_bolivia": True, "confianza_lectura": 80}]
        self.assertEqual(logica.elegir_vehiculo(vs, placas)["categoria"], "moto")

    def test_usa_la_placa_leida_no_la_primera(self):
        vs = [self._v("bus", [0, 0, 300, 300], 50000), self._v("moto", [400, 100, 500, 300], 4000)]
        placas = [{"caja": [100, 200, 140, 220], "formato_bolivia": False, "confianza_lectura": 99},
                  {"caja": [430, 250, 470, 270], "formato_bolivia": True, "confianza_lectura": 70}]
        self.assertEqual(logica.elegir_vehiculo(vs, placas)["categoria"], "moto")

    def test_superpuestos_prefiere_la_mascara(self):
        m = np.zeros((300, 300), bool)
        m[200:260, 100:200] = True
        vs = [self._v("bus", [0, 0, 300, 300], 60000, mascara=np.zeros((300, 300), bool)),
              self._v("auto", [80, 150, 220, 280], 6000, mascara=m)]
        placas = [{"caja": [130, 220, 170, 240], "formato_bolivia": True}]
        self.assertEqual(logica.elegir_vehiculo(vs, placas)["categoria"], "auto")

    def test_sin_placa_grande_y_centrado(self):
        forma = (480, 640)
        esquina = self._v("camion", [0, 0, 200, 160], 32000)
        centro = self._v("minibus", [220, 140, 420, 340], 30000)
        self.assertEqual(logica.elegir_vehiculo([esquina, centro], [], forma)["categoria"], "minibus")

    def test_sin_placa_exige_confianza_040(self):
        forma = (480, 640)
        flojo = self._v("bus", [100, 100, 540, 400], 100000, conf=0.39)
        chico = self._v("auto", [0, 0, 80, 60], 3000, conf=0.41)
        self.assertEqual(logica.elegir_vehiculo([flojo, chico], [], forma)["categoria"], "auto")
        self.assertIsNone(logica.elegir_vehiculo([flojo], [], forma))

    def test_recorte_con_margen_dentro_de_la_imagen(self):
        self.assertEqual(logica.recorte([10, 20, 110, 220], (230, 115), margen=0.1), (0, 0, 115, 230))


class ClasesM2Tests(unittest.TestCase):
    """Las clases salen de model.names y se traducen con el mapa explícito de config.py."""

    def _r(self, names, clases):
        n = len(clases)
        return SimpleNamespace(boxes=_Cajas([[0, 0, 10, 10]] * n, clases, [0.9] * n), masks=None, names=names)

    def test_traduce_con_model_names(self):
        names = {0: "auto", 1: "bus", 2: "camion", 3: "minibus", 4: "moto"}
        with mock.patch.object(servidor, "M2", mock.Mock(listo=True, predecir=mock.Mock(
                return_value=self._r(names, [4, 2, 0])))):
            vs = servidor.detectar_vehiculos(np.zeros((20, 20, 3), np.uint8))
        self.assertEqual([(v["categoria"], v["clase_modelo"]) for v in vs],
                         [("moto", "moto"), ("camion", "camion"), ("auto", "auto")])

    def test_clase_fuera_del_mapa_se_ignora(self):
        with mock.patch.object(servidor, "M2", mock.Mock(listo=True, predecir=mock.Mock(
                return_value=self._r({0: "tractor", 1: "bus"}, [0, 1])))):
            vs = servidor.detectar_vehiculos(np.zeros((20, 20, 3), np.uint8))
        self.assertEqual([v["categoria"] for v in vs], ["bus"])

    def test_el_mapa_cubre_el_modelo_real(self):
        from ultralytics import YOLO
        nombres = set(YOLO(str(config.M2_VEHICULOS)).names.values())
        self.assertEqual(nombres - set(config.M2_CATEGORIAS), set())


def _llanta5(cx, cy, d, ancho=None, conf=0.9):
    """Llanta simulada con los 5 puntos del modelo (centro, abajo, arriba, izquierda, derecha)."""
    a = d if ancho is None else ancho
    pts = [[cx, cy, 0.9], [cx, cy + d / 2, 0.9], [cx, cy - d / 2, 0.9], [cx - a / 2, cy, 0.9], [cx + a / 2, cy, 0.9]]
    return logica.llanta([cx - a / 2, cy - d / 2, cx + a / 2, cy + d / 2], conf, pts)


class LlantaTests(unittest.TestCase):
    def test_5_puntos(self):
        l = _llanta5(100, 200, 40, ancho=36)
        self.assertEqual((l["centro"], l["alto"], l["ancho"]), ([100, 200], 40, 36))

    def test_1_punto_usa_la_caja(self):
        l = logica.llanta([80, 180, 120, 230], 0.8, [[101, 204, 0.9]])
        self.assertEqual((l["centro"], l["alto"], l["ancho"]), ([101, 204], 50, 40))

    def test_solo_cajas(self):
        l = logica.llanta([80, 180, 120, 230], 0.8)
        self.assertEqual((l["centro"], l["alto"], l["puntos"]), ([100, 205], 50, None))

    def test_punto_poco_confiable_cae_a_la_caja(self):
        pts = [[100, 200, 0.9], [100, 300, 0.1], [100, 180, 0.9], [80, 200, 0.9], [120, 200, 0.9]]
        self.assertEqual(logica.llanta([80, 180, 120, 220], 0.8, pts)["alto"], 40)


class EjesTests(unittest.TestCase):
    VEH = [0, 0, 1000, 400]

    def test_auto_lateral_2_ejes(self):
        ejes, propias, d = logica.contar_ejes(self.VEH, [_llanta5(150, 350, 80), _llanta5(800, 350, 80)])
        self.assertEqual((ejes, len(propias), d["vista"]), (2, 2, "lateral"))

    def test_camion_con_rueda_doble_fusiona(self):
        """Tándem trasero pegado (< 0.6 diámetros) cuenta como un eje."""
        llantas = [_llanta5(100, 350, 80), _llanta5(600, 350, 80), _llanta5(640, 350, 80), _llanta5(900, 350, 80)]
        self.assertEqual(logica.contar_ejes(self.VEH, llantas)[0], 3)

    def test_camion_5_ejes(self):
        llantas = [_llanta5(x, 350, 80) for x in (80, 300, 400, 750, 850)]
        self.assertEqual(logica.contar_ejes(self.VEH, llantas)[0], 5)

    def test_lado_lejano_se_descarta(self):
        """Las llantas del lado lejano se ven más chicas y no suman ejes."""
        llantas = [_llanta5(150, 350, 80), _llanta5(800, 350, 80), _llanta5(450, 320, 40), _llanta5(550, 320, 40)]
        ejes, propias, d = logica.contar_ejes(self.VEH, llantas)
        self.assertEqual((ejes, len(propias), d["llantas_lado_cercano"]), (2, 4, 2))

    def test_llantas_fuera_o_arriba_no_son_del_vehiculo(self):
        llantas = [_llanta5(150, 350, 80), _llanta5(1200, 350, 80), _llanta5(500, 60, 80)]
        ejes, propias, _ = logica.contar_ejes(self.VEH, llantas)
        self.assertEqual((ejes, len(propias)), (2, 1))  # una sola llanta → mínimo 2 ejes

    def test_vista_frontal_no_cuenta(self):
        llantas = [_llanta5(150, 350, 80, ancho=20), _llanta5(850, 350, 80, ancho=22)]
        ejes, _, d = logica.contar_ejes(self.VEH, llantas)
        self.assertEqual((ejes, d["vista"]), (None, "frontal_posterior"))

    def test_sin_llantas_es_null(self):
        self.assertIsNone(logica.contar_ejes(self.VEH, [])[0])


class FormasTests(unittest.TestCase):
    def test_poligono_de_un_rectangulo_con_origen(self):
        m = np.zeros((100, 200), np.uint8)
        m[20:60, 30:150] = 1
        (pol,) = logica.poligonos(m, origen=(10, 5))
        self.assertEqual(sorted(map(tuple, pol)), sorted([(40, 25), (40, 64), (159, 64), (159, 25)]))

    def test_simplifica_a_60_puntos_y_vacia_es_lista_vacia(self):
        m = np.zeros((600, 600), np.uint8)
        cv2.circle(m, (300, 300), 250, 1, -1)
        for y in range(60, 540, 6):  # borde dentado: muchos vértices antes de simplificar
            m[y:y + 3, 40:56] = 0
        (pol,) = logica.poligonos(m)
        self.assertLessEqual(len(pol), logica.MAX_PUNTOS_POLIGONO)
        self.assertGreaterEqual(len(pol), 8)
        self.assertEqual(logica.poligonos(None), [])
        self.assertEqual(logica.poligonos(np.zeros((10, 10), np.uint8)), [])

    def test_solo_mayor_y_varias_partes(self):
        m = np.zeros((100, 100), np.uint8)
        m[10:30, 10:30] = 1
        m[50:95, 50:95] = 1
        self.assertEqual(len(logica.poligonos(m)), 2)
        (mayor,) = logica.poligonos(m, solo_mayor=True)
        self.assertTrue(all(x >= 50 for x, _ in mayor))

    def test_keypoints_con_confianza_y_nombres(self):
        l = _llanta5(100, 200, 40)
        self.assertEqual(len(l["keypoints"]), len(logica.NOMBRES_KPT))
        self.assertEqual(l["keypoints"][0], [100, 200, 0.9])
        self.assertIsNone(logica.llanta([0, 0, 10, 10], 0.8)["keypoints"])

    def test_cada_llanta_sabe_su_eje(self):
        """Tándem fusionado = mismo eje; las del lado lejano van al eje más cercano en x."""
        llantas = [_llanta5(100, 350, 80), _llanta5(600, 350, 80), _llanta5(640, 350, 80), _llanta5(900, 350, 80),
                   _llanta5(130, 320, 40)]
        ejes, propias, _ = logica.contar_ejes([0, 0, 1000, 400], llantas)
        self.assertEqual(ejes, 3)
        self.assertEqual([l["eje"] for l in llantas], [1, 2, 2, 3, 1])

    def test_vista_frontal_sin_eje(self):
        llantas = [_llanta5(150, 350, 80, ancho=20), _llanta5(850, 350, 80, ancho=22)]
        logica.contar_ejes([0, 0, 1000, 400], llantas)
        self.assertEqual([l["eje"] for l in llantas], [None, None])


class PasajerosYPrivacidadTests(unittest.TestCase):
    VEH = [100, 100, 300, 300]

    def _persona(self, caja, con_mascara=True):
        x1, y1, x2, y2 = caja
        m = np.ones((y2 - y1, x2 - x1), bool) if con_mascara else None
        return {"caja": caja, "confianza": 0.9, "mascara": m, "origen": (x1, y1)}

    def test_cuenta_solo_las_de_dentro(self):
        personas = [self._persona([150, 150, 200, 250]), self._persona([400, 100, 450, 250]),
                    self._persona([280, 150, 340, 250], con_mascara=False)]  # 1/3 dentro
        self.assertEqual(logica.contar_pasajeros(self.VEH, personas), 1)

    def test_cubre_con_color_solido(self):
        img = np.random.default_rng(0).integers(0, 255, (400, 400, 3), dtype=np.uint8)
        logica.cubrir_personas(img, [self._persona([150, 150, 200, 250]),
                                     self._persona([10, 10, 30, 40], con_mascara=False)])
        self.assertTrue((img[150:250, 150:200] == config.COLOR_PRIVACIDAD).all())
        self.assertTrue((img[10:40, 10:30] == config.COLOR_PRIVACIDAD).all())

    def test_endpoint_cubre_personas_en_evidencia_y_anotada(self):
        """Con modelos simulados: la evidencia y la anotada no muestran a la persona, solo el conteo."""
        img = np.random.default_rng(1).integers(0, 255, (480, 640, 3), dtype=np.uint8)
        persona = self._persona([200, 150, 260, 260])
        vehiculo = {"categoria": "auto", "clase_modelo": "auto", "confianza": 0.9, "caja": [100, 100, 500, 400],
                    "area": 120000, "mascara": None}
        cm = mock.patch.dict(os.environ, {"IAP_TOKEN": TOKEN})
        cm.start()
        self.addCleanup(cm.stop)
        with mock.patch.object(servidor, "leer_placas", return_value=[]), \
                mock.patch.object(servidor, "detectar_vehiculos", return_value=[vehiculo]), \
                mock.patch.object(servidor, "M3", mock.Mock(listo=True)), \
                mock.patch.object(servidor, "M4", mock.Mock(listo=True)), \
                mock.patch.object(servidor, "detectar_llantas",
                                  return_value=[_llanta5(160, 360, 70), _llanta5(440, 360, 70)]), \
                mock.patch.object(servidor, "detectar_personas", return_value=[persona]):
            d = TestClient(servidor.app).post(
                "/inferir", files={"imagen": ("f.png", cv2.imencode(".png", img)[1].tobytes(), "image/png")},
                headers={"X-API-Token": TOKEN}, params={"anotada": "true"}).json()
        self.assertEqual((d["categoria"], d["ejes"], d["llantas_detectadas"], d["pasajeros"]), ("auto", 2, 2, 1))
        self.assertEqual(d["cajas"]["personas"], [[200, 150, 260, 260]])
        (silueta,) = d["formas"]["pasajeros"]
        self.assertEqual(silueta["caja"], [200, 150, 260, 260])
        self.assertEqual(sorted(map(tuple, silueta["poligonos"][0])),
                         sorted([(200, 150), (200, 259), (259, 259), (259, 150)]))  # máscara en la imagen completa
        self.assertEqual([l["eje"] for l in d["formas"]["llantas"]], [1, 2])
        for clave in ("evidencia", "imagen"):
            cubierta = _decodificar_data_url(d[clave])[170:240, 210:250].astype(float)
            self.assertTrue(np.allclose(cubierta.mean(axis=(0, 1)), config.COLOR_PRIVACIDAD, atol=10), clave)
            self.assertLess(cubierta.std(), 12, clave)  # color sólido: nada del original


if __name__ == "__main__":
    unittest.main()
