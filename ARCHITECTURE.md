# BRMS – Architectuur

**Versie 0.92 – concept** · 21 november 2026

Dit document beschrijft wát BRMS doet en waarom. Er wordt niets gebouwd dat hier niet
staat. Elke sectie volgt dezelfde opbouw: eerst de **redenering**, dan de **verworpen
alternatieven** (alleen alternatieven die echt overwogen zijn), dan de **specificatie**.
Wat nog niet besloten is, staat als open vraag in §11 en wordt in de tekst gemarkeerd
met **[OPEN-n]**.

---

## 1. Doel en scope

**Redenering.** De boot ligt 's winters onbeheerd. Problemen die je te laat ziet –
vorst, vocht, lege accu, wegvallende walstroom, water in de bilge – veroorzaken schade
die met een melding op tijd te voorkomen is.

**Specificatie.** BRMS:
- meet periodiek temperatuur, luchtvochtigheid, accu, walstroom, zonnepaneel en bilge;
- slaat alle metingen lokaal op;
- toont actuele waarden en grafieken op een webpagina, alleen bereikbaar via VPN/LAN;
- stuurt pushmeldingen bij alarmtoestanden;
- draait maanden onbeheerd en herstelt zichzelf na stroomuitval of softwarefouten.

Buiten scope (nu): IP-camera (later, §5.7), besturing van apparatuur op afstand anders
dan herstart/uitschakelen.

---

## 2. Ontwerpprincipes

| # | Principe | Reden |
|---|---|---|
| P1 | Betrouwbaarheid en eenvoud gaan boven features | Het systeem draait maanden zonder dat iemand aan boord is; elke extra component is een extra faalkans. |
| P2 | Eigen Python-code, géén Home Assistant | Zie §5.1. |
| P3 | Zo min mogelijk afhankelijkheden, vastgepind | Minder breekbaar; een update kan niet ongemerkt iets kapotmaken. |
| P4 | Kernfunctie volledig lokaal, geen cloud | Mobiel internet kan wegvallen; meten en opslaan moet dan gewoon doorgaan. |
| P5 | Draadloze sensoren waar mogelijk (BLE, wifi) | Minder bedrading aan boord. |
| P6 | Robuust tegen stroomuitval | Herstart vanzelf, geen datacorruptie. |
| P7 | Weinig schrijven naar flash | Flashopslag slijt door schrijven; uitval van de opslag is fataal voor een onbeheerd systeem. |
| P8 | Modulair: één module per sensortype | Sensoren later toevoegen zonder de rest aan te raken. |

---

## 3. Systeemoverzicht

```
 BLE-sensoren ──> BLE-reader ─────┐                       ┌──> alarmen ──> Telegram, e-mail
                                  │                       │
 Shelly's ──────> Shelly-reader ──┼──> MQTT (Mosquitto) ──┼──> opslag ──> SQLite (WAL) ──> back-up
                                  │      lokaal           │                    │
 GPIO ──────────> (reader) ───────┘                       │                    └──> web (Flask)
                                                          └──> (later) Home Assistant e.d.
```

Alles draait op één Raspberry Pi 4B (hostnaam `BRMS`) aan boord. Toegang van buitenaf
uitsluitend via Tailscale (§9).

### 3.1 Processen en onderlinge communicatie (besluit OPEN-1)

**Aanleiding.** Vastgesteld moest worden of readers, alarmen, opslag en web in één proces
draaien of als losse services, en hoe ze met elkaar communiceren. Daarbij speelde de wens
om op enig moment in de toekomst Home Assistant of een andere afnemer te kunnen aansluiten.

**Redenering.** De logische naad in het systeem ligt direct na de correcties uit de
configuratie: daar is een meting compleet en betekenisvol (juiste sensor-ID, grootheid,
gecorrigeerde waarde, soort). Wie op die plek meeluistert, krijgt alles wat het systeem weet.
Een MQTT-broker op die plek levert:
1. *Isolatie.* Readers, alarmen en opslag worden losse processen; een fout in de opslag
   (volle schijf, databasefout) legt de alarmen niet plat.
2. *Live meekijken.* Met `mosquitto_sub` is elke meting vanaf de ontwikkel-pc (via
   Tailscale) te volgen – nuttig bij het ijken van sensoren en testen van BLE-ontvangst.
3. *Eén technologie.* Mosquitto wordt in het JSEM-project al in het kritieke pad gebruikt
   (load balancing op de netaansluiting) en heeft zich daar als betrouwbaar bewezen.
4. *Uitbreidbaar.* Home Assistant of een andere afnemer sluit aan zonder dat er iets verandert.

De kosten zijn eenvoud, niet betrouwbaarheid: een extra pakket met configuratie, een
vaste opstartvolgorde, en elke component moet een tijdelijk afwezige broker verdragen.

**Verworpen alternatieven.**
- *Eén proces met een interne wachtrij, MQTT later als extra afnemer.* Eenvoudiger en
  zonder broker in het kritieke pad, maar zonder isolatie tussen componenten en zonder
  live meekijken. Het betrouwbaarheidsvoordeel weegt niet op tegen Mosquitto's bewezen
  betrouwbaarheid.
- *Losse processen met een andere vorm van communicatie (bijv. via de database).* Geen
  standaard, niet bruikbaar voor Home Assistant, en meer schrijven naar flash.

**Specificatie.**
1. Readers, alarmlaag, opslag en web zijn **losse systemd-services** (`Restart=always`).
2. Readers publiceren elke ontvangen meting op een **lokale Mosquitto-broker**; alarmlaag en
   opslag abonneren zich. Het web leest uit SQLite en geeft opdrachten via MQTT (§5.6).
   Opdrachten (poll-nu, verwijderen) lopen ook via MQTT. De opslag-service is de enige die
   naar de database en de opnamemap schrijft (met uitzondering van het opnamebestand zelf,
   dat de camera-reader schrijft).
3. De broker draait **zonder persistence** (P7) en luistert **alleen op localhost en de
   Tailscale-interface** – niets open op internet.
4. **QoS:** toestanden (§5.3.2) met QoS 1, zodat een verandering niet verloren gaat;
   doorlopende grootheden met QoS 0.
5. Is de broker onbereikbaar, dan **gooit een reader de metingen weg** en blijft hij
   proberen te verbinden. Na herstel gaat hij verder met de dan ontvangen waarden.
6. systemd zorgt dat de broker vóór de overige services start.
7. MQTT-clientbibliotheek: zie §3.2. Topics en berichtformaat: zie §3.3.

### 3.2 MQTT-clientbibliotheek (besluit OPEN-22)

**Aanleiding.** De standaardbibliotheek van Python bevat geen MQTT-client; er moest een
bibliotheek gekozen worden.

**Redenering.** Het JSEM-project gebruikt `paho-mqtt` 2.1.0 rechtstreeks, met één eigen
wrappermodule (`packages/interface/bus.py`) waar al het MQTT-verkeer doorheen loopt. Net
als bij Mosquitto (§3.1) weegt dezelfde technologie, versie en ervaring als in JSEM zwaar.
`paho-mqtt` werkt met een eigen netwerkthread en callbacks, niet met asyncio. In de
asyncio-services (§5.2.1) wordt dat opgevangen in één klein busmodule: paho's thread geeft
ontvangen berichten door aan de asyncio-loop. Daarmee blijft de thread binnen dat ene
module en zien de overige componenten alleen asyncio. Het web (Flask, geen asyncio)
gebruikt hetzelfde busmodule synchroon.

**Verworpen alternatieven.**
- *`aiomqtt` (asyncio-laag op paho-mqtt).* Sluit direct aan op asyncio, maar is een tweede
  pakket en wijkt af van JSEM.
- *`gmqtt` / `amqtt`.* Minder gebruikt en minder onderhouden.
- *Eigen MQTT-client.* Onnodig risico.

**Specificatie.**
1. **`paho-mqtt==2.1.0`**, vastgepind, zelfde versie als JSEM.
2. Eén eigen **busmodule** in BRMS waar al het MQTT-verkeer doorheen loopt, met een
   asyncio-koppeling voor de asyncio-services en synchroon gebruik voor het web. JSEM's
   `bus.py` dient als voorbeeld, maar wordt niet ongezien overgenomen.

### 3.3 Topics en berichtformaat (besluit OPEN-23)

**Aanleiding.** Vastgesteld moest worden welke berichten over de broker gaan, onder welke
topics en in welk formaat.

**Redenering.** Er zijn drie soorten berichten: metingen (§5.2.1), metadata van opnames
(§5.7) en opdrachten (poll-nu, verwijderen; §5.2.1, §5.6). Een hiërarchische topic maakt
filteren eenvoudig (`mosquitto_sub -t 'brms/meting/+/temperatuur'`). Een bericht dat zelf
compleet is, hoeft door een afnemer niet uit de topic te worden afgeleid; de kleine
redundantie neemt dat voordeel niet weg.

Retain (de broker bewaart het laatste bericht per topic) is voor opdrachten gevaarlijk:
een poll-nu of verwijderopdracht zou bij elke herverbinding opnieuw worden uitgevoerd. Voor
metingen is het niet nodig – het web haalt actuele waarden uit SQLite – en een bewaarde,
verouderde meting kan de alarmlaag na een herstart misleiden.

Een verloren opnamebericht laat een bestand op schijf achter dat niet in de database
staat; daarom QoS 1.

**Verworpen alternatieven.**
- *Eén topic voor alle metingen.* Filteren per sensor of grootheid kan dan alleen door de
  inhoud te ontleden.
- *Alleen de topic als drager van sensor-ID en grootheid.* Elke afnemer moet dan de topic
  ontleden.
- *Retain op metingen of opdrachten.* Zie redenering.
- *Eenheid in elk bericht.* Overbodig als elke grootheid één vaste eenheid heeft.

**Specificatie.**

| Bericht | Topic | Inhoud (JSON, UTF-8) | QoS |
|---|---|---|---|
| Meting | `brms/meting/<sensor_id>/<grootheid>` | `{"ts", "sensor_id", "grootheid", "waarde", "soort"}` | 1 voor toestand, 0 voor doorlopend en toename |
| Opname | `brms/opname/<camera_id>` | `{"ts", "camera_id", "type", "duur", "bestand", "bytes", "aanleiding"}` | 1 |
| Opdracht | `brms/opdracht/<doel>/<actie>`, bijv. `brms/opdracht/camera/poll-nu`, `brms/opdracht/camera/richt` (§5.7 punt 19), `brms/opdracht/shelly/poll-nu`, `brms/opdracht/opslag/verwijder`, `brms/opdracht/trace/aan`, `brms/opdracht/simulatie/aan` (§10.3), `brms/opdracht/relais/zet` en `brms/opdracht/relais/stand` (§5.2.6), `brms/opdracht/alarm/schakel` en `brms/opdracht/alarm/stand` (§5.5.3) | afhankelijk van de actie, bijv. `{"type": "video", "aanleiding": "alarm:beweging"}` | 1 |
| Alarm (v0.31) | `brms/alarm/<alarm_id>` | `{"ts", "alarm_id", "sensor_id", "grootheid", "gebeurtenis", "waarde"}`; `gebeurtenis` is `begin`, `herstel` of `uitgezet` (§5.5.3). De alarmlaag meldt bij zijn start en daarna elke minuut op `brms/alarm/herstart` welke alarmen actief zijn: `{"ts", "actief": [[alarm_id, sensor_id], ...]}` (v0.34, §5.5.3 punt 8). De opslag vraagt bij zijn eigen start om die stand op `brms/opdracht/alarm/stand` met `{"ts"}`; de alarmlaag antwoordt direct (v0.86). | 1 |
| Simulatie (v0.55) | `brms/simulatie/<dienst>` | `{"ts", "dienst", "sensoren": [{"sensor_id", "soort", "einde"}, ...]}`: welke sensoren een reader simuleert; `soort` is `test` of `demo`, `einde` in Unix-seconden (leeg bij een demo). Bij de start van de reader, bij elke verandering en elke 30 s (§10.3 punt 10). | 1 |
| Trace (v0.16) | `brms/trace/<sensor_id>` | `{"ts", "sensor_id", "dienst", "stap", "tekst"}`, optioneel `"deel"` – alleen zolang trace voor die sensor aanstaat (§10.2) | 0 |

1. Elk bericht is **compleet**: ook wat in de topic staat, staat in de inhoud.
2. **Geen retain**, voor geen enkel bericht.
3. `ts` is een **geheel getal**, Unix-seconden UTC, zoals in de database (§5.3.1).
4. `sensor_id` is de vaste ID uit de configuratie (bijv. het MAC-adres met dubbele punten,
   toegestaan in MQTT-topics).
5. Elke grootheid heeft **één vaste eenheid** (bijv. °C, %, V, A, dBm, mG); berichten
   bevatten geen eenheid. De tabel van grootheden staat in §5.3.3.

---

## 4. Hardware

De actuele hardwarelijst en status staan in `CLAUDE.md` en `BRMS_componentenlijst.xlsx`.
Architectuurrelevante keuzes:

### 4.1 Bluetooth: de ingebouwde Bluetooth van de Pi (v0.65)

**Aanleiding.** Tot v0.64 ging het ontwerp uit van een USB-dongle met externe antenne,
omdat de ingebouwde Bluetooth van de Pi 4 een te klein bereik zou hebben. Open stond
welke dongle (OPEN-2).

**Redenering.** De sensoren zitten verspreid over hoofdkajuit, vooronder, twee
achterkajuiten, motorruim en buiten (v0.46: RuuviTag 4-in-1 in de hoofdkajuit, RuuviTag
Pro 3-in-1 in het motorruim en buiten, Xiaomi Monitor 3 in het vooronder en de twee
achterkajuiten). Een test thuis (november 2026) met de Pi en de sensoren van Xiaomi en
Ruuvi gaf met de ingebouwde Bluetooth ruim 20 meter bereik, ook door muren. Dat is meer
dan de afstanden aan boord. Een dongle is dan een extra onderdeel zonder nut (P1).
De test was thuis, niet op de boot: of het aan boord ook volstaat, blijkt daar. De
`rssi` per sensor (§5.3.3) en het alarm "sensor stil" laten het zien als het bereik
tekortschiet.

**Verworpen alternatieven.**
- *USB-dongle met externe antenne (ontwerp tot v0.64).* Voorlopig niet nodig; blijft de
  uitwijk als het bereik aan boord tegenvalt. De code hoeft daarvoor niet te veranderen:
  de BLE-reader gebruikt de adapter die het systeem aanbiedt.

**Specificatie.** BLE via de **ingebouwde Bluetooth** van de Pi 4. OPEN-2 (welke dongle)
is daarmee **voorlopig vervallen**; valt het bereik aan boord tegen, dan komt de vraag
terug.

### 4.2 Klok en tijd (besluit OPEN-3)

**Aanleiding.** De Pi 4 heeft geen RTC. Vastgesteld moest worden waar de tijd vandaan komt
en wat BRMS doet zolang de tijd na het opstarten nog niet betrouwbaar is. Een DS3231 is in
bezit, maar past niet zonder meer in de behuizing van de Pi.

**Redenering.** Zonder RTC zet `systemd-timesyncd` de klok na het opstarten op de laatst
opgeslagen tijd en corrigeert pas na synchronisatie. Na een stroomuitval van enkele uren
loopt de klok dan uren achter. Nieuwe metingen zouden tijdstempels krijgen die al in de
database staan (botsende primaire sleutel, door elkaar lopende grafieken), gevolgd door een
sprong vooruit zodra de tijd binnenkomt.

Niet alles heeft echte tijd nodig:
- De **alarmlaag** werkt met tijdsduren ("langdurig", rustperiode, "sensor stil"). Die zijn
  te meten met een monotone klok (loopt alleen vooruit sinds het opstarten), zodat alarmen
  vanaf de eerste seconde werken, ongeacht de kloktijd.
- **Opslag en camera** leggen tijdstempels vast en hebben wél echte tijd nodig.

De router (RUT241) kan de tijd van het mobiele netwerk overnemen (operator time / NITZ) en
via NTP synchroniseren, en kan als NTP-server dienen voor het LAN. Een onbetrouwbare tijd
ontstaat dan alleen als Pi én router zonder stroom zijn geweest (ze delen het 12V-net,
bijvoorbeeld na afschakelen door de BatteryProtect): de router heeft dan ruwweg 1–2 minuten
nodig om het netwerk te vinden en de tijd te ontvangen. Zonder mobiele dekking komt er
geen tijd – maar dan kunnen er ook geen meldingen uit en is de boot niet bereikbaar. Het
verlies beperkt zich tot een gat van enkele minuten in de opslag na een volledige
stroomuitval; dat past bij het uitgangspunt dat metingen worden weggegooid zolang iets
niet gereed is (§3.1). Minder hardware weegt zwaarder (P1).

**Verworpen alternatieven.**
- *DS3231 verplicht.* Maakt de tijd direct na opstarten betrouwbaar, maar past niet zonder
  meer in de behuizing; het voordeel is beperkt tot de eerste minuten na een volledige
  stroomuitval. Blijft een optie (zie specificatie).
- *Direct starten met de klok zoals die is.* Botsende en verkeerde tijdstempels.
- *Alle services laten wachten op tijdsynchronisatie (`time-sync.target`).* Zonder
  internet of netwerktijd starten dan ook de alarmen niet.
- *Metingen bufferen en later terug-dateren.* Complex en foutgevoelig.

**Specificatie.**
1. **De router is de tijdbron.** RutOS: tijdsynchronisatie via de provider (operator
   station) en NTP; de router als NTP-server voor het LAN.
2. Op de Pi gebruikt `systemd-timesyncd` de **router als eerste NTP-server**, met de
   publieke NTP-pool als reserve.
3. De **alarmlaag** gebruikt voor alle tijdsduren een **monotone klok**.
4. **Opslag en camera** schrijven pas als de systeemklok **gesynchroniseerd** is (status
   "System clock synchronized" van `timedatectl`). Daarvóór ontvangen metingen en opnames
   worden niet opgeslagen.
5. Alle tijdstempels in UTC.
6. Een **DS3231 is optioneel** (in bezit). Wordt hij later geplaatst (bijv. een platte
   module op de GPIO-pinnen of met een kort kabeltje buiten de behuizing), dan is de tijd
   direct na opstarten goed en verandert er verder niets aan het ontwerp.

### 4.3 Voeding

12V boordnet → zekering → BatteryProtect → DC-DC 12→5V 3A. Verbruik ca. 11 W
(~21 Ah/dag); de accu overbrugt ca. 5 dagen zonder walstroom.

---

## 5. Softwarecomponenten

### 5.1 Platform: eigen Python-code

**Redenering.** Het systeem moet maanden onbeheerd draaien; de software moet klein en
volledig begrepen zijn.
**Verworpen.** Home Assistant – te zwaar, en updates kunnen ongemerkt iets breken.
**Specificatie.** Python 3 (Raspberry Pi OS Lite 64-bit), eigen venv, dependencies vastgepind.

### 5.2 Readers (één module per sensortype)

**Redenering.** Elk sensortype heeft een eigen protocol; door de decoder per type te
isoleren raakt een nieuwe sensor de rest niet (P8).

#### 5.2.1 Hoe een reader metingen aanlevert (besluit OPEN-4)

**Aanleiding.** Vastgesteld moest worden of een reader per interval wordt uitgelezen of
metingen aanlevert zodra ze binnenkomen, en in welke vorm.

**Redenering.**
- De alarmlaag moet elke ontvangen waarde zien (§5.3.2). Een reader moet dus aanleveren
  zodra er iets binnenkomt.
- Er zijn twee soorten bronnen: BLE-sensoren *pushen* (advertenties komen binnen wanneer de
  sensor zendt), Shelly's worden *gepolld*.
- Er is één Bluetooth-adapter. Meerdere scanners naast elkaar zitten elkaar in de weg. Eén
  scanner kan elke advertentie aan de juiste decoder geven, want het type staat herkenbaar
  in de advertentie: BTHome via service-data UUID `0xFCD2`, Ruuvi via fabrikant-ID `0x0499`,
  Victron via fabrikant-ID `0x02E1`.
- Een decoder die alleen bytes omzet in waarden is te testen met opgenomen advertenties,
  op Windows, zonder Bluetooth of Pi.
- De scanner ziet alle BLE-apparaten in de buurt, ook sensoren op andere boten in de haven.
- Correcties op één centrale plek per reader houden decoders zuiver.
- `bleak` is asyncio-gebaseerd; een tweede concurrency-model (threads) is onnodig complex.
- Pollende readers (Shelly, camera) moeten ook buiten hun interval direct kunnen meten,
  bijvoorbeeld een video bij een bewegingsalarm of een verse waarde op verzoek van de
  gebruiker. Eén uniforme "poll-nu"-ingang via MQTT maakt dat voor elke pollende reader gelijk.

**Verworpen alternatieven.**
- *Poll-nu voor de BLE-reader (laatst ontvangen waarden opnieuw doorsturen).* Een herhaalde
  waarde met een nieuw tijdstip laat een stille sensor levend lijken; met het oude tijdstip
  telt de opslag haar dubbel mee. BLE-sensoren zenden bovendien al elke paar seconden.
- *Pull-interface (`read()` per interval).* Past niet bij BLE-push; de alarmlaag zou niet
  elke waarde zien.
- *Een scanner per sensortype.* Conflicteert op de ene adapter.
- *Threads in plaats van asyncio.* `bleak` vereist asyncio toch al. (De netwerkthread van
  `paho-mqtt` blijft binnen het busmodule, §3.2.)
- *aiohttp/httpx voor de Shelly-polls.* Extra afhankelijkheid (P3); de standaardbibliotheek
  volstaat.

**Specificatie.**
1. **Eén BLE-reader** voor alle BLE-sensoren: één scanner die elke advertentie op basis van
   UUID/fabrikant-ID doorgeeft aan een **decoder** per sensortype (`brms/readers/`). Een
   decoder is een zuivere functie: bytes in, waarden uit.

   *Actief of passief scannen (v0.59).* De scanner scant nu **actief**, de standaard van
   `bleak`. Een advertentie kan maar 31 bytes bevatten; veel apparaten zetten daarom een
   deel van hun gegevens (meestal de naam) in een tweede pakketje, de *scan response*,
   dat ze alleen sturen als een scanner erom vraagt. Een algemene bibliotheek als `bleak`
   wil van elk apparaat een zo compleet mogelijk beeld geven en vraagt er dus standaard
   om; bovendien werkt actief scannen op elk platform zonder extra instellingen. BRMS
   heeft de scan response niet nodig: de meetwaarden van Xiaomi (BTHome), Ruuvi en Victron
   staan volledig in de advertentie zelf. Het antwoord kost de sensor een klein beetje
   extra zendenergie. **[OPEN-26]** Overstappen op passief scannen (alleen luisteren)?
   Dat vraagt op Linux extra BlueZ-instellingen.
2. **Eén Shelly-reader** die alle Shelly's pollt (interval per Shelly instelbaar, standaard
   30 s; bilge 2 s, §5.2.4), via de standaardbibliotheek
   (`urllib`, uitgevoerd in een thread vanuit asyncio).
3. Readers verwerken **alleen apparaten die in de configuratie staan**; al het andere wordt
   genegeerd.
4. Readers passen de **correcties uit de configuratie** toe (§5.4.2), berekenen de
   **afgeleide grootheden** (§5.2.5, v0.31), laten grootheden weg die niet in de lijst van
   de sensor staan en publiceren daarna elke waarde via MQTT (§3.1) als
   `Meting(ts, sensor_id, grootheid, waarde, soort)`:
   - `ts`: ontvangsttijd op de Pi, Unix-tijd UTC (een advertentie heeft zelf geen tijd; zie §4.2);
   - `soort`: doorlopend, toestand of toename, vast per grootheid (§5.3.2);
   - alleen eenvoudige velden (getal, tekst), zodat het bericht direct als JSON te versturen is.
5. Readers zijn asyncio-gebaseerd, **zonder extra bibliotheken** naast `bleak` en de
   `paho-mqtt` (§3.2).
6. Elke **pollende** reader (Shelly, camera) heeft een **poll-nu**-ingang: een MQTT-opdracht
   die direct een poll uitvoert, los van het interval. Alarmlaag, web en gebruiker kunnen
   die opdracht geven. De BLE-reader heeft geen poll-nu.
7. Elke reader heeft een **trace-uitgang** (v0.16, §10.2): staat trace voor een sensor aan,
   dan publiceert de reader per ontvangst wat hij in elke stap doet (ruw, gedecodeerd,
   gecorrigeerd, gepubliceerd). Standaard staat trace uit.
8. De BLE-reader heeft een **simulatie-ingang** (v0.20, §10.3): op verzoek maakt hij zelf
   advertenties aan en verwerkt die via dezelfde ingang als ontvangen advertenties; de
   echte advertenties van die sensor negeert hij zolang. De Shelly-reader heeft dezelfde
   ingang (v0.29): hij maakt dan zelf het antwoord van de Shelly aan en pollt de echte niet.

| Reader | Bron | Methode | Status |
|---|---|---|---|
| BTHome v2 | Xiaomi Monitor 3 (pvvx) | BLE-advertenties, eigen decoder (§5.2.3) | Eerste stap |
| Ruuvi | RuuviTag (Pro) | BLE-advertenties, eigen decoder (§5.2.3) | Gebouwd (v0.19); nog niet getest met een echte RuuviTag |
| Victron | BMV-700, 3× MPPT, MultiPlus | BLE Instant Readout, decoder in de BLE-reader (§5.2.2) | Gebouwd (v0.27); nog niet getest met de echte apparaten |
| Shelly | Bilge: pompspanning achter de bestaande vlotter (Shelly Plus Uni) | Lokale HTTP-API (§5.2.4) | Gebouwd (v0.29); nog niet getest met een echte Shelly |
| Shelly-relais | Tussenstekker met energiemeting voor de kachel (Shelly Plug M Gen3) | Lokale HTTP-API: pollen en schakelen (§5.2.6) | Gebouwd (code v0.27.0); nog niet beproefd op de Pi |
| Camera | IP-camera dek | Foto per interval, video op poll-nu (§5.7) | Later |
| GPIO | Waar nodig | — | Gepland |

#### 5.2.2 Victron-apparaten en walstroom (besluit OPEN-6, OPEN-7, vervallen OPEN-8)

**Aanleiding.** Open stond hoe de BMV-700 wordt uitgelezen (OPEN-6), welk type
laadregelaar het zonnepaneel heeft (OPEN-7), en hoe walstroomuitval met de Shelly aan de
230V-kant wordt vastgesteld (OPEN-8).

**Redenering.** Aan boord zijn:
- Victron BMV-700 met VE.Direct Bluetooth Smart dongle;
- 2× Victron BlueSolar MPPT 75/15 met VE.Direct Bluetooth Smart dongle;
- 1× Victron SmartSolar MPPT 100/30 met ingebouwde Bluetooth;
- Victron MultiPlus 12/3000/120-16 (VE.Bus, geen ingebouwde Bluetooth), plus een MK3-USB
  interface voor configuratie.

Drie zonnebanken: 's winters is alleen één bank aangesloten (50 Wp), 's zomers ook de
andere. Alle drie worden meegenomen.

De VE.Direct Bluetooth Smart dongle (firmware ≥ 2.41, "instant readout" aangezet in
VictronConnect) ondersteunt **Instant Readout** voor BMV-70x en BlueSolar MPPT: de
belangrijkste gegevens worden versleuteld uitgezonden zonder koppeling. De SmartSolar doet
dat met ingebouwde Bluetooth. Een **VE.Bus Smart dongle** voegt Instant Readout toe aan de
MultiPlus, inclusief de toestand van de AC-ingang. Daarmee lopen alle Victron-apparaten via
één decoder in de bestaande BLE-reader (§5.2.1), zonder extra reader of bekabeling.

