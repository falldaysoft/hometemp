import hashlib
import secrets

from django.db import models
from django.utils import timezone


class Zone(models.Model):
    """One HVAC system: the house is split into a Home side and an Office side."""

    name = models.CharField(max_length=60, unique=True)
    order = models.PositiveSmallIntegerField(default=0)
    thermostat = models.ForeignKey(
        "Accessory", null=True, blank=True, on_delete=models.SET_NULL, related_name="thermostat_for"
    )
    safety_min = models.FloatField(null=True, blank=True, help_text="Alert below this (°C) when idle")
    safety_max = models.FloatField(null=True, blank=True, help_text="Alert above this (°C) when idle")

    class Meta:
        ordering = ["order", "name"]

    def __str__(self):
        return self.name


class Room(models.Model):
    """A HomeKit room name, mapped onto a zone."""

    name = models.CharField(max_length=100, unique=True)
    zone = models.ForeignKey(Zone, null=True, blank=True, on_delete=models.SET_NULL, related_name="rooms")

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Accessory(models.Model):
    class Role(models.TextChoices):
        UNCLASSIFIED = "unclassified", "Unclassified"
        AMBIENT = "ambient", "Room"
        APPLIANCE = "appliance", "Appliance"
        OUTDOOR = "outdoor", "Outdoor"
        IGNORE = "ignore", "Hidden"

    uuid = models.UUIDField(unique=True)
    name = models.CharField(max_length=200)
    homekit_room = models.ForeignKey(Room, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    room_override = models.ForeignKey(
        Room, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
        help_text="Use this room instead of the HomeKit one",
    )
    manufacturer = models.CharField(max_length=200, blank=True)
    model = models.CharField(max_length=200, blank=True)
    bridged = models.BooleanField(default=False)
    reachable = models.BooleanField(default=True)
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.UNCLASSIFIED)
    calibration_offset = models.FloatField(default=0, help_text="Added to temperature readings (°C)")
    first_seen = models.DateTimeField(default=timezone.now)
    last_seen = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "accessories"

    def __str__(self):
        return self.name

    @property
    def room(self):
        return self.room_override or self.homekit_room

    @property
    def zone(self):
        return self.room.zone if self.room else None

    @staticmethod
    def suggest_role(name, kinds):
        """Conservative first guess; anything uncertain stays unclassified for review."""
        lowered = name.lower()
        if any(w in lowered for w in ("fridge", "freezer", "refrigerator")):
            return Accessory.Role.APPLIANCE
        if {"currentHeaterCoolerState", "currentHeatingCooling"} & set(kinds):
            return Accessory.Role.AMBIENT  # a thermostat measures its room
        if any(w in lowered for w in ("outdoor", "outside")):
            return Accessory.Role.OUTDOOR
        return Accessory.Role.UNCLASSIFIED


class Characteristic(models.Model):
    """One HomeKit characteristic we record (currentTemperature, heatingThreshold, …)."""

    uuid = models.UUIDField(unique=True)
    accessory = models.ForeignKey(Accessory, on_delete=models.CASCADE, related_name="characteristics")
    kind = models.CharField(max_length=60, db_index=True)
    type_uuid = models.CharField(max_length=40, blank=True, help_text="HomeKit characteristic type")
    description = models.CharField(max_length=100, blank=True, help_text="HomeKit's own label")
    unit = models.CharField(max_length=30, blank=True)
    format = models.CharField(max_length=20, blank=True)

    def __str__(self):
        return f"{self.accessory.name} · {self.kind}"


class Reading(models.Model):
    class Source(models.IntegerChoices):
        EVENT = 0, "event"
        HEARTBEAT = 1, "heartbeat"
        INITIAL = 2, "initial"

    characteristic = models.ForeignKey(Characteristic, on_delete=models.CASCADE, related_name="readings")
    ts = models.DateTimeField()
    value = models.FloatField()
    source = models.SmallIntegerField(choices=Source.choices)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["characteristic", "ts"], name="reading_unique_ts")]
        indexes = [models.Index(fields=["characteristic", "-ts"], name="reading_latest")]


