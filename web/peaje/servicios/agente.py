"""Agente conversacional del peaje: Gemini con function calling y, si no hay LLM, un agente por reglas.

Las respuestas salen SOLO de las herramientas (la base de datos del peaje y la inferencia M1); nunca se inventan
placas, fechas ni montos. Herramientas:

  consultar_placa(placa)   estado de una placa: registrada_con_paso | registrada_sin_paso |
                           identificada_no_registrada (pasó sin estar registrada: posible evasión) | no_existe,
                           con sugerencias "¿Quisiste decir…?" ante errores típicos del OCR (O↔0, I↔1, B↔8, S↔5).
  historial_placa(placa)   últimos pasos de la placa.
  leer_placa_imagen()      lee la placa de la imagen adjunta con la inferencia (M1).

Con GEMINI_API_KEY usa el SDK oficial google-genai (modelo GEMINI_MODEL); si falta la clave o la API falla,
responde el agente por reglas con las mismas herramientas y respuestas en plantilla. Nunca queda sin responder.
"""
import logging
import re

from django.conf import settings
from django.utils import timezone

from ..inferencia import InferenciaNoDisponible, inferir
from ..models import Paso, Vehiculo
from ..placas import corregir_ocr, es_formato_bolivia, normalizar_placa, sugerencias

log = logging.getLogger("peaje.agente")

MODELO_POR_DEFECTO = "gemini-3.8-flash"
MAX_HISTORIAL = 10
LIMITE_HISTORIAL = 5
TIMEOUT_MS = 12000  # por petición a Gemini; con 2 llamadas a herramientas, máx. ~36 s antes del respaldo

INSTRUCCION = """Eres el asistente del sistema de peaje IAP (Bolivia). Respondes en español, breve y claro.
Reglas:
- Antes de hablar de cualquier placa, llama SIEMPRE a la herramienta (consultar_placa o historial_placa).
- Si el usuario adjunta una imagen, llama a leer_placa_imagen.
- Responde SOLO con los datos que devuelven las herramientas. Nunca inventes placas, fechas, horas, montos,
  tipos de vehículo ni ejes. Si un dato no está en la respuesta de la herramienta, di que no lo tienes.
- Estados: registrada_con_paso (está registrada y pasó: da fecha, hora, punto de cobro, tipo, ejes, tarifa y si se
  cobró), registrada_sin_paso (registrada pero sin pasos), identificada_no_registrada (pasó por el peaje sin estar
  registrada: posible evasión), no_existe (sin registros). Si hay sugerencias, pregunta "¿Quisiste decir …?".
- Si la imagen no tiene una placa legible, dilo y pide otra imagen.
- Si la pregunta no trata del peaje o de placas, explica brevemente qué puedes hacer.
- Texto plano, sin emojis ni markdown."""

AYUDA = ("Puedo consultar placas del peaje. Escribe, por ejemplo, \"¿Pasó la placa 1234ABC?\" o "
         "\"historial de 1234ABC\", o envía una foto de una placa.")


# --- Herramientas (lógica del peaje) -------------------------------------------------------------

def _paso_dict(paso):
    local = timezone.localtime(paso.created_at)
    return {
        "fecha": local.strftime("%d/%m/%Y"),
        "hora": local.strftime("%H:%M:%S"),
        "punto_de_cobro": paso.caseta.nombre,
        "tipo": paso.get_categoria_detectada_display() or "Sin tipo",
        "ejes": paso.ejes_texto,
        "tarifa_bs": f"{paso.tarifa:.2f}" if paso.tarifa is not None else None,
        "monto_cobrado_bs": f"{paso.monto_cobrado:.2f}",
        "resultado": paso.resultado_texto,
        "imagen_url": paso.imagen_url or None,
    }


def _sugerencias(placa):
    conocidas = set(Vehiculo.objects.values_list("placa", flat=True))
    conocidas |= set(Paso.objects.values_list("placa_normalizada", flat=True).distinct())
    lista = sugerencias(placa, conocidas)
    corregida = corregir_ocr(placa)
    if corregida != placa and corregida in conocidas and corregida not in lista:
        lista.insert(0, corregida)
    return lista[:5]


