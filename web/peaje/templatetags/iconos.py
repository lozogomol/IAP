"""Íconos SVG de línea (estilo Lucide/Feather, 24x24, trazo 2) incrustados. Uso: {% icono "camara" %}."""
from django import template
from django.utils.html import format_html
from django.utils.safestring import mark_safe

register = template.Library()

TRAZOS = {
    "menu": '<path d="M4 6h16M4 12h16M4 18h16"/>',
    "cerrar": '<path d="M18 6 6 18M6 6l12 12"/>',
    "alerta": '<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/>'
              '<path d="M12 9v4M12 17h.01"/>',
    "camara": '<path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/>'
              '<circle cx="12" cy="13" r="4"/>',
    "buscar": '<circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/>',
    "mas": '<path d="M12 5v14M5 12h14"/>',
    "auto": '<path d="M5 17H3v-5l2-5h14l2 5v5h-2"/><path d="M3 12h18"/><circle cx="7.5" cy="17" r="2"/>'
            '<circle cx="16.5" cy="17" r="2"/><path d="M9.5 17h5"/>',
    "check": '<path d="M20 6 9 17l-5-5"/>',
    "refrescar": '<path d="M21 12a9 9 0 1 1-2.6-6.4L21 8"/><path d="M21 3v5h-5"/>',
    "power": '<path d="M18.4 6.6a9 9 0 1 1-12.8 0"/><path d="M12 2v10"/>',
    "imagen": '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/>'
              '<path d="m21 15-5-5L5 21"/>',
    "bandeja": '<path d="M22 12h-6l-2 3h-4l-2-3H2"/>'
               '<path d="M5.5 5.1 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.5-6.9A2 2 0 0 0 16.8 4H7.2a2 2 0 0 0-1.7 1.1z"/>',
    "reloj": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    "actividad": '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
    "base_datos": '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14c0 1.7 4 3 9 3s9-1.3 9-3V5"/>'
                  '<path d="M3 12c0 1.7 4 3 9 3s9-1.3 9-3"/>',
    "servidor": '<rect x="2" y="3" width="20" height="8" rx="2"/><rect x="2" y="13" width="20" height="8" rx="2"/>'
                '<path d="M6 7h.01M6 17h.01"/>',
    "chip": '<rect x="5" y="5" width="14" height="14" rx="2"/><rect x="9" y="9" width="6" height="6" rx="1"/>'
            '<path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3"/>',
    "flecha": '<path d="M5 12h14M13 6l6 6-6 6"/>',
    "salir": '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="m16 17 5-5-5-5M21 12H9"/>',
    "usuarios": '<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/>'
                '<path d="M23 21v-2a4 4 0 0 0-3-3.9M16 3.1a4 4 0 0 1 0 7.8"/>',
    "campana": '<path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.9 1.9 0 0 0 3.4 0"/>',
    "billetera": '<path d="M20 12V8H6a2 2 0 0 1 0-4h12v4"/><path d="M4 6v12a2 2 0 0 0 2 2h14v-4"/>'
                 '<path d="M18 12a2 2 0 0 0 0 4h4v-4z"/>',
    "lista": '<path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/>',
    "subir": '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m17 8-5-5-5 5M12 3v12"/>',
    "detener": '<rect x="6" y="6" width="12" height="12" rx="2"/>',
    "play": '<path d="M6 4l14 8-14 8z"/>',
    "escudo": '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>',
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 16v-4M12 8h.01"/>',
    "barras": '<path d="M12 20V10M18 20V4M6 20v-4"/>',
    "bot": '<rect x="4" y="8" width="16" height="12" rx="2"/><path d="M12 8V4M8 4h8M9 13h.01M15 13h.01M9 17h6"/>',
    "chat": '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
    "calendario": '<rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/>',
    "monitor": '<rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8M12 17v4"/>',
}


def svg(nombre, clase="ico", etiqueta=""):
    trazo = TRAZOS.get(nombre, TRAZOS["info"])
    accesible = format_html(' role="img" aria-label="{}"', etiqueta) if etiqueta else mark_safe(' aria-hidden="true"')
    return format_html('<svg class="{}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
                       'stroke-linecap="round" stroke-linejoin="round" focusable="false"{}>{}</svg>',
                       clase, accesible, mark_safe(trazo))


@register.simple_tag
def icono(nombre, clase="ico", etiqueta=""):
    return svg(nombre, clase, etiqueta)


@register.filter
def iniciales(nombre):
    """Hasta dos iniciales para el avatar: "Ana Pérez" → "AP"."""
    partes = [p for p in str(nombre or "").split() if p[:1].isalpha()]
    return "".join(p[0] for p in partes[:2]).upper() or "?"
