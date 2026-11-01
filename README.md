# hometemp

Compare HomeKit room temperatures with the thermostat and its settings, measure how well heating and cooling work, and estimate what the weather will cost.

- **`collector/`**: a Mac Catalyst app that records HomeKit thermostat and sensor events (spike stage). Generate the Xcode project with `cd collector && xcodegen generate`. It needs a team with the HomeKit capability.
- **`PLAN.md`**: product plan, architecture, phases and spike findings.

The server (Django, at hometemp.fallday.ca) and dashboard are planned, not yet built. Tasks are tracked in the fdbot room *falltemp*.