Walstroom via de MultiPlus is een *directe* meting: de MultiPlus meldt zelf of er spanning
op de AC-ingang staat. Bij de Shelly aan de 230V-kant was stilte het signaal ("offline =
walstroom weg"), wat botste met de regel dat ontbrekende data geen 0 is (§5.3.2) en een
offline-criterium vereiste (OPEN-8). Met de MultiPlus is `walstroom = 0` een echte meting,
en betekent stilte gewoon "sensor stil". Er is geen werk aan de 230V-kant nodig.

**Verworpen alternatieven.**
- *VE.Direct-USB-kabels (tekstprotocol) voor BMV en MPPT's.* Bekabeling naar elk apparaat;
  de dongles zijn al aanwezig en Instant Readout volstaat.
- *Walstroom via Shelly aan de 230V-kant.* Indirecte meting (stilte = walstroom weg), vereist
  een offline-criterium en installatie aan de 230V-kant.
- *Walstroom via de MK3-USB (MK2-protocol).* Bekabeld en meer gegevens, maar een tweede soort
  Victron-reader met eigen protocol en seriële afhankelijkheid; het protocol kan ook
  instellingen wijzigen; de MK3 is dan bezet voor configuratie en wordt bus master (waarna
  een VE.Bus Smart dongle weinig toont). Eenvoud (P1) weegt zwaarder.
- *Walstroom afleiden uit BMV-stroom of -spanning.* Onbetrouwbaar: laden kan ook van de
  zon komen, druppelladen geeft ~0 A, en bij LiFePO4 ligt de rustspanning dicht bij de
  druppellaadspanning.
- *Alleen de MPPT met het aangesloten winterpaneel meenemen.* 's Zomers zijn alle drie in gebruik.

**Specificatie.**
1. BMV-700, de drie MPPT's en de MultiPlus worden uitgelezen via **BLE Instant Readout**,
   gedecodeerd door de Victron-decoder in de BLE-reader (`victron-ble` als decoder, niet als
   eigen scanner).
2. Elk apparaat heeft een eigen `sensor_id` en een eigen **encryptiesleutel** (uit
   VictronConnect) in de configuratie (§5.4).
3. Vereist: VE.Direct-dongles op firmware ≥ 2.41 met instant readout aan; een **VE.Bus Smart
   dongle** op de MultiPlus (aan te schaffen). Geen bus master (MK3, GX) permanent op de VE.Bus.
4. **Walstroom** komt uit de AC-ingangstoestand van de MultiPlus (grootheid `walstroom`,
   §5.3.3). De veldnaam in de decoder (bekend als `ac_in_state`) wordt bij de eerste test
   met de echte MultiPlus bevestigd.
5. De Shelly-reader (§5.2.1) wordt alleen nog gebruikt voor de **bilge** (Shelly Plus Uni).
6. OPEN-8 vervalt.
7. **Sensortypen in de configuratie** (v0.27), elk met een verplichte **`sleutel`** (32
   hexadecimale tekens) en het MAC-adres van het apparaat als `id`:

   | Type | Apparaat | Grootheden |
   |---|---|---|
   | `victron-bmv` | accumonitor (BMV-700 met dongle) | `spanning`, `stroom`, `soc`, `verbruikt`, `resterende_tijd` |
   | `victron-mppt` | zonnelader (BlueSolar met dongle, SmartSolar) | `zonnevermogen`, `laadstroom_zon` |
   | `victron-multiplus` | MultiPlus met VE.Bus Smart dongle | `walstroom` |

   Een type per soort apparaat (en niet één type `victron`) maakt dat `check-config` en de
   grafiekenpagina per apparaat weten welke grootheden er zijn. Het soort apparaat staat
   ook in de advertentie; wijkt dat af van het type in de configuratie, dan wordt het
   bericht geweigerd.
8. Een **verkeerde sleutel** of een verkeerd type is een fout in de configuratie die elke
   seconde terugkomt: de reader meldt hem één keer per sensor als waarschuwing in het log.
   De sleutel zelf komt nooit in het log, in een foutmelding of in de trace.
9. `verbruikt` is een positief getal. Een veld dat het apparaat als "onbekend" uitzendt
   (de resterende tijd tijdens laden, een onbekende AC-ingang) wordt niet gepubliceerd
   (§5.2.3 punt 4).
10. Victron-apparaten kunnen gesimuleerd worden (v0.30, §10.3): de reader maakt zelf een
    bericht aan en versleutelt het met de sleutel uit de configuratie.

#### 5.2.4 Bilge (v0.14, herzien in v0.60)

**Aanleiding.** In v0.14 was naast de meting van de pompspanning een tweede vlotter
(hoogwater) voorzien. In v0.60 is besloten die tweede vlotter niet te plaatsen: BRMS meet
alleen aan de bestaande vlotter van de bilgepomp.

**Redenering.** De bestaande vlotter staat in serie met de bilgepomp: stijgt het water,
dan sluit hij en staat er 12 V op de pompkant. Die ene spanning zegt genoeg. Draait de
pomp vaker of langer, dan is dat het vroegste signaal van een lek. Houdt de pomp het niet
bij of is hij defect, dan blijft de vlotter gesloten en blijft de spanning staan: het
alarm "bilgepomp lang" (§5.5.2) meldt dat. Een tweede vlotter voegt daar weinig aan toe en
vraagt extra bedrading in de bilge (P1). De Shelly Plus Uni heeft een spanningsmeting
(0–15 / 0–30 V DC) en wordt rechtstreeks uit het 12V-boordnet gevoed (9–28 V DC, < 1 W);
er komt geen 230 V in de bilge. De voeding komt van de kant van de vlotter waar altijd
12 V op staat, de meting van de andere (geschakelde) kant: alles zit bij dezelfde vlotter.
Valt die 12 V weg, dan valt ook de Shelly stil en meldt het alarm "sensor stil" dat. Een
pompbeurt kan korter zijn dan het standaard poll-interval van 30 s en zou dan gemist
worden; vaker pollen via dezelfde lokale HTTP-API is de eenvoudigste oplossing (P1).

Wat zo niet gezien wordt, is een vlotter die niet inschakelt terwijl het water stijgt.
Daarvoor kan later een waterdetector (probe) op een digitale ingang van de Uni komen; de
grootheid `bilge` en het alarm "bilge" blijven daarvoor bestaan, maar worden nu niet
gebruikt.

*Waarom wifi en niet Bluetooth (v0.19).* De overige sensoren komen via Bluetooth binnen
omdat ze hun metingen zelf uitzenden als advertentie: de BLE-reader hoeft alleen te
luisteren (§5.2.1). De Shelly Plus Uni doet dat niet. Hij gebruikt Bluetooth voor de eerste
installatie en om zelf andere sensoren te ontvangen, maar zendt de toestand van zijn eigen
ingangen niet uit. Uitlezen via Bluetooth kan alleen door per poll een verbinding op te
bouwen, en dat elke 2 s op de ene adapter die continu moet scannen. Via wifi is de Uni
rechtstreeks te bevragen met de gedocumenteerde lokale HTTP-API. Wifi kost hier niets
extra: de Uni wordt uit het boordnet gevoed (geen batterij te sparen) en de router staat
er al.

**Verworpen alternatieven.**
- *Bluetooth-waterdetector (bijv. SwitchBot Water Leak Detector).* Gemaakt voor een droge
  vloer: meldt alleen "nat", klotsend water geeft valse meldingen, en het formaat is geen
  BTHome (extra decoder).
- *De Shelly Plus Uni via Bluetooth uitlezen.* De Uni zendt zijn toestand niet uit als
  advertentie; een verbinding per poll botst met de continu scannende adapter (zie
  redenering).
- *Een tweede vlotter voor hoogwater, boven het inschakelniveau van de pomp (ontwerp
  v0.14).* Vervallen in v0.60: een pomp die het niet bijhoudt of defect is, is al te zien
  aan de spanning die blijft staan; de extra vlotter en bedrading wegen daar niet tegen op.
- *Shelly stuurt zelf een bericht bij verandering (webhook of eigen MQTT).* Vraagt een extra
  ontvanger op de Pi; snel pollen volstaat.

**Specificatie.**
1. Eén **Shelly Plus Uni** bij de bestaande vlotter van de bilgepomp:
   - de **voeding** komt van de kant van de vlotter waar altijd 12 V op staat;
   - de **geschakelde kant van de vlotter** (de pompkant) gaat naar de spanningsmeting →
     grootheid `bilgepomp` (1 = vlotter gesloten, pomp krijgt spanning). De drempel
     (spanning → 0/1) staat in de configuratie. De precieze aansluiting wordt bepaald
     zodra de Uni er is;
   - er komt **geen tweede vlotter** (v0.60). Later kan een waterdetector (probe) op een
     digitale ingang komen → grootheid `bilge`. Zolang die er niet is, krijgt `bilge` geen
     `bron` en wordt de grootheid niet geleverd.
2. De bilge-Shelly wordt elke **2 s** gepolld.
3. Een **pompbeurt** is een overgang van `bilgepomp` 0 → 1; de alarmlaag telt de beurten.
4. **Sensortype `shelly-uni`** in de configuratie (v0.29). Het `id` is een vrij gekozen
   vaste naam (letters, cijfers, `-` en `_`), bijvoorbeeld `shelly-bilge`; die staat in de
   database. Extra velden:

   | Veld | Betekenis |
   |---|---|
   | `adres` | IP-adres of hostnaam van de Shelly (verplicht). De Shelly krijgt in de router een vast adres. |
   | `interval` | seconden tussen twee polls, 1–3600; standaard 30, de bilge 2 |
   | `bron` | welke component van de Shelly welke grootheid levert (verplicht): `"input:<nr>"` (digitale ingang) of `"voltmeter:<nr>"` (spanningsmeting) |
   | `drempel` | spanning in volt vanaf waar een voltmeter-bron 1 geeft; verplicht bij een voltmeter-bron |
   | `omgekeerd` | optioneel: grootheden waarvan 0 en 1 verwisseld worden (een contact dat opent als het water stijgt) |

   ```toml
   [[sensor]]
   id = "shelly-bilge"
   type = "shelly-uni"
   naam = "bilge"
   adres = "192.168.1.50"
   interval = 2
   bron = { bilgepomp = "voltmeter:100" }
   drempel = 6.0
   ```
   Met een waterdetector erbij wordt het
   `bron = { bilge = "input:0", bilgepomp = "voltmeter:100" }`.
   Het type levert `bilge`, `bilgepomp` (elk alleen met een `bron`) en `rssi` (de
   signaalsterkte van de wifi, zoals de Shelly die meldt). `check-config` controleert de
   velden.
5. **Uitlezen:** per poll één aanvraag `http://<adres>/rpc/Shelly.GetStatus`; het antwoord
   bevat alle componenten en de wifi. Een digitale ingang geeft `state` true → 1; de
   voltmeter geeft `voltage` ≥ drempel → 1. De voltmeter is op de Plus Uni een randapparaat
   dat eerst in de Shelly moet worden toegevoegd; daarna heet hij `voltmeter:<nr>`.
   De decoder is een zuivere functie (`brms/readers/shelly_uni.py`), de reader pollt
   (`brms/readers/shelly.py`).
6. **Geen aanmelding:** de Shelly heeft geen wachtwoord; hij is alleen bereikbaar op het
   netwerk van de boot (§9). Aanmelden (digest) is niet gebouwd.
7. **Niet bereikbaar** (geen antwoord binnen 5 s, of binnen het interval als dat korter
   is): er wordt niets gepubliceerd; stilte betekent "geen meting". De reader probeert het
   elk interval opnieuw en schrijft één waarschuwing in het log, en één regel bij herstel.
   Een component die ontbreekt of geen waarde geeft, wordt ook niet gepubliceerd (§5.2.3
   punt 4).
8. **Poll-nu** (§5.2.1 punt 6): opdracht `brms/opdracht/shelly/poll-nu` met
   `{"sensor_id": "shelly-bilge"}`, of zonder inhoud voor alle Shelly's.
9. De Shelly-reader is een eigen service, **`brms-shelly-reader`** (`brms shelly-reader`).
   Zonder Shelly in de configuratie draait hij zonder iets te doen.

#### 5.2.3 Decoders: eigen code of bibliotheek (besluit OPEN-5)

**Aanleiding.** Voor de decoderstap in de keten (ontvangen → decoderen → correctie →
MQTT, §5.2.1) moest per sensortype gekozen worden: zelf schrijven of een bestaande
bibliotheek gebruiken.

**Redenering.** RuuviTag (Pro) zendt dataformaat 5 (RAWv2) uit: een vaste, openbaar
gedocumenteerde reeks van 24 bytes. BTHome v2 (pvvx) is eveneens openbaar en onversleuteld.
Een eigen decoder is voor beide klein, is precies de zuivere functie die §5.2.1 vraagt, en
voegt geen afhankelijkheden toe (P3). Ruuvi publiceert officiële testvectoren, waarmee de
decoder exact te toetsen is. Victron-gegevens zijn versleuteld (AES) en kennen veel
recordtypes; daar is een bestaande bibliotheek de veiligere keuze.

**Verworpen alternatieven.**
- *`ruuvitag-sensor`.* Bevat een eigen scanner en extra afhankelijkheden voor functies die
  BRMS niet gebruikt.
- *`bthome-ble`.* Zelfde bezwaar; voor wat pvvx uitzendt is een eigen decoder eenvoudig.
- *Eigen Victron-decoder.* Versleuteling en veel recordtypes; meer risico dan nut.

**Specificatie.**
1. Principe: **eenvoudige, openbare formaten krijgen een eigen decoder; versleutelde of
   complexe formaten gebruiken een bibliotheek.**
2. **Ruuvi** (formaat 5) en **BTHome v2**: eigen decoder. De Ruuvi-decoder wordt getest met
   de officiële testvectoren van Ruuvi.
3. **Victron**: `victron-ble`, alleen als decoder (§5.2.2).
4. Waarden die een sensor als "ongeldig" uitzendt (bijv. luchtdruk bij een Ruuvi Pro
   3-in-1) worden niet gepubliceerd.
5. De BLE-reader houdt per Ruuvi de vorige tellerstand bij en publiceert de toename
   (§5.3.2); de ruwe tellerstand wordt niet gepubliceerd. De vorige stand staat alleen in
   het geheugen: bij de eerste ontvangst na een (her)start is de toename onbekend en wordt
   er geen `beweging` gepubliceerd (v0.19).

#### 5.2.5 Afgeleide grootheden (v0.31, slagzij in v0.66)

*Gebouwd in code v0.24.0 (`brms/afleiden.py`), aangeroepen door de BLE-reader. De
Shelly-reader roept de module nog niet aan: een Shelly levert geen invoer voor een
afleiding. Getest op de pc; nog niet met een echte Ruuvi aan boord.*

**Aanleiding.** Sommige gewenste waarden worden niet gemeten maar uit metingen berekend:
de slagzij van de boot uit de versnellingen van een Ruuvi, later bijvoorbeeld een
aantal Ah als tijdintegraal van een gemeten stroom. Daarvoor moest één vaste plek komen.

**Redenering.** Decoderen (bytes naar getallen) en afleiden (uit getallen nieuwe getallen
berekenen) zijn twee verschillende bezigheden en blijven gescheiden. Een afgeleide waarde
moet uit de **gecorrigeerde** meting komen: een correctie op de invoer moet in de
afgeleide waarde doorwerken. De plek is dus ná de correctie
en vóór het publiceren. Een afleiding hangt af van haar invoer, niet van een sensortype:
elke sensor die de invoer levert, kan de afgeleide waarde leveren, zonder de berekening te
kopiëren. Een afgeleide waarde is daarna een gewone meting: zelfde bus, opslag, alarmen en
grafieken.

*Slagzij (v0.66).* Een Ruuvi die stil ligt meet alleen de zwaartekracht, ongeveer 1000 mG,
verdeeld over zijn drie assen. Helt de boot, dan verschuift die over de assen: bij een
kleine hoek ongeveer 17 mG per graad. De Ruuvi levert de versnelling in stappen van 1 mG;
met de ruis en het temperatuurverloop van de sensor is de hoek op 1 à 2 graden
nauwkeurig. Dat is genoeg om te zien dat de boot scheef komt te liggen. Golven en schokken
vallen weg in het gemiddelde over 5 minuten (§5.3.2).

Gevraagd is alleen de slagzij (dwarsscheeps), niet de trim. De sensor wordt plat
gemonteerd, met één zijde in de lengterichting van de boot; op het oog recht is genoeg.
Welke as van de sensor dan dwars ligt en welke kant stuurboord is, hangt af van hoe hij
wordt neergelegd en is daarom een instelling. Die is zo gekozen dat de gebruiker de assen
van de sensor niet hoeft te kennen: hij kantelt de sensor naar stuurboord en kijkt welke
versnelling positief wordt.

De kalibratie is de gewone correctie (§5.4.2) op de afgeleide grootheid: een offset in
graden vangt in één keer de scheve montage en de afwijking van de sensor op.

De gebruiker rekent met stuurboord (sb) en bakboord (bb), niet met plus en min. Het is
toch één getal met een teken: dat geeft één lijn in de grafiek en één correctie. Het teken
is alleen in `brms.toml` te zien; overal waar de waarde getoond wordt staat sb of bb.

**Verworpen alternatieven.**
- *Eén hoek `helling`, ongeacht de richting (v0.31): de hoek tussen de z-as van de sensor
  en de zwaartekracht, met offsets op de versnellingen voor de montagestand.* Zegt niet
  naar welke kant de boot helt, en een offset op een versnelling is geen draaiing: het
  klopt alleen bij een platte montage en kleine hoeken.
- *De stand bij een rechte boot vastleggen als drie versnellingen en de hoek daarmee
  vergelijken.* Werkt in elke montagestand, maar geeft één hoek zonder richting en vraagt
  drie getallen in de configuratie waar één offset volstaat.
- *Slagzij en trim allebei.* Trim is niet gevraagd.
- *Twee grootheden, `slagzij_sb` en `slagzij_bb`, elk nul of positief.* Nergens een
  minteken, maar twee lijnen in de grafiek, en de correctie moet dan vóór het splitsen.
- *Eén vaste montagestand afspreken.* Bij het monteren moet dan op de stand van de sensor
  worden gelet, en de assen staan niet op de behuizing.
- *Een knop "nu op nul zetten" in de webinterface.* Een tweede plek voor een instelling
  naast `brms.toml`, voor iets dat één keer gebeurt.
- *Afleiden in de decoder.* Mengt twee bezigheden, en de decoder krijgt de correctie niet
  te zien.
- *De correctie naar de decoder verplaatsen, zodat die ook kan afleiden.* Dezelfde
  correctiecode in elke decoder, en decoders zijn niet meer te testen met alleen de
  officiële testvectoren (§5.2.1).
- *Een afleiding vastleggen per sensortype.* Dezelfde berekening komt dan terug bij elk
  type dat dezelfde invoer levert.
- *Afleiden in de opslag of de alarmlaag.* De waarde staat dan niet op de bus en is niet
  voor alle afnemers gelijk.

**Specificatie.**
1. Eén module **`brms/afleiden.py`**, los van de decoders. Elke reader roept hem aan met de
   gecorrigeerde waarden van één ontvangst en krijgt nul of meer afgeleide waarden terug.
2. Een **afleiding** is een regel in een tabel in die module: de afgeleide grootheid, de
   grootheden die ze als invoer nodig heeft, en de berekening.

   | Afgeleide grootheid | Invoer | Berekening |
   |---|---|---|
   | `slagzij` (graden) | de dwarsscheepse versnelling (`versnelling_x` of `versnelling_y`, punt 10) en `versnelling_z` | atan2(dwars, \|z\|), omgerekend naar graden; positief = stuurboord, negatief = bakboord |

3. Een sensortype **levert** een afgeleide grootheid als het alle invoer daarvoor levert
   (§5.4.2 punt 3). Dat volgt uit wat de decoder levert; `brms grootheden <type>`,
   `check-config` en de keuzelijst in de grafieken nemen het vanzelf mee. Heeft een
   afleiding een instelling nodig (`slagzij`, punt 10), dan levert een sensor haar alleen
   als die instelling er staat.
4. **Volgorde in de reader:** decoderen → corrigeren → afleiden → lijst `grootheden`
   toepassen → publiceren. De lijst `grootheden` komt dus ná het afleiden: `slagzij` kan
   worden opgeslagen zonder de versnellingen.
5. Ontbreekt een invoerwaarde (ongeldig, §5.2.3 punt 4), dan is er voor die ontvangst geen
   afgeleide waarde.
6. **Geheugen:** een afleiding over de tijd (een integraal) heeft de vorige ontvangst
   nodig. De reader houdt daarvoor per sensor een klein geheugen bij en geeft dat mee,
   zoals bij de bewegingsteller van de Ruuvi. Het staat alleen in het werkgeheugen en
   begint na een herstart opnieuw.
7. Een afgeleide grootheid staat in de tabel van grootheden (§5.3.3) met eenheid en
   soort. Op een doorlopende afgeleide grootheid is een eigen **correctie** mogelijk.
8. Een **nieuwe afleiding** is een codewijziging: een regel in `brms/grootheden.py`, een
   regel in `brms/afleiden.py`, en de tabellen in dit document.
9. Afgeleide waarden worden **niet apart gesimuleerd** (§10.3): ze volgen uit de
   gesimuleerde meting. In de **trace** (§10.2) krijgen ze een eigen stap `afgeleid`.
10. **Slagzij (v0.66).**
    - *Montage:* de sensor ligt plat, met één zijde in de lengterichting van de boot.
    - *Onderste boven monteren* (bijvoorbeeld tegen een plafond) mag ook:
      - De hoek blijft kloppen. `versnelling_z` wordt dan negatief (ongeveer −1000 mG in
        plaats van +1000), maar de berekening gebruikt alleen de grootte van z.
      - De kant kan omdraaien: door het omkeren kan de dwarsas van de sensor naar de
        andere kant van de boot wijzen. Het veld `stuurboord` vangt dat op, mits de
        kanteltest hieronder gedaan wordt in de stand waarin de sensor uiteindelijk
        hangt. Was het rechtop `"y"`, dan kan het onderste boven `"-y"` zijn.
      - Wordt een al ingestelde sensor later omgedraaid of verplaatst, dan moeten de
        kanteltest (`stuurboord`) en de kalibratie (correctie op `slagzij`) opnieuw.
    - *Instelling:* per sensor het veld **`stuurboord`** in `brms.toml`, met als waarde
      `"x"`, `"-x"`, `"y"` of `"-y"`: de versnelling die **positief** wordt als de boot
      over stuurboord helt. Zo vind je de waarde (de kanteltest): houd de sensor in de
      stand waarin hij aan boord komt, dus ook onderste boven als hij zo gemonteerd
      wordt, kantel hem naar stuurboord (de stuurboordkant omlaag) en kijk op de
      webpagina welke van `versnelling_x` en `versnelling_y` duidelijk verandert. Wordt
      die positief, dan is het `"x"` of `"y"`; wordt die negatief, dan `"-x"` of `"-y"`.
    - Zonder het veld `stuurboord` levert de sensor geen `slagzij`. `check-config` meldt
      een andere waarde dan de vier genoemde, het veld bij een sensor die geen
      versnellingen levert, en `slagzij` in `grootheden`, `correctie` of een alarmregel
      bij een sensor zonder het veld.
    - *Kalibratie:* met de boot recht de waarde van `slagzij` aflezen en het
      tegengestelde als correctie invullen. Staat er 1,8° sb, dan wordt het
      `correctie = { slagzij = -1.8 }`; staat er 1,8° bb, dan `slagzij = 1.8`. Na
      verplaatsen van de sensor opnieuw.
    - *Tonen:* op de webpagina, in meldingen en in het logboek staat de waarde zonder
      teken, met `sb` of `bb` erachter: "3° sb", "1° bb". In een grafiek staan de
      waarden langs de as op dezelfde manier. Het teken staat alleen in `brms.toml`
      (correctie en alarmgrenzen) en in de database.
    - *Hele graden:* een gemeten slagzij staat overal in hele graden: op de webpagina's
      sinds v0.88 (§5.3.3), in de meldingen (Telegram, e-mail) en op de pagina Alarmen
      sinds v0.89. De hoek is op 1 à 2 graden nauwkeurig; een decimaal suggereert meer
      dan de sensor kan. Een half rondt naar boven af; afgerond nul staat er als "0°",
      zonder kant. Een **alarmgrens** in een melding ("boven 4,5° sb") staat er zoals hij
      in `brms.toml` staat: afronden zou een andere grens tonen dan waarop het alarm
      afgaat.
    - *Alarm:* twee regels van de soort `grens` (§5.5.3), één per kant. De grenzen
      hieronder zijn een voorbeeld.

    ```toml
    [[sensor]]
    id = "C1:00:00:00:00:01"
    type = "ruuvi"
    naam = "hoofdkajuit"
    stuurboord = "-y"
    correctie = { slagzij = -1.8 }

    [[alarm]]
    id = "slagzij-sb"
    naam = "Slagzij stuurboord"
    sensoren = ["hoofdkajuit"]
    grootheid = "slagzij"
    soort = "grens"
    boven = 5.0
    duur = "15m"
    herstel = 4.0

    [[alarm]]
    id = "slagzij-bb"
    naam = "Slagzij bakboord"
    sensoren = ["hoofdkajuit"]
    grootheid = "slagzij"
    soort = "grens"
    onder = -5.0
    duur = "15m"
    herstel = -4.0
    ```

#### 5.2.6 Relais (v0.31, uitgewerkt in v0.63)

*Gebouwd in code v0.27.0 (v0.79); beproefd met tests en met het antwoord van de echte
plug, nog niet als geheel op de Pi. Model (v0.63): Shelly Plug M Gen3, een tussenstekker met
energiemeting (3000 W weerstandsbelasting, 13 A, −20 tot +40 °C). De API is met de plug
zelf gecontroleerd (v0.78), zie "Bevindingen met de plug" onder de specificatie.*

**Aanleiding.** Een alarm moet een apparaat kunnen schakelen (§5.5.3), als eerste een
elektrische kachel van 1500 W bij vorstgevaar; later mogelijk boordverlichting.

**Redenering.** Een kachel vraagt een schakelaar voor 230 V en een hoge stroom. Dat is
een ander apparaat dan de Shelly Plus Uni van de bilge (§5.2.4), waarvan de uitgangen
daarvoor niet bedoeld zijn. Een Shelly-schakelaar heeft dezelfde lokale HTTP-API en past
in de bestaande Shelly-reader; een tussenstekker vraagt bovendien geen montage. Hij wordt
via wifi uitgelezen en geschakeld, om dezelfde reden als de bilge-Shelly (§5.2.4).

*Eén regel als thermostaat (v0.63).* De kachel gaat aan en uit met één alarmregel: aan
als het alarm afgaat (temperatuur onder de grens), uit bij herstel (boven de
herstelgrens). De twee grenzen van de regel zijn zo de schakelpunten van een thermostaat.
Een tweede regel die bij een hogere temperatuur "uit" geeft, is niet nodig en zou de
eerste kunnen tegenspreken.

*Beveiliging in de Shelly.* Een kachel die aan blijft staan als BRMS vastloopt, is een
risico; de beveiliging hoort daarom in de Shelly zelf te zitten en niet in BRMS. "Aan"
geldt daarom maar kort, en BRMS moet het blijven herhalen.

*Alleen met walstroom (v0.63).* De kachel mag alleen aan als er walstroom is. Normaal zit
hij op een stopcontact buiten de MultiPlus om en valt hij zonder walstroom vanzelf uit;
maar op een stopcontact achter de omvormer zou hij de accu in korte tijd leegstoken. BRMS
gaat daar niet van uit en controleert het zelf, altijd.

*Vermogen meten (v0.63).* De plug meet vermogen, spanning en stroom. Daarmee is te zien
of de kachel echt stookt: een relais dat aan staat zegt niet dat de kachel het doet (eigen
thermostaat, omgevallen, defect).

**Verworpen alternatieven.**
- *De uitgangen van de Shelly Plus Uni gebruiken.* Niet geschikt voor 230 V en de stroom
  van een kachel.
- *Uitschakelen alleen door BRMS.* Valt BRMS weg, dan blijft het relais aan.
- *Een aparte alarmregel die het relais uitzet bij een hogere temperatuur.* Zie de
  redenering: één regel met een grens en een herstelgrens doet hetzelfde.
- *De walstroom als gewone voorwaarde in de alarmregel.* Dan zou het vorstalarm zelf
  stoppen als de walstroom wegvalt, juist op het moment dat de melding ertoe doet. De
  voorwaarde hoort bij de actie, niet bij het alarm.
- *Uitlezen of schakelen via Bluetooth.* Zie §5.2.4.
- *De energieteller van de plug opslaan (OPEN-27).* Een teller past niet bij de soorten
  van §5.3.2 en voegt niets toe aan `vermogen`.

**Specificatie.**
1. Sensortype **`shelly-relais`**: een aparte Shelly-schakelaar met de Gen2-API en één
   uitgang `switch:0`; het model is de Shelly Plug M Gen3. Configuratie als bij
   `shelly-uni` (§5.2.4 punt 4): `id`, `naam`, `adres`, `interval` (standaard 30 s).
2. De Shelly-reader **pollt** het relais; de stand en de metingen komen als gewone
   metingen binnen:

   | Grootheid | Eenheid | Betekenis |
   |---|---|---|
   | `relais` | 0/1 | 1 = relais aan |
   | `vermogen` | W | wat de kachel nu opneemt (nieuwe grootheid, §5.3.3) |
   | `spanning` | V | de netspanning op de plug |
   | `stroom` | A | de stroom door de plug |
   | `walstroom` | 0/1 | afgeleid: `spanning` > 180 V = 1 (punt 5, v0.80) |
   | `rssi` | dBm | signaalsterkte van de wifi |

   De **energieteller** van de plug (totaal verbruik in Wh) wordt niet opgeslagen
   (besluit OPEN-27, v0.64): het is een teller en geen meetwaarde, en het verbruik is
   achteraf uit `vermogen` te berekenen.
3. **Schakelen:** opdracht `brms/opdracht/relais/zet` met
   `{"sensor_id": ..., "aan": true, "duur": 120}`. De Shelly-reader voert hem uit via de
   API van de Shelly (de uitschakeltijd gaat met de opdracht mee) en pollt direct daarna,
   zodat de nieuwe stand meteen op de bus staat. Kan de plug geen uitschakeltijd per
   opdracht aan, dan wordt dezelfde tijd vast in de Shelly ingesteld.
4. **Beveiliging:**
   - "aan" gaat altijd met een **uitschakeltijd** (`duur`, **2 minuten**) die de Shelly
     zelf bewaakt: na die tijd schakelt hij vanzelf uit.
   - Zolang het alarm actief is, **herhaalt** de alarmlaag de opdracht elke **minuut**.
     Valt BRMS weg, dan gaat het relais binnen de uitschakeltijd uit. Eén gemiste
     herhaling geeft nog geen onderbreking.
   - Deze tijden waren eerst 10 en 5 minuten (v0.63); ingekort bij besluit OPEN-36
     (v0.87), omdat de kachel te lang doorstookte als BRMS wegviel. Een voorwaarde
     (punt 5) moet nu een waarde hebben die jonger is dan 2 minuten; met een poll van
     30 seconden is dat ruim genoeg.
   - De alarmlaag herhaalt alleen zolang de sensor van de alarmregel nog waarden levert
     (laatste waarde jonger dan de uitschakeltijd). Bij een **stille sensor** gaat het
     relais dus uit.
   - Bij herstel van het alarm, en als de regel wordt uitgezet (§5.5.3), volgt direct "uit".
   - In de Shelly staat ingesteld dat hij **uit** begint als de spanning terugkomt; BRMS
     zet hem daarna zelf weer aan als dat nodig is.
5. **Alleen met walstroom** (v0.63). De actie `relais` zet het relais alleen aan, en
   herhaalt "aan" alleen, zolang `walstroom` = 1 (MultiPlus, §5.2.2) en die waarde jonger
   is dan de uitschakeltijd.
   - Valt de walstroom weg terwijl het relais aan staat, dan volgt direct "uit". Komt de
     walstroom terug en is het alarm nog actief, dan gaat het relais weer aan.
   - Is de walstroom **onbekend** (geen sensor die `walstroom` levert, of die sensor is
     stil), dan blijft het relais uit. `check-config` weigert een actie `relais` als geen
     sensor in de configuratie `walstroom` levert.
   - Het alarm zelf en zijn melding hangen niet van de walstroom af: het vorstalarm blijft
     actief en wordt gemeld, ook als de kachel niet aan mag.
   **Herzien in v0.80 en zo gebouwd in code v0.27.0 (v0.81): de voorwaarde staat bij het
   relais zelf. Wat hierboven over de vaste bron MultiPlus staat, geldt niet meer.**

   *Aanleiding.* De plug zit op de walstroom en meet de netspanning: hij is zelf ook een
   bron voor de walstroom (OPEN-33). De MultiPlus-dongle is er nog niet, en welke bron
   geldt, hangt af van waar de plug zit; dat weet de gebruiker, niet BRMS.

   *Verworpen.* (a) `walstroom` als algemene afleiding uit `spanning` (§5.2.5): dan zou
   ook de accumonitor, die `spanning` levert, walstroom melden. (b) Vast de MultiPlus als
   bron, zoals in v0.63: werkt niet zonder dongle en laat de gebruiker geen keus.

   - **Elke plug levert `walstroom`** (0/1), afgeleid van zijn eigen spanning:
     `spanning` > 180 V = 1, anders 0. De Shelly-reader rekent dit uit, met de
     gecorrigeerde spanning, alleen voor het type `shelly-relais`. Het is daarna een
     gewone grootheid (grafiek, alarm, bootpagina). Zonder walstroom heeft de plug geen
     voeding en meldt hij niets; dat is "onbekend", geen 0.
   - **Veld `voorwaarde`** bij de sensor van het type `shelly-relais`: wat moet gelden om
     het relais aan te zetten, als `<sensor>.<grootheid> = <waarde>`:

     ```toml
     [[sensor]]
     id = "kachel"
     type = "shelly-relais"
     naam = "kachelplug"
     adres = "192.168.1.121"
     voorwaarde = { multiplus.walstroom = 1 }     # of: { kachelplug.walstroom = 1 }
     ```
     De sensor staat er op naam of ID (een MAC-adres tussen aanhalingstekens). Meer
     regels in de tabel moeten allemaal gelden. Alleen toestanden (0/1).
   - De alarmlaag zet het relais alleen aan, en herhaalt "aan" alleen, zolang elke
     voorwaarde geldt met een waarde jonger dan de uitschakeltijd. Geldt er één niet
     meer, dan volgt direct "uit"; een **onbekende** waarde (sensor stil) telt als niet
     voldaan. Het alarm zelf en zijn melding hangen er niet van af.
   - De voorwaarde geldt voor het relais, dus voor elke alarmregel die het schakelt.
   - `check-config` weigert een voorwaarde met een onbekende sensor, een grootheid die
     die sensor niet levert, of een waarde anders dan 0 of 1.
   - De controle "geen sensor levert `walstroom`" vervalt. `voorwaarde` is **verplicht**
     bij een relais (besluit OPEN-34, v0.81): de kachel kan nooit aan zonder dat er iets
     over de walstroom is vastgelegd.
   - Zolang de MultiPlus-dongle er niet is, meldt een regel van soort `stil` op de plug
     dat de walstroom is weggevallen (de plug heeft dan geen voeding en zwijgt).
6. Na een **herstart** van de alarmlaag is er geen actief alarm (§5.5.2 punt 6). De
   alarmlaag geeft bij zijn start **"uit" aan elk relais** (besluit OPEN-36, v0.87); het
   relais gaat weer aan zodra het alarm opnieuw afgaat.
   - *Aanleiding.* Zonder die opdracht bleef de kachel na een herstart aan tot de Shelly
     zelf uitschakelde, terwijl het alarm al van de pagina's was.
   - **De Shelly-reader vraagt om de stand (v0.88).** Bij een herstart van alle services
     is de alarmlaag een seconde eerder klaar dan de Shelly-reader, die de "uit" dan
     mist. Daarom vraagt de Shelly-reader bij zijn eigen start om de stand
     (`brms/opdracht/relais/stand` met `{"ts"}`), en geeft de alarmlaag direct voor elk
     relais opnieuw de opdracht: "aan" met de uitschakeltijd, of "uit". Dezelfde aanpak
     als bij de opslag (§5.5.3 punt 8). Het relais gaat zo binnen een paar seconden na
     de herstart uit, in welke volgorde de services ook starten.
   - Herstart alleen de Shelly-reader terwijl een alarm actief is, dan hoort hij zo ook
     direct dat het relais aan moet blijven.
   - Mist de reader toch alles, dan schakelt de Shelly zelf uit na de uitschakeltijd.
   - *Afgewezen:* de "uit" bij de start na 15 seconden herhalen (zo gebouwd in v0.87).
     Op de Pi ging de plug daarmee pas na 16 seconden uit.
7. Handmatig schakelen via het web is niet voorzien.

**Bevindingen met de plug (v0.78).** De eerste plug (model `S3PL-30110EU`, firmware
1.8.99) hangt aan de wifi van de RUT241 met het vaste adres **192.168.1.121**. Vanaf de
Pi gecontroleerd, zonder belasting in de plug:
- *Namen in de API.* `Switch.GetStatus?id=0` geeft `output` (stand van het relais),
  `apower` (W), `voltage` (V) en `current` (A). Dat zijn de bronnen voor `relais`,
  `vermogen`, `spanning` en `stroom` van punt 2. Het antwoord bevat ook de energieteller
  (`aenergy`), de netfrequentie en de temperatuur in de plug; die worden niet opgeslagen.
- *Uitschakeltijd per opdracht* (punt 3). `Switch.Set?id=0&on=true&toggle_after=<seconden>`
  werkt: met 10 seconden ging het relais aan en na 10 seconden vanzelf weer uit (de
  status meldt dan `source` = `timer`). De uitweg van punt 3, een vaste tijd in de Shelly,
  is dus niet nodig.
- *Begintoestand* (punt 4). Stond op `match_input`; omgezet naar `off` met
  `Switch.SetConfig`. Dit moet bij elke nieuwe plug één keer gebeuren.
- De plug heeft geen wachtwoord op de API, net als de bilge-Shelly.
- *Tweede plug (v0.82).* Zelfde model en firmware, vast adres **192.168.1.122**. Op
  dezelfde manier gecontroleerd: de uitschakeltijd per opdracht werkt, en de begintoestand
  is van `match_input` naar `off` gezet.

**Invulling bij het bouwen (v0.79).**
- *Code.* De decoder en het adres van de schakelopdracht staan in
  `brms/readers/shelly_relais.py`; het pollen en schakelen in `brms/readers/shelly.py`; de
  thermostaat in `brms/alarms.py`. De alarmregel krijgt een veld `relais` (het ID van de
  plug), los van de acties met de camera: een regel met alleen het relais heeft geen
  camera nodig.
- *Schakelen.* De reader doet één poging per opdracht. Lukt "aan" niet, dan volgt de
  herhaling; lukt "uit" niet, dan schakelt de plug zelf uit als de uitschakeltijd om is.
  Een mislukte poging geeft een waarschuwing in het log. "Aan" zonder uitschakeltijd wordt
  geweigerd, ook als de opdracht van elders komt.
- *Meer sensoren in één regel.* Het relais staat aan zolang het alarm voor minstens één
  sensor van de regel actief is en die sensor nog waarden levert.
- *Log.* De alarmlaag meldt "relais aan", "relais uit" met de reden, en één keer waarom een
  relais uit blijft terwijl het alarm actief is (een voorwaarde geldt niet of is onbekend,
  de sensor van het alarm is stil). De herhaling van "aan" komt niet in het log.
- *Simulatie.* Het relais doet niet mee met `brms sim`: er is geen testreeks voor
  ontworpen. De regel die het schakelt, is wel te beproeven door de temperatuursensor te
  simuleren; de plug schakelt dan echt.
- *Bootpagina.* De plug is een gewone sensor: `kachel.relais` of `kachel.vermogen` kan in
  de tekening (§5.6).

### 5.3 Opslag

**Redenering.** Eén bestand, geen server, bestand tegen stroomuitval.
**Specificatie.** SQLite in WAL-modus. Tijdstempels in UTC.

#### 5.3.1 Datamodel (besluit OPEN-9)

**Aanleiding.** De keuze stond open tussen een tabel per meting en één generieke tabel.
Daarbij kwam de vraag of later ook camera-stills in diezelfde tabel opgeslagen kunnen
worden, wat een BLOB-kolom zou vereisen.

**Redenering.** Alle metingen – temperatuur, vocht, accu, walstroom, bilge – zijn één
getal op één tijdstip van één sensor. Aan/uit-toestanden (bilgevlotter, walstroom) passen
daarin als 0/1, waardoor ze op dezelfde manier opgeslagen, bewaakt en in grafieken getoond
worden. Eén generieke tabel betekent dat een nieuw sensortype geen schemawijziging vraagt (P8).

Camera-stills (§5.7) zijn in elk opzicht anders dan metingen:

| | Meting | Still |
|---|---|---|
| Grootte | ~30–40 bytes | ~200 KB – 1 MB |
| Aantal | duizenden per dag | enkele per dag / op aanvraag |
| Gebruik | grafieken, drempels, alarmen | bekijken |
| Bewaren | lang (trends over jaren) | waarschijnlijk kort (weken) |

Ter illustratie: één still per uur van 300 KB laat de database ~7 MB per dag groeien,
tegenover ~0,3–0,4 MB per dag voor alle metingen samen. De beelden zouden dan het
schrijfvolume naar flash (P7), de omvang van elke back-up over 4G en het opschonen van oude
data volledig bepalen, terwijl de metingen het eigenlijke doel van het systeem zijn.
Daarom krijgen stills een eigen opslag: als bestand op schijf, met alleen metadata in de
database.

*Aanvulling v0.5.* Naast foto's maakt de camera ook videoclips (§5.7, ~30–60 MB per clip).
Voor video's gelden dezelfde argumenten nog sterker. Foto's en video's verschillen alleen
in type, duur en bewaarbeleid, en delen daarom één tabel `opname`.

**Verworpen alternatieven.**
- *Tabel per meting (per grootheid of sensortype).* Elk nieuw sensortype vraagt een
  schemawijziging en eigen query's, terwijl de gegevens structureel gelijk zijn.
- *Stills als BLOB in dezelfde tabel (of in de database).*
  1. In WAL-modus wordt elk beeld twee keer naar flash geschreven (WAL, daarna checkpoint) – strijdig met P7.
  2. Verwijderen van oude beelden verkleint het databasebestand niet; krimpen kan alleen
     met `VACUUM`, dat de hele database herschrijft (flashslijtage, risico bij stroomuitval).
  3. De back-up van het databasebestand sleept alle beelden mee over 4G; losse bestanden
     zijn incrementeel te synchroniseren.
  4. In één tabel zou per rij óf `waarde` óf de BLOB leeg zijn; elke query moet filteren.
  SQLite zelf adviseert BLOBs groter dan ca. 100 KB als los bestand op te slaan.

**Specificatie.**

```sql
-- Alle numerieke metingen
CREATE TABLE meting (
    ts         INTEGER NOT NULL,  -- Unix-tijd UTC, seconden
    sensor_id  TEXT    NOT NULL,  -- vaste ID (bijv. MAC of 'shelly-bilge'), niet de naam
    grootheid  TEXT    NOT NULL,  -- 'temperatuur', 'vocht', 'soc', 'bilge', ...
    waarde     REAL    NOT NULL,  -- aan/uit als 0/1
    PRIMARY KEY (sensor_id, grootheid, ts)
) WITHOUT ROWID;

-- Camera-opnames: alleen metadata; foto (JPEG) of video staat als bestand op schijf
CREATE TABLE opname (
    ts          INTEGER NOT NULL, -- Unix-tijd UTC, seconden (start van de opname)
    camera_id   TEXT    NOT NULL,
    type        TEXT    NOT NULL, -- 'foto' of 'video'
    duur        INTEGER,          -- seconden; NULL bij foto
    bestand     TEXT    NOT NULL, -- relatief pad, bijv. 'opnames/2026/11/08/120000.jpg'
    bytes       INTEGER NOT NULL,
    aanleiding  TEXT    NOT NULL, -- 'interval', 'handmatig', 'alarm:beweging', ...
    PRIMARY KEY (camera_id, ts)
) WITHOUT ROWID;

-- Opgetreden alarmen (v0.31, §5.5.3): één rij per alarm, met begin en einde
CREATE TABLE alarm (
    alarm_id   TEXT    NOT NULL,  -- id van de alarmregel uit brms.toml, bijv. 'vorst'
    sensor_id  TEXT    NOT NULL,
    begin      INTEGER NOT NULL,  -- Unix-tijd UTC, seconden
    grootheid  TEXT,              -- NULL bij een regel van soort 'stil'
    waarde     REAL,              -- de waarde waarmee het alarm afging
    einde      INTEGER,           -- NULL zolang het alarm actief is
    reden      TEXT,              -- 'herstel', 'herstart' of 'uitgezet'
    PRIMARY KEY (alarm_id, sensor_id, begin)
) WITHOUT ROWID;

-- Alarmregels die via het web zijn uitgezet (v0.31, §5.5.3 punt 7)
CREATE TABLE alarm_uit (
    alarm_id   TEXT    PRIMARY KEY,
    sinds      INTEGER NOT NULL   -- Unix-tijd UTC, seconden
) WITHOUT ROWID;
```

- `ts` is een geheel getal: SQLite heeft geen datumtype; een getal is kleiner dan
  ISO-tekst en sorteert/vergelijkt sneller. Omzetten naar lokale tijd gebeurt pas bij weergave.
- De primaire sleutel `(sensor_id, grootheid, ts)` met `WITHOUT ROWID` slaat de data
  gesorteerd per reeks op: een grafiek over een periode is één aaneengesloten leesactie
  zonder extra index, en dubbele metingen zijn onmogelijk.
- `sensor_id` is een vaste ID; naam en locatie komen uit de configuratie (§5.4), zodat
  hernoemen de historie niet loskoppelt.
- Orde van grootte: ~8 sensoren × ~4 grootheden × elke 5 minuten ≈ 9.000 rijen/dag,
  ≈ 0,3–0,4 MB/dag, ≈ 150 MB/jaar (zie §5.3.2).

#### 5.3.2 Meetinterval en schrijffrequentie (besluit OPEN-10)

**Aanleiding.** Vastgesteld moest worden hoe vaak gemeten en opgeslagen wordt, mede met
het oog op flashslijtage (P7).

**Redenering.** De vraag bestaat uit drie onafhankelijke delen, die bewust losgekoppeld
blijven:

| | Wat | Bepaald door |
|---|---|---|
| Ontvangen | hoe vaak nieuwe data binnenkomt | de bron (BLE-sensor) of ons poll-interval (Shelly) |
| Alarmeren | hoe snel een alarm afgaat | de urgentie van het alarm |
| Opslaan | hoe vaak een waarde in de database komt | hoe snel de grootheid verandert |

*Ontvangen.* De bronnen leveren veel vaker dan nodig: pvvx-firmware zendt in de
standaardinstelling elke ~2,5 s een advertentie met een nieuwe meting elke ~10 s,
RuuviTag elke ~1–1,3 s, Victron Instant Readout ongeveer elke seconde. Elke ontvangen
waarde opslaan levert tienduizenden rijen per sensor per dag op zonder meerwaarde.

*Alarmeren.* Een bilgevlotter of wegvallende walstroom moet binnen seconden tot een
minuut gemeld worden, niet pas bij het volgende opslagmoment. Alarmen mogen daarom niet
afhangen van het opslaginterval.

*Opslaan.* Temperatuur, vocht en accu veranderen in minuten tot uren; 5 minuten geeft
voldoende detail voor grafieken en trends. Aan/uit-toestanden zijn gebeurtenissen: een
bilgepomp die 40 seconden loopt valt tussen twee opslagmomenten en zou bij periodiek
opslaan gemist worden.

*Twee soorten grootheden.* Een gemiddelde is zinvol voor doorlopende grootheden, maar
niet voor toestanden: een vlotter die halverwege een interval inschakelt zou de waarde 0,5
krijgen, wat geen bestaande toestand is. Daarom heeft elke grootheid een vaste soort met
een eigen opslagregel. De soort hoort bij de grootheid zelf en ligt daarom vast in de code
van de reader, niet in de configuratie: een configfout mag een toestand niet tot
gemiddelde maken.

*Beweging (v0.5).* De Ruuvi-bewegingsteller (één byte, loopt op bij versnelling boven
64 mG, vaste firmwaredrempel) loopt van 0 t/m 254 en springt dan terug naar 0; de waarde
255 betekent "ongeldig" (v0.19, volgens de specificatie van formaat 5). Een gemiddelde van
de ruwe teller is betekenisloos (253, 254, 0, 1 → ~127). Wat telt is de *toename*: die is per
ontvangen waarde te berekenen (rekening houdend met het terugspringen) en per interval op
te tellen. Daarvoor is een derde soort nodig. De versnelling X/Y/Z is doorlopend; haar
gemiddelde over 5 minuten geeft in feite de helling van de boot, de schokken zitten in de
toename.

*Ontbrekende data.* Een interval zonder ontvangen data levert geen rij op, en zeker geen 0:
bij walstroom betekent 0 "walstroom weg", wat iets anders is dan "geen bericht van de
MultiPlus" (§5.2.2).

*Flash.* Eén commit per opslagmoment (288 per dag) geeft inclusief WAL en checkpoint
ruwweg 5–10 MB schrijfvolume per dag, enkele GB per jaar – verwaarloosbaar voor de
flashdrive. Het OS zelf (logs, swap, caches) schrijft waarschijnlijk meer; dat is
[OPEN-21].

**Verworpen alternatieven.**
- *Elke ontvangen waarde opslaan.* Tienduizenden rijen per sensor per dag, zonder meerwaarde.
- *Laatste waarde per interval opslaan.* Eenvoudiger, maar één uitschieter bepaalt dan de
  opgeslagen waarde; het gemiddelde onderdrukt ruis.
- *Minimum, gemiddelde en maximum per interval.* Drie keer zoveel rijen. Niet nodig voor
  vorstbewaking, omdat het alarm elke ontvangen waarde beoordeelt.
- *Accu elke minuut.* Overwogen vanwege springende stroom bij verbruikers; niet nodig voor
  winterbewaking. Eén interval voor alles is eenvoudiger (P1).
- *Gemiddelde ook voor toestanden.* Levert betekenisloze tussenwaarden (0,5). Hoe lang een
  pomp liep is achteraf exact te berekenen uit de rijen bij verandering.
- *Rijen bufferen in het geheugen en minder vaak committen.* Bespaart verwaarloosbaar
  schrijfvolume, maar vergroot het dataverlies bij stroomuitval.
- *Ruwe bewegingsteller opslaan (laatste waarde per interval).* Bevat geen informatie die
  niet al in de toename zit, en vraagt een extra opslagregel (v0.5).

**Specificatie.**
1. Readers ontvangen zo vaak als de bron levert. Shelly's worden standaard elke **30 s**
   gepolld; de bilge-Shelly elke **2 s** (§5.2.4).
2. De alarmlaag beoordeelt **elke ontvangen waarde**, onafhankelijk van de opslag.
3. Elke grootheid heeft een vaste soort, vastgelegd in de reader:

   | Soort | Voorbeelden | Opslag |
   |---|---|---|
   | Doorlopend | temperatuur, vocht, spanning, stroom, SoC, RSSI, sensorbatterij, versnelling X/Y/Z | gemiddelde van de ontvangen waarden over **5 minuten** |
   | Toestand (0/1) | bilgevlotter, bilgepomp, walstroom aanwezig | nooit gemiddeld; een rij bij **elke verandering** met het exacte tijdstip, plus elke **5 minuten** de actuele toestand op dat moment |
   | Toename | beweging (toename van de Ruuvi-bewegingsteller) | **som** over 5 minuten |

   De ruwe bewegingsteller wordt niet opgeslagen; de BLE-reader levert de toename
   (§5.2.3 punt 5).

4. Een interval zonder ontvangen data levert geen rij op.
5. Elk opslagmoment is één commit; geen extra buffering. Bij stroomuitval gaat hooguit het
   lopende interval verloren.

#### 5.3.3 Grootheden (besluit OPEN-25)

**Aanleiding.** Berichten en database bevatten geen eenheid (§3.3); daarvoor moet per
grootheid één naam, één eenheid en één soort vastliggen.

**Redenering.** De tabel volgt uit wat de bronnen leveren. Namen zijn Nederlands, in kleine
letters, zonder eenheid in de naam. Enkele keuzes:
- *Sensorbatterij alleen als spanning.* Ruuvi levert alleen spanning; het percentage van
  pvvx is daaruit berekend. Eén grootheid maakt alle sensoren vergelijkbaar en één alarm
  "sensorbatterij laag" mogelijk.
- *`batterijspanning` naast `spanning`.* De sensorbatterij en de accu krijgen verschillende
  namen, zodat ze in configuratie en alarmen niet te verwarren zijn.
- *Luchtdruk.* Kost niets extra (de Ruuvi 4-in-1 zendt hem toch) en een snel dalende druk
  kondigt storm aan.
- *Resterende tijd.* Victron's schatting hoe lang de accu het nog houdt; tijdens laden
  levert de BMV geen waarde, en wordt er niets gepubliceerd.
- *MPPT minimaal.* Alleen vermogen van het paneel en laadstroom naar de accu.

**Verworpen alternatieven.**
- *Sensorbatterij als percentage.* Niet door alle sensoren geleverd.
- *Accuspanning van de MPPT's.* De BMV meet die al en is de referentie.
- *"Opbrengst vandaag" van de MPPT's.* Teller die elke nacht naar 0 gaat; een gemiddelde rond
  middernacht klopt niet. Achteraf te berekenen uit `zonnevermogen`.
- *Laadfase van de MPPT (bulk/absorptie/druppel).* Geen getal en geen 0/1; past niet in de
  soorten van §5.3.2.

**Specificatie.** Een reader kan alleen grootheden uit deze tabel publiceren. De tabel
staat op één plek in de code (module `grootheden`); dit document bevat een kopie.

**Decimalen (v0.61).** Elke grootheid heeft een vast aantal decimalen waarmee het web een
waarde toont (§5.6 punt 4, legenda en aanwijzer van de grafieken). Een vast aantal houdt
de getallen even breed, zodat de tekst niet verspringt. Het geldt alleen voor het tonen:
de opslag en de alarmen rekenen met de volle waarde, en de assen van een grafiek kiezen
hun eigen stappen.

| Decimalen | Grootheden |
|---|---|
| 0 | `luchtdruk`, toestanden 0/1 (`walstroom`, `bilge`, `bilgepomp`, `relais`), `versnelling_x/y/z`, `rssi`, `beweging`, `resterende_tijd`, `slagzij` (hele graden sinds v0.88, eerder 1 decimaal: de hoek is op 1 à 2 graden nauwkeurig) |
| 1 | `temperatuur`, `vocht`, `soc`, `zonnevermogen`, `vermogen`, `verbruikt` |
| 2 | `spanning`, `batterijspanning`, `stroom`, `laadstroom_zon` |

| Grootheid | Eenheid | Soort | Bron | Opmerking |
|---|---|---|---|---|
| `temperatuur` | °C | doorlopend | BTHome, Ruuvi | |
| `vocht` | % | doorlopend | BTHome, Ruuvi | relatieve luchtvochtigheid |
| `luchtdruk` | hPa | doorlopend | Ruuvi (4-in-1) | |
| `versnelling_x`, `versnelling_y`, `versnelling_z` | mG | doorlopend | Ruuvi | invoer voor `slagzij` |
| `slagzij` | graden | doorlopend | afgeleid (§5.2.5) | positief = stuurboord, negatief = bakboord; getoond als "3° sb" (v0.66, vervangt `helling`; hele graden sinds v0.88/v0.89) |
| `beweging` | aantal | toename | Ruuvi | toename van de bewegingsteller |
| `batterijspanning` | V | doorlopend | BTHome, Ruuvi | batterij van de sensor zelf |
| `rssi` | dBm | doorlopend | alle BLE-apparaten | signaalsterkte, gemeten door de scanner |
| `spanning` | V | doorlopend | BMV-700, Shelly-relais | accuspanning; bij het relais de netspanning (§5.2.6) |
| `stroom` | A | doorlopend | BMV-700, Shelly-relais | accu: negatief = ontladen; bij het relais de stroom door de plug |
| `vermogen` | W | doorlopend | Shelly-relais | wat het geschakelde apparaat opneemt (§5.2.6, v0.63) |
| `soc` | % | doorlopend | BMV-700 | laadtoestand |
| `verbruikt` | Ah | doorlopend | BMV-700 | verbruikt sinds vol |
| `resterende_tijd` | min | doorlopend | BMV-700 | geen waarde tijdens laden |
| `zonnevermogen` | W | doorlopend | MPPT (3×) | vermogen van het paneel |
| `laadstroom_zon` | A | doorlopend | MPPT (3×) | stroom van de MPPT naar de accu |
| `walstroom` | 0/1 | toestand | MultiPlus, Shelly-relais | 1 = AC-ingang aanwezig; bij het relais afgeleid van de netspanning op de plug (§5.2.6 punt 5) |
| `bilge` | 0/1 | toestand | Shelly Plus Uni | 1 = waterdetector actief; nu niet aangesloten (§5.2.4) |
| `bilgepomp` | 0/1 | toestand | Shelly Plus Uni | 1 = bilgepomp krijgt spanning (§5.2.4) |
| `relais` | 0/1 | toestand | Shelly-relais | 1 = relais aan (§5.2.6, v0.31) |

**[OPEN-11]** Bewaartermijn / downsampling van oude data.

### 5.4 Configuratie

**Redenering (v0.5).** Drempels en instellingen moeten zonder codewijziging aan te passen
zijn. Eén configuratiebestand naast de sensorconfig is de eenvoudigste plek (P1); instellen
via het web vraagt schrijfrechten, invoercontrole en een extra tabel.

**Verworpen alternatieven.** *Instellingen via de webinterface (opgeslagen in SQLite).*
Complexer; kan later alsnog. In v0.31 opnieuw overwogen voor de alarmregels en opnieuw
verworpen: een aan/uit-schakelaar per regel volstaat (§5.5.3).

**Specificatie.** Eén configuratiebestand, buiten git (bevat de Victron-sleutel), met:
- sensorconfiguratie (MAC → naam/locatie, welke grootheden, correcties; §5.4.2);
- alarmregels: grens, duur, hysterese, notificatie en acties (§5.5.3). Alleen of een
  regel aan of uit staat, is via het web te wijzigen en staat in de database;
- camera-instellingen: foto-interval, videoduur, bewaartermijn foto's, maximale ruimte
  voor video's (§5.7);
- back-up: of foto's en video's worden meegenomen (§7).

#### 5.4.1 Formaat, plaats en wijzigen (besluit OPEN-12)

**Aanleiding.** Het formaat van het configuratiebestand moest worden gekozen, en daarmee
waar het staat en hoe een wijziging wordt doorgevoerd.

**Redenering.** Het bestand wordt met de hand bewerkt via SSH en alleen door de services
gelezen – nooit door BRMS geschreven (§5.4). Het moet commentaar toelaten (uitleg bij
drempels), een lijst van sensoren met per sensor enkele velden kunnen bevatten, en bij
voorkeur zonder extra afhankelijkheid te lezen zijn (P3). TOML voldoet aan alle eisen en
wordt gelezen met `tomllib` uit de standaardbibliotheek (Python 3.11+; de Pi heeft 3.13).
Dat `tomllib` alleen kan lezen is geen bezwaar.

Een configuratie los van de code kan door een code-update nooit overschreven worden.
Herstarten van een service bij een wijziging is eenvoudiger dan inlezen tijdens het
draaien. Het risico daarvan is dat een tikfout een service – en daarmee bijvoorbeeld de
alarmen – laat uitvallen; daarom wordt de configuratie gecontroleerd vóór gebruik.

**Verworpen alternatieven.**
- *YAML.* Vereist PyYAML (P3); inspringen luistert nauw en waarden worden onverwacht
  omgezet (`no` → false, `0755` → octaal).
- *JSON.* Geen commentaar mogelijk; één vergeten komma maakt het bestand ongeldig.
- *INI (`configparser`).* Alles is tekst; lijsten van sensoren passen slecht.
- *Python-bestand.* Configuratie wordt code: een tikfout laat de service crashen, en het
  bestand kan alles uitvoeren.
- *Meerdere bestanden (per service).* Onnodig; elke service leest zijn eigen deel.
- *Inlezen tijdens het draaien (bijv. op signaal).* Complexer dan herstarten.

**Specificatie.**
1. Formaat **TOML**, gelezen met `tomllib`.
2. **Eén bestand** voor alle services: **`~/BRMS/config/brms.toml`** op de Pi (v0.15, §10.1), buiten git en
   los van de code. Elke service leest zijn eigen deel. Een voorbeeldbestand
   (`config/brms.example.toml`) staat wel in git.
3. Geheimen van rclone (OneDrive-token) staan niet in dit bestand maar in rclone's eigen
   configuratie.
4. Een wijziging wordt doorgevoerd door de betreffende service te **herstarten**
   (`systemctl restart`).
5. Elke service **controleert de configuratie bij het starten** en meldt fouten duidelijk
   in het log. Met **`brms check-config`** is de configuratie vooraf te controleren, vóór
   een herstart.

#### 5.4.2 Grootheden en correcties per sensor (v0.18)

**Aanleiding.** Een sensor kan meer grootheden leveren dan nodig (een Ruuvi zendt o.a.
luchtdruk en versnelling). Per sensor moest in te stellen zijn welke grootheden worden
meegenomen en welke correctie er geldt. Ook moest de gebruiker kunnen opzoeken welke
grootheden er zijn en welke een sensortype levert.

**Redenering.** Een lijst van wat je wél wilt is expliciet: na een firmware-update sluipt er
geen nieuwe grootheid ongemerkt in. Zonder lijst gaat alles door, zodat een eenvoudige
sensor geen extra regel nodig heeft. Wat wordt weggelaten, wordt direct in de reader
weggegooid: het komt niet op de bus, niet in de alarmlaag en niet in de database.

Een offset alleen is niet altijd genoeg: een vochtsensor kan bij hoge waarden meer afwijken
dan bij lage. Een lineaire correctie (factor en offset) dekt dat, en blijft eenvoudig.

Nieuwe grootheden kunnen bewust niet via de configuratie worden toegevoegd: een decoder
moet ze kunnen omzetten, en de soort bepaalt de opslagregel en hoort in de code (§5.3.2,
§5.3.3). Elke decoder legt vast welke grootheden hij levert; daarmee kan de configuratie
gecontroleerd worden en kan de gebruiker het opzoeken.

**Verworpen alternatieven.**
- *Lijst van wat níet mee moet.* Korter bij een Ruuvi, maar een nieuwe grootheid (bijv. na
  een firmware-update) gaat dan ongevraagd mee.
- *Alleen offset.* Dekt geen afwijking die met de waarde meeloopt.
- *Grootheden definiëren in de configuratie.* Zie redenering.

**Specificatie.**
1. Per sensor optioneel **`grootheden = [...]`**: alleen deze grootheden worden
   gepubliceerd (ook `rssi` moet er dan in staan als die gewenst is). Zonder lijst: alles.
   De lijst wordt toegepast ná het afleiden (§5.2.5): een afgeleide grootheid kan in de
   lijst staan zonder haar invoer.
2. **`correctie`** per doorlopende grootheid, als getal (offset) of als tabel met `factor`
   en/of `offset`: gecorrigeerd = gemeten × factor + offset (standaard factor 1, offset 0).
   ```toml
   correctie = { temperatuur = -0.3, vocht = { factor = 1.02, offset = -1.5 } }
   ```
3. Elke decoder legt vast **welke grootheden hij levert**; de reader voegt `rssi` toe,
   en de afgeleide grootheden waarvoor het type alle invoer levert (§5.2.5).
4. `brms check-config` meldt: onbekende grootheden, grootheden die het sensortype niet
   levert, correcties op niet-doorlopende of weggelaten grootheden, een factor van 0.
5. **`brms grootheden`** toont de tabel van grootheden (naam, eenheid, soort);
   **`brms grootheden <type>`** toont wat dat sensortype levert.
6. Een **nieuwe grootheid** toevoegen is een codewijziging: een regel in
   `brms/grootheden.py`, de decoder uitbreiden, en de tabel in §5.3.3 bijwerken.
7. In de trace (§10.2) toont de stap `gecorrigeerd` ook welke grootheden zijn weggelaten.

### 5.5 Alarmen en meldingen

**Redenering.** Het doel van het systeem (§1) is op tijd ingrijpen.
**Specificatie.** Meldingen via Telegram, met e-mail als tweede kanaal (§5.5.4). Alarmen:

| Alarm | Voorwaarde |
|---|---|
| Walstroom weg | `walstroom` = 0 (MultiPlus, §5.2.2) |
| Accu laag | SoC onder drempel, bijv. 50 % |
| Vocht | Luchtvochtigheid langdurig > 80 % |
| Vorst | Temperatuur < 2 °C |
| Bilge | Waterdetector actief; nu niet in gebruik, er is geen detector (§5.2.4) |
| Bilgepomp lang | Pomp draait aan één stuk langer dan drempel |
| Bilgepomp vaak | Aantal pompbeurten binnen een tijdvenster boven drempel |
| Sensor stil | Sensor levert geen data meer |
| Sensorbatterij laag | `batterijspanning` onder drempel |
| Beweging | Toename van de Ruuvi-bewegingsteller ≥ N binnen T seconden (instelbaar) |
| Slagzij (v0.66) | `slagzij` voorbij de drempel, een regel voor stuurboord en een voor bakboord (§5.2.5 punt 10); bedoeld met een video als actie |

Alle drempels en instellingen staan in het configuratiebestand (§5.4). Een alarm hoeft
niet tot een melding te leiden en kan een actie uitvoeren; zie §5.5.3.

#### 5.5.1 Bewegingsalarm (v0.5)

**Redenering.** Een Ruuvi meet beweging van de boot zelf (versnellingsmeter), niet in een
ruimte. Iemand die aan boord stapt laat de boot hellen; zware storm laat de boot langdurig
bewegen. In beide gevallen is een video van de gebeurtenis gewenst. De firmwaredrempel van
de teller (64 mG) is niet instelbaar en gevoelig; een enkele golf mag geen alarm geven.
Een aantal bewegingen binnen een tijdvenster onderscheidt een gebeurtenis van achtergrondonrust.
Zonder rustperiode zou storm de ene video na de andere opleveren.

**Verworpen alternatieven.**
- *PIR-bewegingsmelders.* Niet gewenst; de Ruuvi's zijn al aanwezig.
- *Eigen drempel op de momentane versnelling X/Y/Z.* Ca. één meting per seconde mist korte
  schokken; de teller wordt in de sensor zelf continu bewaakt.

**Specificatie.**
1. Het alarm gaat af bij een toename van de bewegingsteller van **≥ N binnen T seconden**
   (beide instelbaar, per sensor).
2. Bij het alarm geeft de alarmlaag de camera-reader een **poll-nu voor een video** (§5.7).
3. Na een video door dit alarm volgt **minstens 10 minuten** (instelbaar) geen nieuwe
   video door hetzelfde alarm.

#### 5.5.2 Alarmdetails (besluit OPEN-13)

**Aanleiding.** Open stond per alarm de exacte drempel, de duur ("langdurig"), de
hysterese, de herhaling van meldingen en de melding bij herstel.

**Redenering.** Eén werking voor alle alarmen houdt de alarmlaag klein (P1); alleen de
waarden verschillen. Een duur voorkomt meldingen bij een enkele afwijkende waarde; een aparte
herstel-drempel voorkomt pendelen rond de drempel. Omdat de boot onbeheerd is, gaat een
gemiste melding vóór een melding te veel: herinneringen zolang een alarm actief is, en na een
herstart opnieuw melden. Een melding bij herstel vertelt dat ingrijpen niet meer nodig is.
Uitblijven van data zegt niets over herstel. Niet elke sensor hoort bij elk alarm: de
buitensensor mag geen vorstalarm geven.

**Verworpen alternatieven.**
- *Alarm bevestigen (via web of in de melding) om herinneringen te stoppen.* Extra toestand en extra
  interface; een herinnering met lange tussenpoos is eenvoudiger.
- *Alarmtoestand bewaren over een herstart heen.* Extra opslag en randgevallen; een dubbele
  melding na een herstart is onschuldig.
- *Eén melding per alarm, zonder herinnering.* Eén gemiste of weggeveegde melding betekent
  dan dat het alarm onopgemerkt blijft.

**Specificatie.**
1. **Duur:** een alarm gaat af als de voorwaarde de ingestelde duur onafgebroken geldt; één
   waarde die niet aan de voorwaarde voldoet, zet de duur terug.
2. **Hysterese:** een actief alarm herstelt pas als de herstelvoorwaarde de ingestelde duur
   onafgebroken geldt.
3. **Herstel:** bij herstel volgt een melding met de duur van het alarm (bijv. "Walstroom
   terug (was 3 u 12 min weg)"). Niet bij beweging: dat is een gebeurtenis, geen toestand.
4. **Herhaling:** zolang een alarm actief is, volgt een herinnering met de ingestelde
   tussenpoos. Er is geen bevestiging.
5. **Geen data is geen herstel:** valt een sensor stil terwijl een alarm van die sensor
   actief is, dan blijft dat alarm actief; daarnaast geldt "sensor stil".
6. **Herstart:** de alarmtoestand wordt niet bewaard. Na een herstart begint elke duur
   opnieuw (monotone klok, §4.2); een nog bestaande afwijking geeft opnieuw een melding.
7. **Per sensor:** in de configuratie staat per sensor welke alarmen gelden; waarden zijn
   per alarm in te stellen en waar nodig per sensor te overschrijven (§5.4).
8. **Prioriteit** (uitwerking per kanaal in §5.5.4): bilge, bilgepomp lang en walstroom *urgent*; vorst, accu laag en
   bilgepomp vaak *hoog*; overige *normaal*.
9. **Standaardwaarden** (in te stellen in `brms.toml`):

   | Alarm | Voorwaarde | Duur | Herstel | Herhaling |
   |---|---|---|---|---|
   | Bilge | `bilge` = 1 | direct | `bilge` = 0 gedurende 5 min | elk uur |
   | Bilgepomp lang | `bilgepomp` = 1 | 3 min | `bilgepomp` = 0 | elk uur |
   | Bilgepomp vaak | ≥ 4 pompbeurten binnen 6 u | — | < 4 binnen 6 u | elke 12 u |
   | Walstroom weg | `walstroom` = 0 | 2 min | `walstroom` = 1 gedurende 2 min | elke 12 u |
   | Vorst | `temperatuur` < 2 °C | 15 min | ≥ 3 °C | elke 12 u |
   | Accu laag | `soc` < 50 % | 10 min | ≥ 55 % | elke 12 u |
   | Vocht | `vocht` > 80 % | 6 u | ≤ 75 % | elke 24 u |
   | Sensor stil | geen waarde | 30 min | eerste nieuwe waarde | elke 24 u |
   | Sensorbatterij laag | `batterijspanning` < 2,5 V | 1 u | ≥ 2,6 V | elke week |
   | Beweging | ≥ N binnen T s (§5.5.1) | — | — | rustperiode 10 min |

#### 5.5.3 Regels, notificaties, acties en de schakelaar (v0.31)

*Gebouwd: stap (a) van punt 11 (v0.33): regels, notificaties, de tabel `alarm` en de
grenzen in de grafieken; en met de camera (v0.40) de foto bij een notificatie (punt 5) en
de acties `video` en `foto` (punt 6); en de schakelaar (punt 7) en "camera stil" (punt 2)
in code v0.19.0; en de actie `relais` (punt 6, §5.2.6) in code v0.27.0.*

**Aanleiding.** Vastgelegd moest worden hoe een alarm wordt ingesteld, wat het teweegbrengt
(een notificatie, een actie, of geen van beide), hoe opgetreden alarmen bewaard en getoond
worden, en hoe een verkeerd ingesteld alarm snel te stoppen is.

**Redenering.** Een alarm, een notificatie en een actie zijn drie dingen. Een alarm stelt
vast dat een voorwaarde geldt; of daar een melding of een actie op volgt, is per regel in
te stellen. Zo is dezelfde werking bruikbaar voor een melding ("walstroom weg"), voor een
regeling zonder melding (verwarming aan bij vorst) en voor het vastleggen van een
gebeurtenis (video bij slagzij).

De regels blijven in `brms.toml` (§5.4): de enige reden om ze via het web te willen
wijzigen was het stoppen van een lawine aan notificaties door een verkeerde grens.
Daarvoor volstaat een aan/uit-schakelaar per regel; de waarden wijzigt de beheerder via
SSH. De stand van de schakelaar moet een herstart overleven (anders begint de lawine
opnieuw) en het web kan `brms.toml` niet schrijven; die ene stand staat daarom in de
database.

Opgetreden alarmen zijn geen metingen: ze hebben een begin en een einde. Een eigen tabel
maakt ze in grafieken en in een overzicht te tonen.

De alarmlaag bedient zelf geen hardware: een actie is een opdracht over de bus, die de
betreffende reader uitvoert. Zo blijft er per apparaat één component die ermee praat.

**Verworpen alternatieven.**
- *Regels bewerken via het web (opgeslagen in de database).* Formulieren, invoercontrole
  en een tweede plek voor instellingen; de schakelaar dekt de aanleiding.
- *De schakelaar dempt alleen de notificaties.* Een regel zonder notificatie is al in
  `brms.toml` in te stellen; een verkeerde grens kan ook een verkeerde actie geven.
- *Alarmen als meting opslaan (0/1 per regel).* Past niet bij "begin en einde", en de
  opslag per 5 minuten (§5.3.2) verliest het precieze tijdstip.
- *De alarmlaag schakelt zelf (HTTP naar de Shelly, opname van de camera).* Twee
  componenten die met hetzelfde apparaat praten.

**Specificatie.**
1. Een **alarmregel** is een blok `[[alarm]]` in `brms.toml`:

   | Veld | Betekenis |
   |---|---|
   | `id` | vaste naam van de regel (staat in de database), bijv. `vorst` |
   | `naam` | tekst voor meldingen en weergave |
   | `sensoren` | lijst van sensoren (naam of ID) waarvoor de regel geldt; elke sensor wordt apart beoordeeld |
   | `grootheid` | de grootheid die beoordeeld wordt (ook een afgeleide, §5.2.5) |
   | `soort` | `grens`, `aantal` of `stil` (punt 2) |
   | `boven` of `onder` | bij `grens`: de grenswaarde |
   | `duur` | hoe lang de voorwaarde onafgebroken moet gelden (§5.5.2 punt 1) |
   | `herstel` | bij `grens`: de herstelgrens; het verschil met de grens is de hysterese |
   | `herstelduur` | hoe lang de herstelvoorwaarde moet gelden (§5.5.2 punt 2) |
   | `aantal`, `venster` | bij `aantal`: het aantal en het tijdvenster |
   | `melding` | `uit`, `eenmalig` of `herhaald` (punt 4). Zonder dit veld: `herhaald` als er een `herhaling` staat, anders `eenmalig` |
   | `herhaling` | bij `herhaald`: de tussenpoos (minstens 1 minuut) |
   | `herstelmelding` | of bij herstel een melding volgt (standaard ja, §5.5.2 punt 3) |
   | `prioriteit` | `urgent`, `hoog` of `normaal` (§5.5.2 punt 8, §5.5.4) |
   | `foto` | `geen`, `begin` of `elke` (punt 5) |
   | `actie` | lijst van acties (punt 6) |
   | `camera` | het ID van de camera voor `foto` en `actie`; zonder dit veld de eerste camera uit de configuratie (v0.40) |

   Tijden worden geschreven als getal met eenheid: `"90s"`, `"15m"`, `"6u"`, `"7d"`.
   Een andere waarde voor een andere sensor is een tweede blok met een eigen `id`.

   ```toml
   [[alarm]]
   id = "vorst"
   naam = "Vorst"
   sensoren = ["kajuit", "vooronder", "motorruim"]
   grootheid = "temperatuur"
   soort = "grens"
   onder = 2.0
   duur = "15m"
   herstel = 3.0
   melding = "herhaald"
   herhaling = "12u"
   prioriteit = "hoog"
   actie = [{ type = "relais", relais = "verwarming" }]
   ```
   Er zijn geen ingebouwde regels: alleen wat in `brms.toml` staat, wordt bewaakt. Het
   voorbeeldbestand bevat de regels van §5.5.2 punt 9. `check-config` controleert de
   regels en meldt hoeveel er zijn.
2. **Soorten regel:**
   - `grens`: de waarde ligt boven of onder een grens. Voor een toestand (0/1) is dat
     "= 1" (`boven = 0.5`) of "= 0" (`onder = 0.5`).
   - `aantal`: binnen het tijdvenster is het aantal bereikt. Bij een toename (`beweging`)
     telt de som; bij een toestand (`bilgepomp`) het aantal overgangen 0 → 1.
   - `stil`: de sensor levert de ingestelde duur geen enkele waarde.

   **Camera stil** (v0.51; gebouwd in code v0.19.0). Een camera is geen sensor en levert geen
   metingen; uitval van de camera gaf daardoor alleen een regel in het log (§5.7 punt
   13). In `sensoren` van een regel met `soort = "stil"` mag daarom ook het ID van een
   **camera** staan. Voor een camera telt elke **geslaagde opname** (het bericht op
   `brms/opname/<camera_id>`, §5.7 punt 13) als ontvangen waarde. De regel gaat dus af
   als de camera onbereikbaar is, en ook als de camera-service zelf niet meer werkt.
   Verder is het een gewone regel: melding, herstelmelding, tabel `alarm`, schakelaar.
   `check-config` weigert een camera in een regel van een andere soort, en een `duur`
   die niet langer is dan het foto-interval van die camera.

   ```toml
   [[alarm]]
   id = "camera_stil"
   naam = "Camera stil"
   sensoren = ["camera1"]
   soort = "stil"
   duur = "15m"
   melding = "herhaald"
   herhaling = "24u"
   ```
   Voor de Shelly is niets extra nodig: dat is een sensor, en een onbereikbare Shelly
   levert geen waarden meer (gewone `stil`-regel).

   *Verworpen (v0.51):* een eigen grootheid `bereikbaar` (0/1) voor camera en Shelly.
   Een nieuwe grootheid naast een afspraak die er al is; en zonder waarden (reader
   vastgelopen) gaat een `grens`-regel niet af.
3. De **werking** per regel en per sensor is die van §5.5.2: duur, herstel, geen data is
   geen herstel, en geen toestand over een herstart. Een grens wordt alleen bij een nieuwe
   waarde beoordeeld (v0.33): zonder data gaat een alarm niet af en herstelt het niet. Een
   regel van soort `aantal` gebruikt de `herstelduur` als rustperiode (§5.5.1 punt 3): het
   alarm herstelt pas als de telling zo lang onder het aantal is gebleven.
4. **Notificatie** (via de meldingskanalen van §5.5.4):
   - `uit`: het alarm wordt vastgelegd en voert zijn acties uit, zonder melding;
   - `eenmalig`: één melding als het alarm afgaat;
   - `herhaald`: daarna een herinnering met de ingestelde tussenpoos zolang het alarm
     actief is.

   Bij herstel volgt een herstelmelding, tenzij die is uitgezet of `melding = "uit"`.
5. **Foto bij een notificatie:** bij `foto = "begin"` vraagt de alarmlaag de camera-reader
   om een foto (poll-nu, §5.7) op het moment dat het alarm afgaat, wacht kort op de opname
   en stuurt die mee met de melding; bij `"elke"` ook bij elke herinnering. Komt de foto
   niet op tijd, dan gaat de melding zonder foto. De foto wordt een gewone opname met het
   alarm als aanleiding, tussen de intervalfoto's, met dezelfde bewaartermijn.
6. **Acties** zijn opdrachten over de bus (§3.3):

   | Actie | Wat er gebeurt |
   |---|---|
   | `{ type = "video" }` | poll-nu voor een video als het alarm afgaat (§5.5.1, §5.7) |
   | `{ type = "foto" }` | poll-nu voor een foto als het alarm afgaat, los van de notificatie |
   | `{ type = "relais", relais = "<sensor>" }` | relais aan als het alarm afgaat, uit bij herstel; alleen aan zolang de `voorwaarde` van het relais geldt (§5.2.6 punt 5) |

   Een regel kan nul of meer acties hebben. Een opdracht aan de MultiPlus is niet
   voorzien: Instant Readout is alleen lezen (§5.2.2). Een actie wordt alleen uitgevoerd
   als het alarm afgaat (niet bij een herinnering of bij herstel), ook als de regel
   `melding = "uit"` heeft (v0.40).
7. **Schakelaar:** in het web is elke regel **aan of uit** te zetten (§5.6).
   - Uit geldt voor de **hele regel**: geen beoordeling, geen notificatie, geen actie. Een
     alarm dat op dat moment actief is, wordt afgesloten en een relais dat door de regel
     aan staat, gaat uit.
   - De stand staat in de database (tabel `alarm_uit`, §5.3.1) en overleeft een herstart.
     Het is de enige instelling buiten `brms.toml`.
   - Een alarm dat door het uitzetten wordt afgesloten, krijgt in de tabel `alarm` de
     reden `uitgezet`; er volgt geen herstelmelding (er is niets hersteld). Gaat de regel
     weer aan, dan begint de beoordeling opnieuw, zoals na een herstart.
   - De alarmlaag leest de tabel daarna elke minuut opnieuw (v0.52): is een opdracht de
     alarmlaag of de opslag ontgaan, dan volgt de alarmlaag alsnog de database.
   - Het web geeft de opdracht `brms/opdracht/alarm/schakel` met
     `{"alarm_id": ..., "aan": false}`. De opslag-service schrijft de stand weg (§3.1); de
     alarmlaag voert de opdracht direct uit en leest bij zijn start de tabel
     (alleen-lezen).
8. **Opgetreden alarmen** staan in de tabel `alarm` (§5.3.1): één rij per alarm, met begin,
   einde en de reden van het einde. De alarmlaag publiceert begin en einde op
   `brms/alarm/<alarm_id>` (§3.3); de opslag-service schrijft. Bij zijn start en daarna
   elke minuut meldt de alarmlaag welke alarmen actief zijn (v0.34); de opslag sluit de
   rijen die open staan en daar niet bij zijn, met reden `herstart` (§5.5.2 punt 6).
   Eén melding bij de start bleek niet genoeg: na een herstart van de Pi starten beide
   services tegelijk en mist de opslag het bericht, en het alarm bleef dan open staan.
   - **De opslag vraagt om de stand (v0.86).** Met alleen de melding per minuut bleven
     de alarmen van vóór een herstart nog een minuut open staan, en zo lang ook op de
     webpagina's: bij `brms herstart` meldt de alarmlaag zijn stand net voordat de opslag
     luistert. Daarom vraagt de opslag bij zijn eigen start om de stand
     (`brms/opdracht/alarm/stand`, §3.3) en antwoordt de alarmlaag direct. De oude
     alarmen zijn dan binnen een paar seconden afgesloten, in welke volgorde de services
     ook starten. De melding per minuut blijft als vangnet.
   - *Afgewezen:* de alarmlaag meldt de eerste minuut na zijn start elke 5 seconden.
     Minder code, maar het helpt niet als alleen de opslag herstart.
9. **In de grafieken** (§5.6): bij een reeks waarvoor een `grens`-regel geldt, staat de
   grens als horizontale lijn in de grafiek en de herstelgrens als dunnere lijn. De
   perioden waarin het alarm actief was, zijn gearceerd.
10. De alarmlaag is een eigen service, **`brms-alarm`** (§3.1). Hij abonneert zich op alle
    metingen, verstuurt de notificaties en geeft de opdrachten.
11. **Bouwvolgorde:** (a) regels, notificaties, tabel `alarm` en de grenzen in de
    grafieken; (b) de schakelaar in het web; (c) het relais (§5.2.6). Foto en video volgen
    met de camera (§5.7).

#### 5.5.4 Meldingskanalen (besluit OPEN-14, v0.32)

*Gebouwd (v0.33), behalve de foto als bijlage. Nog niet getest met een echte Telegram-bot
of een echt mailaccount.*

**Aanleiding.** Open stond welke ntfy-server de meldingen verstuurt (OPEN-14). Daarbij kwam
de vraag of een melding niet via Telegram, WhatsApp of e-mail kan.

**Redenering.** Een melding moet opvallen op de telefoon, een foto kunnen meenemen (§5.5.3
punt 5), privé zijn, en niets vragen dat gehost of onderhouden moet worden (P1, P3).
Telegram voldoet daaraan: een eigen bot stuurt tekst, foto's en video's met een gewone
HTTPS-aanvraag, gratis, en de berichten blijven als gesprek bewaard. E-mail is trager en
valt minder op, maar loopt via een heel andere dienst: als tweede kanaal vangt het een
storing of blokkade van het eerste op. Beide zijn met de standaardbibliotheek te
versturen.

**Verworpen alternatieven.**
- *ntfy (publieke server ntfy.sh).* Het topic is het enige geheim, en bijlagen zijn op de
  gratis dienst beperkt. Wel prioriteiten die door "niet storen" heen breken; dat heeft
  Telegram niet.
- *Een eigen ntfy-server.* Een extra onderdeel dat de telefoon altijd moet kunnen bereiken.
- *WhatsApp.* Alleen via het zakelijke platform van Meta (bedrijfsaccount, goedgekeurde
  sjablonen, kosten per bericht); onofficiële omwegen zijn in strijd met de voorwaarden.
- *Alleen e-mail.* Te traag en te onopvallend voor een dringend alarm.

**Specificatie.**
1. Meldingen gaan via **Telegram** (eerste kanaal) en **e-mail** (tweede kanaal). Elke
   notificatie gaat naar elk kanaal dat in `brms.toml` is ingesteld; mislukt het ene, dan
   gaat het andere gewoon door.
2. **Telegram:** een eigen bot (aangemaakt in de Telegram-app). In `brms.toml` staan de
   sleutel van de bot en het gesprek waarheen de meldingen gaan. BRMS verstuurt tekst en
   foto via de HTTPS-API van Telegram, met `urllib` (§5.2.1).
3. **E-mail:** via een mailaccount met SMTP, met `smtplib` uit de standaardbibliotheek. In
   `brms.toml` staan server, poort, gebruikersnaam, wachtwoord (een app-wachtwoord),
   afzender en ontvanger(s). Een foto gaat mee als bijlage.
4. De **geheimen** (sleutel van de bot, wachtwoord van de mail) staan alleen in
   `brms.toml`, dat buiten git blijft (§5.4.1), en komen nooit in het log, een
   foutmelding of de trace.
5. **Prioriteit** (§5.5.2 punt 8): *urgent* en *hoog* komen in Telegram binnen met geluid,
   *normaal* zonder geluid. In de e-mail staat de prioriteit in het onderwerp.
6. **Mislukt verzenden** (geen internet, dienst onbereikbaar): de alarmlaag probeert het
   elke minuut opnieuw, hooguit een dag lang (v0.33; ook als het alarm intussen hersteld
   is, zodat na een storing van het internet alsnog te zien is wat er gebeurd is), en
   schrijft één waarschuwing in het log. Wachtende meldingen staan alleen in het
   werkgeheugen. Zonder internet vanaf de boot komt er geen melding aan (§8).
7. Is er geen enkel kanaal ingesteld, dan worden alarmen wel vastgelegd en acties
   uitgevoerd, maar gaat er geen melding uit; `check-config` waarschuwt daarvoor.

#### 5.5.5 Systeemmeldingen: logboek en melding bij een fout (v0.51)

*Gebouwd in code v0.19.0 (`brms/systeem.py`). Getest op de pc met nagemaakte uitvoer van
`journalctl` en `systemctl`; nog niet getest op de Pi.*

**Aanleiding.** Een storing in BRMS zelf (camera onbereikbaar, schrijven naar de database
mislukt, een service die steeds herstart) stond alleen in journald en was alleen via SSH
te zien. Er ging geen melding uit.

**Redenering.** Alle services schrijven hun waarschuwingen en fouten al naar journald
(§10.1 punt 4). Dat is daarmee de ene bron voor systeemmeldingen: het kost geen extra
schrijfwerk (P7), het bevat ook Mosquitto, go2rtc en de meldingen van systemd over een
herstart, en het werkt ook als de database of de broker de storing is. Een fout in de
database kan niet in de database worden vastgelegd. Een logboek waar je naar moet gaan
kijken is niet genoeg voor een systeem dat maanden onbeheerd draait; een echte fout moet
zich daarom ook melden.

**Verworpen alternatieven.**
- *Een eigen tabel voor systeemmeldingen, gevuld via de bus.* Extra schrijfwerk, een
  tweede log naast journald, en hij mist precies de storingen van database en broker.
- *Elke waarschuwing als melding.* Te veel: een waarschuwing is iets dat vanzelf overgaat
  of waarvoor al een alarmregel bestaat (sensor stil, camera stil).

**Specificatie.**
1. **Niveaus.** Een *waarschuwing* (WARNING) is een storing die BRMS zelf opvangt (een
   mislukte poll of opname, een melding die nog niet verstuurd kon worden). Een *fout*
   (ERROR en hoger) betekent dat er iets stuk is en aandacht vraagt (schrijven naar de
   database mislukt, een onverwachte uitzondering, go2rtc start niet). Bij het bouwen
   worden de bestaande logregels hierop nagelopen.
2. **Niveau in journald.** De services zetten het niveau voor elke regel in de vorm die
   systemd herkent (`<3>` fout, `<4>` waarschuwing, ...), zodat journald op niveau kan
   filteren. Een onverwachte uitzondering waardoor een service stopt, wordt eerst als
   fout gelogd. Bij een fout met een uitzondering staat de soort en de tekst daarvan
   achter de eerste regel; de regels van de traceback krijgen niveau "gewoon" (wel in
   journald, niet in het logboek). Meldt systemd dat een BRMS-service is gestopt met een
   fout ("Failed with result ..."), dan telt dat als fout.
3. **Logboek in het web** (§5.6, systeempagina): de webservice leest journald met
   `journalctl` (uitvoer als JSON), voor de eenheden `brms-*` en `mosquitto` en voor de
   **kernel** (v0.52), vanaf niveau waarschuwing. De webservice schrijft zelf niets weg.
   Een herstart of een omgezette schakelaar via het web staat er ook in (als
   waarschuwing), zodat terug te vinden is wie wat deed. De kernel meldt lees- en schrijffouten van de schijf, fouten in het bestandssysteem
   en onderspanning van de Pi: dat zegt meer over de gezondheid van de schijf dan SMART,
   dat deze schijf niet doorgeeft (§5.6).
4. **Bewaartermijn:** het logboek gaat zo ver terug als journald bewaart. Op de Pi staat
   journald nu op `Storage=volatile` (de standaard van Raspberry Pi OS): het log staat
   in het werkgeheugen en is **na een herstart van de Pi leeg**. Wat er vlak voor een
   stroomuitval of een vastloper gebeurde, is dan niet meer te zien. Of het log op de
   schijf moet komen, hoort bij **[OPEN-21]**.
5. **Melding bij een fout:** de alarmlaag volgt journald (`journalctl --follow`, dezelfde
   eenheden en de kernel, vanaf niveau fout) en verstuurt bij een fout een melding via de kanalen van
   §5.5.4, met de service en de foutregel, prioriteit *hoog*.
   - Hooguit **één melding per service per uur** (de kernel telt als één service); de melding na dat uur noemt hoeveel
     fouten er intussen bij zijn gekomen. Alles staat in het logboek.
   - Mislukt versturen van een melding is een waarschuwing (§5.5.4 punt 6), geen fout:
     anders zou een melding over een fout zelf weer een melding geven.
   - Stopt het volgen van journald, dan start de alarmlaag het opnieuw.
   - In `brms.toml`: `[melding] systeemfouten = true` (standaard aan).
6. Niet afgedekt: uitval van de alarmlaag zelf, van de Pi of van het internet (§8,
   OPEN-18).

### 5.6 Webinterface

**Redenering (v0.5).** De gebruiker moet foto's en video's kunnen bekijken en verwijderen.
Als alleen de opslag-service naar database en opnamemap schrijft, blijft er één schrijver
(eenvoudig voor SQLite, geen conflicten); het web vraagt het verwijderen daarom via MQTT aan.

**Verworpen alternatieven.** *Het web verwijdert zelf bestand en databaserij.* Twee
schrijvers op dezelfde database en map.

**Specificatie.**
1. Flask + Chart.js: actuele waarden en grafieken. Alleen bereikbaar via Tailscale/LAN.
2. Foto's en video's bekijken, en **verwijderen** (één of een selectie). Verwijderen gaat
   als MQTT-opdracht naar de opslag-service, die bestand en databaserij verwijdert.

   **Opnamepagina** (gebouwd in v0.41), bereikbaar via "Opnames" op de grafiekenpagina:
   - De pagina toont de opnames van **één dag**, de nieuwste bovenaan, als lijst (tijd,
     soort, camera, aanleiding, grootte), met daarboven één grote weergave van de gekozen
     opname: een foto, of een video met afspeelknoppen. Bladeren met "ouder" en "nieuwer"
     of de pijltjestoetsen. De dag loopt van middernacht tot middernacht in de tijd van
     de browser.
   - **Geen miniaturen:** een dag heeft honderden intervalfoto's van enkele honderden kB;
     dat is te veel voor de mobiele verbinding, en miniaturen maken vraagt een extra
     afhankelijkheid (P3). Een foto wordt pas opgehaald als hij gekozen wordt.
   - **Keuze:** de dag, de camera, de soort (foto's, video's of beide) en of de
     intervalfoto's meetellen (uit: alleen wat op verzoek of bij een alarm is gemaakt).
     De lijst toont hooguit 500 opnames; zijn er meer, dan meldt de pagina dat.
   - **Verwijderen:** opnames aanvinken en "Verwijder selectie", met een tweede klik ter
     bevestiging. De opdracht is `brms/opdracht/opslag/verwijder` met
     `{"opnames": [[camera_id, ts], ...]}` (hooguit 500 per keer); de pagina haalt de
     lijst kort daarna opnieuw op.
   - **"Live camera"** (v0.44; gebouwd in v0.45; de techniek is herzien in v0.48 en
     zo gebouwd in v0.49) toont het live-beeld van
     de camera in het fotovak (§5.7 punt 18). Zolang het live-beeld loopt, zijn er twee
     knoppen: **"Foto"** maakt direct een foto en slaat hem op, **"Video"** neemt een
     video op (standaard 60 s) en slaat hem op. Beide geven de camera-reader een poll-nu
     met aanleiding `handmatig` (§5.7 punt 12); het live-beeld loopt intussen door en de
     pagina meldt wanneer de opname is opgeslagen. Deze knoppen vervangen "Foto nu" en
     "Video nu": een opname op verzoek maak je terwijl je kijkt.
     Een opname staat in de lijst zodra hij is opgeslagen, terwijl het live-beeld
     doorloopt (v0.50; eerder pas na het stoppen van het live-beeld). Een camera
     zonder live-beeld (tot v0.48: zonder `live_url`; daarna: zonder `video_url`) heeft
     geen live-knop, en dus ook geen opname op verzoek vanuit de pagina.
     Kan de camera bewegen (veld `richten` in `brms.toml`), dan staan er tijdens het
     live-beeld ook **vier richtknoppen**: op, neer, links, rechts; elke klik beweegt de
     camera een korte stap (§5.7 punt 19; gebouwd in v0.72). Een vaste camera
     heeft deze knoppen niet.
   - Onderaan staan de totalen: aantal en omvang van de foto's en video's, de
     bewaartermijn en de maximale ruimte.
   - De bestanden zelf levert de webservice uit op `/opname/<pad>`, alleen voor een pad
     van de vaste vorm van §5.7 punt 10 binnen de opnamemap. Een video is in delen op te
     vragen, zodat spoelen niet het hele bestand ophaalt.
   - Er is geen aanmelding (OPEN-15): wie de pagina kan openen, kan ook verwijderen.

   **Dagoverzicht** (v0.42; gebouwd in v0.43). De intervalfoto's van een dag
   vormen achter elkaar een versnelde film van die dag; zo worden ze normaal bekeken. Het
   dagoverzicht is een tweede weergave van de opnamepagina, naast de lijst.
   - De pagina speelt de opnames van de gekozen dag (en de gekozen camera) af **van oud
     naar nieuw**, in de browser. Op de Pi wordt er geen videobestand van gemaakt.
   - Een **intervalfoto** staat standaard **1 s** in beeld (v0.91; eerder vast 0,5 s);
     zie "Tempo" hieronder.
   - Een **foto bij een gebeurtenis** (elke andere aanleiding: alarm, op verzoek) staat
     **5 s** in beeld.
   - Een **video** wordt op zijn plaats in de tijd **volledig afgespeeld**; daarna gaat
     het overzicht verder. Met de knop **"verder"** is de video over te slaan.
   - Een **foto die samenvalt met een video** van dezelfde camera doet in het
     dagoverzicht niet mee (v0.47; gebouwd in v0.49): de video laat dat stuk van de dag
     al zien, en de tijdlijn hoort niet dubbel te lopen. Dit geldt voor elke foto, ook
     een intervalfoto. "Samenvallen" is: de foto is gemaakt tijdens de video. Een foto
     bij een gebeurtenis valt ook weg als hij hooguit 5 s voor het begin van de video is
     gemaakt (een alarm maakt eerst de foto en start dan de video). In de lijst blijven
     de foto's gewoon staan.
   - **Bediening:** afspelen en pauze, en een tijdbalk van de dag waarop de gebeurtenissen
     gemarkeerd staan; een klik op de balk springt naar dat moment. Bij het beeld staan
     het tijdstip en de aanleiding.
   - De pagina haalt de volgende foto's alvast op terwijl de huidige in beeld is. Is een
     foto er nog niet, dan **wacht** het overzicht erop; er wordt niets overgeslagen.
   - Het dagoverzicht gebruikt alle opnames van de dag; de grens van 500 van de lijst
     geldt hier niet (een dag heeft 480 intervalfoto's plus de gebeurtenissen).
   - **Dataverbruik:** een dagoverzicht haalt alle foto's van die dag op: bij 480 foto's
     van bijna 300 kB ongeveer 140 MB, plus ongeveer 14 MB per video van een minuut. Vanaf de boot gaat
     dat van de databundel en vraagt het ruim 4 Mbit/s om bij te blijven.
   - **Tempo** (v0.91, gebouwd in code v0.28.0). Gemeten over de mobiele verbinding
     (18 november 2026): de RUT241 levert ongeveer 3 Mbit/s naar buiten, en een nachtfoto
     is ruim 1,3 MB (ruis in het donker; een dagfoto ongeveer 0,3 MB). Een nachtfoto doet
     er dan 3 tot 5 s over, terwijl het overzicht er elke 0,5 s een wilde tonen: een dag
     afspelen duurde een kwartier in plaats van 4 minuten. Daarom is het tempo te kiezen,
     met twee kleine keuzelijsten op de afspeelbalk:
     - **Verversen:** hoe lang een intervalfoto in beeld staat: 0,2 / 0,5 / 1,0 / 1,5 /
       2,0 s; standaard **1,0 s**.
     - **Verspringen:** hoeveel tijd er tussen twee getoonde intervalfoto's ligt, als
       veelvoud van het `foto_interval` van de camera: 1× / 2× / 3× / 5× / 10×; standaard
       **1×** (elke foto). De keuzelijst noemt de tijd erbij, bij een interval van
       3 minuten "3 min (1×)", "6 min (2×)" enzovoort. De overgeslagen foto's worden niet
       opgehaald: bij 2× kost een dag de helft van de data.
     - Het verspringen gaat op **tijd**, niet op aantal: ontbreken er foto's, dan blijft
       de sprong in de tijd gelijk. Een **foto bij een gebeurtenis en een video worden
       nooit overgeslagen**; na zo'n gebeurtenis telt de sprong vanaf de gebeurtenis.
     - Een nieuwe keuze geldt vanaf de volgende foto. De knoppen "één opname terug of
       verder" en de pijltjestoetsen blijven per opname gaan, zonder sprong. De keuzes
       worden in de browser onthouden.
     - Het foto-interval is dat van de gekozen camera (bij "alle camera's" voor de tekst
       in de keuzelijst dat van de eerste; bij het verspringen telt per foto het interval
       van de eigen camera).

   Invulling bij het bouwen (v0.43):
   - De weergave wordt gekozen met "Weergave: lijst / dagoverzicht" en in de browser
     onthouden. Het overzicht begint **stilstaand** op de eerste opname en meldt het
     aantal opnames en hun omvang; afspelen begint pas met de knop (of de spatiebalk),
     zodat het dataverbruik een keuze is.
   - Naast afspelen en pauze: één opname terug of verder (knoppen of pijltjestoetsen);
     dat zet het afspelen stil. Aan het einde van de dag stopt het overzicht; opnieuw
     afspelen begint van voren.
   - De tijdbalk loopt van 00:00 tot 24:00; een klik springt naar de eerste opname vanaf
     dat tijdstip. Een foto bij een gebeurtenis en een video hebben elk een eigen kleur.
   - Een opname waarvan het bestand er niet meer is, wordt overgeslagen.
   - De webservice levert hiervoor hooguit 5000 opnames per dag, en laat de browser het
     bestand van een opname een dag bewaren (een opname verandert niet meer).

   Verworpen voor het dagoverzicht:
   - *Op de Pi een video van de dag maken (ffmpeg).* Omrekenen belast de Pi zwaar, kost
     opslag en moet opnieuw bij elke nieuwe of verwijderde opname.
   - *Kleinere foto's voor het overzicht (miniaturen).* Scheelt data, maar vraagt een
     extra afhankelijkheid of een tweede opname per foto; kan later alsnog. Ook bij het
     trage afspelen over de mobiele verbinding (v0.91) niet gekozen: het tempo kiezen
     (verversen en verspringen) lost het op zonder iets op de Pi te veranderen.
3. Het web leest uit SQLite en geeft opdrachten via MQTT (poll-nu, verwijderen); het
   schrijft niet zelf naar database of opnamemap.
4. **Grafiekenpagina** (v0.22; indeling v0.25), de eerste pagina van het web. De
   gebruiker stelt zelf samen wat hij ziet (v0.25):
   - Met **"+ Grafiek"** voegt hij een grafiek toe; er kunnen meerdere grafieken op de
     pagina staan. Elke grafiek heeft één bron en een eigen venster of periode.
   - De **bron** kiest hij met een keuzelijst linksboven in de grafiek (v0.62): **Live
     (MQTT-bus)** of **Database**. Een nieuwe grafiek begint als live. De bron is ook te
     wijzigen als er al reeksen in de grafiek staan: de reeksen blijven staan, het venster
     of de periode valt terug op de standaard van de nieuwe bron (live: laatste 10
     minuten; database: 24 uur) en de grafiek wordt opnieuw getekend uit de nieuwe bron.
     Tot v0.61 waren er twee knoppen ("+ Live-grafiek" en "+ Database-grafiek") en lag de
     bron daarna vast: wie dezelfde sensoren in de historie wilde zien, moest een tweede
     grafiek opbouwen.
   - Per grafiek kiest hij een **sensor**, daarna een **grootheid** van die sensor (wat
     het sensortype levert, beperkt tot de lijst `grootheden` uit de configuratie, §5.4.2),
     en voegt die met **"Toevoegen"** als reeks toe. Een grafiek kan reeksen van meerdere
     sensoren en grootheden bevatten, hooguit **8** (zoveel kleuren zijn goed te
     onderscheiden). Reeksen en grafieken zijn weer te verwijderen.
   - Reeksen met **dezelfde eenheid** delen één verticale as. Elke **andere eenheid**
     krijgt binnen de grafiek een eigen strook met een eigen as, onder elkaar op dezelfde
     tijdas. Twee schalen door elkaar in één vlak zijn niet af te lezen.
   - Boven de grafiek staat per reeks de kleur, de naam (sensor · grootheid) en de laatste
     waarde met tijdstip. Een reeks houdt zijn kleur zolang hij in de grafiek staat.
   - De **indeling** (grafieken en reeksen, geen meetwaarden) wordt in de browser
     onthouden (`localStorage`); op een ander apparaat begint de pagina opnieuw.
   - Alle live-grafieken samen doen elke 2 s **één** aanvraag aan de webservice.

   De twee bronnen:
   - **Live**: elke meting zoals die over de MQTT-bus gaat. De webservice abonneert zich
     op `brms/meting/#` en bewaart per sensor en grootheid de metingen van de laatste
     **24 uur** in het geheugen (v0.24; hooguit 100.000 per reeks, 24 bytes per meting);
     de pagina vraagt elke 2 s wat er nieuw is. Na een herstart van de webservice begint
     dat geheugen leeg.

     **Het live-geheugen nader (v0.57).** De webservice luistert **altijd** mee en bewaart
     de metingen, ook als er geen live-grafiek openstaat.
     - *Waarom:* een live-grafiek toont zo meteen een gevuld venster (tot "laatste 24 uur")
       en kan terugbladeren, in de volle resolutie van de sensor. De database heeft per
       reeks maar één rij per 5 minuten (§5.3.2). Pas gaan bewaren als er een grafiek
       opengaat zou betekenen dat elke nieuwe grafiek leeg begint.
     - *Waar:* alleen in het werkgeheugen (RAM) van `brms-web`. Er gaat niets naar de
       opslag, dus het kost geen schrijfacties op flash (P7).
     - *Hoe lang:* 24 uur; wat ouder is wordt weggegooid.
     - *Groeit niet door:* naast de grens van 24 uur geldt het maximum van 100.000
       metingen per reeks (2,4 MB). Drie Xiaomi's (4 grootheden, elke 2,5 s) komen op
       ongeveer 10 MB; met de RuuviTags (elke seconde) en de Victron-apparaten erbij
       naar schatting enkele tientallen MB. Dat is ruim binnen het geheugen van de Pi 4.

   **Herstart van de webservice** (v0.36). De sensoren en alarmregels staan in de pagina
   zoals ze waren toen de pagina geladen werd. Na een wijziging van `brms.toml` en een
   herstart kende een pagina die al open stond de nieuwe regels niet (en tekende dan geen
   grenzen en alarmperioden voor een nieuwe regel). Daarom stuurt de webservice bij elk
   antwoord zijn starttijdstip mee (kop `X-BRMS-Gestart`); is dat anders dan bij het
   laden, dan laadt de pagina zichzelf opnieuw. De gekozen grafieken blijven staan.

     Het **venster** is te kiezen (v0.24): laatste 50 of 100 metingen, of laatste 10
     minuten, 30 minuten, 1 uur, 3 uur, 12 uur, 24 uur. Tot en met 30 minuten toont de
     grafiek elke meting. Vanaf 1 uur worden de metingen samengevat per tijdvak (5, 15, 60
     en 120 s), zodat een grafiek hooguit ongeveer 720 punten heeft: alle losse metingen
     van 24 uur zijn te veel voor de browser en voor de mobiele verbinding. Samenvatten
     volgt de soort (§5.3.2): gemiddelde voor doorlopend, som voor toename, en voor een
     toestand het maximum, zodat een korte 1 zichtbaar blijft.

     Met **"eerder"** en **"later"** schuift het venster over de metingen in het geheugen
     (v0.28): bij "laatste 50 metingen" geeft "eerder" de 50 metingen daarvoor (per reeks),
     bij "laatste 10 minuten" de 10 minuten daarvoor. Een eerder venster **staat stil**: het
     wordt geteld vanaf het tijdstip waarop het terugbladeren begon en verschuift niet
     door nieuwe metingen. **"nu"** laat de grafiek weer meelopen. "eerder" is alleen
     actief als de webservice nog oudere metingen heeft dan het getoonde venster; verder
     terug dan 24 uur (of dan de laatste herstart van de webservice) kan alleen via de
     database.

     De drie knoppen zijn **pictogrammen** (v0.56): een haakje naar links (eerder), een
     haakje naar rechts (later) en een haakje tegen een streep (nu). Woorden namen op een
     telefoon een hele regel in. De alarmenpagina gebruikt dezelfde knoppen. Op een smal
     scherm (telefoon rechtop) staat de kop van een grafiek in twee regels: de bron met
     het **kruisje (sluiten) rechtsboven**, daaronder de keuzelijst met de knoppen ernaast.
     Het kruisje staat zo op elk scherm op dezelfde plaats.
   - **Database**: de opgeslagen rijen (per 5 minuten, §5.3.2), over 1 uur, 6 uur, 24 uur,
     7 dagen of 30 dagen, met bladeren naar eerdere perioden. Een interval zonder rij
     blijft in de grafiek open.

   **Legenda** (v0.61). Boven elke grafiek staat per reeks het kleurstreepje, de naam, de
   laatste waarde en een kruisje om de reeks te verwijderen.
   - De reeksen staan in **kolommen van gelijke breedte**; een te lange naam wordt
     afgekapt. Tot v0.60 stonden ze als doorlopende tekst achter elkaar: veranderde een
     waarde van lengte, dan brak de regel op een andere plaats af en versprongen alle
     teksten.
   - Breed scherm (pc, laptop): zoveel kolommen als er passen, met `sensor · grootheid`,
     de waarde met eenheid en het tijdstip met seconden.
   - Telefoon: de korte vorm, alleen de **sensornaam, de waarde met eenheid en de
     leeftijd** van de meting, bijvoorbeeld `buiten · 19,2 °C (-1m)`; met de lange vorm
     past het niet op het scherm. De eenheid en de kleur zeggen welke grootheid het is.
     **Rechtop één kolom, liggend twee kolommen** (v0.62). Een telefoon wordt herkend aan
     het scherm: rechtop hooguit 640 px breed, liggend hooguit 500 px hoog. De leeftijd
     staat in minuten; vanaf 100 minuten in uren (`-5u`) en vanaf twee dagen in dagen
     (`-12d`), voor een oude meting uit de database.
   - Waarden staan met het **vaste aantal decimalen** van de grootheid (§5.3.3), in de
     legenda en in de aanwijzer van de grafiek.

   Toestanden (0/1) worden als trap getekend. Tijden staan in de lokale tijd van de browser.
5. **Server** (v0.22): de ingebouwde server van Flask (Werkzeug), één thread per aanvraag,
   als service `brms-web` (`brms web`). Adres en poort staan in de configuratie
   (`[web]`, standaard alle interfaces, poort **8080**): `http://brms:8080` via Tailscale.
   De server schrijft geen regel per aanvraag in het log (P7).
6. **Chart.js staat als bestand in de repo** (`brms/web/static/`), niet via een CDN: de
   pagina werkt ook zonder internet (P3).
7. De database wordt per aanvraag geopend met een **alleen-lezen** verbinding.

*Verworpen bij de testpagina (v0.22).*
- *Live via een open verbinding (Server-Sent Events of websockets).* Houdt per kijker een
  verbinding en een thread vast; elke 2 s vragen is eenvoudiger en voor een grafiek genoeg.
- *De browser rechtstreeks op de broker (MQTT over websockets).* Vraagt een extra ingang op
  Mosquitto en een MQTT-bibliotheek in de pagina.
- *Een aparte webserver (gunicorn, waitress).* Extra afhankelijkheid (P3); voor één of
  enkele kijkers achter Tailscale volstaat de ingebouwde server.

**Alarmen in het web (v0.31; §5.5.3).**
- Een overzicht van de alarmregels met per regel een **aan/uit-schakelaar**, de alarmen
  die nu actief zijn, en de opgetreden alarmen uit de tabel `alarm`: de alarmenpagina
  hieronder (v0.52).
- In de grafieken de grens (streeplijn) en herstelgrens (stippellijn) in de kleur van de
  reeks, en de alarmperioden gearceerd in de strook van die reeks. *Gebouwd (v0.33).* Een
  grens buiten het bereik van de as wordt niet getekend: de as volgt de metingen. De
  perioden komen van `/api/alarmen`; een live-grafiek ververst ze elke 30 s.
- Het web leest de regels uit `brms.toml` en de alarmen uit de database; de schakelaar
  gaat als opdracht over MQTT (punt 3).

**Indeling, menu en thema (v0.52; gebouwd in code v0.19.0).** Het web heeft vier pagina's: **Grafieken** (`/`),
**Opnames** (`/opnames`), **Alarmen** (`/alarmen`) en **Systeem** (`/systeem`). Het wordt
ook op een telefoon gebruikt.
1. Elke pagina heeft dezelfde **bovenbalk**: links de menuknop (drie streepjes), daarnaast
   de titel van de pagina, rechts een **statusstip**. Links van de stip staat een
   **tegeltje** (v0.55) met één woord: **Actief** (alle sensoren zijn echt),
   **Simulatie** (er loopt een test met de testreeks) of **Demo** (er loopt een demo);
   zie §10.3 punt 10. Loopt er een test én een demo, dan staat er Simulatie. De uitleg
   bij het tegeltje noemt de gesimuleerde sensoren.
2. De menuknop schuift een **zijbalk** in beeld met de vier pagina's. Op een smal scherm
   ligt hij over de pagina en sluit hij na een keuze, een tik ernaast of Escape; op een
   breed scherm (vanaf 1000 px) staat hij vast in beeld en is er geen menuknop.
3. De **statusstip** en het menu laten op elke pagina zien of er iets mis is: bij
   "Alarmen" staat het aantal actieve alarmen; de stip is rood als een service niet
   draait of de broker onbereikbaar is, oranje bij een actief alarm, anders groen. De
   pagina vraagt dit elke **5 s** op (`/api/status`).
   - *Waarom 5 s (v0.90, eerder 30 s).* De bootpagina ververst haar waarden elke 5 s
     (punt 7) en de grafieken elke 2 s. Met 30 s stond een waarde al lang rood en vet
     voordat de stip en de teller het alarm toonden, en na het einde van een alarm bleven
     ze tot een halve minuut staan (gemeld door de gebruiker, 2026-11-18). Met hetzelfde
     interval als de bootpagina veranderen ze vrijwel tegelijk.
   - *Kosten.* Het antwoord is één korte telling in de database. De status van de
     services onthoudt de webservice 10 s, zodat er niet vaker een vraag aan systemd
     gaat. Over 4G is het enkele MB per dag, en alleen zolang er een pagina open staat.
   - *Verworpen:* het aantal alarmen meesturen met het antwoord van `/api/boot`. Dat
     helpt alleen op de bootpagina; op de andere pagina's bleef de stip achterlopen.
4. **Thema:** onderin de zijbalk de keuze *Automatisch* (volgt het toestel), *Licht* of
   *Donker*. De keuze wordt in de browser onthouden (`localStorage`), per apparaat. Een
   aparte instellingenpagina is er niet: dit is de enige instelling van de weergave.
5. De iconen in het menu zijn **getekende lijn-iconen** (SVG in de pagina, geen
   lettertype of extern bestand, P3); ze kleuren mee met het thema.
6. Eén **gedeeld stijlbestand** (`static/brms.css`) en één gedeeld sjabloon voor
   bovenbalk en zijbalk. Op een smal scherm worden tabellen (opnames, alarmen, logboek)
   kaartjes onder elkaar; knoppen zijn groot genoeg voor een vinger.
7. De werking van de grafieken en van de opnamepagina verandert niet.

*Verworpen:* een CSS- of JavaScript-raamwerk (Bootstrap e.d.): een afhankelijkheid en
honderden kB voor vier pagina's (P3). *Emoji als iconen:* zien er op elk toestel anders
uit en volgen het thema niet.

**Alarmenpagina (v0.52; gebouwd in code v0.19.0).** Van boven naar beneden:
1. De alarmen die **nu actief** zijn (regel, sensor, sinds wanneer, waarde).
2. De **alarmregels**, met per regel de voorwaarde in woorden, de sensoren, de melding en
   de **aan/uit-schakelaar** (§5.5.3 punt 7). De pagina toont de nieuwe stand zodra de
   opslag hem heeft vastgelegd.
3. De **opgetreden alarmen** uit de tabel `alarm`, de nieuwste bovenaan, met begin,
   einde, duur en de reden van het einde. De pagina toont niet de hele tabel maar een
   **keuze** (v0.56; gebouwd in code v0.21.0):

   *Redenering.* De tabel groeit de hele winter door. Tot v0.55 stonden de nieuwste 200
   alarmen onder elkaar: op een telefoon een lange lijst waarin het recente wegvalt. De
   gebruiker wil standaard alleen de laatste alarmen zien en gericht kunnen terugkijken,
   op dezelfde manier als bij de grafieken.

   *Specificatie.*
   - Een keuzelijst: **laatste 10** (de standaard bij elk openen van de pagina),
     **vandaag**, **deze week** (maandag t/m zondag), **deze maand**, **dit jaar**.
   - Ernaast de knoppen **eerder**, **later** en **nu** (dezelfde pictogrammen als bij de
     grafieken, punt 4 hierboven). Bij "laatste 10" geeft "eerder" de 10 alarmen
     daarvoor; bij een periode de vorige dag, week, maand of het vorige jaar. "nu" gaat
     terug naar de nieuwste alarmen of de lopende periode. "eerder" is alleen actief als
     er nog oudere alarmen zijn.
   - Achter de knoppen staat welk deel er getoond wordt (de datum, de week, de maand,
     het jaar, of "alarm 11 t/m 20").
   - Een alarm hoort bij de periode waarin het **begon**.
   - De browser rekent de grenzen van een periode uit en stuurt ze mee: een dag begint in
     de tijdzone van de kijker.
   - Van één periode staan er hooguit 200 alarmen; zijn het er meer, dan zegt de pagina
     dat en kiest de gebruiker een kortere periode.
   - De alarmen die **nu actief** zijn (punt 1) staan er altijd allemaal, los van de keuze.

**Systeempagina (v0.51; herzien in v0.52; gebouwd in code v0.19.0, de herstartknoppen en
het logboek nog niet getest op de Pi).** Van boven naar beneden:
1. **Services:** per service (`brms-*`, `mosquitto`) of hij draait, sinds wanneer, en
   hoe vaak systemd hem sinds het opstarten van de Pi opnieuw heeft gestart. De
   webservice vraagt dit op met `systemctl show`. Een service die niet draait, valt op
   door kleur.
2. **Herstarten (v0.52):** twee knoppen.
   - **"Herstart BRMS"** herstart alle BRMS-services (`systemctl restart`). Eerst wordt
     de configuratie gecontroleerd, zoals bij `brms herstart` (§10.1 punt 9): bij een
     fout gebeurt er niets en toont de pagina de fouten.
   - **"Herstart de Pi"** start de Pi opnieuw op (`systemctl reboot`).
   - Beide vragen eerst om **bevestiging** in een eigen venster ("Weet je het zeker?"),
     bij de Pi met de waarschuwing dat BRMS ongeveer een minuut onbereikbaar is en dat
     het logboek daarna leeg is (§5.5.5 punt 4). Daarna wacht de pagina tot de
     webservice weer antwoordt en laadt zichzelf opnieuw.
   - **Rechten:** de services draaien als gewone gebruiker en zonder nieuwe rechten
     (§10.1 punt 5). Een **polkit-regel** (`deploy/polkit/50-brms.rules`, door
     `install.sh` en `update.sh` in `/etc/polkit-1/rules.d/` gezet) staat alleen die
     gebruiker toe de eenheden `brms-*` te starten, stoppen en herstarten en de Pi te
     herstarten. De webservice wordt geen root; systemd voert alleen die handeling uit.
     Het plaatsen van de regel en het recht om journald te lezen regelt
     `deploy/rechten.sh`.
   - De webservice herstart eerst de andere services en een seconde na zijn antwoord
     zichzelf, zodat de pagina het antwoord nog krijgt.
   - *Verworpen:* `sudo` zonder wachtwoord voor deze opdrachten (de webservice zou dan
     `NoNewPrivileges` kwijt moeten); een eigen root-service die op een opdracht over de
     bus herstart (een extra onderdeel, en het werkt niet als de broker de storing is).
   - Wie de pagina kan openen, kan herstarten (OPEN-15).
3. **Logboek** (§5.5.5): waarschuwingen en fouten, de nieuwste bovenaan, met tijd,
   service, niveau en tekst.
   - **Filters:** de service (standaard **alle**) en het niveau: alleen fouten of ook
     waarschuwingen. De pagina opent op **alleen fouten** (v0.58; eerder op waarschuwingen
     en fouten): waarschuwingen zijn er veel en zelden dringend.
   - **Welk deel** (v0.61), met dezelfde opzet als de grafieken en de alarmen: een
     keuzelijst met **laatste 10** (de standaard), **laatste 50**, **laatste 3 uur**,
     **laatste 6 uur**, **laatste 24 uur** en **laatste 48 uur** (v0.62; "vandaag" uit
     v0.61 is vervallen), en ernaast de knoppen **eerder**, **later** en
     **nu** (dezelfde pictogrammen, punt 4 van de testpagina). "Eerder" geeft de 10 of 50
     regels daarvoor, of het venster van 3, 6, 24 of 48 uur daarvoor; "nu" gaat terug naar
     de nieuwste. "Eerder" is alleen actief als er nog oudere regels zijn. Achter de
     knoppen staat welk deel er staat (de periode, of "regel 11 t/m 20").
   - De filters gelden bovenop de keuze: "laatste 10" met "alleen fouten" zijn de laatste
     10 fouten. Een ander filter of een andere keuze begint weer bij nu.
   - Een periode toont hooguit 200 regels (de nieuwste); zijn er meer, dan staat dat
     eronder.
   - Niets wordt onthouden: na opnieuw laden gelden de standaarden. Zolang de pagina open
     staat en er niet teruggebladerd is, ververst het logboek elke 30 s.

   *Waarom (v0.61).* Tot v0.60 stonden de laatste 200 regels onder elkaar met een knop
   "ouder". Met minder dan 200 regels deed die knop niets, en de lijst was lang. Een korte
   lijst met dezelfde keuze en knoppen als elders in het web is overzichtelijker en hoeft
   niet apart geleerd te worden.
4. **Opslag:**
   - *Schijfruimte:* grootte, gebruikt en vrij van de schijf waarop `data/` staat, en
     daarvan de omvang van de database en van de opnames. De webservice vraagt dit zelf
     op (standaardbibliotheek).
   - *Verworpen: gezondheid van de schijf via SMART.* Getest op 12 november 2026 met
     `smartctl` 7.4 op de Raspberry Pi Flash Drive 256 GB: de schijf geeft via USB geen
     SMART-gegevens door (`-d sat` en de NVMe-varianten: "unsupported scsi opcode").
     Alleen `-d scsi` antwoordt, met niet meer dan "Health Status: OK" en een
     temperatuur van 0 °C; geen slijtage, geen bedrijfsuren. Daarvoor een eigen eenheid
     met root-rechten en een extra pakket is het niet waard. In plaats daarvan staan de
     foutmeldingen van de kernel in het logboek (§5.5.5 punt 3).

De gebruiker waaronder de services draaien moet journald mogen lezen (groep `adm` of
`systemd-journal`); `install.sh` en `update.sh` controleren dat (`deploy/rechten.sh`). Op
de Pi is `jandirk` al lid van `adm`.

**Bootpagina (v0.75; herzien in v0.76; gebouwd in code v0.26.0, beproefd in de browser op
de pc met nagemaakte metingen, nog niet op de Pi).**

*Redenering.* De grafiekenpagina laat zien wat de gebruiker zelf heeft samengesteld; om te
weten hoe de boot erbij ligt, moet hij eerst reeksen kiezen. Een plattegrond van de boot
met op elke plek de actuele waarde geeft dat overzicht in één blik, en laat direct zien
wáár een alarm is. Deze pagina wordt daarom de eerste pagina van het web.

*Verworpen.*
- *De plekken en sensoren vast in de pagina (HTML).* De tekening en de plekken horen bij
  deze ene boot; een andere sensor of een andere plek zou dan een codewijziging zijn.
- *De plekken in `brms.toml`, in procenten van het plaatje (ontwerp v0.75).* Een plek
  goed krijgen vraagt veel keren proberen, en elke poging een herstart op de Pi. In een
  tekenprogramma zet de gebruiker de naam gewoon op de plek waar de waarde moet komen.
- *Twee bestanden: een achtergrond en een overlay met de namen.* Eén tekening met de boot
  erin past altijd op zichzelf en is één bestand om bij te houden.
- *Een overlay als PNG.* Tekst is daarin alleen beeld; de namen teruglezen vraagt
  tekstherkenning.
- *De tekening in de repo (`static/`).* De repo is openbaar (§10.1) en de boot is een
  tekening van de werf; ze hoort bij de configuratie, niet bij de code.
- *Voor het donkere thema het plaatje omkeren of op een wit vlak zetten.* Omkeren maakt
  van het hout en de bekleding vreemde kleuren; een wit vlak is in het donker een lamp.

*Specificatie (herzien in v0.76).*
1. **Plaats in het web.** De pagina heet **Boot** en staat op `/`; in het menu staat ze
   bovenaan. De grafiekenpagina verhuist naar `/grafieken`. Het web heeft dan vijf
   pagina's. **Zonder blok `[boot]` met het veld `tekening`** in `brms.toml` is er geen
   bootpagina: "Boot" staat dan niet in het menu en `/` toont de grafiekenpagina, zoals
   tot nu toe. `/grafieken` werkt in beide gevallen.
2. **Tekening.** Eén **SVG-bestand** dat de gebruiker zelf tekent (beproefd met een dia
   uit PowerPoint, opgeslagen als SVG): de boot van bovenaf, en op elke plek waar een
   waarde moet komen de tekst **`sensor.grootheid`**, bijvoorbeeld
   `motorruimte.slagzij`. De waarde komt op de plek van de **eerste letter** van die
   tekst. Al het andere in de tekening (pijlen, een titel) blijft staan zoals getekend.
   - Het bestand staat naast `brms.toml` (buiten git). In `brms.toml` staat alleen de
     naam: blok `[boot]` met het veld `tekening` (ten opzichte van de map van
     `brms.toml`). `brms check-config` controleert dat het bestand bestaat.
   - De webservice levert het bestand uit zoals het op dat moment is. Een nieuwe versie
     van de tekening is zichtbaar na het verversen van de pagina, **zonder herstart**.
     De browser bewaart het bestand (enkele honderden kB, de boot zit erin) en haalt het
     alleen opnieuw op als het gewijzigd is.
   - **Donker thema:** de boot in de tekening heeft een **doorzichtige achtergrond**
     buiten de romp (`Privateer_transparant.png`, eenmalig gemaakt uit het plaatje van de
     werf, met een dunne grijze lijn om de romp). De tekening staat zo op de achtergrond
     van de pagina, licht of donker; de boot houdt zijn kleuren.
3. **Namen herkennen.** De pagina leest de teksten uit de tekening.
   - Een tekst van de vorm `sensor.grootheid` waarvan de sensor bestaat en die grootheid
     levert (§5.4.2), wordt een waarde. Hoofdletters tellen niet mee (PowerPoint maakt
     van `vooronder` vanzelf `Vooronder`).
   - PowerPoint knipt een naam soms in stukken (`s` + `b_achter.temperatuur`). Stukken
     die op dezelfde regel direct op elkaar aansluiten, worden eerst aan elkaar geplakt.
   - Een naam die niet herkend wordt, blijft als tekst staan: een tikfout valt zo op.
   - Een sensor toevoegen of verplaatsen is alleen een wijziging van de tekening.

   De eerste tekening (`Privateer.svg`) bevat: de temperatuur van `vooronder`,
   `sb_achter`, `bb_achter`, `kajuit`, `motorruimte` en `buiten`; `bilge.bilgepomp`;
   `BMV700-accumonitor.soc`; `Multiplus.walstroom`; en twee keer `motorruimte.slagzij`
   (punt 5). De kachels (§5.2.6) staan er niet in; ze komen er in de tekening bij als
   het relais gebouwd is.
4. **Waarden.** De pagina zet elke waarde in een **eigen letter** (groter dan in de
   tekening, leesbaar op een telefoon) met daarachter een **klein vakje in de kleuren
   van het thema**, zodat de waarde ook op het hout en in het donkere thema te lezen is.
   - Het getal staat met het vaste aantal decimalen en de eenheid (§5.3.3); een toestand
     in woorden (`aan`/`uit`).
   - **Gewoon:** de tekstkleur van het thema (zwart in het lichte thema).
   - **In alarm: rood en vet.** Een waarde staat in alarm als er een actief alarm is op
     die sensor én die grootheid (tabel `alarm`, zonder einde).
   - **Stille of ontbrekende sensor:** in plaats van de waarde staat er `--`, in
     **lichtgrijs**. Staat er op die sensor een actief alarm "sensor stil", dan is de
     `--` rood en vet. Een waarde geldt als stil als de laatste meting op de bus ouder
     is dan **5 minuten** (besluit OPEN-35, v0.85; eerder 15 minuten, OPEN-32). Een
     actief alarm "sensor stil" maakt alle waarden van die sensor **direct** stil (v0.83,
     code v0.27.1), ook als de regel een kortere duur heeft. Een Shelly met een
     poll-interval langer dan 5 minuten zou tussen twee polls `--` tonen; de standaard is
     30 s.
   - Overige tekst in de tekening (de titel) krijgt de tekstkleur van het thema.
5. **Slagzij.** `motorruimte.slagzij` staat twee keer in de tekening: aan bakboord
   (boven) en aan stuurboord (onder). De waarde staat alleen aan de kant waar de boot
   naartoe helt, met de kant erbij (`2° bb`, §5.2.5 punt 10); de andere plek is leeg.
   Algemeen: staat een grootheid met een kant twee keer in de tekening, dan is de
   bovenste plek voor bakboord en de onderste voor stuurboord.
   - De slagzij staat er in **hele graden** (v0.88, §5.3.3).
   - Is de waarde afgerond **0**, dan staat er **niets** (v0.88; eerder `0,0°` op beide
     plekken): een boot die recht ligt, hoeft dat niet te melden. Alleen bij een actief
     alarm op die waarde staat de `0°` er wel, in rood.
   - Een stille sensor toont `--`, zoals elke waarde (punt 4).
6. **Klikken** op een waarde opent de grafiekenpagina met die sensor en die grootheid
   in de **bootgrafiek**:
   - De eerste klik maakt een nieuwe grafiek **bovenaan** de grafiekenpagina, de
     bootgrafiek (live, zoals elke nieuwe grafiek). De bestaande grafieken blijven
     ongewijzigd en staan eronder.
   - Een volgende klik op de bootpagina voegt die waarde als reeks toe aan dezelfde
     bootgrafiek. Een reeks die er al in staat, komt er niet nog eens bij; bij het
     maximum van 8 reeksen (punt 4 van de grafiekenpagina) meldt de pagina dat.
   - De bootgrafiek blijft staan tot de gebruiker hem **sluit met het kruisje**; hij
     wordt met de indeling in de browser onthouden. Na het sluiten maakt een volgende
     klik een nieuwe bootgrafiek.
   - Verder is het een gewone grafiek: bron, venster en reeksen zijn te wijzigen.
7. **Verversen.** De pagina vraagt elke **5 s** met één aanvraag de waarden op die in de
   tekening staan. De webservice geeft per waarde de laatste meting met haar tijdstip
   uit het live-geheugen (punt 4 van de grafiekenpagina), en alleen daaruit (besluit
   OPEN-35, v0.85): na een herstart van de webservice staat er `--` tot de eerste meting
   van de bus binnen is. Eerder kwam dan de laatste rij uit de database; van een sensor
   die niet meer zond, stond er zo minutenlang een waarde van de vorige sessie. De
   alarmstand komt uit de tabel `alarm`.
8. **Telefoon rechtop.** De tekening staat dan een kwartslag gedraaid, met de boeg naar
   boven; de waarden en de overige tekst blijven rechtop staan op hun plek. Liggend zou
   de boot op een smal scherm te klein zijn om de waarden naast elkaar te lezen.

*Invulling bij het bouwen (v0.77, code v0.26.0).*
- **Ontbrekende tekening:** staat het bestand uit `[boot]` er niet, dan is dat bij
  `brms check-config` een waarschuwing ("Let op"), geen fout: een vergeten tekening mag
  de services (en daarmee de alarmen) niet tegenhouden. De bootpagina meldt het zelf.
- **Adressen:** de tekening staat op `/boot/tekening.svg`; de waarden komen van
  `/api/boot` (de pagina stuurt de lijst van sensor en grootheid mee en krijgt per waarde
  het getal, het tijdstip en of ze in alarm staat).
- **De vakjes zijn HTML over de tekening heen**, niet deel van de SVG: de letter is zo op
  elk scherm even groot (14 px; 13 px op een telefoon) en een vakje is een gewone link.
  De plek is een percentage van de tekening, dus de vakjes schalen mee. Een vakje dat
  over de rand zou steken, schuift terug tot het past.
- **Namen in stukken:** teksten die direct na elkaar in het bestand staan, op dezelfde
  regel en verder naar rechts, worden aan elkaar geplakt zolang het resultaat het begin
  van een bekende naam is; de langste bekende naam telt.
- **Overige tekst** krijgt alleen de themakleur als de gebruiker er zelf geen kleur aan
  heeft gegeven. In de gedraaide stand schuift een titel zo nodig opzij tot hij binnen
  de tekening past.
- **Scripts in de tekening** (en gebeurtenissen als `onclick`) worden niet uitgevoerd:
  de pagina haalt ze eruit.
- **De bootgrafiek** heeft een regel erboven ("Bootgrafiek: de waarden die je op de
  bootpagina hebt aangeklikt") en een rand in de accentkleur.
- De tekening staat in `.gitignore` (`config/*.svg`).

**Aanmelding (besluit OPEN-15, v0.69).** Er komt voorlopig geen aanmelding op de
webinterface. Van buitenaf is Tailscale de enige ingang (§9); aan boord luistert de
webinterface op alle netwerkaansluitingen van de Pi, en is het wifi-wachtwoord van de
router de beveiliging. Wie op het netwerk aan boord zit, kan dus alarmregels uitzetten,
opnames verwijderen en herstarten.

*Verworpen:* de webinterface alleen op het Tailscale-adres laten luisteren. Sluit het
netwerk aan boord uit, maar dan is de pagina aan boord ook alleen via Tailscale te
bereiken; het wifi-wachtwoord is voor nu genoeg.

### 5.7 Camera

*Alle stappen van punt 17 zijn gebouwd: de camera-reader met een foto per interval en op
verzoek (poll-nu), de tabel `opname` en het opruimen van oude foto's (v0.38), video op
verzoek met de maximale ruimte (v0.39), foto en video bij een alarm (v0.40, punt 15), en
bekijken en verwijderen in het web (v0.41, §5.6 punt 2). Foto's, video's en de foto bij
een alarm zijn getest met de testcamera (Foscam) en Telegram; de opnamepagina alleen op
de pc. Het richten van de camera (punt 19) is gebouwd in code v0.25.0 en werkt met de
Foscam FI9826W (beproefd door de gebruiker, 2026-11-16).*

**Redenering.** De camera bekijkt het dek. Een regelmatige foto toont de toestand van de
boot; een video legt een gebeurtenis vast (iemand stapt aan boord, storm). Een IP-camera
heeft na inschakelen ~30–60 s nodig om op te starten; een video bij een bewegingsalarm
komt dan te laat. Met 450 Ah accucapaciteit is er bij walstroomuitval ruim tijd om in te
grijpen, dus de camera kan altijd aan staan.

Foto's: één per 3 minuten ≈ 480 per dag × ~300 KB ≈ 150 MB/dag, ≈ 2 GB per twee weken.
Video's: ongeveer 14 MB per clip van 60 s (testcamera, 1,8 Mbit/s). Video's worden bewust bewaard en verdwijnen niet
vanzelf; een maximale ruimte voorkomt dat een reeks bewegingsalarmen de schijf vult.

**Verworpen alternatieven.**
- *Camera via relais uit bij walstroomuitval.* Bewegingsvideo's zouden te laat starten;
  de accucapaciteit maakt besparen onnodig. Het relais vervalt.
- *Lopende video verlengen bij een nieuwe poll-nu.* Onnodig complex; de rustperiode
  (§5.5.1) en de vaste duur volstaan.
- *Ook de foto via RTSP met `ffmpeg`* (v0.37). Werkt bij elk merk, maar start voor elke
  foto een zwaar programma en wacht op het eerste volledige beeld van de stroom. Een
  snapshot-adres is één HTTP-aanvraag.
- *ONVIF gebruiken om de adressen op te vragen* (v0.37). Een extra protocol en een extra
  afhankelijkheid voor twee adressen die de gebruiker één keer in de configuratie zet.
- *Video omrekenen naar een kleiner formaat* (v0.37). Belast de Pi zwaar; de camera levert
  al H.264 en de maximale ruimte (punt 5) begrenst het gebruik.
- *Live-beeld met HLS: `ffmpeg` schrijft de RTSP-stroom weg in korte stukjes die de browser
  afspeelt* (v0.44). Werkt met elke camera en geeft het volle beeld, maar loopt 6 tot 10
  seconden achter en vraagt een extra JavaScript-bibliotheek (hls.js) en een `ffmpeg` die
  meeloopt. Eerst zo ontworpen, daarna vervangen door de MJPEG-stroom van de camera.
- *De browser rechtstreeks met de camera verbinden* (v0.44). Het video-adres is RTSP; dat
  speelt geen enkele browser af. De MJPEG-stroom kan een browser wel tonen, maar dan staat
  het wachtwoord van de camera in de pagina en is het aantal beelden niet te beperken.
- *Live-beeld alleen tijdens een opname* (v0.44). Kijken en opnemen zijn twee dingen: de
  gebruiker wil kunnen kijken zonder op te nemen, en opnemen terwijl hij kijkt.
- ~~*Een aparte streamingdienst (bijv. go2rtc, MediaMTX)* (v0.44). Een extra programma om
  te installeren en bij te houden (P3).~~ **Herzien in v0.48:** go2rtc is alsnog gekozen,
  nadat de MJPEG-stroom in de praktijk niet voldeed (punt 18).
- *Een video-deurbel op batterij als camera* (v0.70). Aantrekkelijk omdat hij waterdicht
  is, een groothoeklens heeft en geen kabel vraagt. Maar hij slaapt om de batterij te
  sparen en wordt alleen wakker bij beweging of een druk op de knop: de Pi kan geen foto
  of stroom opvragen wanneer hij dat wil (punt 2). De gangbare modellen (Ring, Eufy, Arlo,
  Nest, Reolink Battery) werken bovendien via de cloud en de app van de fabrikant, zonder
  lokaal snapshot- of RTSP-adres (P2, punt 6). Kou kost capaciteit en na enkele maanden
  moet de batterij geladen worden, terwijl er niemand aan boord is.
- *Een wifi-camera met een eigen zonnepaneel, zonder 12V-aansluiting* (v0.70). Dit is
  dezelfde batterijcamera met een paneeltje van 3–6 W erbij: hij slaapt ook en heeft
  vrijwel nooit een lokaal adres (Reolink Argus, Eufy SoloCam, Tapo, Arlo). De winter is
  juist het slechtste seizoen: weinig opbrengst in december en januari, zeker laag tussen
  masten en steigers, en een lithium-accu laadt niet onder 0 °C. Lokaal uitlezen kan bij
  Reolink alleen met een omweg (een Reolink Home Hub of de brug `neolink`): een extra
  apparaat of een extra afhankelijkheid (P3), en elke opvraging kost batterij.
- *De binnencamera (Foscam FI9826W) via wifi aan de RUT241* (v0.89). Geprobeerd door de
  gebruiker op 2026-11-18, omdat wifi een kabel spaart (P5). Het videobeeld liep via wifi
  te ver achter om bruikbaar te zijn; met een netwerkkabel is dat niet zo. De oorzaak van
  de vertraging is niet onderzocht. Daarnaast was de wifi van deze camera lastig in te
  stellen: Foscam VMS ondersteunt de wifi-instellingen van dit model niet ("Un-support",
  een lege lijst met netwerken); het kan alleen met de CGI-commando's van de camera en
  een account op beheerdersniveau. Een camera die altijd aan staat en een videostroom
  levert, is met een kabel ook betrouwbaarder dan via wifi (P1). De camera hangt daarom
  aan een netwerkkabel (punt 1).

De eis achter beide afwijzingen: de camera staat altijd aan, heeft vaste voeding en geeft
de Pi op elk moment een foto (snapshot-adres) en een H.264-stroom over RTSP (punt 1, 6 en
18). Een **bedrade** video-deurbel voldoet daar wel aan en is dus niet afgewezen,
bijvoorbeeld de Reolink Video Doorbell WiFi (RTSP, ONVIF en een snapshot-adres; voeding
12–24 VAC of 24 VDC, aan boord dus met een omvormer 12→24 V). Een gewone
wifi-buitencamera op 12 VDC is eenvoudiger: rechtstreeks op het boordnet, één dun
tweeaderig draadje, ongeveer 3–5 W. Is de kabeldoorvoer naar buiten het bezwaar, dan kan
de camera binnen achter een raam of in de kuip onder de buiskap; achter glas gaat de
infraroodverlichting uit, want die spiegelt in het raam. Het model is nog niet gekozen.
De gegevens van de genoemde modellen zijn niet nagezocht: controleren voor aankoop.

Verworpen bij het richten van de camera (punt 19, v0.71):
- *Een adres per richting in `brms.toml`* (zoals `foto_url`). Werkt alleen bij merken met
  een eenvoudig HTTP-adres per commando (Foscam, Dahua/Amcrest, Axis); Hikvision, Reolink
  en Tapo vragen een bericht met inhoud of een eigen protocol. Een ander merk vraagt dus
  toch nieuwe code, en dan is één veld met het merk korter en minder foutgevoelig.
- *ONVIF.* De enige standaard die vrijwel elke beweegbare camera ondersteunt, maar een
  zwaar protocol (SOAP/XML) voor vier knoppen bij één camera. Het blijft de uitwijk als er
  later een beweegbare camera van een ander merk komt.
- *Bewegen zolang de knop ingedrukt is.* Het live-beeld loopt tot 2 s achter, dus de
  camera schiet door; en een stopcommando dat verloren gaat, laat hem doorlopen tot zijn
  eindstand.
- *Na het live-beeld vanzelf terug naar een vaste stand, of een knop daarvoor.* Niet
  gewenst: de camera blijft staan waar de gebruiker hem laat.

**Specificatie.**
1. IP-camera (wifi 2,4 GHz, RTSP/ONVIF, 12V), **altijd aan**. Voorlopig (v0.71) is dat
   een **binnencamera**, de Foscam FI9826W, die kan draaien en kantelen (punt 19). Een
   buitencamera en de voeding daarvoor komen later.
   - De Foscam hangt met een **netwerkkabel** aan de RUT241, niet via wifi (v0.89): via
     wifi liep het videobeeld te ver achter (zie de verworpen alternatieven). Voor BRMS
     maakt het geen verschil: de camera blijft bereikbaar op het adres in `[[camera]]`.
   - Voor de buitencamera is de aansluiting (wifi of kabel) nog niet gekozen; de
     ervaring met de Foscam weegt daarbij mee.
2. De camera-reader is een pollende reader (§5.2.1) met twee soorten opname:

   | | Foto | Video |
   |---|---|---|
   | Wanneer | elke **3 min**, plus poll-nu | alleen **poll-nu** (gebruiker, of alarmlaag bij beweging) |
   | Duur | — | vast, **60 s** (v0.47) |
   | Bewaren | **2 weken**, daarna automatisch verwijderd; eerder handmatig mogelijk | tot de gebruiker hem verwijdert |

   Alle waarden instelbaar in de configuratie (§5.4). Een foto bij een alarm (§5.5.3
   punt 5) is een gewone foto via poll-nu, met het alarm als aanleiding en dezelfde
   bewaartermijn.
3. Een poll-nu voor video **tijdens een lopende video wordt genegeerd**.
4. De camera-reader schrijft het bestand naar de opnamemap en publiceert de metadata via
   MQTT; de opslag-service legt die vast in de tabel `opname` (§5.3.1). Het beeld zelf gaat
   niet via MQTT.
5. Voor video's geldt een **maximale ruimte** (instelbaar). Dreigt die vol te raken, dan
   verwijdert de opslag-service de **oudste video's** tot er weer ruimte is.
6. **Opnamemethode** (besluit OPEN-24, v0.37): de **foto** komt van het snapshot-adres
   van de camera (één HTTP-aanvraag), de **video** van de RTSP-stroom met `ffmpeg`. Beide
   adressen staan per camera in de configuratie, zodat een ander merk camera geen nieuwe
   code vraagt.
7. **Configuratie** (§5.4): één blok `[[camera]]` per camera en één blok `[opnames]`.

   | Veld van `[[camera]]` | Betekenis |
   |---|---|
   | `id` | vaste naam; staat in de database en in de bestandsnaam |
   | `naam` | naam voor weergave |
   | `foto_url` | het snapshot-adres (`http://` of `https://`) |
   | `video_url` | het adres van de RTSP-stroom (`rtsp://`); zonder dit veld maakt de camera geen video's |
   | `gebruiker`, `wachtwoord` | het account op de camera |
   | `foto_interval` | standaard `"3m"` |
   | `video_duur` | standaard `"60s"` (v0.47; was `"120s"`) |
   | `richten` | het merk van een camera die kan bewegen, nu alleen `"foscam"`; zonder dit veld kan de camera niet gericht worden (punt 19, v0.71) |
   | `richt_stap` | hoe lang de camera per klik beweegt; standaard `"0.5s"` (punt 19) |

   | Veld van `[opnames]` | Betekenis |
   |---|---|
   | `map` | de opnamemap; standaard `data/opnames` |
   | `foto_bewaren` | standaard `"14d"` |
   | `video_ruimte` | maximale ruimte voor video's in GB; standaard 20 |

   In de adressen staan `{gebruiker}` en `{wachtwoord}` op de plaats waar het account
   moet komen; de camera-reader vult ze in (gecodeerd, zodat een wachtwoord met tekens
   als `&` of `@` het adres niet breekt). Het wachtwoord staat zo op één plek. Het
   account mag ontbreken (een camera zonder aanmelding); `gebruiker` en `wachtwoord`
   horen wel bij elkaar. Zonder `naam` geldt het `id`. Grenzen (v0.38): `foto_interval`
   10 s tot 24 uur, `video_duur` 5 s tot 10 min, `foto_bewaren` minstens een uur. De
   opnamemap hoort binnen `data/` te staan: alleen daar mogen de services schrijven.
8. Het **wachtwoord** van de camera komt nooit in het log, een foutmelding of de trace
   (zoals de Victron-sleutel en de Telegram-token): een adres wordt alleen getoond als
   schema, host en poort (v0.38), ook in een foutmelding van `check-config`. Bij `ffmpeg` staat het adres met wachtwoord in de opdrachtregel en is het
   zolang de opname loopt zichtbaar voor wie op de Pi is ingelogd; dat is alleen de
   gebruiker zelf.
9. De camera-reader is een eigen service, **`brms-camera`** (§3.1). Hij heeft `ffmpeg`
   nodig (via apt; `deploy/install.sh` installeert het). `check-config` meldt het als er
   een `video_url` is ingesteld en `ffmpeg` ontbreekt.
10. **Foto.** De reader haalt het snapshot-adres op (timeout 10 s) en controleert dat het
    antwoord een JPEG is. Het bestand wordt eerst onder een tijdelijke naam geschreven en
    dan hernoemd, zodat er nooit een half bestand staat. Plaats:
    `<map>/<camera_id>/JJJJ/MM/DD/UUMMSS.jpg` (UTC). In de tabel `opname` staat het pad
    binnen de opnamemap (`dek/2026/11/10/203000.jpg`). Een antwoord groter dan 20 MB is
    geen foto.
11. **Video.** De reader start `ffmpeg` met de RTSP-stroom over TCP, de vaste duur en
    **zonder omrekenen** (het beeld wordt gekopieerd naar een MP4-bestand); het geluid
    wordt niet opgenomen. Dezelfde plaats en naamgeving als de foto, met `.mp4`. Is
    `ffmpeg` 30 s na de duur nog niet klaar, dan wordt het gestopt. Een mislukte opname
    laat geen bestand achter. Tijdens een video gaan de intervalfoto's gewoon door.
    De tijd van een video (bestandsnaam en tabel) is het begin van de opname. Stopt de
    service tijdens een video, dan wordt de opname afgebroken en verdwijnt het halve
    bestand (v0.39). Vanaf v0.48 (gebouwd in v0.49) komt de stroom van go2rtc op de Pi,
    met de camera zelf als terugval (punt 18).
12. **Poll-nu** (§5.2.1 punt 6): opdracht `brms/opdracht/camera/poll-nu` met
    `{"camera_id": ..., "type": "foto"|"video", "aanleiding": ...}`. Zonder `camera_id`:
    alle camera's.
13. Na een geslaagde opname publiceert de reader de metadata op `brms/opname/<camera_id>`
    (§3.3). Een **mislukte** opname (camera onbereikbaar, geen JPEG, `ffmpeg` geeft een
    fout) geeft één waarschuwing in het log en geen bericht; de reader probeert het niet
    opnieuw, de volgende intervalfoto komt vanzelf. Mislukken de intervalfoto's keer op
    keer, dan komt alleen de eerste keer een waarschuwing en één regel bij herstel
    (v0.38, zoals bij de Shelly); een mislukte foto op verzoek wordt altijd gemeld. Uitval van de camera
    is te bewaken met een alarmregel "camera stil" (§5.5.3 punt 2, v0.51). Zolang de klok niet gesynchroniseerd
    is, maakt de reader geen opnames (§4.2).
14. De **opslag-service** legt de metadata vast in de tabel `opname` en ruimt op: foto's
    ouder dan de bewaartermijn eenmaal per uur, en de oudste video's zodra de maximale
    ruimte vol raakt (punt 5): dat wordt bekeken na elke nieuwe video en eenmaal per uur,
    met de groottes uit de tabel (v0.39). Een bestand zonder rij in de tabel (het bericht ging
    verloren, §3.3) wordt na de bewaartermijn ook verwijderd, en mappen die leeg raken
    ook. De opslag neemt alleen een pad van de vaste vorm van punt 10 aan (v0.38), zodat
    het opruimen nooit een bestand buiten de opnamemap raakt.
15. **Foto bij een alarm** (§5.5.3 punt 5 en 6). De alarmlaag geeft een poll-nu met de
    regel als aanleiding (`alarm:<alarm_id>`), wacht hooguit 15 s op het bericht op
    `brms/opname/<camera_id>` met die aanleiding en leest het bestand uit de opnamemap
    (alleen-lezen). Telegram krijgt de foto als afbeelding met de meldingstekst als
    onderschrift, de e-mail als bijlage. Welke camera, staat in de regel (`camera =
    "<id>"`); zonder dat veld de eerste camera uit de configuratie.

    Gebouwd in v0.40, met deze invulling:
    - Het wachten gebeurt naast de alarmlaag: metingen worden intussen gewoon beoordeeld.
    - Gaat dezelfde regel voor meer sensoren tegelijk af, dan delen die meldingen één
      opdracht en één foto.
    - Een herstelmelding krijgt geen foto. Een foto groter dan 10 MB gaat niet mee (de
      grens van Telegram).
    - Mislukt het versturen, dan blijft de foto bij de melding voor de volgende poging
      (§5.5.4 punt 6).
    - `foto` kan niet samen met `melding = "uit"`; een losse foto is een actie.
    - De alarmlaag leest alleen een pad van de vaste vorm van punt 10.
16. De camera wordt **niet gesimuleerd** (§10.3): de reader is te testen met een echte
    camera. De testcamera thuis is een Foscam FI9826W (192.168.178.202, webpoort 89),
    met deze adressen (beide beproefd, v0.39):

    ```toml
    [[camera]]
    id = "dek"
    naam = "Dek"
    gebruiker = "brms"
    wachtwoord = "..."
    foto_url = "http://192.168.178.202:89/cgi-bin/CGIProxy.fcgi?cmd=snapPicture2&usr={gebruiker}&pwd={wachtwoord}"
    video_url = "rtsp://{gebruiker}:{wachtwoord}@192.168.178.202:89/videoMain"
    ```
17. **Bouwvolgorde:** (a) camera-reader met foto per interval en poll-nu, tabel `opname`
    en het opruimen van foto's; (b) video en de maximale ruimte; (c) foto en video bij
    een alarm (alarmlaag en meldingskanalen); (d) bekijken en verwijderen in het web
    (§5.6); (e) het live-beeld (punt 18); (f) het richten van de camera (punt 19).
18. **Live-beeld** (herzien in v0.48; zo gebouwd in v0.49, code v0.18.0. De versie van
    v0.44/v0.45 met de MJPEG-stroom van de camera is vervallen). Kijken en opnemen zijn los van
    elkaar: "Live camera" toont wat de camera nu ziet; "Foto" en "Video" leggen iets vast
    terwijl het live-beeld doorloopt (§5.6 punt 2).

    **Aanleiding voor de herziening** (gemeten op 2026-11-11 met de testcamera). Het
    live-beeld via de MJPEG-stroom was in de praktijk onbruikbaar:
    - de **achterstand liep op**: ongeveer 1 s na 5 s kijken, ongeveer 3 s na 25 s. De
      camera maakt 30 beelden per seconde maar krijgt er ongeveer 27 verstuurd; de rest
      stapelt zich op. Dat de webservice er maar 5 doorgaf, hielp daar niet tegen;
    - het **eerste beeld** kwam na 3 tot 6 s, en bij 1 op de 10 pogingen helemaal niet;
    - 5 beelden per seconde op 640×480.

    **Redenering.** De camera levert zijn beeld als H.264 over RTSP. Een browser kan geen
    RTSP openen; er moet dus iets tussen. **go2rtc** pakt de H.264-video alleen opnieuw in
    voor de browser (WebRTC), zonder omrekenen: het volle beeld op volle snelheid, met
    weinig vertraging en vrijwel geen rekenwerk op de Pi. Het houdt één verbinding met de
    camera voor alle afnemers, en alleen zolang er een afnemer is. RTSP heeft vrijwel
    elke IP-camera (ONVIF schrijft het voor); een MJPEG-stroom niet. De prijs is een extra
    programma van een ander op de Pi (P3); dat weegt hier op tegen een live-beeld dat
    werkt. De databundel is 40 GB per maand, dus het volle beeld is betaalbaar.

    **Gemeten met de proef** (go2rtc 1.9.15 op de test-Pi, 2026-11-11):

    | | MJPEG van de camera | go2rtc (WebRTC) |
    |---|---|---|
    | Eerste beeld | 3 tot 6 s; 1 op de 10 keer niet | 1,4 s |
    | Beelden per seconde | 5 | 30 |
    | Resolutie | 640×480 | 1280×720 |
    | Vertraging | loopt op (ongeveer 3 s na 25 s) | minder dan 2 s, loopt niet op |
    | Data | ongeveer 180 kB/s | ongeveer 230 kB/s (1,8 Mbit/s, ongeveer 14 MB per minuut) |
    | Processor Pi | vrijwel niets | ongeveer 5% van één kern |

    - Getest in Chrome op de pc (thuisnetwerk) en in Safari op een iPhone via het mobiele
      netwerk en Tailscale: vloeiend beeld, met een rechtstreekse Tailscale-verbinding.
    - **Kijken en opnemen tegelijk:** een opname via go2rtc tijdens het kijken hield 30
      beelden per seconde. Een opname met een tweede, eigen verbinding naar de camera
      zakte naar 17 beelden per seconde, en het live-beeld naar ongeveer 24.
    - Na de laatste afnemer verbreekt go2rtc de verbinding met de camera vanzelf.
    - **Niet getest:** de 4G-verbinding van de router op de boot (de stroom vraagt
      1,8 Mbit/s omhoog); dat kan pas met de RUT241.

    **Specificatie.**
    - **go2rtc is een eigen service, `brms-go2rtc`**, op een **vaste versie** (1.9.15).
      `install.sh` en `update.sh` halen het programma op als het ontbreekt of de versie
      niet klopt, en controleren het met een controlegetal (SHA-256) dat in de repo
      staat (§10.1 punt 6). Het wordt niet vanzelf bijgewerkt.
    - **De instellingen van go2rtc komen uit `brms.toml`.** Bij het starten van de service
      schrijft BRMS het instellingenbestand van go2rtc in `data/`, alleen leesbaar voor de
      eigenaar (het bevat het wachtwoord van de camera, punt 8). Elke camera met een
      `video_url` wordt een stroom met het `id` van de camera als naam. Er zijn geen
      nieuwe velden in `brms.toml`; een wijziging van een camera vraagt een herstart van
      `brms-go2rtc`.
    - **go2rtc is alleen op de Pi zelf te bereiken** voor beheer en voor RTSP
      (`127.0.0.1`, poort 1984 en 8554): zijn beheerpagina heeft geen aanmelding en toont
      het wachtwoord van de camera. Alleen de poort voor de video zelf (**8555**, TCP en
      UDP) staat open voor het thuisnetwerk en Tailscale.
    - **Via de webservice.** De pagina vraagt het live-beeld aan op `/live/<camera_id>`:
      zij stuurt haar WebRTC-aanbod, de webservice geeft dat door aan go2rtc en stuurt het
      antwoord terug. Daarna loopt de video rechtstreeks tussen de browser en go2rtc. Het
      wachtwoord van de camera blijft zo op de Pi. De webservice praat niet meer zelf met
      de camera; de uitzondering van v0.44 vervalt.
    - **Adressen.** go2rtc biedt de browser de adressen van de Pi aan: dat van het
      (boord)netwerk en dat van Tailscale. In de proef zijn ze met de hand ingesteld; bij
      het bouwen wordt nagegaan of go2rtc ze zelf vindt. Zo niet, dan schrijft BRMS ze bij
      het starten in het instellingenbestand.
    - **Elke camera met een `video_url` heeft live-beeld.** De velden `live_url` en
      `live_beelden` vervallen. Staan ze nog in `brms.toml`, dan geeft `check-config` een
      waarschuwing en geen fout, zodat een update er niet op stukloopt.
    - **Video-opnames lopen via go2rtc** (wijzigt punt 11): de camera-reader neemt op van
      de stroom die go2rtc op de Pi doorgeeft, zodat er één verbinding met de camera is.
      Is go2rtc niet bereikbaar, dan neemt de reader rechtstreeks van de camera op, zoals
      tot nu toe: een video bij een alarm mag niet afhangen van go2rtc. Foto's blijven van
      het snapshot-adres komen.
    - **Vanzelf stoppen.** Het live-beeld stopt als de gebruiker het stopt of de pagina
      verlaat, en anders na **10 minuten**: een vergeten tabblad kost anders ongeveer
      20 GB per dag. De pagina meldt dat; opnieuw starten kan direct. De pagina bewaakt
      deze tijd zelf.
    - **Hooguit 3 kijkers** tegelijk; de webservice vraagt het aantal bij go2rtc op en
      weigert een vierde.
    - **De pagina meldt** wat er gebeurt: "verbinden", en als er na 10 s geen beeld is,
      waarom niet (go2rtc of de camera onbereikbaar, te veel kijkers).
    - **Geen geluid.**
    - **Eis aan de camera:** H.264 over RTSP. H.265 spelen browsers op deze manier niet
      af; een camera die beide kan, wordt op H.264 gezet.
    - De code voor de MJPEG-stroom verdwijnt uit de webservice en de pagina.

    **Verworpen bij de herziening** (v0.48):
    - *De MJPEG-stroom van de camera houden.* Zie de aanleiding; bovendien heeft niet
      elke camera zo'n stroom.
    - *`ffmpeg` zet de RTSP-stroom om in losse JPEG-beelden.* In een proef goed bruikbaar
      (10 beelden per seconde op 640 breed, ongeveer 100 kB/s, vertraging ongeveer 1 s),
      maar het kost ongeveer 35% van een processorkern en geeft een kleiner en trager
      beeld dan de camera levert.
    - *De camera-reader leest de stroom doorlopend en verdeelt de beelden over de
      MQTT-bus.* Doorlopend decoderen belast de Pi en de camera maandenlang voor niets,
      en video hoort niet op de bus waar de metingen overheen gaan.
    - *MSE in plaats van WebRTC.* Eenvoudiger (alles over de gewone webverbinding), maar
      het gaf met de testcamera geen beeld in Chrome; de oorzaak is niet gevonden.
    - *HLS.* Loopt seconden achter (zie hierboven, v0.44).
    - *Een zuinige tweede stroom voor mobiel kijken.* Niet nodig met 40 GB per maand; bij
      de testcamera is de tweede stroom bovendien gelijk aan de eerste.

    **Invulling bij het bouwen** (v0.49):
    - **Adressen:** go2rtc vindt de adressen van de Pi zelf (het netwerk en Tailscale);
      BRMS hoeft ze niet in te stellen. go2rtc leest ze alleen bij het starten. Daarom
      wacht `brms go2rtc` bij het starten hooguit 30 s tot Tailscale een adres heeft
      (alleen als Tailscale op de Pi staat): na het opstarten van de Pi kan Tailscale
      iets later klaar zijn dan deze service.
    - **Geen hulp van een server op internet (STUN)**, niet bij go2rtc en niet in de
      pagina: de browser bereikt de Pi rechtstreeks via het netwerk of Tailscale. Met de
      standaardinstelling van go2rtc (een STUN-server van Google) duurde het aanvragen
      van het live-beeld 9 s; zonder een halve seconde.
    - **`brms go2rtc`** is de opdracht van de service: hij schrijft `data/go2rtc.yaml` en
      start dan go2rtc in plaats van zichzelf. Het programma staat in `bin/go2rtc`.
    - **Ontbreekt go2rtc** (ophalen mislukt, verkeerd controlegetal), dan gaat een update
      of installatie gewoon door met een waarschuwing: alleen het live-beeld ontbreekt.
      `check-config` meldt het; de service probeert het elke 30 s opnieuw.
    - **Terugval bij een video:** de camera-reader neemt ook rechtstreeks van de camera
      op als de opname via go2rtc mislukt (niet alleen als go2rtc niet draait). De opname
      begint dan opnieuw.
    - **De pagina** stopt het live-beeld met een melding als er na 10 s nog geen beeld is,
      als er 15 s geen nieuw beeld komt, of als de verbinding wegvalt.
    - **Waarschuwingen in de configuratie:** `check-config` en het log van elke service
      tonen een waarschuwing voor `live_url` en `live_beelden`; de waarde wordt niet
      gecontroleerd.
    - **Gemeten met de gebouwde pagina** (test-Pi, Chrome op de pc): eerste beeld na
      2,1 s, 1280×720, 30 beelden per seconde. Een video van 10 s via go2rtc: 271 beelden.
    - **Eerste update naar deze versie:** de update wordt nog uitgevoerd door het oude
      `update.sh`, dat go2rtc niet kent. Daarna is één keer extra `brms update` nodig: die
      haalt go2rtc op en start de service.
19. **Richten van de camera** (ontwerp v0.71; gebouwd in v0.72, code v0.25.0; werkt met
    de Foscam FI9826W, v0.73).

    **Redenering.** De binnencamera (Foscam FI9826W) kan draaien en kantelen. Tijdens het
    live kijken wil de gebruiker hem kunnen richten. Een vaste camera kan dat niet en
    krijgt de knoppen dus niet; welke camera het kan, staat in `brms.toml`. De
    verworpen alternatieven staan bovenaan deze paragraaf.

    **Specificatie.**
    - **Configuratie.** Het veld `richten` van `[[camera]]` (punt 7) noemt het merk; nu
      bestaat alleen `"foscam"`. Zonder het veld is het een vaste camera. `check-config`
      weigert een onbekend merk, en `richten` bij een camera zonder `video_url` (zonder
      live-beeld zijn er geen knoppen). `richt_stap`: 0,1 s tot 5 s.
    - **Knoppen.** Zolang het live-beeld loopt van een camera met `richten`, toont de
      pagina vier knoppen: op, neer, links, rechts (§5.6 punt 2). Bij een camera zonder
      `richten` staan ze er niet.
    - **Stap per klik.** Eén klik beweegt de camera gedurende `richt_stap` en stopt hem
      dan. Het stopcommando komt van de camera-reader op de Pi, niet van de browser: valt
      de verbinding met de browser weg, dan stopt de camera toch.
    - **De weg van een klik.** De pagina stuurt de richting naar de webservice
      (`/richt/<camera_id>`); die geeft de opdracht `brms/opdracht/camera/richt` met
      `{"camera_id": ..., "richting": "op"|"neer"|"links"|"rechts"}` (§3.3). De
      camera-reader voert hem uit. De webservice praat zo niet zelf met de camera en het
      wachtwoord blijft bij de camera-reader (punt 8 en 18).
    - **Tijdens een lopende stap** wordt een nieuwe opdracht voor die camera genegeerd
      (zoals bij video, punt 3).
    - **Geen vaste stand.** De camera blijft staan waar hij gelaten is. De intervalfoto's
      en de foto of video bij een alarm tonen dus wat de camera op dat moment ziet.
    - **Foscam.** De commando's gaan naar hetzelfde adres als de foto: schema, host en
      poort komen uit `foto_url`, het account uit `gebruiker` en `wachtwoord`. Bewegen:
      `cmd=ptzMoveUp`, `ptzMoveDown`, `ptzMoveLeft`, `ptzMoveRight`; stoppen:
      `cmd=ptzStopRun` (op `/cgi-bin/CGIProxy.fcgi`). Beproefd met de testcamera (v0.73).
    - **Fouten.** Mislukt een commando (camera onbereikbaar, afgewezen), dan komt er één
      waarschuwing in het log, zonder het wachtwoord (punt 8). Mislukt het bewegen, dan
      volgt geen stop; mislukt het stoppen, dan probeert de reader het één keer opnieuw.
      De pagina toont geen bevestiging: het live-beeld laat zien of de camera beweegt.
    - **Een ander merk** vraagt nieuwe code in de camera-reader en een nieuwe waarde voor
      `richten`; de rest (knoppen, opdracht, stap) blijft gelijk.
    - Zoomen en opgeslagen standen (presets) horen er niet bij.

    **Invulling bij het bouwen** (v0.72):
    - **Het antwoord van de camera wordt gecontroleerd.** Een Foscam antwoordt ook bij een
      verkeerd wachtwoord of te weinig rechten gewoon (HTTP 200); alleen `<result>0</result>`
      in het antwoord betekent "gelukt". Al het andere is een mislukt commando, met de
      reden in het log.
    - **Rechten van het account.** Het account van de camera moet hem mogen bewegen: bij
      een Foscam een "operator"; een "visitor" (alleen kijken) wordt afgewezen. Het
      voorbeeldbestand vermeldt dit.
    - **Los van de foto's.** Een richtopdracht gaat niet door de wachtrij van de opnames
      maar start direct: een klik wacht niet op een foto die net wordt opgehaald.
    - **Stopt de service tijdens een stap**, dan maakt de reader de stap eerst af, zodat
      de camera zijn stopcommando krijgt.
    - **De pagina** schakelt de vier knoppen uit zolang een stap loopt (de duur van
      `richt_stap`); zo stapelen klikken zich niet op. De pagina krijgt niet het merk van
      de camera, alleen of hij gericht kan worden.
    - **Wachttijd** op het antwoord van de camera: 5 s per commando.
    - `richt_stap` zonder `richten` is een fout in `check-config`.
    - **Beproefd met de camera** (v0.73, door de gebruiker op 2026-11-16): het richten
      werkt met de Foscam FI9826W.

---

## 6. Gegevensstroom

Reader ontvangt/leest → decodeert → past correcties uit config toe → berekent afgeleide
grootheden (§5.2.5) → publiceert elke waarde via MQTT (§3.1, §5.2.1) → de alarmlaag beoordeelt elke waarde; de opslag middelt of
registreert per 5 minuten (§5.3.2) → web leest uit SQLite.

---

## 7. Betrouwbaarheid

| Maatregel | Vangt af |
|---|---|
| systemd met `Restart=always` | Crash van de software |
| Hardware-watchdog van de Pi | Vastgelopen systeem |
| "Ping Reboot" van de router (RutOS) | Router of 4G-verbinding vastgelopen: de router herstart zichzelf als internet een tijd onbereikbaar is |
| SQLite WAL | Datacorruptie bij stroomuitval |
| Wekelijkse back-up naar OneDrive (§7.1) | Verlies van de opslag |

**Geen noodingang via Teltonika RMS (v0.54).** Tot v0.53 stonden hier Teltonika RMS (de
router op afstand beheren en herstarten) en een relais op de digitale uitgang van de
router om de Pi te herstarten. Beide zijn vervallen: RMS is een extra externe dienst waar
het systeem dan van afhangt, en het nut is klein. Wat ervoor in de plaats komt, werkt
lokaal en vanzelf: de hardware-watchdog herstart een vastgelopen Pi, en de router herstart
zichzelf met "Ping Reboot". Zonder RMS is het relais niet op afstand te bedienen; het
vervalt daarom ook. Geaccepteerd gevolg: werkt Tailscale niet meer terwijl de Pi wel
draait, dan is er geen toegang op afstand en moet er iemand naar de boot.

**[OPEN-21]** Maatregelen op OS-niveau tegen flashslijtage (o.a. journald-logs, swap,
caches). Het schrijfvolume van de database zelf is verwaarloosbaar (§5.3.2).
### 7.1 Back-up (besluit OPEN-16)

**Aanleiding.** Vastgesteld moest worden hoe vaak, hoe en waarheen een back-up wordt gemaakt.

**Redenering.** Een back-up naar een machine thuis vereist een machine die altijd aan staat.
Cloudopslag heeft dat niet nodig en staat direct buiten de boot. Microsoft 365 is aanwezig
(1 TB OneDrive), ruim genoeg voor database, foto's en video's. `rclone` ondersteunt OneDrive
stabiel en kan na eenmalige autorisatie op de pc (`rclone authorize`) maanden onbeheerd
werken; het token wordt bij gebruik vanzelf vernieuwd. Het raakt P4 niet: de back-up is
geen onderdeel van de kernfunctie.

`rclone copy` kopieert alleen nieuwe of gewijzigde bestanden; foto's en video's die al in
OneDrive staan gaan niet opnieuw over de lijn. De database is één bestand dat steeds
verandert en gaat elke keer volledig mee (na een jaar ~150 MB per back-up); Microsoft 365
bewaart eerdere versies van dat bestand. Een draaiende WAL-database rechtstreeks kopiëren is
onbetrouwbaar; er is eerst een consistente kopie nodig.

Eén keer per week is voldoende. Gevolg: bij uitval van de flashdrive gaat hooguit een week
aan data verloren. Omdat foto's lokaal 2 weken bewaard blijven, is elke foto bij minstens
één back-up aanwezig – mits het back-up-interval korter blijft dan die bewaartermijn.

Foto's alleen al kunnen ~4,5 GB per maand aan mobiele data kosten; daarom is het meenemen
van opnames instelbaar.

**Verworpen alternatieven.**
- *Naar een machine thuis via Tailscale (pull of push).* Vereist een machine die altijd aan staat.
- *iCloud Drive.* Ondersteuning in `rclone` is relatief nieuw, en de sessie vraagt periodiek
  opnieuw tweestapsverificatie – niet onbeheerd te gebruiken.
- *Spiegelen (`rclone sync`).* Wat op de boot verwijderd wordt (ook per ongeluk) verdwijnt
  dan ook uit de back-up.
- *Apart Microsoft-account voor BRMS.* Zou de schade beperken als de Pi gestolen wordt (het
  token geeft toegang tot de hele OneDrive). Bewust niet gekozen; het risico is geaccepteerd.
- *Versleutelen met `rclone crypt`.* Niet nodig geacht.
- *Dagelijks.* Niet nodig; wekelijks is voldoende.

**Specificatie.**
1. **Eén keer per week** een back-up naar **OneDrive** (eigen Microsoft 365-account) met
   **`rclone copy`** (kopiëren, niet spiegelen), onversleuteld.
2. De database wordt eerst als **consistente kopie** weggeschreven en die kopie gaat mee.
3. Foto's en video's gaan mee als dat in de configuratie aanstaat (§5.4); alleen nieuwe
   bestanden worden overgezet.
4. Het back-up-interval moet korter zijn dan de bewaartermijn van foto's (§5.7).
5. Hooguit een week dataverlies bij uitval van de opslag is geaccepteerd.
**[OPEN-17]** Wat bewaakt de watchdog precies (alleen kernel, of ook of de logger leeft)?

---

## 8. Bekende beperking: melden zonder internet

Als het mobiele internet uitvalt, komen meldingen niet aan en is het systeem niet
bereikbaar; metingen gaan wel door (P4). **[OPEN-18]** Moet uitval van BRMS zelf
(geen heartbeat meer) thuis gedetecteerd worden?

---

## 9. Netwerk en toegang

- Pi via ethernet aan Teltonika RUT241 (wifi van router als reserve).
- De binnencamera (Foscam) ook met een netwerkkabel aan de RUT241 (v0.89, §5.7 punt 1);
  de Shelly's via de wifi van de router.
- Mobiel internet zit achter CGNAT → Tailscale op de Pi (key expiry uit), met subnet-route
  naar router en camera. Niets open op internet.
- *Subnet-route (v0.92, ingericht en gecontroleerd op de boot-Pi).* De Pi biedt het hele
  bootnetwerk `192.168.1.0/24` aan in Tailscale. Elk apparaat in het tailnet (pc thuis,
  telefoon) bereikt daarmee de router (`http://192.168.1.1`), de camera en de Shelly's op
  hun eigen adres, bijvoorbeeld om de camera in te stellen via zijn webpagina. Er is geen
  SSH-tunnel en geen browser op de Pi voor nodig. Voorwaarde: het netwerk waar je zit
  gebruikt niet zelf `192.168.1.x` (thuis is dat `192.168.178.x`); anders botsen de
  adressen en blijft een SSH-tunnel over (`ssh -L 8088:<adres>:<poort> brms`).
  Inrichten, eenmalig per Pi:
  1. Doorsturen van IP-verkeer aanzetten, blijvend na een herstart:
     `echo 'net.ipv4.ip_forward = 1' | sudo tee /etc/sysctl.d/99-tailscale.conf` en
     `sudo sysctl -p /etc/sysctl.d/99-tailscale.conf`.
  2. De route aanbieden: `sudo tailscale set --advertise-routes=192.168.1.0/24`.
  3. De route goedkeuren in de beheerpagina van Tailscale (Machines → `brms` →
     *Edit route settings*).

  Windows en iOS nemen een aangeboden route vanzelf over; een Linux-apparaat pas na
  `tailscale set --accept-routes`.
- Geen noodingang buiten Tailscale om (v0.54): Teltonika RMS staat uit en het relais op de
  router-uitgang vervalt (§7). De router herstart zichzelf met "Ping Reboot".

---

## 10. Ontwikkeling en uitrol

- Ontwikkelen in PyCharm op Windows; testen op de test-Pi 4 thuis (zelfde OS).
- Uitrol als systemd-services uit `deploy/systemd/`.

### 10.1 Uitrol en updates (besluit OPEN-19)

**Aanleiding.** Vastgesteld moest worden hoe de code op de (test-)Pi komt, hoe updates aan
boord worden uitgerold, en hoe de installatie op de Pi is ingedeeld.

**Redenering.** De code staat in een repo op GitHub. De boot-Pi draait onbeheerd en is
alleen via Tailscale bereikbaar; een update mag niets breken en moet terug te draaien zijn.
Met git op de Pi hoort elke installatie bij één commit en is teruggaan één commando.

*Ophalen via https (v0.74).* De repo was eerst privé; de Pi las hem met een deploy key (een
SSH-sleutel met alleen leesrechten op deze ene repo). De repo is nu openbaar: lezen kan via
https, zonder sleutel of account. Dat scheelt een stap bij het inrichten (de sleutel bij
GitHub toevoegen) en er staat geen sleutel meer op de Pi. Geheimen staan niet in de repo:
`config/brms.toml` en `data/` staan in `.gitignore`.

Updates worden met de hand gestart. Daarmee kan ook de boot-Pi gewoon `main` volgen: de
gebruiker kiest zelf het moment, nadat `main` op de test-Pi goed draait. Voorwaarde is dat
tussen "getest" en "bijgewerkt aan boord" niets meer naar `main` gaat; met één ontwikkelaar
is dat overzichtelijk. Eén spoor is eenvoudiger dan versietags (P1).

*Indeling (herzien in v0.15).* Oorspronkelijk volgde de indeling de Linux-conventie
(`/opt/brms`, `/etc/brms`, `/var/lib/brms`) met een eigen systeemgebruiker `brms`. In de
praktijk bleken de bestanden zo lastig te vinden. De gebruiker wil op de Pi dezelfde
indeling als op de pc: één map `~/BRMS` (een git-clone) met daarin `brms/`, `config/` en
`data/`. Configuratie en data staan in `.gitignore`, dus een `git pull` raakt ze nog steeds
niet. Een systeemgebruiker kan niet zonder meer in een home-map lezen en schrijven; de
services draaien daarom als de gebruiker die BRMS beheert (`jandirk`). Het nadeel – de
services hebben meer rechten dan nodig – is bewust geaccepteerd; systemd beperkt ze
(alleen-lezen systeem en home, schrijven alleen in `data/`).

Automatische OS-updates kunnen ongemerkt iets breken (CLAUDE.md); er staat niets open op
internet (alleen Tailscale), dus het veiligheidsrisico van handmatig bijwerken is klein.

**Verworpen alternatieven.**
- *Kopiëren vanaf de pc (rsync / PyCharm-deployment).* Wat draait is niet te herleiden tot
  een commit; niet-gecommitte wijzigingen kunnen meegaan.
- *Als pakket (wheel) bouwen en installeren.* Extra stap bij elke update.
- *Docker.* Zwaar; een extra laag die moet blijven werken (P1).
- *Boot-Pi alleen op versietags.* Markeert expliciet "getest", maar is bij handmatige
  updates niet nodig; tags blijven mogelijk als mijlpaal.
- *Linux-conventie met systeemgebruiker `brms` (`/opt/brms`, `/etc/brms`, `/var/lib/brms`).*
  Besluit tot v0.14; verlaten in v0.15 omdat de bestanden op de Pi lastig te vinden waren.
- *Automatische OS-updates (unattended-upgrades).* Zie redenering.
- *Ophalen via SSH met een deploy key.* Besluit tot v0.73, toen de repo privé was; verlaten
  in v0.74. Bij een openbare repo voegt de sleutel niets toe en kost hij een extra stap.

**Specificatie.**
1. De Pi haalt de code met **git** uit de openbare GitHub-repo, via **https**
   (`https://github.com/jdderijke/BRMS.git`), zonder sleutel (v0.74). `install.sh` zet bij
   een bestaande clone het adres van `origin` om naar https; de oude deploy key en de
   SSH-alias `github-brms` blijven staan en mogen met de hand weg.
2. **Test-Pi en boot-Pi volgen beide `main`.** Een update aan boord gebeurt pas als `main`
   op de test-Pi goed draait.
3. Updates via **`deploy/update.sh`**:
   1. de huidige commit wegschrijven (voor teruggaan);
   2. `git fetch` en `main` uitchecken;
   3. ontbrekende systeempakketten uit `deploy/pakketten.txt` installeren (apt; v0.17)
      en `pip install -r requirements.txt` in de venv (vastgepinde versies);
   4. `brms check-config` (§5.4.1) – bij een fout stopt de update en draait de oude
      versie door;
   5. de services herstarten.

   **`deploy/update.sh --terug`** gaat terug naar de weggeschreven vorige commit.

   Welke services er zijn, leest het script uit de units in `deploy/systemd/` ná het
   ophalen van de nieuwe code (v0.23): een service die met de update nieuw is, wordt
   vanzelf geïnstalleerd, aangezet en gestart. Is de code al bij, dan controleert het
   script of elke service aanstaat en draait, en herstelt dat zo nodig. De gebruiker
   hoeft voor een update nooit zelf een `systemctl`-opdracht te geven.

   **Verder met het nieuwe script (v0.53; gebouwd in code v0.19.1).** Na het uitchecken
   (stap 2) neemt het `update.sh` uit de nieuwe code het over en doet de stappen 3 tot en
   met 5. Zonder dat voert het oude script de hele update uit, en dat kent een stap die
   met de update nieuw is niet (een nieuwe service, de polkit-regel van v0.52); er was dan
   een tweede `brms update` nodig. Nu volstaat één keer. Terugdraaien bij een fout werkt
   hetzelfde: het nieuwe script krijgt de oude commit mee. Bij `--terug` naar een versie
   van vóór v0.19.1 maakt het huidige script de update zelf af. Dit werkt vanaf de update
   ná v0.19.1: de update naar v0.19.1 zelf wordt nog door het oude script uitgevoerd.

   De gebruiker start dit met **`brms update`** en **`brms update --terug`** (v0.21). De
   opdracht doet zelf niets anders dan het script uitvoeren, en vervangt daarbij zijn eigen
   proces door het script: tijdens de update verandert de code van BRMS, en zo draait er
   geen half oude, half nieuwe code.
4. Indeling op de Pi:

   | Wat | Waar |
   |---|---|
   | Wat | Waar |
   |---|---|
   | Code (git-clone) | `~/BRMS` (o.a. `brms/`, `deploy/`, `tests/`) |
   | Python-venv | `~/BRMS/.venv` |
   | Configuratie | `~/BRMS/config/brms.toml` (§5.4.1) |
   | Database en opnames | `~/BRMS/data/` |
   | Logs | journald |

   `config/brms.toml`, `data/` en `.venv/` staan in `.gitignore`; updates raken ze niet.
5. De services draaien als de beheerder van de Pi (**`jandirk`**, lid van de groep
   `bluetooth`). systemd beperkt ze: systeem en home alleen-lezen, schrijven alleen in
   `~/BRMS/data`. De git-clone hoort ook bij `jandirk`; alleen pakketten
   installeren en services (her)starten vraagt `sudo`.
6. Systeempakketten via apt (Mosquitto, rclone, later ffmpeg; als beheerhulpmiddelen
   `mosquitto-clients`, `sqlite3` en `tmux`). Ze staan in één lijst,
   **`deploy/pakketten.txt`** (v0.17); `install.sh` en `update.sh` installeren wat daarvan
   ontbreekt, zodat een nieuw pakket bij de eerstvolgende update vanzelf meekomt. Een
   eenmalig **`deploy/install.sh`** richt de Pi in:
   pakketten, clone, venv, configuratie, Mosquitto en systemd-units.
   Twee programma's komen niet uit apt (v0.48; gebouwd in v0.49):
   - **go2rtc** (§5.7 punt 18): één los programma in `~/BRMS/bin/` (in `.gitignore`),
     op een vaste versie. `install.sh` en `update.sh` halen het op bij GitHub als het
     ontbreekt of de versie niet klopt, en controleren het met het SHA-256-controlegetal
     dat in de repo staat. Klopt dat niet, dan wordt het niet geïnstalleerd.
   - **Tailscale:** `install.sh` installeert het met het script van Tailscale en toont
     de aanmeldlink; de gebruiker meldt de Pi één keer aan in de browser. Het script
     herinnert eraan om daarna "key expiry" voor de Pi uit te zetten (§9). Een
     aanmeldsleutel in `brms.toml` is verworpen: hij is één keer nodig en verloopt.
7. **Geen automatische OS-updates.** OS, Tailscale en router-firmware worden met de hand
   bijgewerkt: eerst de test-Pi, daarna de boot-Pi.
8. **Opdracht `brms`** (v0.18): de beheeropdrachten (`check-config`, `grootheden`,
   `monitor`, `simuleer`, `update`) zijn voor de gebruiker; de service-opdrachten (`ble-reader`, `opslag`) zijn
   voor systemd. Een service-opdracht weigert te starten als systemd die service al draait
   (een tweede exemplaar botst op de broker en, bij de opslag, op de database), behalve met
   `--toch` voor testen.
9. **`brms start`, `brms stop`, `brms herstart`** (v0.18) starten, stoppen of herstarten
   alle BRMS-services tegelijk via systemd (met `sudo`). Welke services dat zijn, volgt uit
   de units in `deploy/systemd/`, zodat een nieuwe service vanzelf meedoet. `start` en
   `herstart` controleren eerst de configuratie: bij een fout gebeurt er niets en draait
   de oude configuratie door. `stop` geldt tot de volgende herstart van de Pi (de services
   blijven bij het opstarten automatisch starten). Mosquitto valt hierbuiten. Na afloop
   tonen de opdrachten de status van elke service.

### 10.2 Beheerhulpmiddel: monitor met trace (v0.16)

**Aanleiding.** Bij het ijken en testen moet één meting door de hele keten te volgen zijn:
de ruwe gegevens zoals de reader ze ontvangt, de decodering (ruwe getallen en schaal), de
correctie, het MQTT-bericht, en wat de opslag ermee doet tot en met de regel in de database.
Dat moet kunnen terwijl de gebruiker in dezelfde ssh-sessie gewoon doorwerkt.

**Redenering.** Op de bus is alleen het MQTT-bericht zichtbaar; de andere stappen gebeuren
binnen de reader en de opslag-service. Die moeten dus zelf laten zien wat ze doen. Via de
bus, op een eigen topic, past dat in de bestaande opzet (§3.1): de monitor is gewoon een
extra afnemer, en het werkt voor elke reader op dezelfde manier – BLE (BTHome, Ruuvi,
Victron), Shelly, GPIO. Alleen op aanvraag en per sensor: aan boord komen straks tientallen
metingen per seconde binnen, en traceberichten die niemand bekijkt zijn verspilling. Een
vaste looptijd voorkomt dat trace per ongeluk blijft aanstaan.

Een databaseregel is geen kopie van één bericht: per interval worden alle waarden
samengevoegd (§5.3.2). De keten van één bericht eindigt daarom bij "opgenomen in het
interval"; bij het opslagmoment volgt de databaseregel met het aantal samengevoegde waarden.

Een vast vak onder in het scherm terwijl de shell erboven doorloopt, kan het best met
`tmux` (vensterverdeling in de terminal). Zelf een vast gebied bijhouden met
escape-codes is breekbaar (venstergrootte, programma's als `nano` en `less`).

**Verworpen alternatieven.**
- *Trace altijd aan.* Permanent veel extra berichten, ook als niemand kijkt.
- *Trace via logging (journald) en de monitor leest het log.* Minder gestructureerd, en het
  log op flash groeit mee (P7).
- *Volledige console zoals in JSEM (`tools/busmonitor`).* Meer dan nodig: BRMS heeft geen
  nodes die vanuit een console gestart en gestopt worden.

**Specificatie.**
1. Opdracht **`brms monitor [sensor ...]`** (sensor = naam uit de configuratie of
   sensor-ID). Start of hervat een tmux-sessie: bovenin de shell, onderin een vak van
   ongeveer 20 regels. De ssh-sessie mag wegvallen; opnieuw
   `brms monitor` brengt terug in dezelfde sessie.
   **`brms monitor stop`** (in de shell) sluit het vak en zet trace uit; de shell blijft.
   Muisbediening staat aan: klikken kiest een vak, de rand slepen maakt het vak groter.
2. Zonder sensor toont de monitor het verkeer op de bus (metingen en opdrachten), met naam
   uit de configuratie en eenheid uit de tabel van grootheden. Met sensor zet hij trace aan
   voor die sensor(en) en toont hij per meting de keten.

   Het vak is geen doorlopende lijst (v0.21): elke waarde heeft een **vaste plaats** en
   een nieuwe waarde overschrijft de vorige, zodat er niets verspringt.
   - *Zonder sensor:* één regel per sensor en grootheid met de laatste waarde en het
     tijdstip; bovenaan de laatste opdracht.
   - *Met sensor:* per sensor één blok met de zes stappen van punt 4, elk met het tijdstip
     van zijn laatste bericht. Een stap houdt het grootste aantal regels dat hij gehad
     heeft; kortere inhoud wordt aangevuld met lege regels, zodat de stappen eronder op
     hun plaats blijven (het aantal regels verschilt per sensor en per bericht).
   - Past niet alles in het vak, dan meldt de laatste regel hoeveel regels niet te zien
     zijn; terugbladeren kan niet in een scherm dat overschreven wordt.
3. **Trace aanzetten:** opdracht `brms/opdracht/trace/aan` met
   `{"sensor_ids": [...], "duur": 600}`; uitzetten met `brms/opdracht/trace/uit`. Trace gaat
   na `duur` seconden vanzelf uit; de monitor vernieuwt de opdracht elke minuut zolang hij
   draait en zet trace uit bij afsluiten. Alle readers en de opslag luisteren mee.
4. **Stappen** in een tracebericht (`stap`):
   | Stap | Dienst | Inhoud |
   |---|---|---|
   | `ruw` | reader | de ontvangen gegevens: bytes (BLE), JSON-antwoord (Shelly), pinstand (GPIO); bij Victron ook ontsleuteld |
   | `gedecodeerd` | reader | per grootheid: ruw getal, schaalfactor, resultaat |
   | `gecorrigeerd` | reader | per grootheid: waarde, offset uit de configuratie, resultaat |
   | `afgeleid` | reader | per afgeleide grootheid: de invoer en de berekening (§5.2.5, v0.31) |
   | `gepubliceerd` | reader | topic en inhoud van het MQTT-bericht, of "weggegooid" |
   | `opgenomen` | opslag | in welk interval, en het tussenresultaat (aantal, gemiddelde/som, toestandsverandering) |
   | `opgeslagen` | opslag | de regel(s) in de database bij het opslagmoment, met het aantal samengevoegde waarden; of "weggegooid" (klok niet gesynchroniseerd) |

   Een stap waarvoor per meting meerdere berichten komen, zet in elk bericht een veld
   **`deel`** (v0.21): de opslag stuurt `opgenomen` per grootheid, met de grootheid als
   deel. De monitor geeft elk deel een eigen vaste plaats binnen de stap.
5. De monitor **publiceert alleen trace-opdrachten**; verder luistert hij alleen. Hij hoort
   niet bij het draaiende systeem: BRMS werkt zonder monitor precies hetzelfde.
6. `tmux` wordt door `deploy/install.sh` geïnstalleerd.

### 10.3 Beheerhulpmiddel: simulatie (v0.20)

**Aanleiding.** De keten moet te testen zijn met waarden die een echte sensor thuis niet
levert (vorst, hoge luchtvochtigheid, een bijna lege sensorbatterij), en met sensoren die er
nog niet zijn (Ruuvi). De gebruiker wil daarvoor zelf een testreeks van gesimuleerde
sensordata door het systeem kunnen sturen.

**Redenering.** Een test is het meest waard als de testdata zo vroeg mogelijk in de keten
binnenkomt. Het vroegste punt is de interface zelf (de Bluetooth-adapter); dat vraagt een
tweede adapter die echte advertenties uitzendt en is voor nu te hoog gegrepen. Het vroegste
punt binnen de eigen code is waar bleak een ontvangen advertentie aflevert. Daar maakt de
reader zelf de bytes aan, in het echte formaat van de sensor, en verwerkt ze via dezelfde
ingang als een ontvangen advertentie. Daarmee worden decoder, grootheden-filter, correctie,
bus, opslag en (later) de alarmen getest; alleen de radio, BlueZ en bleak blijven buiten de
test.

De testdata loopt niet over MQTT: in het echt krijgt een reader zijn data ook niet van de
bus. Alleen het startsein gaat via de bus, zoals bij trace (§10.2).

Zendt de echte sensor tegelijk, dan lopen echte en gesimuleerde waarden door elkaar en zegt
het gemiddelde in de database niets meer. Daarom negeert de reader de echte sensor zolang
de simulatie loopt, en vraagt de opdracht daarvoor eerst om bevestiging. Een vaste looptijd
voorkomt dat een sensor per ongeluk genegeerd blijft.

**Test en demo (v0.55).** De gebruiker wil de simulatie ook veel langer dan een uur
kunnen laten lopen, om te laten zien hoe BRMS eruitziet met sensoren die er (nog) niet
zijn. Dat is een ander doel dan testen: een testreeks die dwars door de alarmgrenzen
loopt, is daar niet bruikbaar, en een einde is er niet. Daarom twee soorten. Een **test**
heeft een duur (hooguit 60 minuten) en de testreeks; een **demo** heeft geen duur en
geeft waarden rond normaal. Bij een demo vervalt de vaste looptijd als vangnet. Wat
ervoor in de plaats komt: het web toont op elke pagina of er gesimuleerd wordt, en een
herstart van de reader beëindigt elke simulatie (ze staat alleen in het geheugen van de
reader).

De gesimuleerde waarden komen als gewone metingen in de database: dat hoort bij een
volledige test, en de opslag hoeft dan geen onderscheid te kennen.

**Verworpen alternatieven.**
- *Injecteren op adapterniveau (een tweede Bluetooth-adapter zendt echte advertenties uit).*
  De meest volledige test, maar vraagt extra hardware en is niet beproefd: past een
  Ruuvi-bericht van 31 bytes, welk MAC-adres ziet de scanner. Kan later alsnog.
- *Testbytes via MQTT naar de reader sturen.* Een weg waarlangs data in het echt niet
  binnenkomt.
- *Kant-en-klare metingen direct op de bus zetten.* Slaat decoder en correctie over, en de
  echte sensor zendt er dan tussendoor.
- *Gesimuleerde waarden buiten de database houden.* Dan is de opslag niet getest.
- *Testreeks uit een bestand van de gebruiker.* Niet nodig; een vaste reeks per grootheid
  volstaat.

**Specificatie.**
1. Opdracht **`brms sim <sensor ...>`** (ook `brms simuleer`; sensor = naam uit de
   configuratie of sensor-ID). Meerdere sensoren tegelijk kan (v0.25): los of met komma's
   gescheiden, of **`all`** voor alle sensoren die gesimuleerd kunnen worden.
   Voor BTHome, Ruuvi, Victron (v0.30) en de Shelly (v0.29); GPIO volgt met zijn reader.
2. De opdracht **waarschuwt** dat de echte sensoren worden genegeerd zolang de simulatie
   loopt, vraagt om akkoord (Enter = ja) en vraagt hoe lang de simulatie moet lopen
   (v0.55):
   - **een getal** (minuten, hooguit 60): een **test**. De testreeks van punt 5 loopt
     binnen die tijd twee keer heen en weer; daarna stopt de simulatie vanzelf.
   - **alleen Enter**: een **demo** (punt 5a). Die loopt door tot `brms sim stop`.

   Daarna begint de simulatie meteen. Er wordt niet meer eerst geluisterd of de echte
   sensor zendt (v0.25): dat kostte 15 s wachttijd. Tot v0.54 betekende alleen Enter
   "10 minuten".
3. **Startsein:** opdracht `brms/opdracht/simulatie/aan` met
   `{"sensor_id": ..., "duur": 60}` (seconden) voor een test, of
   `{"sensor_id": ..., "demo": true}` voor een demo (v0.55); stoppen met
   `brms/opdracht/simulatie/uit` (`{"sensor_id": ...}`). Elke reader luistert mee en
   voert alleen de opdrachten voor zijn eigen sensoren uit. Een nieuwe opdracht voor een
   sensor vervangt de simulatie die voor die sensor loopt.
4. Zolang de simulatie loopt:
   - maakt de reader elke **2 s** een advertentie aan in het formaat van de sensor en
     verwerkt die via dezelfde ingang als een advertentie van bleak, met het MAC-adres van
     de sensor;
   - **negeert** de reader de echte advertenties van die sensor (bypass). Andere sensoren
     lopen gewoon door.

   Na `duur` stopt een test vanzelf en telt de echte sensor weer mee. Een demo stopt
   met `brms sim stop`, of als de reader herstart (`brms update`, "Herstart BRMS", een
   herstart van de Pi).

   Bij een **Shelly** (v0.29) maakt de Shelly-reader bij elke poll zelf het antwoord aan,
   in de vorm van `Shelly.GetStatus`, en verwerkt dat als een echt antwoord. De echte
   Shelly wordt zolang niet gepolld; hij hoeft er dus niet te zijn.
5. **Testreeks** (bij een test)**:** elke grootheid loopt lineair van begin- naar eindwaarde **en weer
   terug**, en doet dat binnen de duur **twee keer** (v0.35): begin → eind → begin → eind
   → begin, elk stuk een kwart van de duur. Door het teruglopen is ook het herstellen van
   een alarm te testen. De simulatie eindigt op de beginwaarden. De tabellen hieronder
   geven één heengang; dat geldt ook voor de Victron-apparaten en de bilge.

   | Grootheid | Verloop | Sensortype |
   |---|---|---|
   | `temperatuur` | 25 → −5 °C | BTHome, Ruuvi |
   | `vocht` | 40 → 95 % | BTHome, Ruuvi |
   | `batterijspanning` | 3,0 → 2,2 V | BTHome, Ruuvi |
   | `luchtdruk` | 1020 → 980 hPa | Ruuvi |
   | `versnelling_x` / `_y` / `_z` | 0 → 200 / 0 → −100 / 1000 → 975 mG | Ruuvi |
   | `beweging` | 1 per advertentie (de teller loopt telkens één op) | Ruuvi |
   | `rssi` | rond −60 dBm (er is geen adapter die hem meet) | BTHome, Ruuvi |

   Op elke waarde komt een beetje **ruis** (v0.26), zodat een gesimuleerde reeks eruitziet
   als een echte, ook voor demonstraties: standaardafwijking 0,15 °C, 0,5 % vocht, 0,005 V,
   0,15 hPa, 8 mG en 2 dBm. Het verloop van de testreeks blijft goed zichtbaar.

   De lijst `grootheden` en de `correctie` van de sensor (§5.4.2) gelden ook voor
   gesimuleerde waarden. Afgeleide grootheden (§5.2.5) worden niet apart gesimuleerd: ze
   volgen uit de gesimuleerde meting.

   **Testreeks van de Victron-apparaten** (v0.30). De reader maakt een Instant
   Readout-bericht aan en versleutelt het met de sleutel van het apparaat uit de
   configuratie; daarmee wordt ook het ontsleutelen getest.

   | Sensortype | Grootheid | Verloop |
   |---|---|---|
   | `victron-bmv` | `spanning` | 12,8 → 11,6 V |
   | | `stroom` | −2 → −12 A |
   | | `soc` | 100 → 20 % |
   | | `verbruikt` | 0 → 160 Ah |
   | | `resterende_tijd` | 1200 → 60 min |
   | `victron-mppt` | `zonnevermogen` | 0 → 45 → 0 W (een dag zon, piek halverwege) |
   | | `laadstroom_zon` | vermogen / 13 V: 0 → 3,5 → 0 A |
   | `victron-multiplus` | `walstroom` | 1, vanaf halverwege 0 (walstroom valt weg) |

   Ruis: 0,02 V, 0,15 A, 5 min en 1,5 W. Laadtoestand en verbruik krijgen geen ruis (een
   tellerstand springt niet heen en weer). `rssi` rond −60 dBm.

   **Testreeks van de bilge** (Shelly, v0.29): een lek dat erger wordt. De duur is
   verdeeld in 10 gelijke perioden; in elke periode loopt de bilgepomp één keer, van 10 %
   van de periode aan het begin tot 50 % aan het einde (bij 10 minuten: elke minuut een
   beurt, van 6 s oplopend tot 30 s). In het laatste vijfde deel staat ook de
   hoogwatervlotter aan. De pompspanning is 12,6 V of 0 V, met wat ruis; `rssi` rond
   −55 dBm.
5a. **Demo (v0.55):** geen reeks, maar waarden rond normaal, zodat er wat te zien valt
   zonder dat er een alarm afgaat. Elke waarde is de som van een normale waarde, een
   vaste afwijking per sensor, het dag- en nachtritme volgens de klok, een langzame
   willekeurige afwijking en de ruis van punt 5. Elke sensor heeft zijn eigen toeval:
   twee sensoren tekenen nooit dezelfde lijn.

   | Grootheid | Normaal | Per sensor | Dag en nacht | Langzame afwijking |
   |---|---|---|---|---|
   | `temperatuur` | 8 °C | ± 2 °C | ± 2,5 °C (koudst 6 uur, warmst 15 uur) | 0,8 °C, over uren |
   | `vocht` | 70 % | ± 5 % | ∓ 6 % (tegen de temperatuur in) | 3 %, over uren; blijft tussen 30 en 90 % |
   | `batterijspanning` | 2,95 V | ± 0,05 V | ± 0,02 V | – |
   | `luchtdruk` | 1013 hPa | – | – | 8 hPa, over dagen |
   | `versnelling_x` / `_y` / `_z` | 0 / 0 / 1000 mG | ± 20 mG (x, y) | – | 15 / 15 / 5 mG, over minuten |
   | `beweging` | gemiddeld 1 per 10 minuten | | | |
   | `spanning`, `stroom`, `soc` (accu) | accu van 200 Ah, begint tussen 80 en 95 % | | verbruik 0,5 A; overdag tot 2,4 A erbij van de zon | 0,25 A; de laadtoestand volgt uit de stroom en blijft tussen 50 en 100 % |
   | `zonnevermogen` | 0 W in het donker; licht van 8.30 tot 16.30 uur, piek 45 W | | | bewolking laat 5 tot 100 % door |
   | `walstroom` | 1 (blijft aan) | | | |
   | `bilgepomp` | uit; loopt 10–25 s, de eerste keer 2–10 minuten na de start, daarna elke 1–3 uur | | | |
   | `bilge` (hoogwater) | 0 (blijft uit) | | | |

   De waarden gaan door dezelfde keten als bij een test (bytes in het formaat van de
   sensor, decoder, correctie, bus, opslag) en komen als gewone metingen in de database.
   Een alarmregel met een grens dicht bij deze waarden kan ook in een demo afgaan.
6. In de trace (§10.2) begint de stap `ruw` van een gesimuleerde advertentie met
   `SIMULATIE`.
7. Gesimuleerde waarden zijn voor de rest van het systeem **gewone metingen**: ze komen
   onder het ID van de sensor in de database en laten (later) alarmen afgaan.
8. De opdracht geeft alleen het startsein en is dan klaar (v0.25): de shell is meteen weer
   vrij, bijvoorbeeld voor een tweede `brms sim` voor een andere sensor terwijl de eerste
   nog loopt. Elke test stopt vanzelf na zijn eigen duur. Eerder stoppen, of een demo
   stoppen: **`brms sim stop <sensor ...>`** of **`brms sim stop all`**.
9. Draait de reader van een sensor niet (volgens systemd), dan meldt de opdracht dat er
   voor die sensor niets gesimuleerd wordt.
10. **Zichtbaar op het web (v0.55).** Elke reader meldt op `brms/simulatie/<dienst>`
    (§3.2) welke van zijn sensoren gesimuleerd worden en of dat een test of een demo is:
    bij zijn start, bij elke verandering en elke 30 s. Er is geen retain op de bus; door
    het herhalen weet de webservice het na een eigen herstart binnen 30 s weer. De
    webservice onthoudt per reader de laatste melding en laat die na 90 s vervallen (de
    reader is dan gestopt). Het tegeltje in de bovenbalk (§5.6) toont de stand.

*Gebouwd in code v0.20.0. Getest op de pc; nog niet op de Pi.*

---

## 11. Open vragen

| # | Vraag | Sectie |
|---|---|---|
| 1 | ~~Eén proces of aparte services~~ – **besloten in v0.4**: losse services, lokale Mosquitto ertussen | §3.1 |
| 2 | ~~Welke USB-BLE-dongle~~ – **voorlopig vervallen in v0.65**: de ingebouwde Bluetooth van de Pi haalt thuis ruim 20 m; komt terug als het bereik aan boord tegenvalt | §4.1 |
| 3 | ~~Gedrag zolang de systeemtijd nog niet betrouwbaar is~~ – **besloten in v0.10**: router als tijdbron, alarmen op monotone klok, opslag wacht op synchronisatie | §4.2 |
| 4 | ~~Interface van een reader~~ – **besloten in v0.4**: één BLE-reader met decoders, één Shelly-reader, push via MQTT | §5.2.1 |
| 5 | ~~Ruuvi: `ruuvitag-sensor` of eigen decoder~~ – **besloten in v0.12**: eigen decoder (ook BTHome) | §5.2.3 |
| 6 | ~~Victron: BLE of VE.Direct-USB~~ – **besloten in v0.9**: BLE Instant Readout | §5.2.2 |
| 7 | ~~Type laadregelaar zonnepaneel~~ – **besloten in v0.9**: 3× Victron MPPT via BLE | §5.2.2 |
| 8 | ~~Wanneer geldt de walstroom-Shelly als offline~~ – **vervallen in v0.9**: walstroom via MultiPlus | §5.2.2 |
| 9 | ~~Datamodel SQLite~~ – **besloten in v0.2**: generieke tabel `meting`, stills als bestand + tabel `still` (sinds v0.5 `opname`) | §5.3.1 |
| 10 | ~~Meetinterval en schrijffrequentie~~ – **besloten in v0.3**: opslag per 5 min (gemiddelde; toestanden bij verandering), alarm op elke waarde, Shelly-poll 30 s | §5.3.2 |
| 11 | Bewaartermijn / downsampling | §5.3 |
| 12 | ~~Formaat configuratiebestand~~ – **besloten in v0.7**: TOML, één bestand `config/brms.toml` (sinds v0.15 in `~/BRMS`), herstart bij wijziging, controle bij start | §5.4.1 |
| 13 | ~~Alarmdetails: drempels, duur, hysterese, herhaling, herstel~~ – **besloten in v0.13**: één werking voor alle alarmen met duur, herstel-drempel, herinnering en herstelmelding; alarm "sensorbatterij laag" toegevoegd | §5.5.2 |
| 14 | ~~ntfy.sh of zelf gehost~~ – **besloten in v0.32**: geen ntfy; meldingen via Telegram, met e-mail als tweede kanaal | §5.5.4 |
| 15 | ~~Authenticatie op webinterface~~ – **voorlopig besloten in v0.69**: geen aanmelding; Tailscale van buitenaf, het wifi-wachtwoord van de router aan boord | §5.6 |
| 16 | ~~Back-up~~ – **besloten in v0.6**: wekelijks naar OneDrive met `rclone copy` | §7.1 |
| 17 | Reikwijdte van de watchdog | §7 |
| 18 | Detectie thuis als BRMS zelf uitvalt | §8 |
| 19 | ~~Uitrol van code en updates~~ – **besloten in v0.11**: git met deploy key, beide Pi's volgen `main`, `update.sh` met terugdraaien; sinds v0.15 alles in `~/BRMS` | §10.1 |
| 20 | ~~Bewaartermijn van stills~~ – **besloten in v0.5**: foto's 2 weken, video's tot de gebruiker ze verwijdert, met maximale ruimte | §5.7 |
| 21 | Maatregelen op OS-niveau tegen flashslijtage | §7 |
| 22 | ~~MQTT-clientbibliotheek~~ – **besloten in v0.8**: `paho-mqtt==2.1.0` direct, met eigen busmodule | §3.2 |
| 23 | ~~MQTT-topic-indeling en berichtformaat~~ – **besloten in v0.8**: `brms/meting`, `brms/opname`, `brms/opdracht`, complete JSON-berichten, geen retain | §3.3 |
| 24 | ~~Opnamemethode camera~~ – **besloten in v0.37**: foto via het snapshot-adres, video via RTSP met `ffmpeg` zonder omrekenen; beide adressen per camera in de configuratie | §5.7 |
| 25 | ~~Tabel van grootheden~~ – **besloten in v0.9** | §5.3.3 |
| 26 | BLE passief scannen in plaats van actief (spaart een beetje sensorbatterij; vraagt extra BlueZ-instellingen) | §5.2.1 |
| 27 | ~~Energieteller van het Shelly-relais (Wh) ook opslaan~~ – **besloten in v0.64**: niet opslaan; vermogen, spanning en stroom volstaan | §5.2.6 |
| 28 | ~~Bootpagina: wat een klik op een waarde doet met de indeling van de grafiekenpagina~~ – **besloten in v0.76**: één bootgrafiek bovenaan, waar elke klik een reeks aan toevoegt, tot de gebruiker hem sluit | §5.6 |
| 29 | ~~Bootpagina: de slagzij aan één of aan beide kanten~~ – **besloten in v0.76**: alleen aan de kant waar de boot naartoe helt (bakboord boven, stuurboord onder) | §5.6 |
| 30 | ~~Bootpagina: wanneer een waarde rood is~~ – **besloten in v0.76**: rood en vet bij een actief alarm op die sensor en die grootheid; `--` in lichtgrijs bij een stille of ontbrekende sensor zonder alarm | §5.6 |
| 31 | ~~Bootpagina: `heater1` en `heater2`~~ – **vervallen in v0.76**: de kachels staan niet in de eerste tekening; ze komen er in de tekening bij als het relais gebouwd is | §5.6, §5.2.6 |
| 32 | ~~Bootpagina: na hoeveel tijd zonder meting toont de pagina `--`~~ – **besloten in v0.76**: na 15 minuten; **herzien in v0.85**: na 5 minuten (OPEN-35) | §5.6 |
| 33 | ~~De plug zelf als sensor voor de walstroom~~ – **besloten in v0.80**: elke plug levert `walstroom` (spanning > 180 V = 1); welke bron geldt voor het aanzetten, kiest de gebruiker met het veld `voorwaarde` bij het relais | §5.2.6 punt 5 |
| 35 | ~~Bootpagina: oude waarden na een herstart~~ – **besloten in v0.85**: na een herstart geen waarden meer uit de database (`--` tot de eerste meting van de bus), en een waarde geldt als stil na 5 minuten in plaats van 15 | §5.6 punt 4 en 7 |
| 34 | ~~Is `voorwaarde` bij een relais verplicht?~~ – **besloten in v0.81**: verplicht | §5.2.6 punt 5 |
| 36 | ~~Relais na een herstart van de alarmlaag, en een kortere uitschakeltijd~~ – **besloten in v0.87**: de alarmlaag geeft bij zijn start "uit" aan elk relais; de uitschakeltijd wordt 2 minuten en de herhaling van "aan" 1 minuut (eerder 10 en 5) | §5.2.6 punt 4 en 6 |

Daarnaast uit `CLAUDE.md`: de accu is nu 4× Victron AGM 12 V 110 Ah en wordt later deze
winter vervangen door LiFePO4 (2× 300 Ah); bij die wissel de BMV-instellingen en de grens
van het alarm "accu laag" opnieuw bekijken. Het data-abonnement is afgesloten (40 GB per
maand).

---

## Revisiegeschiedenis

| Versie | Datum | Auteur | Wijziging |
|---|---|---|---|
| **0.92** | 2026-11-21 | Claude Code, Fable 5.2 | Netwerk (§9): de subnet-route van Tailscale is op de boot-Pi ingericht en gecontroleerd. De Pi biedt `192.168.1.0/24` aan, zodat router, camera en Shelly's vanaf thuis op hun eigen adres bereikbaar zijn (bijvoorbeeld de webpagina van de camera). De drie stappen om het in te richten staan erbij. Geen wijziging in de code. |
| 0.91 | 2026-11-18 | Claude Code, Fable 5.2 | Dagoverzicht (§5.6 punt 2), gebouwd in code v0.28.0: het tempo is te kiezen met twee keuzelijsten op de afspeelbalk. "Verversen": een intervalfoto staat 0,2 / 0,5 / 1,0 / 1,5 / 2,0 s in beeld (standaard 1,0 s; eerder vast 0,5 s). "Verspringen": 1× / 2× / 3× / 5× / 10× het foto-interval van de camera tussen twee getoonde intervalfoto's (standaard 1×); gebeurtenissen en video's worden nooit overgeslagen. Aanleiding: over de mobiele verbinding (ongeveer 3 Mbit/s) doet een nachtfoto van 1,3 MB er 3 tot 5 s over. |
| 0.1 | 2026-11-08 | Claude Code, Opus 5.5 | Eerste concept, opgesteld uit `CLAUDE.md`; open vragen geïnventariseerd. |
| 0.2 | 2026-11-08 | Claude Code, Opus 5.5 | OPEN-9 besloten: generieke tabel `meting`; stills als bestand met tabel `still` (§5.3.1), met aanleiding, redenering en verworpen alternatieven. OPEN-20 toegevoegd; OPEN-16 uitgebreid met stills. |
| 0.3 | 2026-11-08 | Claude Code, Opus 5.5 | OPEN-10 besloten (§5.3.2): ontvangen, alarmeren en opslaan losgekoppeld; opslag per 5 min als gemiddelde voor doorlopende grootheden, toestanden nooit gemiddeld maar bij verandering; Shelly-poll 30 s. OPEN-21 toegevoegd (flashslijtage door OS). §5.2, §6 bijgewerkt. |
| 0.4 | 2026-11-08 | Claude Code, Opus 5.5 | OPEN-1 besloten (§3.1): losse services met lokale Mosquitto-broker (geen persistence, QoS 1 toestanden / 0 doorlopend, weggooien bij onbereikbare broker). OPEN-4 besloten (§5.2.1): één BLE-reader met decoders, één Shelly-reader, alleen geconfigureerde apparaten, asyncio. OPEN-22 en OPEN-23 toegevoegd. Systeemoverzicht (§3) en §6 bijgewerkt. |
| 0.5 | 2026-11-08 | Claude Code, Opus 5.5 | Poll-nu voor pollende readers, niet voor BLE (§5.2.1). Camera uitgewerkt (§5.7): altijd aan (relais vervalt), foto elke 3 min / 2 weken, video 120 s op poll-nu / tot handmatig verwijderd, maximale videoruimte; OPEN-20 besloten, OPEN-24 toegevoegd. Tabel `still` → `opname` (§5.3.1). Derde soort "toename" voor de Ruuvi-bewegingsteller; versnelling X/Y/Z opgeslagen (§5.3.2). Bewegingsalarm met rustperiode 10 min (§5.5.1). Drempels en instellingen in configuratiebestand (§5.4). Verwijderen van opnames via web → opslag-service (§5.6). Back-up van opnames instelbaar (§7). |
| 0.6 | 2026-11-08 | Claude Code, Opus 5.5 | OPEN-16 besloten (§7.1): wekelijkse back-up naar OneDrive (eigen Microsoft 365-account) met `rclone copy`, onversleuteld; consistente databasekopie; opnames instelbaar; back-up-interval korter dan bewaartermijn foto's. |
| 0.7 | 2026-11-08 | Claude Code, Opus 5.5 | OPEN-12 besloten (§5.4.1): TOML via `tomllib`, één bestand `/etc/brms/brms.toml` buiten git en los van de code, wijzigen door herstart van de service, configuratie gecontroleerd bij start en met `brms check-config`. |
| 0.8 | 2026-11-08 | Claude Code, Opus 5.5 | OPEN-22 besloten (§3.2): `paho-mqtt==2.1.0` direct, zoals JSEM, met eigen busmodule als koppeling naar asyncio. OPEN-23 besloten (§3.3): topics `brms/meting`, `brms/opname`, `brms/opdracht`; complete JSON-berichten; geen retain; QoS per berichtsoort. OPEN-25 toegevoegd (tabel van grootheden). §5.2.1 bijgewerkt. |
| 0.9 | 2026-11-08 | Claude Code, Opus 5.5 | OPEN-6 en OPEN-7 besloten (§5.2.2): BMV-700, 3× MPPT (2× 75/15 met VE.Direct-dongle, 100/30 SmartSolar) en MultiPlus (VE.Bus Smart dongle) via BLE Instant Readout. Walstroom via MultiPlus in plaats van Shelly; OPEN-8 vervallen; Shelly alleen nog voor bilge. OPEN-25 besloten (§5.3.3): tabel van grootheden. §5.3.2, §5.5 bijgewerkt. |
| 0.10 | 2026-11-08 | Claude Code, Opus 5.5 | OPEN-3 besloten (§4.2): router (RUT241) als tijdbron via operator time en NTP, Pi synchroniseert via router; alarmlaag op monotone klok; opslag en camera schrijven pas na tijdsynchronisatie; DS3231 optioneel. |
| 0.11 | 2026-11-08 | Claude Code, Opus 5.5 | OPEN-19 besloten (§10.1): uitrol via git met deploy key; test- en boot-Pi volgen `main`; `deploy/update.sh` met config-controle en `--terug`; indeling `/opt/brms`, `/etc/brms`, `/var/lib/brms`; systeemgebruiker `brms`; `deploy/install.sh`; geen automatische OS-updates. |
| 0.12 | 2026-11-08 | Claude Code, Opus 5.5 | OPEN-5 besloten (§5.2.3): eigen decoders voor Ruuvi (formaat 5, getest met officiële testvectoren) en BTHome v2; Victron via `victron-ble`; ongeldige waarden niet gepubliceerd; toename bewegingsteller in de reader. |
| 0.13 | 2026-11-09 | Claude Code, Opus 5.5 | OPEN-13 besloten (§5.5.2): duur, hysterese, herstelmelding, herinnering zolang actief, geen bevestiging, alarmtoestand niet bewaard over herstart, alarmen per sensor, ntfy-prioriteiten, standaardwaarden. Alarm "sensorbatterij laag" toegevoegd (§5.5). |
| 0.14 | 2026-11-09 | Claude Code, Opus 5.5 | Bilge uitgewerkt (§5.2.4): Shelly Plus Uni op 12 V met hoogwatervlotter (`bilge`) en pompspanning (nieuwe grootheid `bilgepomp`); bilge-Shelly elke 2 s gepolld, poll-interval per Shelly instelbaar. Alarmen "bilgepomp lang" en "bilgepomp vaak" toegevoegd (§5.5, §5.5.2). |
| 0.15 | 2026-11-09 | Claude Code, Opus 5.5 | Indeling op de Pi herzien (§10.1, §5.4.1): alles in `~/BRMS` zoals op de pc (`config/brms.toml`, `data/`, `.venv`); services als `jandirk` in plaats van systeemgebruiker `brms`; `sqlite3` en `mosquitto-clients` als beheerhulpmiddelen. |
| 0.16 | 2026-11-09 | Claude Code, Opus 5.5 | Monitor met trace (§10.2): `brms monitor [sensor]` in tmux; trace per sensor op aanvraag via `brms/opdracht/trace/aan` (vaste looptijd), traceberichten op `brms/trace/<sensor_id>` (§3.3); trace-uitgang voor elke reader (§5.2.1 punt 7) en de opslag. |
| 0.17 | 2026-11-09 | Claude Code, Opus 5.5 | Systeempakketten in één lijst `deploy/pakketten.txt` (§10.1 punt 3 en 6); `install.sh` en `update.sh` installeren wat ontbreekt. |
| 0.18 | 2026-11-09 | Claude Code, Opus 5.5 | Grootheden en correcties per sensor (§5.4.2): optionele lijst `grootheden`, correctie met factor en offset, elke decoder legt vast wat hij levert, `brms grootheden [type]`, controles in `check-config`. Opdracht `brms`: service-opdrachten weigeren te starten naast de systemd-service (§10.1 punt 8). |
| **0.90** | 2026-11-18 | Claude Code, Fable 5.2 | Statusstip en teller bij "Alarmen" (§5.6 punt 3), gebouwd in code v0.27.7: de pagina vraagt `/api/status` elke 5 s in plaats van elke 30 s, zodat de alarmindicatoren niet meer achterlopen op de rode waarden van de bootpagina. |
| 0.89 | 2026-11-18 | Claude Code, Fable 5.2 | Slagzij in meldingen (§5.2.5 punt 10), gebouwd in code v0.27.6: een gemeten slagzij staat ook in de meldingen (Telegram, e-mail) en op de pagina Alarmen in hele graden; een alarmgrens blijft staan zoals in `brms.toml`. Camera (§5.7 punt 1, §9): de Foscam hangt met een netwerkkabel aan de RUT241 in plaats van via wifi, omdat het videobeeld via wifi te ver achterliep; wifi als verworpen alternatief vastgelegd. |
| 0.88 | 2026-11-17 | Claude Code, Fable 5.2 | Gebouwd in code v0.27.5. Relais (§5.2.6 punt 6): de Shelly-reader vraagt bij zijn start de alarmlaag om de stand van de relais, zodat een relais na een herstart binnen seconden uit gaat; de herhaling na 15 seconden vervalt. Slagzij (§5.3.3, §5.6 punt 5): in hele graden, en op de bootpagina staat bij afgerond 0 niets. |
| 0.87 | 2026-11-17 | Claude Code, Fable 5.2 | OPEN-36 besloten en gebouwd in code v0.27.4 (§5.2.6 punt 3, 4 en 6): de alarmlaag geeft bij zijn start "uit" aan elk relais (twee keer: direct en na 15 seconden); de uitschakeltijd is 2 minuten en de herhaling 1 minuut, eerder 10 en 5. |
| 0.86 | 2026-11-17 | Claude Code, Fable 5.2 | Alarmen na een herstart (§3.3, §5.5.3 punt 8), gebouwd in code v0.27.3: de opslag vraagt bij zijn start de alarmlaag om de stand, zodat alarmen van vóór de herstart binnen seconden zijn afgesloten in plaats van na een minuut. OPEN-36 toegevoegd: het relais na een herstart van de alarmlaag en een kortere uitschakeltijd. |
| 0.85 | 2026-11-17 | Claude Code, Fable 5.2 | OPEN-35 besloten en gebouwd in code v0.27.2 (§5.6 punt 4 en 7): de bootpagina leest na een herstart geen waarden meer uit de database, en een waarde geldt als stil na 5 minuten in plaats van 15. |
| 0.84 | 2026-11-17 | Claude Code, Fable 5.2 | OPEN-35 toegevoegd: de bootpagina toont na een herstart nog minutenlang oude waarden van een sensor die niet meer zendt (laatste rij uit de database; "sensor stil" begint na een herstart opnieuw te tellen). |
| 0.83 | 2026-11-17 | Claude Code, Fable 5.2 | Bootpagina (§5.6 punt 4), fout hersteld in code v0.27.1: bij een actief alarm "sensor stil" staat er direct `--` in rood en vet, ook als de laatste waarde nog geen 15 minuten oud is. Gevonden door de pluggen uit het stopcontact te trekken (regel `stil` met een duur van 5 minuten). |
| 0.82 | 2026-11-17 | Claude Code, Fable 5.2 | Relais (§5.2.6): de tweede Shelly Plug M Gen3 staat op 192.168.1.122; gecontroleerd als de eerste (uitschakeltijd werkt, begintoestand op `off` gezet). |
| 0.81 | 2026-11-17 | Claude Code, Fable 5.2 | OPEN-34 besloten: `voorwaarde` is verplicht bij een relais. De herziening van v0.80 gebouwd in code v0.27.0: de plug levert `walstroom` (spanning > 180 V), het veld `voorwaarde` bij het relais, en de alarmlaag schakelt alleen aan zolang elke voorwaarde geldt. Zonder MultiPlus-dongle meldt een `stil`-regel op de plug dat de walstroom weg is. Nog niet beproefd op de Pi. |
| 0.80 | 2026-11-17 | Claude Code, Fable 5.2 | Relais (§5.2.6 punt 5) herzien, op aanwijzing van de gebruiker: elke plug levert `walstroom`, afgeleid van zijn spanning (> 180 V = 1); het veld `voorwaarde` bij het relais bepaalt wat moet gelden om het aan te zetten, bijv. `{ multiplus.walstroom = 1 }`. OPEN-33 besloten, OPEN-34 toegevoegd (is `voorwaarde` verplicht). Ontwerp; nog niet gebouwd. |
| 0.79 | 2026-11-17 | Claude Code, Fable 5.2 | Relais gebouwd (§5.2.6, met de invulling bij het bouwen) in code v0.27.0: sensortype `shelly-relais` (pollen en schakelen door de Shelly-reader), de grootheden `relais` en `vermogen`, en de actie `relais` in de alarmlaag (uitschakeltijd 10 minuten, herhaling elke 5 minuten, alleen met walstroom, uit bij een stille sensor). `check-config` weigert de actie zonder sensor die `walstroom` levert. OPEN-33 toegevoegd: de plug zelf als sensor voor de walstroom. Nog niet beproefd op de Pi. |
| 0.78 | 2026-11-17 | Claude Code, Fable 5.2 | Relais (§5.2.6): de eerste Shelly Plug M Gen3 staat op 192.168.1.121 en is vanaf de Pi gecontroleerd. De namen in de API kloppen (`output`, `apower`, `voltage`, `current`); de uitschakeltijd per opdracht werkt (`toggle_after`); de begintoestand na spanningsuitval is op `off` gezet. Nog niet gebouwd. |
| 0.77 | 2026-11-17 | Claude Code, Fable 5.2 | Bootpagina gebouwd (§5.6, met de invulling bij het bouwen) in code v0.26.0: blok `[boot]` met `tekening` in de configuratie, `/` is de bootpagina als dat blok er is, grafieken op `/grafieken`, de tekening op `/boot/tekening.svg`, de waarden via `/api/boot`, de bootgrafiek op de grafiekenpagina. Een ontbrekende tekening is een waarschuwing, geen fout. Beproefd in de browser op de pc met nagemaakte metingen; nog niet op de Pi. |
| 0.76 | 2026-11-17 | Claude Code, Fable 5.2 | Bootpagina herzien (§5.6): de plekken staan niet meer in `brms.toml` maar in één SVG-tekening van de gebruiker met de boot en op elke plek de tekst `sensor.grootheid`; `[boot]` heeft alleen het veld `tekening`; de tekening wordt zonder herstart opnieuw gelezen. De pagina zet de waarden in een eigen letter met een vakje in de themakleuren. OPEN-28 (bootgrafiek), OPEN-29 (slagzij aan één kant) en OPEN-30 (rood en vet bij alarm, `--` in lichtgrijs bij een stille sensor) besloten; OPEN-31 vervallen; OPEN-32 besloten (`--` na 15 minuten zonder meting). Nog niet gebouwd; de code is niet gewijzigd. |
| 0.75 | 2026-11-17 | Claude Code, Fable 5.2 | Bootpagina ontworpen (§5.6): plattegrond van de boot met de actuele waarden op hun plek, als eerste pagina (`/`); Grafieken naar `/grafieken`. Plaatje met doorzichtige achtergrond naast `brms.toml`, plekken in `[boot]` / `[[boot.waarde]]`; waarde in alarm rood en dik; klik opent de grafiek. OPEN-28 t/m OPEN-31 toegevoegd. Nog niet gebouwd; de code is niet gewijzigd. |
| 0.74 | 2026-11-16 | Claude Code, Fable 5.2 | Uitrol (§10.1 punt 1): de repo is openbaar, `deploy/install.sh` haalt de code op via https in plaats van via SSH met een deploy key. Bij een bestaande clone zet `install.sh` het adres van `origin` om. De deploy key staat bij de verworpen alternatieven. README.md: beschrijving, schermafdrukken, installatie en opdrachten. De code in `brms/` is niet gewijzigd. |
| 0.73 | 2026-11-16 | Claude Code, Fable 5.2 | Richten van de camera (§5.7 punt 19): beproefd door de gebruiker, werkt met de Foscam FI9826W. Alleen de status; ontwerp en code zijn niet gewijzigd. |
| 0.72 | 2026-11-16 | Claude Code, Fable 5.2 | Richten van de camera gebouwd (§5.7 punt 19, met de invulling bij het bouwen) in code v0.25.0: velden `richten` en `richt_stap`, opdracht `brms/opdracht/camera/richt`, de commando's van Foscam met controle van het antwoord, `/richt/<camera_id>` in de webservice en vier richtknoppen in het live-beeld. Nog niet beproefd met de camera zelf. |
| 0.71 | 2026-11-16 | Claude Code, Fable 5.2 | Camera (§5.7): voorlopig een binnencamera, de Foscam FI9826W; buitencamera en voeding later. Richten van de camera ontworpen (§5.7 punt 19, §5.6 punt 2, §3.3): veld `richten` (merk, nu alleen `"foscam"`) en `richt_stap` per camera; vier richtknoppen tijdens het live-beeld, alleen bij een camera met `richten`; stap per klik, het stoppen door de camera-reader; opdracht `brms/opdracht/camera/richt`; geen vaste stand. Verworpen: adressen per richting, ONVIF, ingedrukt houden, terug naar een vaste stand. Ontwerp; nog niet gebouwd. |
| 0.70 | 2026-11-16 | Claude Code, Fable 5.2 | Camera (§5.7): twee verworpen alternatieven vastgelegd: een video-deurbel op batterij en een wifi-camera met eigen zonnepaneel (slapen, geen lokaal adres, batterij in de winter). Een bedrade deurbel met RTSP en snapshot-adres voldoet wel; een gewone 12V-buitencamera is eenvoudiger. Het model is nog niet gekozen. Alleen tekst; de code is niet gewijzigd. |
| 0.69 | 2026-11-15 | Claude Code, Fable 5.2 | OPEN-15 voorlopig besloten (§5.6): geen aanmelding op de webinterface; Tailscale van buitenaf en het wifi-wachtwoord van de router aan boord. §11: accutype (nu AGM 4× 110 Ah, later LiFePO4 2× 300 Ah) en het afgesloten data-abonnement bijgewerkt. Alleen tekst; de code is niet gewijzigd. |
| 0.68 | 2026-11-15 | Claude Code, Fable 5.2 | Slagzij (§5.2.5 punt 10): uitgeschreven wat er gebeurt bij onderste boven monteren: de hoek blijft kloppen, de kant kan omdraaien, en de kanteltest en de kalibratie horen in de definitieve stand; na omdraaien of verplaatsen beide opnieuw. Alleen tekst; de code is niet gewijzigd. |
| 0.67 | 2026-11-15 | Claude Code, Fable 5.2 | Slagzij gebouwd (§5.2.5) in code v0.24.0: module `brms/afleiden.py`, veld `stuurboord`, grootheid `slagzij`, de stap `afgeleid` in de trace, en sb/bb op de webpagina en in meldingen. Alleen de status in §5.2.5 is gewijzigd; het ontwerp niet. |
| 0.66 | 2026-11-15 | Claude Code, Fable 5.2 | Slagzij in plaats van helling (§5.2.5): afgeleide grootheid `slagzij` in graden, positief = stuurboord, negatief = bakboord, getoond als "3,2° sb"; sensor plat gemonteerd; nieuw veld `stuurboord` per sensor (`x`, `-x`, `y`, `-y`); kalibratie met de gewone correctie op `slagzij`; twee alarmregels, één per kant. `helling` vervalt (§5.3.3, §5.5). Ontwerp; nog niet gebouwd. |
| 0.65 | 2026-11-14 | Claude Code, Fable 5.2 | Bluetooth (§4.1): de ingebouwde Bluetooth van de Pi in plaats van een USB-dongle met antenne; een test thuis gaf ruim 20 m bereik, ook door muren. OPEN-2 voorlopig vervallen; de dongle blijft de uitwijk als het bereik aan boord tegenvalt. |
| 0.64 | 2026-11-14 | Claude Code, Fable 5.2 | OPEN-27 besloten (§5.2.6): de energieteller van het Shelly-relais wordt niet opgeslagen. |
| 0.63 | 2026-11-14 | Claude Code, Fable 5.2 | Relais uitgewerkt (§5.2.6): model Shelly Plug M Gen3 voor een kachel van 1500 W; één alarmregel als thermostaat; uitschakeltijd 10 minuten, herhalen elke 5 minuten; de actie `relais` schakelt alleen aan zolang `walstroom` = 1 (onbekend = uit); de plug levert ook `vermogen` (nieuwe grootheid, §5.3.3), `spanning` en `stroom`. OPEN-27 toegevoegd (energieteller). Ontwerp; nog niet gebouwd. |
| 0.62 | 2026-11-14 | Claude Code, Fable 5.2 | Grafiekenpagina (§5.6 punt 4): één knop "+ Grafiek"; de bron (live of database) als keuzelijst linksboven in de grafiek, ook te wijzigen met reeksen erin (reeksen blijven, venster of periode terug naar de standaard). Legenda op de telefoon: rechtop één kolom, liggend twee kolommen, beide in de korte vorm. Systeempagina (§5.6 punt 3): logboek met "laatste 24 uur" en "laatste 48 uur" in plaats van "vandaag". Gebouwd in code v0.23.0. |
| 0.61 | 2026-11-14 | Claude Code, Fable 5.2 | Systeempagina (§5.6 punt 3): het logboek als keuze (laatste 10 als standaard, laatste 50, laatste 3 uur, laatste 6 uur, vandaag) met bladeren eerder / later / nu, in plaats van de laatste 200 met een knop "ouder"; de filters service en niveau blijven. Grafiekenpagina (§5.6 punt 4): de legenda in kolommen van gelijke breedte; op een smal scherm alleen sensornaam, waarde en leeftijd. Vast aantal decimalen per grootheid bij het tonen (§5.3.3). Gebouwd in code v0.22.0. |
| 0.60 | 2026-11-14 | Claude Code, Fable 5.2 | Bilge herzien (§5.2.4): geen tweede vlotter voor hoogwater. De Shelly Plus Uni zit bij de bestaande vlotter van de bilgepomp: voeding van de kant met altijd 12 V, de geschakelde kant op de spanningsmeting → `bilgepomp`. Grootheid `bilge` en alarm "bilge" blijven bestaan voor een eventuele latere waterdetector, maar zijn nu niet in gebruik (§5.3.3, §5.5). Alleen tekst en voorbeeldconfiguratie; de code is niet gewijzigd. |
| 0.59 | 2026-11-14 | Claude Code, Fable 5.2 | OPEN-26 toegevoegd (§5.2.1 punt 1, §11): de BLE-scanner scant nu actief (standaard van `bleak`), met de reden waarom `bleak` dat doet; open of dat passief moet worden om sensorbatterij te sparen. Alleen tekst; de code is niet gewijzigd. |
| 0.58 | 2026-11-13 | Claude Code, Fable 5.2 | Systeempagina (§5.6): het logboek opent op "alleen fouten". Zijbalk: de naam in twee regels, "BRMS" met eronder "Boat Remote Monitoring System" (was "BRMS boot op afstand"). Gebouwd in code v0.21.1. |
| 0.57 | 2026-11-13 | Claude Code, Fable 5.2 | Grafiekenpagina (§5.6 punt 4): het live-geheugen van de webservice toegelicht: bewaart altijd (ook zonder open grafiek), waarom, alleen in RAM, 24 uur, en waarom het niet doorgroeit. Alleen tekst; de code is niet gewijzigd. |
| 0.56 | 2026-11-13 | Claude Code, Fable 5.2 | Alarmenpagina (§5.6): de opgetreden alarmen als keuze (laatste 10 als standaard, vandaag, deze week, deze maand, dit jaar) met bladeren eerder / later / nu, in plaats van de nieuwste 200 onder elkaar. Grafiekenpagina (§5.6 punt 4): de knoppen eerder / later / nu als pictogram; op een smal scherm het kruisje van een grafiek rechtsboven. Gebouwd in code v0.21.0. |
| 0.55 | 2026-11-12 | Claude Code, Fable 5.2 | Simulatie (§10.3): naast de test (duur opgegeven, hooguit 60 minuten, testreeks) een **demo** (alleen Enter): waarden rond normaal met dag- en nachtritme, elke sensor onafhankelijk, tot `brms sim stop`. Readers melden op `brms/simulatie/<dienst>` wat ze simuleren (§3.2); tegeltje Actief / Simulatie / Demo in de bovenbalk (§5.6). Gebouwd in code v0.20.0. |
| 0.54 | 2026-11-12 | Claude Code, Fable 5.2 | Teltonika RMS en het relais op de router-uitgang (Pi-herstart) vervallen (§7, §9): geen extra externe dienst. In de plaats: "Ping Reboot" van de router, naast de hardware-watchdog van de Pi. |
| 0.53 | 2026-11-12 | Claude Code, Fable 5.2 | `update.sh` (§10.1 punt 3): na het uitchecken neemt het script uit de nieuwe code het over, zodat één keer `brms update` volstaat. Gebouwd in code v0.19.1. Herstartknoppen van v0.52 werken op de test-Pi. |
| 0.52 | 2026-11-12 | Claude Code, Fable 5.2 | Gebouwd in code v0.19.0, samen met v0.51 (nog niet getest op de Pi). Web (§5.6): vier pagina's (Grafieken, Opnames, Alarmen, Systeem) met een gedeelde bovenbalk, een zijbalk als menu, statusstip, thema licht/donker/automatisch en lijn-iconen; geschikt voor de telefoon. Systeempagina: knoppen "Herstart BRMS" en "Herstart de Pi" met bevestiging, via een polkit-regel. Foutmeldingen van de kernel in het logboek en als melding (§5.5.5). |
| 0.51 | 2026-11-12 | Claude Code, Fable 5.2 | Ontwerp (gebouwd met v0.52). Systeempagina in het web (§5.6): alarmregels met schakelaar, status van de services, logboek, schijfruimte (SMART getest en verworpen: de schijf geeft via USB geen bruikbare gegevens). Systeemmeldingen (§5.5.5): logboek uit journald; een fout (ERROR) geeft een melding, hooguit één per service per uur. Camera in een `stil`-regel, met elke geslaagde opname als levensteken (§5.5.3 punt 2); `bereikbaar` verworpen. |
| 0.50 | 2026-11-11 | Claude Code, Fable 5.2 | Opnamepagina (§5.6 punt 2, code v0.18.1): een foto of video die tijdens het live kijken is gemaakt, staat in de lijst zodra hij is opgeslagen; het live-beeld loopt door. |
| 0.49 | 2026-11-11 | Claude Code, Fable 5.2 | Gebouwd (code v0.18.0): live-beeld via go2rtc en WebRTC (§5.7 punt 18, met de invulling bij het bouwen), service `brms-go2rtc`, `deploy/go2rtc.sh`, go2rtc en Tailscale in `install.sh` (§10.1 punt 6); video-opnames via go2rtc met de camera als terugval (punt 11); standaard `video_duur` 60 s; foto's die samenvallen met een video doen niet mee in het dagoverzicht (§5.6 punt 2). De MJPEG-code, `live_url` en `live_beelden` zijn vervallen. |
| 0.48 | 2026-11-11 | Claude Code, Fable 5.2 | Ontwerp, nog niet gebouwd: live-beeld via go2rtc en WebRTC in plaats van de MJPEG-stroom van de camera (§5.7 punt 18), na metingen en een proef op de test-Pi; go2rtc als service `brms-go2rtc` op een vaste versie; video-opnames via go2rtc met de camera als terugval (punt 11); `live_url` en `live_beelden` vervallen; go2rtc en Tailscale in `install.sh` (§10.1 punt 6). |
| 0.47 | 2026-11-11 | Claude Code, Fable 5.2 | Ontwerp, nog niet gebouwd: standaard `video_duur` van 120 s naar 60 s (§5.7); in het dagoverzicht doet een foto (ook een intervalfoto) die samenvalt met een video van dezelfde camera niet mee (§5.6 punt 2). |
| 0.46 | 2026-11-11 | Claude Code, Fable 5.2 | Sensorindeling herzien (§4.1, `CLAUDE.md`): 1× RuuviTag 4-in-1 in de hoofdkajuit, 2× RuuviTag Pro 3-in-1 (motorruim en buiten, in plaats van een Pro 2-in-1 buiten), de 3 Xiaomi Monitor 3's in het vooronder en de twee achterkajuiten. Geen gevolgen voor de software. |
| 0.45 | 2026-11-10 | Claude Code, Fable 5.2 | Live-beeld gebouwd (§5.6 punt 2, §5.7 punt 18): `live_url` en `live_beelden` per camera, de webservice geeft de MJPEG-stroom door, knop "Live camera" met "Foto" en "Video". Grenzen van de testcamera vastgelegd. |
| 0.44 | 2026-11-10 | Claude Code, Fable 5.2 | "Foto nu" toont de nieuwe foto direct (gebouwd, §5.6 punt 2). Ontwerp: live-beeld via de MJPEG-stroom van de camera, doorgegeven door de webservice (`live_url`, hooguit 5 beelden per seconde, stopt na 10 minuten); knop "Live camera" met daarbinnen "Foto" en "Video" (§5.6 punt 2, §5.7 punt 18). HLS overwogen en verworpen. Nog niet gebouwd. |
| 0.43 | 2026-11-10 | Claude Code, Fable 5.2 | Dagoverzicht gebouwd (§5.6 punt 2): tweede weergave van de opnamepagina, met afspelen, pauze, "verder" bij een video en een tijdbalk van de dag. |
| 0.42 | 2026-11-10 | Claude Code, Fable 5.2 | Ontwerp: dagoverzicht op de opnamepagina (§5.6 punt 2): de opnames van een dag afspelen van oud naar nieuw, intervalfoto 0,5 s, foto bij een gebeurtenis 5 s, video volledig met een knop "verder". Nog niet gebouwd. |
| 0.41 | 2026-11-10 | Claude Code, Fable 5.2 | Camera stap (d) gebouwd (§5.6 punt 2): opnamepagina in het web (bekijken per dag, verwijderen via de opslag-service, "Foto nu" en "Video nu"). Het log vermeldt of een melding met foto is verstuurd. |
| 0.40 | 2026-11-10 | Claude Code, Fable 5.2 | Camera stap (c) gebouwd (§5.5.3 punt 5 en 6, §5.7 punt 15): foto bij een notificatie (Telegram als afbeelding, e-mail als bijlage), acties `video` en `foto`, veld `camera` in een alarmregel. |
| 0.39 | 2026-11-10 | Claude Code, Fable 5.2 | Camera stap (b) gebouwd (§5.7 punt 11 en 5): video op verzoek met `ffmpeg` (pakket in `deploy/pakketten.txt`), oudste video's weg als de maximale ruimte vol is. |
| 0.38 | 2026-11-10 | Claude Code, Fable 5.2 | Camera stap (a) gebouwd (§5.7): service `brms-camera`, blokken `[[camera]]` en `[opnames]`, foto per interval en op poll-nu, tabel `opname`, opruimen van oude foto's. |
| 0.37 | 2026-11-10 | Claude Code, Fable 5.2 | OPEN-24 besloten en de camera uitgewerkt (§5.7 punt 6–17): foto via het snapshot-adres, video via RTSP met `ffmpeg`; blokken `[[camera]]` en `[opnames]`; service `brms-camera`; foto bij een alarm; bouwvolgorde. Testcamera thuis: Foscam. Nog niet gebouwd. |
| 0.36 | 2026-11-10 | Claude Code, Fable 5.2 | Web: de pagina laadt zichzelf opnieuw na een herstart van de webservice, zodat nieuwe sensoren en alarmregels meteen gelden (§5.6). |
| 0.35 | 2026-11-10 | Claude Code, Fable 5.2 | Simulatie: de testreeks loopt binnen de duur twee keer heen en weer, zodat ook het herstellen van een alarm te testen is (§10.3 punt 5). |
| 0.34 | 2026-11-10 | Claude Code, Fable 5.2 | Fout hersteld: na een herstart van de Pi bleef een alarm in de database (en de grafiek) open staan. De alarmlaag meldt nu elke minuut welke alarmen actief zijn; de opslag sluit de rest af (§3.3, §5.5.3 punt 8). |
| 0.33 | 2026-11-10 | Claude Code, Fable 5.2 | Alarmlaag stap (a) gebouwd (§5.5.3, §5.5.4): service `brms-alarm`, alarmregels `[[alarm]]` en kanalen `[melding.telegram]`/`[melding.email]` in `brms.toml`, tabel `alarm`, grenzen en alarmperioden in de grafieken. Meldingen worden hooguit een dag opnieuw geprobeerd. |
| 0.32 | 2026-11-10 | Claude Code, Fable 5.2 | OPEN-14 besloten (§5.5.4): meldingen via een Telegram-bot, met e-mail (SMTP) als tweede kanaal; ntfy vervalt. Ontwerp, nog niet gebouwd. |
| 0.31 | 2026-11-10 | Claude Code, Fable 5.2 | Ontwerp, nog niet gebouwd. Alarmlaag (§5.5.3): alarmregels in `brms.toml` met notificatie (uit, eenmalig, herhaald), foto en acties; aan/uit-schakelaar per regel via het web; tabellen `alarm` en `alarm_uit` (§5.3.1); grenzen en alarmperioden in de grafieken. Afgeleide grootheden (§5.2.5): aparte module na de correctie, eerste afleiding `helling`. Relais (§5.2.6): sensortype `shelly-relais`, grootheid `relais`, beveiligd met een uitschakeltijd in de Shelly. |
| 0.30 | 2026-11-10 | Claude Code, Fable 5.2 | Simulatie van Victron-apparaten (§10.3 punt 5, §5.2.2 punt 10): testreeks voor accu, zonnepaneel en walstroom; het bericht wordt versleuteld met de sleutel uit de configuratie. |
| 0.29 | 2026-11-10 | Claude Code, Fable 5.2 | Shelly-reader gebouwd (§5.2.4 punt 4–9): sensortype `shelly-uni` met `adres`, `interval`, `bron`, `drempel`, `omgekeerd`; uitlezen via `Shelly.GetStatus`; service `brms-shelly-reader`; poll-nu. Simulatie van de bilge (§10.3). |
| 0.28 | 2026-11-10 | Claude Code, Fable 5.2 | Grafiekenpagina (§5.6 punt 4): bij een live-grafiek met "eerder" en "later" door de metingen in het geheugen bladeren; een eerder venster staat stil, "nu" loopt weer mee. |
| 0.27 | 2026-11-10 | Claude Code, Fable 5.2 | Victron-decoder gebouwd (§5.2.2 punt 7–10): sensortypen `victron-bmv`, `victron-mppt`, `victron-multiplus` met `sleutel` in de configuratie; `victron-ble` 0.9.3 als decoder; één waarschuwing per sensor bij een verkeerde sleutel. |
| 0.26 | 2026-11-10 | Claude Code, Fable 5.2 | Simulatie (§10.3 punt 5): ruis op de testreeks en op de signaalsterkte. |
| 0.25 | 2026-11-10 | Claude Code, Fable 5.2 | Grafiekenpagina (§5.6 punt 4): zelf grafieken toevoegen (live of database), per grafiek reeksen van meerdere sensoren en grootheden; een strook per eenheid; indeling onthouden in de browser. Simulatie (§10.3): `brms sim`, meerdere sensoren of `all`, geen wachttijd, standaard 10 minuten, opdracht keert direct terug, `brms sim stop`. |
| 0.24 | 2026-11-10 | Claude Code, Fable 5.2 | Testpagina (§5.6 punt 4): live-venster te kiezen (laatste 50/100 metingen, 10 min t/m 24 uur); de webservice bewaart 24 uur aan metingen in het geheugen; vanaf 1 uur samengevat per tijdvak. |
| 0.23 | 2026-11-10 | Claude Code, Fable 5.2 | `update.sh` (§10.1 punt 3): services volgen uit `deploy/systemd/`; een nieuwe service wordt vanzelf aangezet en gestart; bij "al bijgewerkt" worden niet-actieve services hersteld. |
| 0.22 | 2026-11-10 | Claude Code, Fable 5.2 | Webinterface (§5.6): testpagina met grafieken per sensor uit de bus (live) of de database (historie); ingebouwde Flask-server als service `brms-web` op poort 8080 (`[web]` in de configuratie); Chart.js als bestand in de repo. |
| 0.21 | 2026-11-10 | Claude Code, Fable 5.2 | Monitor (§10.2): vaste plaats per meting en per ketenstap in plaats van een doorlopende lijst; optioneel veld `deel` in traceberichten (§3.3). `brms update [--terug]` voert `deploy/update.sh` uit (§10.1 punt 3). |
| 0.20 | 2026-11-10 | Claude Code, Fable 5.2 | Simulatie (§10.3): `brms simuleer <sensor>` stuurt een testreeks door de keten; de BLE-reader maakt de advertenties zelf aan en negeert zolang de echte sensor (§5.2.1 punt 8); startsein via `brms/opdracht/simulatie/aan` (§3.3); gesimuleerde waarden komen in de database. |
| 0.19 | 2026-11-10 | Claude Code, Fable 5.2 | Bilge (§5.2.4): vastgelegd waarom de Shelly Plus Uni via wifi wordt uitgelezen en niet via Bluetooth. Ruuvi-decoder gebouwd (§5.2.1): bewegingsteller loopt van 0 t/m 254, 255 = ongeldig (§5.3.2); geen `beweging` bij de eerste ontvangst na een herstart (§5.2.3 punt 5). |
