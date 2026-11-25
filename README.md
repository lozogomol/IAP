# IAP — Inteligencia Artificial de Peaje

Laboratorio Final de Hito · UNIFRANZ

## Integrantes
- Oliver Gonzales Morales — (rol)

## Estructura
```
/entrenamiento   notebooks de Colab (M2 segmentación, M3 keypoints)
/inferencia      endpoint único con los 4 modelos (Colab + ngrok / RunPod)
  /modelos       pesos: iap_m1_placas_*.onnx, iap_m2_vehiculos_seg.pt, iap_m3_llantas_pose.pt, iap_m4_pasajeros_seg.pt
  /servidor      FastAPI (app.py), lógica (logica.py) y rutas de los modelos (config.py)
  /m1_placas     scripts de M1: detección de placas (YOLOv9-t ONNX) + OCR (fast-plate-ocr)
/web             aplicación web Django: panel, API, agente
/movil           PWA empaquetada como APK
/docs            documentación de la API y diagramas
```

## Enlaces
- Web: https://iap-peaje.onrender.com
- Datasets Roboflow: (pendiente)
- Endpoint de inferencia: (pendiente)

## Contrato del endpoint de inferencia
La web llama a `POST {INFERENCE_URL}/inferir` (multipart, campo `imagen`, cabecera `X-API-Token`) y recibe
`placa`, `confianza`, `categoria` (auto/bus/camion/minibus/moto), `confianza_categoria`, `ejes`, `llantas_detectadas`,
`pasajeros`, `latencia_ms` (total y por modelo), `cajas`, `detalle` y `evidencia` (detalle en `inferencia/README.md`).
Tarifa = categoría detectada + ejes detectados; si M3 no da los ejes, la mínima de esa categoría (y si no hay
categoría, la del vehículo registrado). Cada paso guarda categoría, ejes y pasajeros (null si no se detectaron) y,
como captura, la evidencia con las personas cubiertas por su máscara. Métricas de M2 en `docs/metricas/m2/`.

## Modo caseta (cámara del celular)
`/caseta` usa la cámara trasera (vertical 9:16 u horizontal 16:9, se adapta al girar) y envía frames JPEG 0.8 de
hasta 1280 px a `POST /api/v1/pasos/frame`. La cámara es continua: solo se detiene con «Detener» o al salir de la
página. **Un vehículo = un cobro**: la placa se confirma con 2 lecturas iguales en 4 s o 1 con confianza ≥ 0.80 y se
bloquea; con tipo (M2) y ejes (M3) por mayoría, o a los 6 s con respaldos (tipo del registro, clase M2 más vista o
«auto»; 2 ejes estimados), se registra y cobra una sola vez. El resultado queda fijo (`accion: fijo`) mientras se vea
la misma placa o ninguna; solo una placa distinta confirmada inicia otro vehículo, y la misma placa vuelve a cobrarse
tras otra placa distinta o 10 min sin verla. Frecuencia adaptativa para ahorrar peticiones: 2 fps capturando un
vehículo, 1 fps en espera y 0.5 fps con resultado fijo, nunca dos frames a la vez; el diagnóstico muestra las
peticiones de la sesión. Cada frame devuelve `accion` (`sin_placa`, `esperando_confirmacion`, `en_curso`,
`registrado`, `fijo` o `error`), `mensaje`, `flujo` y `lectura`, con las cajas dibujadas sobre el video. Cada frame queda
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
