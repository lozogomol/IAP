# inferencia

Endpoint único que servirá los 3 modelos (RunPod).

## servidor — API FastAPI (`servidor/app.py`)

- `GET /salud` — estado y modelos cargados (público).
- `POST /inferir` — multipart `imagen` + cabecera `X-API-Token` →
  `{"placas": [{"texto", "formato_bolivia", "confianza_deteccion", "confianza_lectura", "caja"}], "categoria", "confianza_categoria", "caja_vehiculo", "ejes": null, "milisegundos"}`.
  `categoria` (moto/auto/minibus/camion/bus) sale de M2 (`modelos/m2_seg.pt`, conf 0.35, imgsz 640): el vehículo que
  contiene el centro de la placa o, sin placa, el de mayor área de máscara. `ejes` queda en `null` hasta M3.
  `?anotada=true` añade la imagen con las cajas de la placa y la máscara y etiqueta del vehículo.
- Variables: `IAP_TOKEN` (obligatoria; en Render es `INFERENCE_TOKEN`), `IAP_MAX_MB` (6), `IAP_UMBRAL` (0.4).
- Local: `cd inferencia/servidor && pip install -r requirements.txt && IAP_TOKEN=... uvicorn app:app --port 8000`
- Tests: `cd inferencia/servidor && python -m unittest -v test_app`

## colab_servidor_iap.ipynb — servidor en Colab + ngrok

Abre el notebook en Colab, crea los secretos `NGROK_AUTHTOKEN` e `IAP_TOKEN` (opcional `NGROK_DOMINIO` para una URL fija)
y ejecuta todo. Imprime la URL pública para poner en Render como `INFERENCE_URL`.

## m1_placas — Modelo M1: detección de placas + OCR

Reutilizado del proyecto anterior (placas-web).

- `detector.py` — detector compartido: YOLOv9-t (384 px, ONNX end2end) que devuelve las cajas de las placas y valida el formato boliviano (`123ABC` / `1234ABC`).
- `placas_fastocr.py` — lectura con fast-plate-ocr (opción principal).
- `placas_easyocr.py`, `placas_tesseract.py` — lectores alternativos usados para comparar.
- `modelo/`
  - `yolo-v9-t-384-license-plates-end2end.onnx` — pesos del detector.
  - `cct_xs_v2_global.onnx` + `cct_xs_v2_global_plate_config.yaml` — modelo OCR de fast-plate-ocr y su configuración.
