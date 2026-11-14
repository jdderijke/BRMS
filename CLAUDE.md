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
| Klok | Tijd via router (RUT241, operator time + NTP), zie ARCHITECTURE.md §4.2. DS3231 RTC-module optioneel (past niet zonder meer in behuizing) | DS3231 in bezit |
| Router | Teltonika RUT241 eSIM (4G Cat 4, 9–30 VDC, RutOS, 1 DI/1 DO, wifi 2,4 GHz); "Ping Reboot" aan, RMS uit | In bezit |
| Bluetooth | Ingebouwde BT van de Pi 4 (test thuis: ruim 20 m, ook door muren). USB-BLE-dongle met antenne alleen als het bereik aan boord tegenvalt | Voorlopig geen dongle |
| Sensoren Xiaomi | 3× Xiaomi Monitor 3 (MJWSD05MMC), pvvx-firmware, advertising **BTHome v2**. Eerst test-sensoren, daarna vooronder + 2 achterkajuiten | Besteld |
| Sensoren Ruuvi | 1× RuuviTag (4-in-1) hoofdkajuit; 2× RuuviTag Pro 3-in-1: motorruim en buiten (IP66: beschut ophangen) | Gepland |
| Accu | Victron BMV-700 + VE.Direct Bluetooth Smart dongle (Instant Readout, `victron-ble`, encryptiesleutel uit VictronConnect) | In bezit |
| Zonnepanelen | 3 banken: 2× Victron BlueSolar MPPT 75/15 + VE.Direct Bluetooth Smart dongle, 1× SmartSolar MPPT 100/30 (ingebouwde BT). Winter: alleen 50 Wp aangesloten | In bezit |
| Lader/omvormer, walstroom | Victron MultiPlus 12/3000/120-16 (VE.Bus) + VE.Bus Smart dongle (Instant Readout, AC-ingang = walstroom). MK3-USB alleen voor configuratie | MultiPlus/MK3 in bezit, dongle nog kopen |
| Bilge | Shelly Plus Uni (12V), lokale API: spanning achter de bestaande vlotter = bilgepomp aan; geen tweede vlotter, later eventueel een waterdetector (ARCHITECTURE.md §5.2.4) | Shelly besteld |
| Kachel | Shelly Plug M Gen3 (tussenstekker met energiemeting, wifi, lokale API) schakelt een elektrische kachel van 1500 W bij vorst; alleen met walstroom (ARCHITECTURE.md §5.2.6) | Nog kopen |
| Camera | IP-camera wifi 2,4 GHz, RTSP/ONVIF, 12V, altijd aan (zie ARCHITECTURE.md §5.7) | Later |
| Voeding | 12V boordnet → zekering → BatteryProtect → DC-DC 12→5V 3A | Nog kopen |

Energie: systeem ca. 11 W (~21 Ah/dag). Walstroom aanwezig maar kan wegvallen; accu overbrugt ca. 5 dagen.

## Netwerk en toegang
- Pi via ethernet aan RUT241 (wifi van router als reserve).
- Mobiel internet zit achter CGNAT → **Tailscale** op de Pi (key expiry uit), subnet-route
  voor router en camera. Niets open op internet.
- Geen noodingang buiten Tailscale om: Teltonika RMS wordt niet gebruikt (geen extra externe
  dienst) en het relais voor Pi-herstart via de router-uitgang is vervallen. Vangnet: de router
  herstart zichzelf ("Ping Reboot"), de Pi via de hardware-watchdog (ARCHITECTURE.md §7).
- Hostnaam Pi: `BRMS` (via Tailscale MagicDNS bereikbaar als `brms`).
- Test-/ontwikkel-Pi 4 thuis: 192.168.178.114, user `jandirk`, SSH-alias `brms` (sleutel `~/.ssh/id_ed25519`).

## Software-architectuur
Zie **ARCHITECTURE.md** – het ontwerpdocument is leidend voor architectuur, besluiten en open vragen.
Er wordt niets gebouwd dat daar niet in staat.

