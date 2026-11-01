from django.urls import path

from . import api, views

urlpatterns = [
    path("", views.index, name="index"),
    path("health/", views.health, name="health"),
    path("api/ingest", api.ingest, name="ingest"),
]
