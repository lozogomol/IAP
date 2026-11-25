from django.urls import path

from . import api, pwa, views

urlpatterns = [
    path("", views.panel, name="panel"),
    path("login", views.login, name="login"),
    path("auth/google/web", views.auth_google_web, name="auth_google_web"),
    path("acceso-denegado", views.acceso_denegado, name="acceso_denegado"),
    path("acceso-denegado/solicitar", views.solicitar_acceso, name="solicitar_acceso"),
    path("logout", views.logout, name="logout"),
    path("panel/datos", views.panel_datos, name="panel_datos"),
    path("pusher/auth", views.pusher_auth, name="pusher_auth"),
    path("pasos/<int:pk>/imagen", views.paso_imagen, name="paso_imagen"),
    path("vehiculos", views.vehiculos, name="vehiculos"),
    path("vehiculos/<int:pk>", views.vehiculo_accion, name="vehiculo_accion"),
    path("pasos", views.pasos, name="pasos"),
    path("alertas", views.alertas, name="alertas"),
    path("alertas/<int:pk>/atender", views.alerta_atender, name="alerta_atender"),
    path("chat", views.chat, name="chat"),
    path("caseta", views.caseta, name="caseta"),
    path("consulta", views.consulta, name="consulta"),
    path("usuarios", views.usuarios, name="usuarios"),
    path("usuarios/<int:pk>", views.usuario_accion, name="usuario_accion"),
    # API v1
    path("api/v1/pasos/frame", api.paso_frame, name="api_paso_frame"),
    path("api/v1/caseta/cerrar", api.caseta_cerrar, name="api_caseta_cerrar"),
    path("api/v1/vehiculos/consulta", api.vehiculo_consulta, name="api_vehiculo_consulta"),
    path("api/v1/placas/leer", api.placa_leer, name="api_placa_leer"),
    path("api/v1/agente/chat", api.agente_chat, name="api_agente_chat"),
    path("estado", views.estado, name="estado"),
    # PWA / APK (TWA): públicos
    path("manifest.webmanifest", pwa.manifest, name="manifest"),
    path("service-worker.js", pwa.service_worker, name="service_worker"),
    path("offline", pwa.offline, name="offline"),
    path(".well-known/assetlinks.json", pwa.assetlinks, name="assetlinks"),
]
