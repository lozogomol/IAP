"""Configuración única de los modelos del servidor de inferencia de IAP.

Para cambiar de modelo basta con cambiar la ruta aquí (o con la variable de entorno indicada). Todo lo demás
(app.py, m1_placas/detector.py, tests, notebook de Colab) lee las rutas y umbrales de este archivo.

Modelos (inspeccionados con ultralytics, no supuestos):
    M1  placas      iap_m1_placas_detector.onnx (YOLOv9-t 384, ONNX) + iap_m1_placas_ocr.onnx/.yaml (fast-plate-ocr)
    M2  vehículos   iap_m2_vehiculos_seg.pt  task=segment  names={0: auto, 1: bus, 2: camion, 3: minibus, 4: moto}
                                             imgsz=640 (YOLO11s-seg, Roboflow)
    M3  llantas     iap_m3_llantas_pose.pt   task=pose     names={0: llanta}  kpt_shape=[5, 3]  imgsz=640
                    (YOLO11s-pose). Orden de los 5 puntos, verificado sobre fotos: 0 centro, 1 abajo (contacto
                    con el suelo), 2 arriba, 3 izquierda, 4 derecha.
    M4  pasajeros   iap_m4_pasajeros_seg.pt  task=segment  80 clases COCO (0 = person)  imgsz=640
                    (es el YOLO11s-seg preentrenado en COCO; solo se usa la clase "person").
    Archivo: modelos/archivo/iap_m2_vehiculos_seg_v1_propio.pt (nuestro M2 v1, solo para comparar; no se carga).
"""
import os
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent  # inferencia/
MODELOS = RAIZ / "modelos"


def _ruta(variable, nombre):
    return Path(os.environ.get(variable) or MODELOS / nombre)


# --- Rutas ------------------------------------------------------------------------------------------
M1_DETECTOR = _ruta("IAP_M1_DETECTOR", "iap_m1_placas_detector.onnx")
M1_OCR = _ruta("IAP_M1_OCR", "iap_m1_placas_ocr.onnx")
M1_OCR_CONFIG = _ruta("IAP_M1_OCR_CONFIG", "iap_m1_placas_ocr.yaml")
M2_VEHICULOS = _ruta("IAP_M2_MODELO", "iap_m2_vehiculos_seg.pt")
M3_LLANTAS = _ruta("IAP_M3_MODELO", "iap_m3_llantas_pose.pt")
M4_PASAJEROS = _ruta("IAP_M4_MODELO", "iap_m4_pasajeros_seg.pt")
ARCHIVO = MODELOS / "archivo"  # modelos viejos para el informe (no se cargan)

# --- M1: placas -------------------------------------------------------------------------------------
M1_LADO = 384                                         # entrada del detector ONNX
M1_UMBRAL = float(os.environ.get("IAP_UMBRAL", "0.4"))

# --- M2: vehículos ----------------------------------------------------------------------------------
M2_IMGSZ = 640
M2_CONF = 0.25              # detección; para asociar la placa basta con esto
M2_CONF_SIN_PLACA = 0.40    # sin placa: el más grande y centrado, pero solo con confianza >= 0.40
# Nombre real de la clase en el modelo (model.names) → categoría del sistema (web: models.CATEGORIAS).
# Mapa explícito: si el modelo trae una clase que no está aquí, se avisa al cargarlo y esa detección se ignora.
M2_CATEGORIAS = {
    "auto": "auto",
    "bus": "bus",
    "camion": "camion",
    "minibus": "minibus",
    "moto": "moto",
}

# --- M3: llantas → ejes (lógica del notebook del ingeniero) ----------------------------------------
M3_IMGSZ = 640
M3_CONF = 0.35
M3_KPT_CONF = 0.5           # un punto con menos confianza no se usa (se cae a la caja)
M3_PARTE_BAJA = 0.45        # la llanta pertenece al vehículo si su centro está por debajo de este % de su alto
M3_LADO_CERCANO = 0.75      # lado cercano: llantas con altura visible >= 75 % de la más alta
M3_FUSION_DIAMETROS = 0.6   # dos llantas a menos de 0.6 diámetros en horizontal = un solo eje (rueda doble)
M3_EJES_MIN = 2
M3_VISTA_FRONTAL = 0.45     # ancho/alto mediano de las llantas por debajo de esto = vista frontal/posterior
M3_MARGEN_RECORTE = 0.08    # margen alrededor de la caja del vehículo (las llantas tocan el borde inferior)

# --- M4: pasajeros ----------------------------------------------------------------------------------
M4_IMGSZ = 640
M4_CONF = 0.35
M4_CLASE_PERSONA = "person"
M4_DENTRO = 0.6             # pasajero si >= 60 % de su máscara (o caja) cae dentro de la caja del vehículo
COLOR_PRIVACIDAD = (60, 60, 60)  # BGR: las personas se cubren con este color sólido (sin rostros visibles)

# --- Servidor ---------------------------------------------------------------------------------------
MAX_BYTES = int(float(os.environ.get("IAP_MAX_MB", "6")) * 1024 * 1024)
DISPOSITIVO = os.environ.get("IAP_DISPOSITIVO")  # "cpu", "0" (GPU); vacío = GPU si hay CUDA
