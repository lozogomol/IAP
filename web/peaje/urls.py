from django.urls import path

from . import views

urlpatterns = [
    path("", views.inicio, name="inicio"),
    path("estado", views.estado, name="estado"),
]
