# inferencia

Endpoint único que servirá los 3 modelos (RunPod).

## m1_placas — Modelo M1: detección de placas + OCR

Reutilizado del proyecto anterior (placas-web).

- `detector.py` — detector compartido: YOLOv9-t (384 px, ONNX end2end) que devuelve las cajas de las placas y valida el formato boliviano (`123ABC` / `1234ABC`).
- `placas_fastocr.py` — lectura con fast-plate-ocr (opción principal).
- `placas_easyocr.py`, `placas_tesseract.py` — lectores alternativos usados para comparar.
- `modelo/`
  - `yolo-v9-t-384-license-plates-end2end.onnx` — pesos del detector.
  - `cct_xs_v2_global.onnx` + `cct_xs_v2_global_plate_config.yaml` — modelo OCR de fast-plate-ocr y su configuración.
