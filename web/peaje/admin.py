from django.contrib import admin

from .models import Alerta, Caseta, Paso, Tarifa, Usuario, Vehiculo


@admin.register(Usuario)
class UsuarioAdmin(admin.ModelAdmin):
    list_display = ("email_google", "nombre", "rol", "activo")
    list_filter = ("rol", "activo")
    search_fields = ("email_google", "nombre")


@admin.register(Vehiculo)
class VehiculoAdmin(admin.ModelAdmin):
    list_display = ("placa", "propietario", "categoria", "ejes", "saldo", "activo")
    list_filter = ("categoria", "activo")
    search_fields = ("placa", "propietario")


@admin.register(Caseta)
class CasetaAdmin(admin.ModelAdmin):
    list_display = ("nombre", "ubicacion")
    search_fields = ("nombre", "ubicacion")


@admin.register(Tarifa)
class TarifaAdmin(admin.ModelAdmin):
    list_display = ("categoria", "ejes", "monto")
    list_filter = ("categoria",)


@admin.register(Paso)
class PasoAdmin(admin.ModelAdmin):
    list_display = ("created_at", "placa_normalizada", "placa_leida", "estado", "categoria_detectada",
                    "ejes_detectados", "monto", "caseta", "confianza_ocr")
    list_filter = ("estado", "caseta", "categoria_detectada")
    search_fields = ("placa_normalizada", "placa_leida")
    date_hierarchy = "created_at"


@admin.register(Alerta)
class AlertaAdmin(admin.ModelAdmin):
    list_display = ("created_at", "tipo", "paso", "atendida")
    list_filter = ("tipo", "atendida")
    search_fields = ("paso__placa_normalizada", "paso__placa_leida")
    list_editable = ("atendida",)
