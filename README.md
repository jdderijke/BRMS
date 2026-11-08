# BRMS – Boot Remote Monitoring Systeem

Monitort de boot in de winter op afstand: temperatuur, luchtvochtigheid, accu,
walstroom, zonnepaneel en bilge. Draait onbeheerd op een Raspberry Pi; toegang via Tailscale.

Zie `CLAUDE.md` voor ontwerp, hardware en architectuur.

## Structuur
```
brms/            Python-package
  readers/       één module per sensortype (BTHome, Ruuvi, Victron, Shelly)
  storage.py     SQLite (WAL)
  alarms.py      drempels + ntfy-meldingen
  config.py      inlezen config/brms.toml
  web/           Flask + Chart.js
config/          brms.example.toml (op de Pi: /etc/brms/brms.toml, niet in git)
deploy/systemd/  service-bestanden
data/            SQLite-database (niet in git)
tests/
```
