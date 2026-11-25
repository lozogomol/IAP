"""Servidor de inferencia de IAP (FastAPI): M1 placas, M2 tipo de vehículo, M3 ejes (llantas), M4 pasajeros.

Rutas de los modelos y umbrales: config.py (un único archivo para cambiar de modelo).

    GET  /salud     → estado del servidor y de los 4 modelos (público, sin datos sensibles)
    POST /inferir   → multipart con el campo `imagen` (JPEG/PNG) y la cabecera X-API-Token

Respuesta de /inferir (confianzas en 0..100, cajas [x1, y1, x2, y2] en píxeles de la imagen recibida):

    {"placa": "2451KTP" | null, "confianza": 93.4 | null,          M1, solo lecturas con formato boliviano
     "categoria": "auto" | null, "confianza_categoria": 91.2,       M2 del vehículo principal (español del sistema)
     "ejes": 2 | null, "llantas_detectadas": 4,                      M3 sobre el recorte del vehículo principal
     "pasajeros": 1 | null,                                          M4 sobre el recorte del vehículo principal
     "latencia_ms": {"total", "m1", "m2", "m3", "m4"},
     "cajas": {"placa", "vehiculo", "placas": [...], "llantas": [...], "personas": [...]},
     "detalle": {"vista", "llantas_lado_cercano", "motivo_ejes", "clase_modelo", ...},
     "placas": [{"texto", "formato_bolivia", "confianza_deteccion", "confianza_lectura", "caja",
                 "categoria", "confianza_categoria", "caja_vehiculo"}],   cada placa con SU vehículo
     "caja_vehiculo": [...], "milisegundos": 42,                     compatibilidad
     "evidencia": "data:image/jpeg;base64,..." | null,              frame con las personas cubiertas (si hubo)
     "formas": {                                                     para dibujar en el punto de cobro:
        "vehiculo": {"categoria", "caja", "poligono": [[x, y], ...] | null} | null,   contorno de la máscara M2
        "llantas": [{"caja", "confianza", "centro", "eje": 1 | null, "keypoints": [[x, y, conf], ...] | null}],
        "nombres_keypoints": ["centro", "abajo", "arriba", "izquierda", "derecha"],   orden de los keypoints de M3
        "pasajeros": [{"caja", "poligonos": [[[x, y], ...], ...]}],   máscaras M4 (sin recortes ni datos de rostro)
        "placa": {"caja", "texto"} | null},                            M1: detección (rectángulo), no segmentación
     "imagen": "data:image/jpeg;base64,..."}                        solo con ?anotada=true

Polígonos simplificados con approxPolyDP (máx. ~60 puntos). Los campos anteriores no cambian.

Vehículo principal = el que contiene el centro de la placa leída; sin placa, el más grande y centrado con confianza
>= 0.40. `ejes` = null si no hay llantas, si es vista frontal/posterior o si M3 no está cargado; nunca se inventa.

PRIVACIDAD: no hay reconocimiento facial ni recortes de rostros. En la imagen anotada y en la evidencia las personas
se cubren con su máscara en color sólido; solo se informa el conteo.

Variables de entorno: IAP_TOKEN (obligatorio para /inferir), IAP_MAX_MB, IAP_UMBRAL, IAP_DISPOSITIVO y las rutas
IAP_M1_DETECTOR, IAP_M1_OCR, IAP_M1_OCR_CONFIG, IAP_M2_MODELO, IAP_M3_MODELO, IAP_M4_MODELO (ver config.py).

Ejecutar:  uvicorn app:app --host 0.0.0.0 --port 8000   (desde inferencia/servidor)
"""
import base64
import logging
import os
import secrets
import sys
import threading
import time
from contextlib import asynccontextmanager

import cv2
import numpy as np
from fastapi import FastAPI, File, Header, HTTPException, Query, UploadFile

import config
import logica

sys.path.insert(0, str(config.RAIZ / "m1_placas"))
import detector  # noqa: E402  (m1_placas/detector.py: YOLOv9-t ONNX compartido)