## Ontwikkelaanpak
- Ontwikkelen in PyCharm op Windows; testen op Pi 3B/Pi 4 thuis (zelfde OS).
- Code modulair: één module per sensortype (decoder/reader) + gemeenschappelijke opslag- en alarmlaag,
  zodat sensoren later makkelijk toe te voegen zijn.
- Eerst werkend en eenvoudig; daarna uitbreiden.

## Werkafspraken voor Claude
Deze regels gelden in elke sessie, op elke machine.

- **Nooit committen of pushen zonder toestemming.** Na een wijziging: melden wat er veranderd is,
  vragen "Zal ik committen en pushen?" en wachten op een uitdrukkelijk ja. Toestemming geldt alleen
  voor die ene wijziging, en apart voor commit en voor push; eerdere commits zijn geen doorlopende
  toestemming. Commit en push nooit in één shell-commando aan elkaar rijgen.
- **Stil werken, één keer rapporteren.** Geen stroom van bestandswijzigingen of tussentijds
  commentaar: wijzigingen met `Edit`/`Write`, één keer per bestand, geen bestandsinhoud in een
  Bash-commando plakken, niet teruglezen wat net geschreven is. Aan het eind één samenvatting van
  wat er veranderd is en wat de gebruiker moet controleren.
- **Eerst ontwerpen, dan bouwen.** Geen code in `brms/` voordat ARCHITECTURE.md het beschrijft én de
  gebruiker opdracht geeft om te bouwen. Onbesliste punten komen als [OPEN-n] in ARCHITECTURE.md §11;
  Claude beslist die nooit zelf en verzint niets buiten het ontwerp.
- **ARCHITECTURE.md bijhouden.** Opbouw per onderdeel: redenering → afgewezen alternatieven →
  specificatie. Elke wijziging krijgt een regel in de revisietabel (Versie / Datum / Auteur /
  Wijziging), auteur "Claude Code, <modelnaam>". De codeversie staat in `brms/__init__.py` (`VERSION`).
- **Ontwerpantwoorden kort en helder.** Begin met de keuze in één zin plus een aanbeveling. Noem
  uitvoeringsdetails alleen als ze de keuze veranderen; presenteer details niet als problemen.
  Korte vragenlijsten.
- **Ruim verklarend commentaar in de code, in het Nederlands.** Bij elk blok wat het doet en waarom,
  met verwijzing naar de paragraaf in ARCHITECTURE.md; elke ontwerpkeuze en elk shell-commando in
  deploy-scripts toegelicht. Liever iets te veel dan te weinig: de code moet maanden later nog te
  begrijpen zijn. Geldt voor Python, tests en shell.
- **Hoofdbranch is altijd `main`**, nooit `master`.
- **Taal:** Nederlands, in antwoorden, documenten en commentaar.
- **Sudo op de Pi** vraagt een wachtwoord: stappen met sudo voert de gebruiker zelf uit.
  `sudo rpi-clone` alleen op uitdrukkelijk verzoek.
- **`diversen/`** bevat eigen bestanden van de gebruiker (met geheimen) en blijft buiten git.

Claude Code-instellingen die met het project meekomen staan in `.claude/`: `settings.json`
(statusregel) en `statusline-command.sh` (branch | model | BRMS-versie | verbruik).

## Eerste stap
Eerste bouwstap (pas na expliciete opdracht): de 3 Xiaomi Monitor 3's (pvvx, BTHome v2) door de keten
volgens ARCHITECTURE.md – BLE-reader met BTHome-decoder → MQTT (Mosquitto) → opslag-service →
SQLite (tabel `meting`): temperatuur, vocht, batterijspanning, RSSI.
Daarna: Ruuvi- en Victron-decoder, Shelly-reader (bilge), alarmlaag met Telegram en e-mail, web, camera.

## Open punten
- Accutype (bij LiFePO4: niet laden onder 0 °C – BMS/lader regelt dit; geen eigen accutemperatuurmeting gepland).
- Data-abonnement voor de router: 40 GB per maand (afgesloten november 2026). eSIM-profiel thuis installeren (RutOS-app of WAN-kabel).
