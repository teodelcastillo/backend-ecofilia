from django.urls import path

from apps.proyeccion.api import views

urlpatterns = [
    path("meses/", views.ProyeccionesView.as_view(), name="proyeccion-meses"),
    path("meses/<int:pk>/", views.ProyeccionView.as_view(), name="proyeccion-mes"),
    path("meses/<int:pk>/analizar/", views.AnalizarView.as_view(), name="proyeccion-analizar"),
    path("fichas-ndc/", views.FichasView.as_view(), name="proyeccion-fichas"),
    path("fichas-ndc/<int:pk>/", views.FichaView.as_view(), name="proyeccion-ficha"),
]
