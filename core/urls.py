from django.urls import path

from . import api, views

urlpatterns = [
    path("", views.index, name="index"),
    path("health/", views.health, name="health"),
    path("api/ingest", api.ingest, name="ingest"),
    path("accessory/<int:pk>/", views.accessory, name="accessory"),
    path("accessory/<int:pk>/update/", views.accessory_update, name="accessory_update"),
    path("rooms/", views.rooms, name="rooms"),
    path("outdoor/", views.outdoor, name="outdoor"),
    path("settings/", views.settings_sheet, name="settings"),
    path("settings/places/", views.place_search, name="place_search"),
    path("settings/prices/", views.save_prices, name="save_prices"),
    path("settings/zone/<int:pk>/", views.save_equipment, name="save_equipment"),
    path("cost/", views.cost, name="cost"),
    path("history/", views.history, name="history"),
    path("zone/<int:pk>/schedule/", views.schedule, name="schedule"),
]