log = logging.getLogger("iap.inferencia")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

VERSION = "2.0.0"
_lector = None
_lock_carga = threading.Lock()
_inicio = time.time()
_contador = {"inferencias": 0, "errores": 0}
_dispositivo = "cpu"


# --- Modelos -------------------------------------------------------------------------------------------

class Modelo:
    """Un modelo de ultralytics: se carga una vez al arrancar; predict con lock (no son seguros entre hilos)."""

    def __init__(self, clave, nombre, ruta, tarea, imgsz):
        self.clave, self.nombre, self.ruta, self.tarea, self.imgsz = clave, nombre, ruta, tarea, imgsz
        self.yolo, self.error, self._lock = None, "", threading.Lock()
        self.estado = "pendiente" if not ruta.exists() else "sin_cargar"

    @property
    def listo(self):
        return self.yolo is not None

    def cargar(self, dispositivo):
        if self.yolo is not None or not self.ruta.exists():
            return
        self.estado = "cargando"
        try:
            from ultralytics import YOLO
            modelo = YOLO(str(self.ruta))
            if modelo.task != self.tarea:
                raise ValueError(f"se esperaba task={self.tarea} y el modelo es task={modelo.task}")
            modelo.predict(np.zeros((self.imgsz, self.imgsz, 3), np.uint8), imgsz=self.imgsz, device=dispositivo,
                           verbose=False)  # calentamiento
            self.yolo, self.estado = modelo, "cargado"
            log.info("%s cargado (%s, task=%s, %d clases)", self.clave, self.ruta.name, modelo.task,
                     len(modelo.names))
        except Exception as e:  # noqa: BLE001  (el servidor sigue sirviendo los demás modelos)
            self.estado, self.error = "error", str(e)
            log.exception("No se pudo cargar %s (%s)", self.clave, self.ruta)

    def predecir(self, imagen, **kw):
        with self._lock:
            return self.yolo.predict(imagen, imgsz=self.imgsz, device=_dispositivo, verbose=False, **kw)[0]

    def info(self):
        datos = {"estado": self.estado, "nombre": self.nombre, "archivo": self.ruta.name, "tarea": self.tarea}
        if self.yolo is not None:
            nombres = list(self.yolo.names.values())
            datos["clases"] = nombres if len(nombres) <= 10 else f"{len(nombres)} clases"
            kpt = getattr(self.yolo.model.model[-1], "kpt_shape", None)
            if kpt:
                datos["kpt_shape"] = list(kpt)
        if self.error:
            datos["error"] = self.error
        return datos


M2 = Modelo("m2_vehiculos", "Tipo de vehículo", config.M2_VEHICULOS, "segment", config.M2_IMGSZ)
M3 = Modelo("m3_llantas", "Conteo de ejes", config.M3_LLANTAS, "pose", config.M3_IMGSZ)
M4 = Modelo("m4_pasajeros", "Conteo de pasajeros", config.M4_PASAJEROS, "segment", config.M4_IMGSZ)
_persona_id = None  # id de la clase "person" en M4 (se busca en model.names al cargar)


def lector():
    """OCR de fast-plate-ocr, cargado una sola vez con el modelo del repo (sin descargas)."""
    global _lector
    if _lector is None:
        with _lock_carga:
            if _lector is None:
                from fast_plate_ocr import LicensePlateRecognizer
                _lector = LicensePlateRecognizer(onnx_model_path=config.M1_OCR,
                                                 plate_config_path=config.M1_OCR_CONFIG, device="cpu")
    return _lector


