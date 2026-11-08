# BRMS – Boot Remote Monitoring Systeem

Klein, betrouwbaar systeem om de boot in de winter op afstand te monitoren: temperatuur,
luchtvochtigheid, accu, walstroom, zonnepaneel, bilge en (later) een IP-camera.
Metingen worden lokaal opgeslagen; toegang van thuis via VPN (Tailscale).
Het systeem draait maanden **onbeheerd**: betrouwbaarheid en eenvoud gaan boven features.

## Ontwerpprincipes
- Eigen Python-code, géén Home Assistant (te zwaar, updates kunnen iets breken).
- Zo min mogelijk afhankelijkheden; alles lokaal, geen cloud nodig voor de kernfunctie.
- Zoveel mogelijk draadloze sensoren (Bluetooth LE, wifi) om bedrading te vermijden.
- Robuust tegen stroomuitval: herstart vanzelf, geen datacorruptie (SQLite met WAL).
- Weinig schrijven naar opslag (flash).

## Hardware (status november 2026)
| Onderdeel | Keuze | Status |
|---|---|---|
| Computer | Raspberry Pi 4B (Pi 3B als testbank) | In bezit |
| OS | Raspberry Pi OS Lite 64-bit, headless, SSH met sleutel | — |
| Opslag | Raspberry Pi Flash Drive 256 GB (USB 3); tijdelijk SanDisk 32 GB USB-stick | Besteld |
| Klok | DS3231 RTC-module (I2C) – Pi 4 heeft geen RTC | Nog kopen |
| Router | Teltonika RUT241 eSIM (4G Cat 4, 9–30 VDC, RutOS, RMS, 1 DI/1 DO, wifi 2,4 GHz) | Besteld |
| Bluetooth | USB-BLE-dongle met antenne (ingebouwde BT Pi 4 heeft beperkt bereik) | Nog kopen |
| Test-sensoren | 3× Xiaomi Monitor 3 (MJWSD05MMC), pvvx-firmware, advertising **BTHome v2** | Besteld |
| Sensoren (gepland) | RuuviTag (4-in-1) kajuit + vooronder; RuuviTag Pro 3-in-1 motorruim; RuuviTag Pro 2-in-1 buiten | Gepland |
| Accu | Victron BMV-700 + VE.Direct Bluetooth Smart dongle (Instant Readout, `victron-ble`, encryptiesleutel uit VictronConnect). Alternatief: VE.Direct-USB-kabel (tekstprotocol) | In bezit |
| Zonnepaneel | 50 Wp; type laadregelaar **nog onbekend** | Open |
| Walstroom | Shelly aan 230V-kant (offline = walstroom weg), lokale HTTP-API; evt. ook op DI van router | Gepland |
| Bilge | Shelly Plus Uni (12V) met vlottercontact, lokale API | Gepland |
| Camera | IP-camera wifi 2,4 GHz, RTSP/ONVIF, 12V, altijd aan (zie ARCHITECTURE.md §5.7) | Later |
| Voeding | 12V boordnet → zekering → BatteryProtect → DC-DC 12→5V 3A | Nog kopen |
| Relais | Router-DO → relais voor Pi-herstart | Gepland |

Energie: systeem ca. 11 W (~21 Ah/dag). Walstroom aanwezig maar kan wegvallen; accu overbrugt ca. 5 dagen.

## Netwerk en toegang
- Pi via ethernet aan RUT241 (wifi van router als reserve).
- Mobiel internet zit achter CGNAT → **Tailscale** op de Pi (key expiry uit), subnet-route
  voor router en camera. Niets open op internet.
- Noodingang: Teltonika RMS (router beheren/herstarten) + Pi-herstart via router-DO.
- Hostnaam Pi: `BRMS` (via Tailscale MagicDNS bereikbaar als `brms`).
- Test-/ontwikkel-Pi 4 thuis: 192.168.178.114, user `jandirk`, SSH-alias `brms` (sleutel `~/.ssh/id_ed25519`).

## Software-architectuur
- **Logger**: Python-service (systemd, `Restart=always`), leest periodiek:
  - BLE-advertenties: Xiaomi/pvvx (BTHome v2), RuuviTag (`ruuvitag-sensor`/`bleak`), Victron (`victron-ble`).
  - Shelly's via lokale HTTP-API (walstroom, bilge).
  - GPIO waar nodig.
- **Opslag**: SQLite (één bestand, WAL-modus). Tabel per meting of generieke tabel
  (`timestamp UTC, sensor_id, grootheid, waarde`). Sensorconfig (MAC → naam/locatie, offsets) in config-bestand.
- **Web**: Flask + Chart.js (actuele waarden + grafieken), alleen bereikbaar via Tailscale/LAN.
- **Meldingen**: ntfy (push naar telefoon). Alarmen:
  - walstroom weg
  - accu SoC onder drempel (bijv. 50 %)
  - luchtvochtigheid langdurig > 80 %
  - temperatuur < 2 °C (vorst)
  - bilgevlotter actief
  - sensor geeft geen data meer (batterij leeg / buiten bereik)
- **Betrouwbaarheid**: hardware-watchdog Pi, systemd-herstart, periodieke back-up SQLite naar thuis via Tailscale.
- Python-omgeving: venv, dependencies vastgepind.

## Ontwikkelaanpak
- Ontwikkelen in PyCharm op Windows; testen op Pi 3B/Pi 4 thuis (zelfde OS).
- Code modulair: één module per sensortype (decoder/reader) + gemeenschappelijke opslag- en alarmlaag,
  zodat sensoren later makkelijk toe te voegen zijn.
- Eerst werkend en eenvoudig; daarna uitbreiden.

## Eerste stap
Python-script dat de 3 Xiaomi Monitor 3's (pvvx, BTHome v2) via BLE uitleest met `bleak`,
de waarden (temperatuur, luchtvochtigheid, batterij, RSSI) decodeert en in SQLite opslaat.
Daarna: RuuviTag- en Victron-reader, Flask-pagina, ntfy-alarmen, systemd-service.

## Open punten
- Type laadregelaar zonnepaneel (bepaalt uitleesmethode).
- Accutype (bij LiFePO4: niet laden onder 0 °C – BMS/lader regelt dit; geen eigen accutemperatuurmeting gepland).
- Data-abonnement / eSIM-provider kiezen; eSIM-profiel thuis installeren (RutOS-app of WAN-kabel).
