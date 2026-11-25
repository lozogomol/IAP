# inferencia

Endpoint único que sirve los 4 modelos (Colab + ngrok o RunPod).

## modelos/ — pesos (se suben al repo; rutas en `servidor/config.py`)

| Archivo | Modelo | Tarea | Clases / salida |
|---|---|---|---|
| `iap_m1_placas_detector.onnx` | M1 detector de placas (YOLOv9-t 384, ONNX end2end) | detect | caja de la placa |
| `iap_m1_placas_ocr.onnx` + `iap_m1_placas_ocr.yaml` | M1 OCR (fast-plate-ocr cct-xs-v2) | ocr | texto (formato boliviano) |
| `iap_m2_vehiculos_seg.pt` | M2 tipo de vehículo (YOLO11s-seg, imgsz 640) | segment | auto, bus, camion, minibus, moto |
| `iap_m3_llantas_pose.pt` | M3 llantas → ejes (YOLO11s-pose, imgsz 640) | pose | llanta, `kpt_shape` [5, 3]: centro, abajo, arriba, izquierda, derecha |
| `iap_m4_pasajeros_seg.pt` | M4 pasajeros (YOLO11s-seg COCO, imgsz 640) | segment | 80 clases COCO; solo se usa `person` |
| `archivo/iap_m2_vehiculos_seg_v1_propio.pt` | M2 v1 entrenado por nosotros (solo para comparar en el informe; no se carga) | segment | mismas 5 clases |

Para cambiar de modelo: edita la ruta en `servidor/config.py` (o usa `IAP_M2_MODELO`, `IAP_M3_MODELO`, `IAP_M4_MODELO`,
`IAP_M1_DETECTOR`, `IAP_M1_OCR`, `IAP_M1_OCR_CONFIG`). Al cargar, el servidor comprueba la tarea de cada `.pt` y que
las clases de M2 estén en `config.M2_CATEGORIAS` (mapa explícito nombre del modelo → categoría del sistema).

## servidor — API FastAPI (`servidor/app.py`, lógica en `servidor/logica.py`)

- `GET /salud` — estado del servidor y de los 4 modelos (`modelos` y `detalle_modelos` con archivo, tarea y clases).
- `POST /inferir` — multipart `imagen` + cabecera `X-API-Token` →
  `placa`, `confianza`, `categoria`, `confianza_categoria`, `ejes`, `llantas_detectadas`, `pasajeros`,
  `latencia_ms` (`total`, `m1`…`m4`), `cajas` (`placa`, `vehiculo`, `placas`, `llantas`, `personas`), `detalle`
  (vista, llantas del lado cercano, motivo de los ejes, clase del modelo), `placas` (cada una con SU vehículo),
  `evidencia` (frame con las personas cubiertas, si hubo personas) y, con `?anotada=true`, `imagen`.
  `formas` (para dibujar en el punto de cobro, en coordenadas de la imagen recibida): `vehiculo.poligono` (contorno
  de la máscara de M2, simplificado con approxPolyDP a ~60 puntos), `llantas` (cada una con sus `keypoints`
  `[x, y, confianza]` en el orden de `nombres_keypoints` y el `eje` al que pertenece), `pasajeros` (polígonos de las
  máscaras de M4, sin recortes ni datos de rostro) y `placa.caja` (rectángulo de M1). Los demás campos no cambian.
  - **M1**: lectura restringida a formato boliviano (`123ABC` / `1234ABC`).
  - **M2**: vehículo principal = el que contiene el centro de la placa leída; sin placa, el más grande y centrado con
    confianza ≥ 0.40.
  - **M3** (sobre el recorte del vehículo principal): llantas en la parte baja del vehículo → solo las del lado
    cercano (altura visible ≥ 75 % de la mayor) → ordenadas en x, las que están a < 0.6 diámetros son un mismo eje →
    mínimo 2. Vista frontal/posterior (llantas de canto) o sin llantas → `ejes: null`.
  - **M4** (sobre el recorte): pasajeros = personas con ≥ 60 % de su máscara dentro de la caja del vehículo.
    **Privacidad**: sin reconocimiento facial ni recortes de rostros; en la anotada y en la evidencia las personas se
    cubren con su máscara en color sólido y solo se informa el conteo.
- Variables: `IAP_TOKEN` (obligatoria; en Render es `INFERENCE_TOKEN`), `IAP_MAX_MB` (6), `IAP_UMBRAL` (0.4),
  `IAP_DISPOSITIVO` (`cpu` / `0`).
- Local: `cd inferencia/servidor && pip install -r requirements.txt && IAP_TOKEN=... uvicorn app:app --port 8000`
- Tests: `cd inferencia/servidor && python -m unittest -v test_app`

## colab_servidor_iap.ipynb — servidor en Colab + ngrok

Abre el notebook en Colab, crea los secretos `NGROK_AUTHTOKEN` e `IAP_TOKEN` (opcional `NGROK_DOMINIO` para una URL fija)
y ejecuta todo. Imprime la URL pública para poner en Render como `INFERENCE_URL`.

## m1_placas — scripts de M1 (detección de placas + OCR)

Reutilizado del proyecto anterior (placas-web). Los pesos están en `modelos/` (ver arriba).

- `detector.py` — detector compartido: YOLOv9-t (384 px, ONNX end2end) que devuelve las cajas de las placas y valida el formato boliviano.
- `placas_fastocr.py` — lectura con fast-plate-ocr (opción principal).
- `placas_easyocr.py`, `placas_tesseract.py` — lectores alternativos usados para comparar.