def cargar_modelos():
    """M1 + M2/M3/M4 una sola vez. Verifica los nombres de clase reales contra config.py."""
    global _dispositivo, _persona_id
    detector.sesion()
    lector()
    try:
        import torch
        _dispositivo = config.DISPOSITIVO or ("0" if torch.cuda.is_available() else "cpu")
    except ImportError:
        _dispositivo = "cpu"
    for m in (M2, M3, M4):
        m.cargar(_dispositivo)
    if M2.listo:
        faltan = [n for n in M2.yolo.names.values() if n not in config.M2_CATEGORIAS]
        if faltan:
            log.warning("M2 trae clases sin traducir en config.M2_CATEGORIAS (se ignoran): %s", faltan)
    if M4.listo:
        _persona_id = next((i for i, n in M4.yolo.names.items() if n == config.M4_CLASE_PERSONA), None)
        if _persona_id is None:
            M4.estado, M4.error = "error", f"el modelo no tiene la clase '{config.M4_CLASE_PERSONA}'"
            M4.yolo = None


@asynccontextmanager
async def precargar(_app):
    t0 = time.perf_counter()
    cargar_modelos()
    log.info("Modelos cargados en %d ms (dispositivo %s)", (time.perf_counter() - t0) * 1000, _dispositivo)
    if not os.environ.get("IAP_TOKEN"):
        log.warning("IAP_TOKEN no está configurado: /inferir responderá 503.")
    yield


app = FastAPI(title="IAP · Servidor de inferencia", version=VERSION, lifespan=precargar,
              description="M1 placas (YOLOv9-t ONNX + fast-plate-ocr), M2 tipo de vehículo (YOLO11s-seg), "
                          "M3 ejes (YOLO11s-pose, llantas), M4 pasajeros (YOLO11s-seg, personas).")


def _token_valido(recibido):
    esperado = os.environ.get("IAP_TOKEN", "")
    if not esperado:
        raise HTTPException(503, "El servidor no tiene IAP_TOKEN configurado.")
    if not recibido or not secrets.compare_digest(recibido.encode(), esperado.encode()):
        raise HTTPException(401, "Token inválido.")


def _decodificar(datos):
    if not datos:
        raise HTTPException(400, "La imagen está vacía.")
    if len(datos) > config.MAX_BYTES:
        raise HTTPException(413, "La imagen es demasiado grande.")
    imagen = cv2.imdecode(np.frombuffer(datos, np.uint8), cv2.IMREAD_COLOR)
    if imagen is None:
        raise HTTPException(400, "No se pudo leer la imagen (¿es JPEG o PNG?).")
    return imagen


# --- Etapas ---------------------------------------------------------------------------------------------

def leer_placas(imagen):
    """M1: detecta y lee las placas, de mayor a menor confianza de lectura."""
    cajas = detector.detectar(imagen, umbral=config.M1_UMBRAL)
    placas = []
    ocr = lector()
    for x1, y1, x2, y2, conf_det in cajas:
        if x2 - x1 < 8 or y2 - y1 < 4:
            continue
        recorte = cv2.cvtColor(imagen[y1:y2, x1:x2], cv2.COLOR_BGR2RGB)
        pred = ocr.run_one(recorte, return_confidence=True)
        texto = detector.limpiar(pred.plate or "")
        conf_lec = float(np.mean(pred.char_probs)) if pred.char_probs is not None else None
        placas.append({
            "texto": texto,
            "formato_bolivia": bool(detector.FORMATO_BOLIVIA.match(texto)),
            "confianza_deteccion": round(conf_det * 100, 1),
            "confianza_lectura": None if conf_lec is None else round(conf_lec * 100, 1),
            "caja": [x1, y1, x2, y2],
        })
    placas.sort(key=lambda p: p["confianza_lectura"] or 0, reverse=True)
    return placas


