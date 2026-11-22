# Ford EV for Home Assistant

A Home Assistant custom integration for Ford electric vehicles that adds what the car's own app does not give you:

- an independent range estimate that accounts for outside temperature and cabin climate, and recalibrates itself every time you charge to 100 % (useful for LFP batteries, whose displayed state of charge drifts between full charges)
- trip, energy and charging reports (km per day/week/month, kWh used and charged, consumption, charging sessions and cost)
- notifications (car left unlocked, door or window open, plug-in reminder, warning lights, charging finished, and more)
- a ready-made dashboard

It builds on the [FordConnect Query](https://github.com/marq24/ha-fordconnect-query) integration, which must be installed and working first.

> **Status:** in development, not yet installable. Installation and configuration instructions will be added here with the first release.

## License

[MIT](LICENSE)
