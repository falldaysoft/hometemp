import json

from django.contrib.auth.models import User
from django.test import TestCase

from .models import Accessory, CollectorDevice, Reading

ACC = "81E5E153-84FC-55ED-9E9C-7E9B46EF2401"
FRIDGE = "11111111-84FC-55ED-9E9C-7E9B46EF2401"


def value(acc, ch_uuid, kind, v, ts, name="Office Thermostat", room="Office", source="event"):
    return {"type": "value", "accessory": name, "accessoryUUID": acc, "room": room, "characteristic": kind,
            "characteristicUUID": ch_uuid, "unit": "celsius", "value": v, "source": source, "ts": ts}


INVENTORY = {"type": "inventory", "ts": "2026-11-01T10:55:00.000Z", "home": "Home", "accessories": [
    {"name": "Office Thermostat", "uuid": ACC, "room": "Office", "manufacturer": "Resideo", "model": "X2S",
     "bridged": False, "reachable": True, "services": [{"characteristics": [{"type": "currentHeaterCoolerState"}]}]},
    {"name": "Second Fridge", "uuid": FRIDGE, "room": "Office", "manufacturer": "YoLink", "model": "YS8003",
     "bridged": True, "reachable": True, "services": [{"characteristics": [{"type": "currentTemperature"}]}]},
]}


class IngestTests(TestCase):
    def setUp(self):
        self.token = CollectorDevice.issue("test")

    def post(self, records, token=None):
        return self.client.post("/api/ingest", json.dumps({"records": records}), content_type="application/json",
                                headers={"Authorization": f"Bearer {token or self.token}"})

    def test_requires_token(self):
        self.assertEqual(self.post([], token="nope").status_code, 401)

    def test_ingest_is_idempotent_and_normalizes(self):
        recs = [INVENTORY,
                value(ACC, "E827076A-8A8A-503C-8994-32892C7875E5", "currentHeaterCoolerState", 2, "2026-11-01T11:02:48.805Z"),
                value(ACC, "E827076A-8A8A-503C-8994-32892C7875E5", "currentHeaterCoolerState", False, "2026-11-01T11:03:52.609Z"),
                value(ACC, "00000000-0000-0000-0000-000000000001", "00000093-0000-1000-8000-0026BB765291", 800, "2026-11-01T11:03:53Z")]
        r = self.post(recs)
        self.assertEqual(r.status_code, 200, r.content)
        self.post(recs)
        self.assertEqual(Reading.objects.count(), 2)
        self.assertEqual(sorted(Reading.objects.values_list("value", flat=True)), [0.0, 2.0])
        self.assertEqual(r.json()["through"], "2026-11-01T11:03:53+00:00")

    def test_everything_is_kept(self):
        recs = [INVENTORY, {"type": "lifecycle", "event": "didEnterBackground", "ts": "2026-11-01T12:00:00Z"},
                {**value(ACC, "D0000000-0000-0000-0000-000000000001", "0000004C-0000-1000-8000-0026BB765291", "abc",
                         "2026-11-01T12:00:01Z"), "description": "Some text", "characteristicType": "0000004C"},
                {**value(ACC, "D0000000-0000-0000-0000-000000000002", "00000022-0000-1000-8000-0026BB765291", True,
                         "2026-11-01T12:00:02Z"), "description": "Motion Detected"}]
        self.post(recs)
        self.post(recs)
        from .models import Characteristic, RawRecord
        self.assertEqual(sorted(RawRecord.objects.values_list("type", flat=True)), ["inventory", "lifecycle", "value"])
        self.assertEqual(Reading.objects.get(characteristic__description="Motion Detected").value, 1.0)
        self.assertTrue(Characteristic.objects.filter(description="Some text").exists())

    def test_roles_and_zones(self):
        self.post([INVENTORY])
        thermo, fridge = Accessory.objects.get(uuid=ACC), Accessory.objects.get(uuid=FRIDGE)
        self.assertEqual(thermo.role, "ambient")
        self.assertEqual(fridge.role, "appliance")
        self.assertEqual(thermo.zone.name, "Office")


class NowViewTests(TestCase):
    def test_requires_login_then_renders(self):
        self.assertEqual(self.client.get("/").status_code, 302)
        token = CollectorDevice.issue("test")
        self.client.post("/api/ingest", json.dumps({"records": [
            INVENTORY,
            value(ACC, "C0000000-0000-0000-0000-000000000001", "currentTemperature", 17, "2026-11-01T11:00:00Z"),
            value(ACC, "C0000000-0000-0000-0000-000000000002", "currentHeaterCoolerState", 2, "2026-11-01T11:00:00Z"),
            value(ACC, "C0000000-0000-0000-0000-000000000003", "heatingThreshold", 19, "2026-11-01T11:00:00Z"),
        ]}), content_type="application/json", headers={"Authorization": f"Bearer {token}"})
        User.objects.create_user("u", password="p")
        self.client.login(username="u", password="p")
        body = self.client.get("/").content.decode()
        self.assertIn("Heating", body)
        self.assertIn("17.0°", body)
        self.assertIn("No thermostat in HomeKit", body)
        acc = Accessory.objects.get(uuid=ACC)
        sheet = self.client.get(f"/accessory/{acc.pk}/").content.decode()
        self.assertIn("What HomeKit reports", sheet)
        self.assertIn("Heating", sheet)
        r = self.client.post(f"/accessory/{acc.pk}/update/", {"role": "ignore", "room": "homekit", "offset": "0.5"})
        self.assertEqual(r.headers["HX-Trigger"], "dashboard-changed")
        acc.refresh_from_db()
        self.assertEqual((acc.role, acc.calibration_offset), ("ignore", 0.5))
        self.assertIn("1 hidden sensor", self.client.get("/").content.decode())