def detectar_vehiculos(imagen):
    """M2 → [{"categoria", "clase_modelo", "confianza", "caja", "area", "mascara"}] en coordenadas de la imagen.

    La clase sale de model.names y se traduce con el mapa explícito config.M2_CATEGORIAS."""
    if not M2.listo:
        return []
    r = M2.predecir(imagen, conf=config.M2_CONF, retina_masks=True)
    if r.boxes is None or len(r.boxes) == 0:
        return []
    cajas = r.boxes.xyxy.cpu().numpy()
    clases = r.boxes.cls.cpu().numpy().astype(int)
    confs = r.boxes.conf.cpu().numpy()
    mascaras = (r.masks.data.cpu().numpy() > 0.5) if r.masks is not None else [None] * len(cajas)
    vehiculos = []
    for i, (caja, clase, conf) in enumerate(zip(cajas, clases, confs)):
        nombre = str(r.names[int(clase)])
        categoria = config.M2_CATEGORIAS.get(nombre)
        if categoria is None:
            continue
        x1, y1, x2, y2 = (int(round(float(v))) for v in caja)
        vehiculos.append({
            "categoria": categoria, "clase_modelo": nombre, "confianza": float(conf), "caja": [x1, y1, x2, y2],
            "area": int(mascaras[i].sum()) if mascaras[i] is not None else (x2 - x1) * (y2 - y1),
            "mascara": mascaras[i],
        })
    return vehiculos


def detectar_llantas(imagen, caja_recorte):
    """M3 sobre el recorte del vehículo → llantas en coordenadas de la imagen completa."""
    x1, y1, x2, y2 = caja_recorte
    r = M3.predecir(imagen[y1:y2, x1:x2], conf=config.M3_CONF)
    if r.boxes is None or len(r.boxes) == 0:
        return []
    cajas = r.boxes.xyxy.cpu().numpy() + [x1, y1, x1, y1]
    confs = r.boxes.conf.cpu().numpy()
    kpts = r.keypoints.data.cpu().numpy() if getattr(r, "keypoints", None) is not None else None
    llantas = []
    for i, (caja, conf) in enumerate(zip(cajas, confs)):
        puntos = None
        if kpts is not None and len(kpts[i]):
            puntos = kpts[i].copy()
            puntos[:, 0] += x1
            puntos[:, 1] += y1
        llantas.append(logica.llanta(caja, conf, puntos))
    return llantas


def detectar_personas(imagen, caja_recorte):
    """M4 (solo clase person) sobre el recorte → [{"caja", "confianza", "mascara", "origen"}]."""
    x1, y1, x2, y2 = caja_recorte
    r = M4.predecir(imagen[y1:y2, x1:x2], conf=config.M4_CONF, classes=[_persona_id], retina_masks=True)
    if r.boxes is None or len(r.boxes) == 0:
        return []
    cajas = r.boxes.xyxy.cpu().numpy() + [x1, y1, x1, y1]
    confs = r.boxes.conf.cpu().numpy()
    mascaras = (r.masks.data.cpu().numpy() > 0.5) if r.masks is not None else [None] * len(cajas)
    return [{"caja": [int(round(float(v))) for v in caja], "confianza": float(conf), "mascara": mascaras[i],
             "origen": (x1, y1)} for i, (caja, conf) in enumerate(zip(cajas, confs))]


def _datos_vehiculo(v):
    return {"categoria": v["categoria"] if v else None,
            "confianza_categoria": round(v["confianza"] * 100, 1) if v else None,
            "caja_vehiculo": v["caja"] if v else None}


def _jpeg(imagen, calidad=80):
    return "data:image/jpeg;base64," + base64.b64encode(
        cv2.imencode(".jpg", imagen, [cv2.IMWRITE_JPEG_QUALITY, calidad])[1]).decode()


