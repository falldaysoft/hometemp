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