def consultar_placa(placa):
    """Estado de una placa en el peaje (normaliza, valida el formato boliviano y busca registro y último paso)."""
    normal = normalizar_placa(placa)
    datos = {"placa": normal, "formato_valido": es_formato_bolivia(normal), "sugerencias": [], "registrada": False,
             "ultimo_paso": None}
    if not normal:
        datos.update(estado="no_existe", mensaje="No se indicó una placa.")
        return datos
    vehiculo = Vehiculo.objects.filter(placa=normal).first()
    ultimo = (Paso.objects.select_related("caseta").filter(placa_normalizada=normal).order_by("-created_at").first())
    datos["registrada"] = vehiculo is not None
    if vehiculo:
        datos["vehiculo"] = {"tipo": vehiculo.get_categoria_display(), "ejes": vehiculo.ejes,
                             "activo": vehiculo.activo}
    if ultimo:
        datos["ultimo_paso"] = _paso_dict(ultimo)
    if vehiculo and ultimo:
        datos["estado"] = "registrada_con_paso"
    elif vehiculo:
        datos["estado"] = "registrada_sin_paso"
    elif ultimo:
        datos["estado"] = "identificada_no_registrada"
        datos["mensaje"] = "Pasó por el peaje sin estar registrada: posible evasión."
    else:
        datos["estado"] = "no_existe"
        datos["sugerencias"] = _sugerencias(normal)
        if not datos["formato_valido"]:
            datos["mensaje"] = "No tiene el formato de placa boliviana (3 o 4 números y 3 letras)."
    return datos


def historial_placa(placa, limite=LIMITE_HISTORIAL):
    """Últimos pasos de una placa (más reciente primero)."""
    normal = normalizar_placa(placa)
    pasos = Paso.objects.select_related("caseta").filter(placa_normalizada=normal).order_by("-created_at")
    total = pasos.count()
    datos = consultar_placa(normal)
    datos.update(total_pasos=total, pasos=[_paso_dict(p) for p in pasos[:limite]])
    return datos


def leer_placa_imagen(imagen):
    """Lee la placa de la imagen con la inferencia (M1) y consulta su estado."""
    if not imagen:
        return {"estado": "sin_imagen", "placa": None, "mensaje": "No se adjuntó ninguna imagen."}
    try:
        lecturas, _, _ = inferir(imagen, nombre="chat.jpg")
    except InferenciaNoDisponible as e:
        log.warning("agente: inferencia no disponible: %s", e)
        return {"estado": "inferencia_no_disponible", "placa": None,
                "mensaje": "El servicio de lectura de placas no está disponible ahora. Intenta más tarde o escribe la placa."}
    if not lecturas:
        return {"estado": "sin_lectura", "placa": None,
                "mensaje": "No encontré una placa legible en la imagen. Envía otra foto más cerca y de frente."}
    mejor = lecturas[0]
    datos = consultar_placa(mejor["placa"])
    datos["lectura"] = {"texto_leido": mejor.get("texto_leido") or mejor["placa"], "confianza": mejor.get("confianza")}
    return datos


# --- Respuestas en plantilla (agente por reglas y respaldo del LLM) ----------------------------------

def plantilla(datos):
    estado, placa = datos.get("estado"), datos.get("placa")
    leido = f"Leí la placa {placa} en la imagen. " if datos.get("lectura") else ""
    up = datos.get("ultimo_paso")
    detalle = ""
    if up:
        detalle = (f"Último paso: {up['fecha']} a las {up['hora']} en {up['punto_de_cobro']}; {up['tipo']}, "
                   f"{up['ejes']} ejes; tarifa {up['tarifa_bs'] or '–'} Bs ({up['resultado']}).")
    if estado == "registrada_con_paso":
        texto = f"La placa {placa} está registrada. {detalle}"
    elif estado == "registrada_sin_paso":
        texto = f"La placa {placa} está registrada, pero no tiene pasos por el peaje."
    elif estado == "identificada_no_registrada":
        texto = f"La placa {placa} pasó por el peaje pero NO está registrada (posible evasión). {detalle}"
    elif estado == "no_existe":
        texto = f"No hay registros de la placa {placa}." if placa else AYUDA
        if placa and datos.get("mensaje") and not datos.get("formato_valido"):
            texto += " " + datos["mensaje"]
        if datos.get("sugerencias"):
            texto += " ¿Quisiste decir " + " o ".join(datos["sugerencias"]) + "?"
    elif estado in ("sin_lectura", "sin_imagen", "inferencia_no_disponible"):
        texto = datos["mensaje"]
    else:
        texto = AYUDA
    if "pasos" in datos and datos.get("total_pasos"):
        lineas = [f"{p['fecha']} {p['hora']} · {p['tipo']} · {p['tarifa_bs'] or '–'} Bs · {p['resultado']}"
                  for p in datos["pasos"]]
        texto += f" Historial ({datos['total_pasos']} pasos, últimos {len(lineas)}): " + "; ".join(lineas) + "."
    return (leido + texto).strip()


PLACA_EN_TEXTO = re.compile(r"\b(\d{3,4})[\s-]?([A-Za-z]{3})\b")
CANDIDATA = re.compile(r"\b[A-Za-z0-9][A-Za-z0-9-]{4,8}[A-Za-z0-9]\b")


