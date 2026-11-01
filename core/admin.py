from django.contrib import admin

from .models import Accessory, Characteristic, CollectorDevice, Room, Zone


@admin.register(Zone)
class ZoneAdmin(admin.ModelAdmin):
    list_display = ["name", "order", "thermostat", "safety_min", "safety_max"]
    list_editable = ["order", "thermostat"]


@admin.register(Room)
class RoomAdmin(admin.ModelAdmin):
    list_display = ["name", "zone"]
    list_editable = ["zone"]


class CharacteristicInline(admin.TabularInline):
    model = Characteristic
    fields = ["kind", "unit", "uuid"]
    readonly_fields = fields
    extra = 0
    can_delete = False


@admin.register(Accessory)
class AccessoryAdmin(admin.ModelAdmin):
    list_display = ["name", "role", "homekit_room", "room_override", "manufacturer", "model", "bridged",
                    "reachable", "last_seen"]
    list_editable = ["role", "room_override"]
    list_filter = ["role", "homekit_room", "bridged", "manufacturer"]
    search_fields = ["name", "model"]
    inlines = [CharacteristicInline]


@admin.register(CollectorDevice)
class CollectorDeviceAdmin(admin.ModelAdmin):
    list_display = ["name", "last_seen", "last_record_ts"]
    readonly_fields = ["token_hash", "last_seen", "last_record_ts"]
