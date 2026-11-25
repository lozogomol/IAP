# App móvil Peaje IAP (APK)

La app Android de Peaje IAP es la **misma web empaquetada como APK**: una PWA (Progressive Web App) instalable,
convertida en APK con [PWABuilder](https://www.pwabuilder.com) como **Trusted Web Activity (TWA)**.

- La TWA abre la web dentro de Chrome a pantalla completa, sin barra de direcciones, con el ícono y el nombre
  «Peaje IAP». No es un WebView: es el motor de Chrome, así que la cámara, el login con Google y Pusher funcionan
  igual que en el navegador.
- Usa **la misma API** (`/api/v1/...`) y **el mismo login con Google** (botón de Google Identity Services) que la
  web. No hay backend aparte: un cambio en la web se ve en la app al instante (el service worker es «network
  first» y nunca guarda la API ni páginas con datos).
- Abre directamente el **Punto de cobro** (`/caseta`).

## Qué expone la web para la app

| Ruta | Para qué |
|---|---|
| `/manifest.webmanifest` | Nombre, colores (azul noche `#050b1e`), íconos 192/512/512 maskable, `start_url` `/caseta`, `display: standalone`. |
| `/service-worker.js` | Desde la raíz (`Service-Worker-Allowed: /`). Solo cachea estáticos (CSS, JS, íconos, fuentes), «network first». Sin conexión muestra `/offline`. |
| `/.well-known/assetlinks.json` | Digital Asset Links: lo que haya en la variable `ASSETLINKS_JSON` (vacía = `[]`). |

Los íconos están en `web/static/iconos/` y se regeneran con `movil/generar_iconos.py` (necesita Pillow solo para
eso: `web\venv\Scripts\python.exe -m pip install Pillow` y `web\venv\Scripts\python.exe movil\generar_iconos.py`).

## Generar el APK con PWABuilder

1. Publica la web en Render (HTTPS) con los últimos cambios. Comprueba que abren:
   `https://<tu-app>.onrender.com/manifest.webmanifest` y `.../service-worker.js`.
2. Entra a <https://www.pwabuilder.com>, pega la URL de la web (`https://<tu-app>.onrender.com`) y pulsa **Start**.
   Debe reconocer el manifest, el service worker y los íconos.
3. Pulsa **Package For Stores** → **Android** → **Generate Package** (o *Options* para revisar):
   - *Package ID*: por ejemplo `com.iap.peaje` (no cambiarlo después: identifica la app).
   - *App name* / *Launcher name*: `Peaje IAP`. *Start URL*: `/caseta`. *Display mode*: `standalone`.
   - *Signing key*: **Create new** (o usa la tuya si ya existe).
4. Descarga el ZIP. Contiene:
   - el **APK** (para instalar directo) y el **AAB** (para Google Play),
   - la **llave de firma** (`signing.keystore` + `signing-key-info.txt`): **guárdala en un lugar seguro y no la
     subas a git**; sin ella no se puede publicar una actualización con el mismo Package ID,
   - `assetLinks.json`: el contenido que debe servir la web (paso siguiente).

## Configurar `ASSETLINKS_JSON` en Render

Sin este archivo la app funciona, pero muestra una barra de direcciones arriba (Chrome no puede comprobar que la
app y la web son del mismo dueño).

1. Abre el `assetLinks.json` que vino en el ZIP de PWABuilder. Es algo así:
   ```json
   [{"relation": ["delegate_permission/common.handle_all_urls"],
     "target": {"namespace": "android_app", "package_name": "com.iap.peaje",
                "sha256_cert_fingerprints": ["AB:CD:...:EF"]}}]
   ```
2. En Render → tu servicio web → **Environment** → **Add Environment Variable**:
   - Key: `ASSETLINKS_JSON`
   - Value: el contenido completo del archivo (puede ir en una sola línea).
3. Guarda (Render vuelve a desplegar) y comprueba que
   `https://<tu-app>.onrender.com/.well-known/assetlinks.json` devuelve ese JSON.
4. Si publicas en Google Play con «Play App Signing», agrega también la huella SHA-256 que muestra Play Console
   (Configuración → Integridad de la app) a `sha256_cert_fingerprints`.

## Instalar el APK en Android

1. Copia el `.apk` al teléfono (cable, Drive, WhatsApp, etc.) y ábrelo.
2. Android pedirá permitir **instalar apps de origen desconocido** para la app con la que lo abriste
   (Ajustes → Apps → Acceso especial → Instalar apps desconocidas). Acepta e instala.
3. Abre **Peaje IAP**. La primera vez:
   - Inicia sesión con **Google** (la cuenta debe estar autorizada en la web, igual que en el navegador).
   - En el Punto de cobro pulsa **Iniciar**: Chrome pedirá permiso para la **cámara**. Si lo rechazaste, actívalo en
     Ajustes → Apps → Peaje IAP (o Chrome) → Permisos → Cámara.
4. Requisitos: Android 7 o superior con **Google Chrome** instalado y actualizado (la TWA usa Chrome).

## Notas

- El login usa el botón de Google Identity Services (ventana emergente de Google), no un WebView, por eso es
  compatible con la TWA. En Google Cloud Console el «Origen autorizado de JavaScript» es el mismo dominio de
  Render que ya usa la web; la app no necesita otro.
- La API también acepta JWT (`Authorization: Bearer ...`) para clientes nativos; la app TWA usa la sesión web.