def placa_en_texto(texto):
    """Primera placa del texto: formato boliviano directo o un token que lo cumple tras corregir el OCR."""
    texto = texto or ""
    m = PLACA_EN_TEXTO.search(texto)
    if m:
        return normalizar_placa(m.group(1) + m.group(2))
    for token in CANDIDATA.findall(texto):
        normal = normalizar_placa(token)
        if len(normal) in (6, 7) and any(c.isdigit() for c in normal) and es_formato_bolivia(corregir_ocr(normal)):
            return normal
    return None


def agente_reglas(mensaje, imagen=None):
    """Agente sin LLM: imagen → leer_placa_imagen; placa en el texto → historial o consulta; si no, ayuda."""
    if imagen:
        datos = leer_placa_imagen(imagen)
    else:
        placa = placa_en_texto(mensaje)
        if placa is None:
            return {"respuesta": AYUDA, "estado": "ayuda", "placa": None, "sugerencias": [], "datos": None}
        quiere_historial = re.search(r"historial|pasos|veces|[uú]ltimos", mensaje or "", re.I)
        datos = historial_placa(placa) if quiere_historial else consultar_placa(placa)
    return _salida(plantilla(datos), datos)


def _salida(respuesta, datos):
    datos = datos or {}
    return {"respuesta": respuesta, "estado": datos.get("estado", "ayuda"), "placa": datos.get("placa"),
            "sugerencias": datos.get("sugerencias", []), "datos": datos or None}


# --- Agente con Gemini -----------------------------------------------------------------------------

def modelo():
    return getattr(settings, "GEMINI_MODEL", "") or MODELO_POR_DEFECTO


def agente_gemini(mensaje, historial=None, imagen=None):
    """Gemini con function calling automático sobre las herramientas. Lanza excepción si la API falla."""
    from google import genai
    from google.genai import types

    ultimo = {}

    def consultar_placa_tool(placa: str) -> dict:
        """Consulta el estado de una placa boliviana en el peaje: registro y último paso.

        Args:
            placa: La placa tal como la escribió el usuario, por ejemplo 1234ABC o 1234-abc.
        """
        ultimo["datos"] = consultar_placa(placa)
        return ultimo["datos"]

    def historial_placa_tool(placa: str) -> dict:
        """Devuelve los últimos pasos de una placa por el peaje.

        Args:
            placa: La placa, por ejemplo 1234ABC.
        """
        ultimo["datos"] = historial_placa(placa)
        return ultimo["datos"]

    def leer_placa_imagen_tool() -> dict:
        """Lee la placa de la imagen que adjuntó el usuario y consulta su estado en el peaje."""
        ultimo["datos"] = leer_placa_imagen(imagen)
        return ultimo["datos"]

    consultar_placa_tool.__name__ = "consultar_placa"
    historial_placa_tool.__name__ = "historial_placa"
    leer_placa_imagen_tool.__name__ = "leer_placa_imagen"
    herramientas = [consultar_placa_tool, historial_placa_tool]
    if imagen:
        herramientas.append(leer_placa_imagen_tool)

    contenidos = []
    for turno in (historial or [])[-MAX_HISTORIAL:]:
        texto = str(turno.get("texto") or "").strip()[:2000]
        if texto:
            rol = "user" if turno.get("rol") == "usuario" else "model"
            contenidos.append(types.Content(role=rol, parts=[types.Part.from_text(text=texto)]))
    texto = (mensaje or "").strip() or "Lee la placa de la imagen adjunta."
    if imagen:
        texto += "\n[El usuario adjuntó una imagen: usa leer_placa_imagen.]"
    contenidos.append(types.Content(role="user", parts=[types.Part.from_text(text=texto)]))

    # Tiempo máximo por llamada: si Gemini no responde, cae al agente por reglas (nunca queda sin responder).
    cliente = genai.Client(api_key=settings.GEMINI_API_KEY,
                           http_options=types.HttpOptions(timeout=TIMEOUT_MS, retry_options=types.HttpRetryOptions(attempts=1)))
    respuesta = cliente.models.generate_content(
        model=modelo(), contents=contenidos,
        config=types.GenerateContentConfig(
            system_instruction=INSTRUCCION, tools=herramientas, temperature=0.2,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(maximum_remote_calls=2)))
    datos = ultimo.get("datos")
    texto = (respuesta.text or "").strip()
    if not texto:
        texto = plantilla(datos) if datos else AYUDA
    return _salida(texto, datos)


def responder(mensaje, historial=None, imagen=None):
    """Punto de entrada: Gemini si hay clave; si no hay o falla, el agente por reglas. Siempre devuelve respuesta."""
    if getattr(settings, "GEMINI_API_KEY", ""):
        try:
            r = agente_gemini(mensaje, historial, imagen)
            r["motor"] = "gemini"
            return r
        except Exception:  # noqa: BLE001 - cualquier fallo del LLM cae al agente por reglas
            log.exception("agente: Gemini falló; respondo con el agente por reglas")
    r = agente_reglas(mensaje, imagen)
    r["motor"] = "reglas"
    return r