def _anotar(imagen, placas, vehiculo, llantas, personas, ejes, pasajeros):
    """Imagen anotada. Las personas ya vienen cubiertas (cubrir_personas): solo se escribe el conteo."""
    if vehiculo is not None:
        color = (40, 40, 200)
        m = vehiculo.get("mascara")
        if m is not None and m.shape == imagen.shape[:2]:
            contornos, _ = cv2.findContours(m.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            cv2.drawContours(imagen, contornos, -1, color, 2, cv2.LINE_AA)
        x1, y1, x2, y2 = vehiculo["caja"]
        cv2.rectangle(imagen, (x1, y1), (x2, y2), color, 2)
        etiqueta = f"{vehiculo['categoria']} {vehiculo['confianza'] * 100:.0f}%"
        etiqueta += f" | ejes: {ejes if ejes is not None else '-'} | pasajeros: {pasajeros if pasajeros is not None else '-'}"
        (ancho, alto), _ = cv2.getTextSize(etiqueta, cv2.FONT_HERSHEY_SIMPLEX, 0.7, 2)
        y_txt = max(alto + 8, y1)
        cv2.rectangle(imagen, (x1, y_txt - alto - 8), (x1 + ancho + 8, y_txt), color, -1)
        cv2.putText(imagen, etiqueta, (x1 + 4, y_txt - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2,
                    cv2.LINE_AA)
    for l in llantas:
        cx, cy = (int(v) for v in l["centro"])
        cv2.circle(imagen, (cx, cy), max(3, int(l["alto"] / 2)), (0, 165, 255), 2, cv2.LINE_AA)
    for p in placas:
        x1, y1, x2, y2 = p["caja"]
        cv2.rectangle(imagen, (x1, y1), (x2, y2), (0, 200, 0), 3)
        cv2.putText(imagen, p["texto"] or "?", (x1, max(25, y1 - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.9,
                    (0, 200, 0), 2, cv2.LINE_AA)
    return _jpeg(imagen)


def _publica(llanta):
    return {k: llanta[k] for k in ("caja", "confianza", "centro", "puntos")}


def _formas(vehiculo, llantas, personas, leida):
    """Formas reales de cada modelo, en coordenadas de la imagen recibida, para dibujar sobre el video.

    PRIVACIDAD: de las personas solo salen los contornos de su máscara (para taparlas en pantalla) y su caja;
    nunca recortes, píxeles ni datos de rostro."""
    poligono = logica.poligonos(vehiculo.get("mascara"), solo_mayor=True) if vehiculo else []
    return {
        "vehiculo": {"categoria": vehiculo["categoria"], "caja": vehiculo["caja"],
                     "poligono": poligono[0] if poligono else None} if vehiculo else None,
        "llantas": [{"caja": l["caja"], "confianza": round(l["confianza"] * 100, 1), "centro": l["centro"],
                     "eje": l.get("eje"), "keypoints": l.get("keypoints")} for l in llantas],
        "nombres_keypoints": list(logica.NOMBRES_KPT),
        "pasajeros": [{"caja": p["caja"], "poligonos": logica.poligonos(p.get("mascara"), p.get("origen", (0, 0)))}
                      for p in personas],
        "placa": {"caja": leida["caja"], "texto": leida["texto"]} if leida else None,
    }


def analizar(img):
    """Corre M1 → M2 → (M3, M4 sobre el recorte del vehículo principal). Devuelve (respuesta, extras)."""
    lat = {}

    t = time.perf_counter()
    placas = leer_placas(img)
    lat["m1"] = round((time.perf_counter() - t) * 1000)

    t = time.perf_counter()
    vehiculos = detectar_vehiculos(img)
    vehiculo = logica.elegir_vehiculo(vehiculos, placas, img.shape)
    if vehiculos:  # cada placa con SU vehículo: la web muestra el de la placa que realmente lee
        for p in placas:
            p.update(_datos_vehiculo(logica.vehiculo_de_placa(vehiculos, p, img.shape)))
    lat["m2"] = round((time.perf_counter() - t) * 1000) if M2.listo else None

    ejes = pasajeros = None
    llantas, personas = [], []
    detalle = {"vehiculos_detectados": len(vehiculos), "clase_modelo": vehiculo["clase_modelo"] if vehiculo else None,
               "vista": None, "llantas_lado_cercano": 0, "motivo_ejes": ""}
    lat["m3"] = lat["m4"] = None
    if vehiculo is None:
        detalle["motivo_ejes"] = "Sin vehículo principal." if M2.listo else "M2 no está cargado."
    else:
        caja_rec = logica.recorte(vehiculo["caja"], img.shape)
        if M3.listo:
            t = time.perf_counter()
            todas = detectar_llantas(img, caja_rec)
            ejes, llantas, d = logica.contar_ejes(vehiculo["caja"], todas)
            detalle.update(d)
            lat["m3"] = round((time.perf_counter() - t) * 1000)
        else:
            detalle["motivo_ejes"] = "M3 no está cargado."
        if M4.listo:
            t = time.perf_counter()
            personas = detectar_personas(img, caja_rec)
            pasajeros = logica.contar_pasajeros(vehiculo["caja"], personas)
            lat["m4"] = round((time.perf_counter() - t) * 1000)

    leida = next((p for p in placas if p["formato_bolivia"]), None)  # M1 restringido a formato boliviano
    respuesta = {
        "placa": leida["texto"] if leida else None,
        "confianza": leida["confianza_lectura"] if leida else None,
        **_datos_vehiculo(vehiculo),
        "ejes": ejes,
        "llantas_detectadas": len(llantas),
        "pasajeros": pasajeros,
        "latencia_ms": lat,
        "cajas": {
            "placa": leida["caja"] if leida else None,
            "vehiculo": vehiculo["caja"] if vehiculo else None,
            "placas": [p["caja"] for p in placas],
            "llantas": [_publica(l) for l in llantas],
            "personas": [p["caja"] for p in personas],  # solo cajas: nunca recortes de rostros
        },
        "detalle": detalle,
        "placas": placas,
        "formas": _formas(vehiculo, llantas, personas, leida),
    }
    return respuesta, {"vehiculo": vehiculo, "llantas": llantas, "personas": personas}


@app.get("/salud")
def salud():
    m1 = "cargado" if _lector is not None and detector._SESION is not None else "cargando"
    return {
        "estado": "ok",
        "version": VERSION,
        "dispositivo": _dispositivo,
        "modelos": {"m1_placas": m1, **{m.clave: m.estado for m in (M2, M3, M4)}},
        "detalle_modelos": {
            "m1_placas": {"estado": m1, "nombre": "Lectura de placas", "archivo": config.M1_DETECTOR.name,
                          "ocr": config.M1_OCR.name, "tarea": "detect + ocr"},
            **{m.clave: m.info() for m in (M2, M3, M4)},
        },
        "token_configurado": bool(os.environ.get("IAP_TOKEN")),
        "inferencias": _contador["inferencias"],
        "errores": _contador["errores"],
        "activo_s": round(time.time() - _inicio),
    }


@app.post("/inferir")
def inferir(imagen: UploadFile = File(...),
            x_api_token: str | None = Header(default=None),
            anotada: bool = Query(False, description="Devuelve también la imagen anotada (más pesado).")):
    _token_valido(x_api_token)
    t0 = time.perf_counter()
    img = _decodificar(imagen.file.read(config.MAX_BYTES + 1))
    try:
        respuesta, extra = analizar(img)
    except Exception:  # noqa: BLE001
        _contador["errores"] += 1
        log.exception("Fallo en la inferencia")
        raise HTTPException(500, "Error interno en la inferencia.")
    _contador["inferencias"] += 1
    ms = round((time.perf_counter() - t0) * 1000)
    respuesta["latencia_ms"]["total"] = ms
    respuesta["milisegundos"] = ms

    # PRIVACIDAD: la evidencia (y la anotada) llevan a las personas cubiertas con color sólido.
    respuesta["evidencia"] = None
    if extra["personas"]:
        logica.cubrir_personas(img, extra["personas"])
        respuesta["evidencia"] = _jpeg(img, 85)
    if anotada:
        respuesta["imagen"] = _anotar(img, respuesta["placas"], extra["vehiculo"], extra["llantas"],
                                      extra["personas"], respuesta["ejes"], respuesta["pasajeros"])
    log.info("inferir %dx%d → %s · %s · ejes=%s · pasajeros=%s en %d ms %s", img.shape[1], img.shape[0],
             respuesta["placa"] or "-", respuesta["categoria"] or "-", respuesta["ejes"], respuesta["pasajeros"],
             ms, respuesta["latencia_ms"])
    return respuesta
