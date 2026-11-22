# IAP — Inteligencia Artificial de Peaje

Laboratorio Final de Hito · UNIFRANZ

## Integrantes
- Oliver Gonzales Morales — (rol)

## Estructura
```
/entrenamiento   notebooks de Colab (M2 segmentación, M3 keypoints)
/inferencia      endpoint único con los 3 modelos (RunPod)
  /m1_placas     modelo M1: detección de placas (YOLOv9-t ONNX) + OCR (fast-plate-ocr), reutilizado del proyecto anterior
/web             aplicación web Django: panel, API, agente
/movil           PWA empaquetada como APK
/docs            documentación de la API y diagramas
```

## Enlaces
- Web: https://iap-peaje.onrender.com
- Datasets Roboflow: (pendiente)
- Endpoint de inferencia: (pendiente)

## Contrato del endpoint de inferencia
La web llama a `POST {INFERENCE_URL}/inferir` (multipart, campo `imagen`, cabecera `X-API-Token`) y espera
`{"placas": [{"texto": "2451KTP", "confianza_lectura": 93.4}], "categoria": "auto", "ejes": 2}`.
`categoria`/`ejes` son opcionales hasta que existan M2/M3; sin ellos la tarifa sale del vehículo registrado.

## Modo caseta (cámara del celular)
`/caseta` usa la cámara trasera y envía 2 frames/s a `POST /api/v1/pasos/frame` durante 60 s como máximo.
La cámara del navegador exige **HTTPS** (Render, o un túnel ngrok hacia el servidor local).

## Despliegue de la web (Render)
- Root Directory: `web`
- Build: `pip install -r requirements.txt && python manage.py collectstatic --noinput && python manage.py migrate`
- Start: `gunicorn config.wsgi:application`
- Variables de entorno: ver `.env.example`
