from django.urls import path

from . import api, views

urlpatterns = [
    path("", views.panel, name="panel"),
    path("login", views.login, name="login"),
    path("auth/google/web", views.auth_google_web, name="auth_google_web"),
    path("acceso-denegado", views.acceso_denegado, name="acceso_denegado"),
    path("acceso-denegado/solicitar", views.solicitar_acceso, name="solicitar_acceso"),
    path("logout", views.logout, name="logout"),
    path("vehiculos", views.vehiculos, name="vehiculos"),
    path("pasos", views.pasos, name="pasos"),
    path("alertas", views.alertas, name="alertas"),
    path("chat", views.chat, name="chat"),
    path("caseta", views.caseta, name="caseta"),
    path("consulta", views.consulta, name="consulta"),
    path("usuarios", views.usuarios, name="usuarios"),
    path("usuarios/<int:pk>", views.usuario_accion, name="usuario_accion"),
    # API v1
    path("api/v1/pasos/frame", api.paso_frame, name="api_paso_frame"),
    path("api/v1/vehiculos/consulta", api.vehiculo_consulta, name="api_vehiculo_consulta"),
    path("api/v1/placas/leer", api.placa_leer, name="api_placa_leer"),
    path("estado", views.estado, name="estado"),
]