class WeatherTests(TestCase):
    def test_store_actuals_snapshot_and_current(self):
        from datetime import datetime, timezone as tz
        from unittest import mock
        from .models import HomeSettings, WeatherHour, WeatherNow
        from . import weather
        now = datetime(2026, 11, 1, 13, 10, tzinfo=tz.utc)
        base = int(datetime(2026, 11, 1, 10, tzinfo=tz.utc).timestamp())
        hours = [base + 3600 * i for i in range(8)]  # 10:00 … 17:00
        payload = {"current": {"time": int(now.timestamp()), "interval": 900, "temperature_2m": -3.2, "weather_code": 71},
                   "hourly": {"time": hours, "temperature_2m": [-5 + i for i in range(8)],
                              "wind_speed_10m": [10] * 8}}
        s = HomeSettings.load()
        s.latitude, s.longitude, s.place = 45.4, -75.7, "Ottawa"
        s.save()
        with mock.patch.object(weather, "get_json", return_value=payload):
            st1 = weather.fetch(s, now=now)
            st2 = weather.fetch(s, now=now)
        self.assertEqual((st1["actual"], st1["forecast"], st1["current"]), (4, 4, 1))
        self.assertEqual(st2["forecast"], 0)  # one snapshot per 6 h slot
        self.assertEqual(WeatherHour.objects.filter(issued=None).count(), 4)
        self.assertEqual(WeatherHour.objects.get(issued=None, ts=datetime(2026, 11, 1, 11, tzinfo=tz.utc)).data["wind_speed_10m"], 10)
        self.assertEqual(WeatherNow.objects.get().temperature, -3.2)

        User.objects.create_user("u", password="p")
        self.client.login(username="u", password="p")
        with mock.patch("django.utils.timezone.now", return_value=now):
            body = self.client.get("/").content.decode()
            sheet = self.client.get("/outdoor/").content.decode()
        self.assertIn("-3.2°", body)
        self.assertIn("light snow", body)
        self.assertIn("Forecast snapshots", sheet)


class HvacTests(TestCase):
    def test_debounce_gap_and_rebuild(self):
        from datetime import datetime, timedelta, timezone as tz
        from .hvac import intervals, rebuild
        from .models import Characteristic, HvacInterval
        acc = Accessory.objects.create(uuid=ACC, name="Office Thermostat")
        ch = Characteristic.objects.create(uuid="E827076A-8A8A-503C-8994-32892C7875E5", accessory=acc,
                                           kind="currentHeaterCoolerState")
        t0 = datetime(2026, 11, 1, 6, tzinfo=tz.utc)
        seq = [(0, 0), (60, 2), (60.5, 0),            # 0.5 s blip: dropped
               (120, 2), (720, 2), (1320, 0),          # 20 min run
               (1800, 2), (2100, 2), (5000, 2)]        # run, then a 48 min silence: closed at 2100 s, uncertain
        for sec, v in seq:
            Reading.objects.create(characteristic=ch, ts=t0 + timedelta(seconds=sec), value=v, source=0)
        runs = intervals(acc, until=t0 + timedelta(seconds=5000))
        self.assertEqual([(r["start"] - t0).total_seconds() for r in runs], [120, 1800, 5000])
        self.assertEqual((runs[0]["end"] - t0).total_seconds(), 1320)
        self.assertTrue(runs[1]["uncertain"])
        self.assertEqual((runs[1]["end"] - t0).total_seconds(), 2100)
        rebuild(acc)
        rebuild(acc)  # idempotent
        self.assertEqual(HvacInterval.objects.filter(thermostat=acc, mode="heating").count(), 3)


class CostTests(TestCase):
    def test_run_cost_and_views(self):
        from datetime import timedelta
        from django.utils import timezone
        from .cost import daily
        from .models import Characteristic, HomeSettings, HvacInterval, Zone
        office = Zone.objects.get(name="Office")
        acc = Accessory.objects.create(uuid=ACC, name="Office Thermostat")
        Characteristic.objects.create(uuid="E827076A-8A8A-503C-8994-32892C7875E5", accessory=acc,
                                      kind="currentHeaterCoolerState")
        office.thermostat = acc
        office.furnace_input_btuh = 70600  # 2 m³/h
        office.blower_kw = 0.5
        office.save()
        s = HomeSettings.load()
        s.gas_price_m3, s.electricity_kwh = 0.5, 0.2
        s.save()
        end = timezone.now() - timedelta(minutes=5)
        HvacInterval.objects.create(thermostat=acc, start=end - timedelta(minutes=30), end=end, mode="heating")
        today = daily(1)["days"][0]["zones"][office.id]
        self.assertAlmostEqual(today["heat_min"], 30, places=3)
        self.assertAlmostEqual(today["m3"], 1.0, places=3)
        self.assertAlmostEqual(today["cost"], 0.5 + 0.25 * 0.2, places=3)

        User.objects.create_user("u", password="p")
        self.client.login(username="u", password="p")
        self.assertIn("$0.55", self.client.get("/cost/").content.decode())
        r = self.client.post(f"/settings/zone/{office.pk}/", {"btuh": "60000", "afue": "96", "blower": "0.4", "ac": "2.8"})
        office.refresh_from_db()
        self.assertEqual((office.furnace_input_btuh, office.furnace_afue, office.equipment_confirmed), (60000, 0.96, True))
        self.client.post("/settings/prices/", {"gas": "0.42", "electricity": "0.14"})
        self.assertTrue(HomeSettings.load().prices_confirmed)
