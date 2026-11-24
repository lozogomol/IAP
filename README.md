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
`categoria` la da M2 (tipo de vehículo, YOLO11s-seg) y `ejes` llegará con M3; mientras no haya ejes la tarifa usa la
categoría detectada con 2 ejes, y si no hay categoría, la del vehículo registrado. Métricas de M2 en `docs/metricas/m2/`.

## Modo caseta (cámara del celular)
`/caseta` usa la cámara trasera y envía 2 frames/s (1280 px, JPEG 0.8) a `POST /api/v1/pasos/frame`; se pausa sola
a los 60 s y muestra un botón «Reanudar». Cada frame devuelve su resultado (`accion`: `registrado`,
`esperando_confirmacion`, `duplicado`, `sin_placa` o `error`, con `mensaje` y `lectura`), que se ve en la franja
«En vivo» con las cajas de la placa y del vehículo dibujadas sobre el video. Registra con 2 lecturas iguales en 4 s
o 1 lectura con confianza ≥ 0.80; la misma placa en la misma caseta no se cobra dos veces en 30 s. Cada frame queda
en el log del servidor (`peaje.caseta`, nivel con `IAP_LOG_NIVEL`).
La cámara del navegador exige **HTTPS** (Render, o un túnel ngrok hacia el servidor local).

## Despliegue de la web (Render)
- Root Directory: `web`
- Build: `pip install -r requirements.txt && python manage.py collectstatic --noinput && python manage.py migrate`
- Start: `gunicorn config.wsgi:application`
- Variables de entorno: ver `.env.example`

## Tiempo real y capturas
- **Pusher** (`PUSHER_APP_ID`, `PUSHER_KEY`, `PUSHER_SECRET`, `PUSHER_CLUSTER`): cada paso nuevo se emite en el canal
  privado `private-peaje` (autorizado en `/pusher/auth` con la sesión). Panel, KPIs, gráfica y banner de alertas se
  actualizan sin recargar. Sin Pusher, el panel consulta `/panel/datos` cada 10 s.
- **Capturas**: el frame que registró el paso se guarda en Cloudinary si existe `CLOUDINARY_URL`; si no, en
  `web/media/capturas/` (en Render el disco es efímero: configurar Cloudinary en producción).
- El modo caseta usa siempre la caseta **"Peaje IAP"** (migración 0004); las demás se conservan con su historial.
- La migración 0005 hace una **limpieza única**: borra pasos, alertas, lecturas y capturas (disco local y carpeta
  `iap/capturas` de Cloudinary), vacía el propietario de los vehículos (campo opcional que ya no se muestra) y no
  toca la tabla `usuarios`.
- Estilos: un solo archivo `web/static/css/iap.css` (tokens, mobile first, breakpoints 640/1024, esquinas rectas).
