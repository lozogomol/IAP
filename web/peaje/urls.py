from django.urls import path

from . import views

urlpatterns = [
    path("", views.panel, name="panel"),
    path("login", views.login, name="login"),
    path("auth/google/web", views.auth_google_web, name="auth_google_web"),
    path("acceso-denegado", views.acceso_denegado, name="acceso_denegado"),
    path("logout", views.logout, name="logout"),
    path("vehiculos", views.vehiculos, name="vehiculos"),
    path("pasos", views.pasos, name="pasos"),
    path("alertas", views.alertas, name="alertas"),
    path("chat", views.chat, name="chat"),
    path("estado", views.estado, name="estado"),
]
