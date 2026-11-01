# hometemp — product plan

Compare temperatures around the house against the HomeKit thermostat and its settings, measure how well heating and cooling work, and predict what the weather will cost.

Lives at **https://hometemp.fallday.ca** (fdbot room: falltemp).

## Questions this should answer

1. **Right now:** what is it in every room, what does the thermostat read, what is it set to, and is the furnace or AC running?
2. **Room vs thermostat:** which rooms run warmer or colder than the thermostat, by how much, and at what time of day (e.g. the bedroom is 2.5 °C colder overnight)?
3. **Run time and cost:** how many minutes did the furnace or AC run today, this week, this month, and what did that cost?
4. **Recovery:** after the setpoint goes up (morning warm-up), how long does the house take to reach it, and how does that change with outdoor temperature? The same question for each room, not just the thermostat.
5. **Coast-down:** with the HVAC off, how fast does the house lose (or gain) heat? This gives the house's thermal leakiness, which drives setback advice.
6. **Forecast cost:** given tomorrow's or this week's forecast and the current schedule, what will heating or cooling cost?
7. **What-if:** what does a nightly 3 °C setback save? Is it worth it on very cold nights? (Uses the models from 4–6.)

## Zones: a split house

The house has **two independent HVAC systems**, each with its own furnace, AC and thermostat:

| Zone | Use | Comfort policy |
|---|---|---|
| **Home** (living side) | lived in every day | normal schedule, comfort matters |
| **Office** | your work days; occasionally used for other things (guests, events) | allowed to drift to uncomfortable temperatures when idle; heated or cooled only while occupied |

Today only **one thermostat is in HomeKit**. The design treats a zone as a first-class object and supports any number of them from day one:
- Every sensor belongs to a zone, set from its HomeKit room. A zone has 0 or 1 thermostat and its own equipment and cost settings.
- A zone without a HomeKit thermostat is **sensors-only**: temperatures and room offsets work, but run time, cost and recovery need the thermostat. Adding the second thermostat later just lights those up.
- Every report is per zone, with a whole-house total where it makes sense (cost).

**Office occupancy** is a core concept, not an afterthought:
- Each zone has a timeline of **occupancy periods**: `work`, `other use` (guests or events), `idle`. They're inferred from the setpoint (comfort setpoint = occupied, deep setback = idle) and can be overridden or labelled by hand ("guests this weekend").
- Questions this enables:
  - **Pre-heat lead time:** "To have the office at 20 °C by 9:00 with −15 °C outside, start heating at 7:35." The office recovers from a much deeper setback than the living side, so this is the most useful prediction for it.
  - **Cost by use:** what the office costs per work day, per idle day, and per "other use" event (what did hosting guests on that side cost?).
  - **Setback depth:** how cold or hot can the office drift when idle before recovery costs more than it saves, or before it risks frozen pipes or humidity problems? Add a configurable "never below/above" safety bound and alerts.
  - **Cross-zone coupling:** when the office is idle, does it pull heat from the living side through the shared wall? Correlate the idle office's ΔT with the living side's run time.

## Architecture

```
 HomeKit accessories ──► Collector (Mac Catalyst app, always-on Mac)
 (thermostat, HomePods,      │  local SQLite buffer, batched HTTPS POST + device token
  Eve, Aqara…)               ▼
                     Django + Postgres on ovm  (hometemp.fallday.ca, fallday-scaffold)
                       ├── ingest API (idempotent upserts)
 Open-Meteo ───────────┤── weather ingest: hourly history + forecast
                       ├── analysis jobs: run-time intervals, recovery/coast curves, regression
                       └── dashboard: HTMX + Alpine + charts
```

### Collector (riskiest piece, so it gets a spike first)
- HomeKit has no server or cloud API. The only way in is the HomeKit framework (iOS, iPadOS, Mac Catalyst, tvOS, watchOS; not AppKit). It needs the HomeKit entitlement (Apple developer team) and `NSHomeKitUsageDescription`.
- Plan: a **Mac Catalyst** app left running on an always-on Mac. Fallback: an old iPad kept plugged in with the app in the foreground (iOS doesn't reliably deliver HomeKit updates to a backgrounded app).
- For each accessory, subscribe with `enableNotification` (don't poll `readValue`, which drains battery on BLE sensors), plus a **heartbeat** every 5 minutes that snapshots the last known values. The heartbeat lets the server tell "nothing changed" apart from "collector was down."
- Characteristics to capture:
  - Thermostat: `CurrentTemperature`, `TargetTemperature`, `CurrentHeatingCoolingState` (off/heat/cool, **the run-time signal**), `TargetHeatingCoolingState` (mode), heating and cooling thresholds, humidity.
  - Sensors: temperature, humidity, battery, reachability.
  - Metadata: accessory UUID, name, room, manufacturer and model, so readings stay attributed through renames.
- Buffer readings locally and retry on network loss, so no data is lost while ovm is down.

### Server
- Models: Home, **Zone** (name, thermostat sensor nullable, comfort policy, safety bounds), Room (→ Zone), Sensor (accessory + characteristic), Reading (sensor, ts, value; unique on sensor+ts), HvacInterval (derived per zone: start, end, mode), **OccupancyPeriod** (zone, start, end, kind work/other/idle, source inferred/manual, note), WeatherHour (observed and forecast, with fetch time), **Equipment/CostSettings per zone**, CollectorDevice (token, last seen).
- Raw readings are append-only. Everything else is derived and can be rebuilt.

### Cost model (entered by you, not inferred)
- Furnace: nameplate input BTU/h, AFUE, gas price ($/m³ or $/GJ, Enbridge-style tiers optional).
- AC: rated kW (or tonnage + SEER), electricity $/kWh (TOU optional).
- Run time comes from `CurrentHeatingCoolingState`. **Limitation:** HomeKit reports heat or off only, with no stage, fan or blower power, so costs are estimates. A calibration step against a real gas or hydro bill fixes the scale.

## Phases (driven by how much data each one needs)

| Phase | Needs | Delivers |
|---|---|---|
| **1 — Collect and see** | day one | collector, ingest, live "now" view, timeline chart (rooms, thermostat, setpoint, HVAC-on shading, outdoor), per-room delta, daily run time × cost |
| **2 — Curves** | a few days | recovery detection (setpoint step → time to within 0.5 °C), per-room lag, coast-down rate vs indoor–outdoor ΔT |
| **3 — Predict** | a few weeks | regression of run time per hour against (setpoint − outdoor) or degree-hours, applied to the Open-Meteo forecast → projected daily and weekly cost; setback what-if |
| **v2+ (later)** | — | schedule-adherence check, coldest-room-overnight report, alerts (unusually long run, sensor offline, furnace on but temp not rising → filter or failure), CSV export, bill calibration |

## Assumptions (correct me in the room)
- One home, two HVAC zones (Home and Office), only one thermostat in HomeKit so far. °C, Canadian $.
- Both zones' sensors are in the same HomeKit home, so one collector covers both.
- An always-on Apple Silicon Mac (or a spare iPad) is available to run the collector, signed into the iCloud account that owns the Home.
- A paid Apple developer team is available for the HomeKit entitlement.
- Single user. Login protects the dashboard; the collector authenticates with a device token.
- Gas furnace for heat, central AC for cooling.
