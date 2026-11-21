"""Utilidades para placas bolivianas: normalización, validación y corrección de errores típicos del OCR."""
import re

FORMATO_BOLIVIA = re.compile(r"^\d{3,4}[A-Z]{3}$")

# Confusiones típicas del OCR entre letras y números
LETRA_A_NUMERO = {"O": "0", "I": "1", "B": "8", "S": "5", "Z": "2", "G": "6"}
NUMERO_A_LETRA = {v: k for k, v in LETRA_A_NUMERO.items()}


def normalizar_placa(texto):
    """Mayúsculas y solo letras A-Z y dígitos (quita guiones, espacios y símbolos)."""
    return re.sub(r"[^A-Z0-9]", "", (texto or "").upper())


def es_formato_bolivia(placa):
    return bool(FORMATO_BOLIVIA.match(placa or ""))


def corregir_ocr(placa):
    """Corrige confusiones letra/número según la posición.

    Placas de 7 caracteres = 4 dígitos + 3 letras; de 6 = 3 dígitos + 3 letras.
    Otras longitudes se devuelven sin cambios (no se sabe qué posición es qué).
    """
    placa = normalizar_placa(placa)
    if len(placa) not in (6, 7):
        return placa
    n = len(placa) - 3
    numeros = "".join(LETRA_A_NUMERO.get(c, c) for c in placa[:n])
    letras = "".join(NUMERO_A_LETRA.get(c, c) for c in placa[n:])
    return numeros + letras


def distancia_edicion(a, b, maximo=None):
    """Distancia de Levenshtein. Con `maximo`, corta en cuanto se supera (devuelve maximo + 1)."""
    if maximo is not None and abs(len(a) - len(b)) > maximo:
        return maximo + 1
    anterior = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        actual = [i]
        for j, cb in enumerate(b, 1):
            actual.append(min(anterior[j] + 1, actual[j - 1] + 1, anterior[j - 1] + (ca != cb)))
        if maximo is not None and min(actual) > maximo:
            return maximo + 1
        anterior = actual
    return anterior[-1]


def sugerencias(placa, placas_existentes, maximo=1):
    """Placas existentes cercanas a la leída, para el "¿Quisiste decir…?".

    Compara tanto la placa normalizada como su versión corregida y acepta distancia de edición <= maximo.
    Devuelve las placas ordenadas de más a menos parecida; no incluye la propia placa si ya existe tal cual.
    """
    original = normalizar_placa(placa)
    corregida = corregir_ocr(original)
    candidatas = []
    for existente in placas_existentes:
        e = normalizar_placa(existente)
        if not e or e == original:
            continue
        d = min(distancia_edicion(original, e, maximo), distancia_edicion(corregida, e, maximo))
        if d <= maximo:
            candidatas.append((d, e))
    candidatas.sort()
    vistas = []
    for _, e in candidatas:
        if e not in vistas:
            vistas.append(e)
    return vistas
