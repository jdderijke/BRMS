# BRMS – Boot Remote Monitoring Systeem

Monitort de boot in de winter op afstand: temperatuur, luchtvochtigheid, accu,
walstroom, zonnepaneel en bilge. Draait onbeheerd op een Raspberry Pi; toegang via Tailscale.

Zie `CLAUDE.md` voor ontwerp, hardware en architectuur.

## Structuur
```
brms/            Python-package
  readers/       één module per sensortype (BTHome, Ruuvi, Victron, Shelly, camera)
  storage.py     SQLite (WAL)
  alarms.py      alarmlaag: alarmregels beoordelen
  melding.py     meldingen via Telegram en e-mail
  config.py      inlezen config/brms.toml
  go2rtc.py      live-beeld van de camera's via het programma go2rtc (WebRTC)
  web/           Flask + Chart.js: grafieken, camera-opnames en live-beeld
config/          brms.example.toml; eigen brms.toml ernaast (niet in git)
deploy/          install.sh, update.sh, go2rtc.sh (haalt go2rtc op naar bin/)
deploy/systemd/  service-bestanden
bin/             het programma go2rtc (niet in git)
data/            SQLite-database en camera-opnames (niet in git)
tests/
```