class RawRecord(models.Model):
    """Every collector record that isn't a numeric reading, verbatim: text values, inventories,
    reachability, lifecycle, errors. Kept so future features can use data nobody models yet."""

    ts = models.DateTimeField(db_index=True)
    type = models.CharField(max_length=40, db_index=True)
    data = models.JSONField()
    digest = models.CharField(max_length=64, unique=True, help_text="sha256 of the canonical record; dedupes resends")
    device = models.ForeignKey("CollectorDevice", null=True, blank=True, on_delete=models.SET_NULL)
    received_at = models.DateTimeField(auto_now_add=True)

    @staticmethod
    def digest_of(rec):
        import json
        return hashlib.sha256(json.dumps(rec, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class CollectorDevice(models.Model):
    name = models.CharField(max_length=100, unique=True)
    token_hash = models.CharField(max_length=64, unique=True)
    last_seen = models.DateTimeField(null=True, blank=True)
    last_record_ts = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return self.name

    @staticmethod
    def hash_token(token):
        return hashlib.sha256(token.encode()).hexdigest()

    @classmethod
    def issue(cls, name):
        """Create or rotate a device token; returns the plaintext token (shown once)."""
        token = secrets.token_urlsafe(32)
        cls.objects.update_or_create(name=name, defaults={"token_hash": cls.hash_token(token)})
        return token


class HomeSettings(models.Model):
    """Singleton (pk=1). Location lives here, not in the public repo."""

    place = models.CharField(max_length=200, blank=True)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    timezone = models.CharField(max_length=60, blank=True)

    @classmethod
    def load(cls):
        return cls.objects.get_or_create(pk=1)[0]

    @property
    def has_location(self):
        return self.latitude is not None and self.longitude is not None


WEATHER_FIELDS = [
    "temperature_2m", "relative_humidity_2m", "dew_point_2m", "apparent_temperature", "precipitation", "snowfall",
    "weather_code", "cloud_cover", "pressure_msl", "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m",
    "shortwave_radiation", "snow_depth",
]


class WeatherHour(models.Model):
    """Hourly outdoor weather from Open-Meteo. issued=None is the best-known actual for that hour;
    otherwise a forecast snapshot issued at that time (kept so forecasts can be scored later)."""

    ts = models.DateTimeField()
    issued = models.DateTimeField(null=True, blank=True)
    temperature = models.FloatField(null=True)
    data = models.JSONField(default=dict, help_text="All variables Open-Meteo returned for this hour")
    fetched_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["ts"], condition=models.Q(issued__isnull=True), name="weather_actual"),
            models.UniqueConstraint(fields=["ts", "issued"], name="weather_forecast"),
        ]
        indexes = [models.Index(fields=["issued", "ts"])]


class WeatherNow(models.Model):
    """Open-Meteo 'current' conditions, recorded every fetch (15 min)."""

    ts = models.DateTimeField(unique=True)
    temperature = models.FloatField(null=True)
    data = models.JSONField(default=dict)


class HvacInterval(models.Model):
    """A furnace or AC run, derived from a thermostat's state readings (rebuildable; see core/hvac.py)."""

    class Mode(models.TextChoices):
        HEATING = "heating"
        COOLING = "cooling"

    thermostat = models.ForeignKey(Accessory, on_delete=models.CASCADE, related_name="runs")
    start = models.DateTimeField()
    end = models.DateTimeField()
    mode = models.CharField(max_length=10, choices=Mode.choices)
    open = models.BooleanField(default=False, help_text="Still running at the last reading")
    uncertain = models.BooleanField(default=False, help_text="Collector went quiet during the run")

    class Meta:
        ordering = ["start"]
        indexes = [models.Index(fields=["thermostat", "start"])]

    @property
    def minutes(self):
        return (self.end - self.start).total_seconds() / 60
