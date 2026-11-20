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

## Despliegue de la web (Render)
- Root Directory: `web`
- Build: `pip install -r requirements.txt && python manage.py collectstatic --noinput && python manage.py migrate`
- Start: `gunicorn config.wsgi:application`
- Variables de entorno: ver `.env.example`
