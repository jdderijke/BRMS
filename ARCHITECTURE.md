# BRMS – Architectuur

**Versie 0.43 – concept** · 10 november 2026

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
| Opdracht | `brms/opdracht/<doel>/<actie>`, bijv. `brms/opdracht/camera/poll-nu`, `brms/opdracht/shelly/poll-nu`, `brms/opdracht/opslag/verwijder`, `brms/opdracht/trace/aan`, `brms/opdracht/simulatie/aan` (§10.3), `brms/opdracht/relais/zet` (§5.2.6), `brms/opdracht/alarm/schakel` (§5.5.3) | afhankelijk van de actie, bijv. `{"type": "video", "aanleiding": "alarm:beweging"}` | 1 |
| Alarm (v0.31) | `brms/alarm/<alarm_id>` | `{"ts", "alarm_id", "sensor_id", "grootheid", "gebeurtenis", "waarde"}`; `gebeurtenis` is `begin`, `herstel` of `uitgezet` (§5.5.3). De alarmlaag meldt bij zijn start en daarna elke minuut op `brms/alarm/herstart` welke alarmen actief zijn: `{"ts", "actief": [[alarm_id, sensor_id], ...]}` (v0.34, §5.5.3 punt 8). | 1 |
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

### 4.1 Bluetooth: USB-dongle met antenne

**Redenering.** De ingebouwde Bluetooth van de Pi 4 heeft beperkt bereik; sensoren
zitten verspreid over kajuit, vooronder, motorruim en buiten.
**Verworpen.** Ingebouwde Bluetooth – bereik te beperkt (wel bruikbaar voor tests thuis).
**Specificatie.** BLE via een USB-dongle met externe antenne. **[OPEN-2]** Welke dongle.

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
| Shelly | Bilge: hoogwatervlotter en pompspanning (Shelly Plus Uni) | Lokale HTTP-API (§5.2.4) | Gebouwd (v0.29); nog niet getest met een echte Shelly |
| Shelly-relais | Schakelaar voor o.a. verwarming | Lokale HTTP-API: pollen en schakelen (§5.2.6) | Ontworpen (v0.31) |
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

#### 5.2.4 Bilge (v0.14)

**Aanleiding.** De bestaande vlotter is in gebruik door de bilgepomp en niet beschikbaar
voor BRMS.

**Redenering.** Twee signalen vullen elkaar aan. Een tweede vlotter, hoger dan het
inschakelniveau van de pomp, gaat pas af als de pomp het niet bijhoudt of defect is: dan
moet worden ingegrepen. Meten of de pomp draait geeft het vroegste signaal van een lek: de
pomp draait vaker of langer. De Shelly Plus Uni kan beide: een digitale ingang voor een
contact en een spanningsmeting (0–15 / 0–30 V DC), en wordt rechtstreeks uit het
12V-boordnet gevoed (9–28 V DC, < 1 W); er komt geen 230 V in de bilge. Een pompbeurt kan
korter zijn dan het standaard poll-interval van 30 s en zou dan gemist worden; vaker
pollen via dezelfde lokale HTTP-API is de eenvoudigste oplossing (P1).

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
- *De bestaande pompvlotter meegebruiken.* In gebruik door de pomp.
- *Shelly stuurt zelf een bericht bij verandering (webhook of eigen MQTT).* Vraagt een extra
  ontvanger op de Pi; snel pollen volstaat.

**Specificatie.**
1. Eén **Shelly Plus Uni** op 12 V met:
   - een **hoogwatervlotter** (tweede vlotter, boven het inschakelniveau van de pomp) op een
     digitale ingang → grootheid `bilge`;
   - de **spanning op de bilgepomp** via de spanningsmeting → grootheid `bilgepomp`
     (1 = pomp krijgt spanning). De drempel (spanning → 0/1) staat in de configuratie. De
     precieze aansluiting wordt bepaald zodra de Uni er is.
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
   bron = { bilge = "input:0", bilgepomp = "voltmeter:100" }
   drempel = 6.0
   ```
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

#### 5.2.5 Afgeleide grootheden (v0.31)

*Ontworpen; nog niet gebouwd.*

**Aanleiding.** Sommige gewenste waarden worden niet gemeten maar uit metingen berekend:
de helling van de boot uit de drie versnellingen van een Ruuvi, later bijvoorbeeld een
aantal Ah als tijdintegraal van een gemeten stroom. Daarvoor moest één vaste plek komen.

**Redenering.** Decoderen (bytes naar getallen) en afleiden (uit getallen nieuwe getallen
berekenen) zijn twee verschillende bezigheden en blijven gescheiden. Een afgeleide waarde
moet uit de **gecorrigeerde** meting komen: de offset op de versnelling zet de montagestand
van de sensor op nul, en pas daarna zegt de helling iets. De plek is dus ná de correctie
en vóór het publiceren. Een afleiding hangt af van haar invoer, niet van een sensortype:
elke sensor die de invoer levert, kan de afgeleide waarde leveren, zonder de berekening te
kopiëren. Een afgeleide waarde is daarna een gewone meting: zelfde bus, opslag, alarmen en
grafieken.

**Verworpen alternatieven.**
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
   | `helling` (graden) | `versnelling_x`, `versnelling_y`, `versnelling_z` | hoek tussen de z-as van de sensor en de zwaartekracht: atan2(√(x² + y²), z) |

   Met de correcties op de versnellingen (§5.4.2) wordt de montagestand van de sensor op
   nul gezet.
3. Een sensortype **levert** een afgeleide grootheid als het alle invoer daarvoor levert
   (§5.4.2 punt 3). Dat volgt uit wat de decoder levert; `brms grootheden <type>`,
   `check-config` en de keuzelijst in de grafieken nemen het vanzelf mee.
4. **Volgorde in de reader:** decoderen → corrigeren → afleiden → lijst `grootheden`
   toepassen → publiceren. De lijst `grootheden` komt dus ná het afleiden: `helling` kan
   worden opgeslagen zonder de drie versnellingen.
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

#### 5.2.6 Relais (v0.31)

*Ontworpen; nog niet gebouwd. Het model van de Shelly-schakelaar is nog niet gekozen.*

**Aanleiding.** Een alarm moet een apparaat kunnen schakelen (§5.5.3), als eerste een
elektrische verwarming bij vorstgevaar; later mogelijk boordverlichting.

**Redenering.** Een verwarming vraagt een schakelaar voor 230 V en een hoge stroom. Dat is
een ander apparaat dan de Shelly Plus Uni van de bilge (§5.2.4), waarvan de uitgangen
daarvoor niet bedoeld zijn. Een Shelly-schakelaar heeft dezelfde lokale HTTP-API en past
in de bestaande Shelly-reader. Een verwarming die aan blijft staan als BRMS vastloopt, is
een risico; de beveiliging hoort daarom in de Shelly zelf te zitten en niet in BRMS.

**Verworpen alternatieven.**
- *De uitgangen van de Shelly Plus Uni gebruiken.* Niet geschikt voor 230 V en de stroom
  van een verwarming.
- *Uitschakelen alleen door BRMS.* Valt BRMS weg, dan blijft het relais aan.

**Specificatie.**
1. Sensortype **`shelly-relais`**: een aparte Shelly-schakelaar (230 V, 16 A of
   vergelijkbaar; een model met de Gen2-API en één uitgang `switch:0`). Configuratie als
   bij `shelly-uni` (§5.2.4 punt 4): `id`, `naam`, `adres`, `interval` (standaard 30 s).
2. De Shelly-reader **pollt** het relais; de stand komt als gewone meting binnen:
   grootheid **`relais`** (0/1, toestand), en `rssi`.
3. **Schakelen:** opdracht `brms/opdracht/relais/zet` met
   `{"sensor_id": ..., "aan": true, "duur": 900}`. De Shelly-reader voert hem uit via de
   API van de Shelly en pollt direct daarna, zodat de nieuwe stand meteen op de bus staat.
4. **Beveiliging:**
   - "aan" gaat altijd met een **uitschakeltijd** (`duur`, standaard 15 minuten) die de
     Shelly zelf bewaakt: na die tijd schakelt hij vanzelf uit.
   - Zolang het alarm actief is, **ververst** de alarmlaag de opdracht elke 5 minuten.
     Valt BRMS weg, dan gaat het relais binnen de uitschakeltijd uit.
   - De alarmlaag ververst alleen zolang de sensor van de alarmregel nog waarden levert
     (laatste waarde jonger dan de uitschakeltijd). Bij een **stille sensor** gaat het
     relais dus uit.
   - Bij herstel van het alarm, en als de regel wordt uitgezet (§5.5.3), volgt direct "uit".
5. Na een **herstart** van de alarmlaag is er geen actief alarm (§5.5.2 punt 6); het relais
   gaat binnen de uitschakeltijd uit, en weer aan zodra het alarm opnieuw afgaat.
6. Handmatig schakelen via het web is niet voorzien.

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

| Grootheid | Eenheid | Soort | Bron | Opmerking |
|---|---|---|---|---|
| `temperatuur` | °C | doorlopend | BTHome, Ruuvi | |
| `vocht` | % | doorlopend | BTHome, Ruuvi | relatieve luchtvochtigheid |
| `luchtdruk` | hPa | doorlopend | Ruuvi (4-in-1) | |
| `versnelling_x`, `versnelling_y`, `versnelling_z` | mG | doorlopend | Ruuvi | invoer voor `helling` |
| `helling` | graden | doorlopend | afgeleid (§5.2.5) | hoek van de sensor met waterpas (v0.31) |
| `beweging` | aantal | toename | Ruuvi | toename van de bewegingsteller |
| `batterijspanning` | V | doorlopend | BTHome, Ruuvi | batterij van de sensor zelf |
| `rssi` | dBm | doorlopend | alle BLE-apparaten | signaalsterkte, gemeten door de scanner |
| `spanning` | V | doorlopend | BMV-700 | accuspanning |
| `stroom` | A | doorlopend | BMV-700 | negatief = ontladen |
| `soc` | % | doorlopend | BMV-700 | laadtoestand |
| `verbruikt` | Ah | doorlopend | BMV-700 | verbruikt sinds vol |
| `resterende_tijd` | min | doorlopend | BMV-700 | geen waarde tijdens laden |
| `zonnevermogen` | W | doorlopend | MPPT (3×) | vermogen van het paneel |
| `laadstroom_zon` | A | doorlopend | MPPT (3×) | stroom van de MPPT naar de accu |
| `walstroom` | 0/1 | toestand | MultiPlus | 1 = AC-ingang aanwezig |
| `bilge` | 0/1 | toestand | Shelly Plus Uni | 1 = hoogwatervlotter actief (§5.2.4) |
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
| Bilge | Hoogwatervlotter actief (§5.2.4) |
| Bilgepomp lang | Pomp draait aan één stuk langer dan drempel |
| Bilgepomp vaak | Aantal pompbeurten binnen een tijdvenster boven drempel |
| Sensor stil | Sensor levert geen data meer |
| Sensorbatterij laag | `batterijspanning` onder drempel |
| Beweging | Toename van de Ruuvi-bewegingsteller ≥ N binnen T seconden (instelbaar) |
| Helling (v0.31) | `helling` boven drempel (§5.2.5); bedoeld met een video als actie |

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
de acties `video` en `foto` (punt 6). Nog niet gebouwd: de schakelaar (punt 7) en de actie
`relais`; `check-config` weigert die actie tot hij er is.*

**Aanleiding.** Vastgelegd moest worden hoe een alarm wordt ingesteld, wat het teweegbrengt
(een notificatie, een actie, of geen van beide), hoe opgetreden alarmen bewaard en getoond
worden, en hoe een verkeerd ingesteld alarm snel te stoppen is.

**Redenering.** Een alarm, een notificatie en een actie zijn drie dingen. Een alarm stelt
vast dat een voorwaarde geldt; of daar een melding of een actie op volgt, is per regel in
te stellen. Zo is dezelfde werking bruikbaar voor een melding ("walstroom weg"), voor een
regeling zonder melding (verwarming aan bij vorst) en voor het vastleggen van een
gebeurtenis (video bij helling).

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
   | `{ type = "relais", relais = "<sensor>" }` | relais aan als het alarm afgaat, uit bij herstel (§5.2.6) |

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
   - **"Foto nu"** en **"Video nu"** geven de camera-reader een poll-nu met aanleiding
     `handmatig` (§5.7 punt 12).
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
   - Een **intervalfoto** staat **0,5 s** in beeld.
   - Een **foto bij een gebeurtenis** (elke andere aanleiding: alarm, op verzoek) staat
     **5 s** in beeld.
   - Een **video** wordt op zijn plaats in de tijd **volledig afgespeeld**; daarna gaat
     het overzicht verder. Met de knop **"verder"** is de video over te slaan.
   - **Bediening:** afspelen en pauze, en een tijdbalk van de dag waarop de gebeurtenissen
     gemarkeerd staan; een klik op de balk springt naar dat moment. Bij het beeld staan
     het tijdstip en de aanleiding.
   - De pagina haalt de volgende foto's alvast op terwijl de huidige in beeld is. Is een
     foto er nog niet, dan **wacht** het overzicht erop; er wordt niets overgeslagen.
   - Het dagoverzicht gebruikt alle opnames van de dag; de grens van 500 van de lijst
     geldt hier niet (een dag heeft 480 intervalfoto's plus de gebeurtenissen).
   - **Dataverbruik:** een dagoverzicht haalt alle foto's van die dag op: bij 480 foto's
     van bijna 300 kB ongeveer 140 MB, plus ongeveer 27 MB per video. Vanaf de boot gaat
     dat van de databundel en vraagt het ruim 4 Mbit/s om bij te blijven.

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
     extra afhankelijkheid of een tweede opname per foto; kan later alsnog.
3. Het web leest uit SQLite en geeft opdrachten via MQTT (poll-nu, verwijderen); het
   schrijft niet zelf naar database of opnamemap.
4. **Grafiekenpagina** (v0.22; indeling v0.25), de eerste pagina van het web. De
   gebruiker stelt zelf samen wat hij ziet (v0.25):
   - Met **"+ Live-grafiek"** en **"+ Database-grafiek"** voegt hij een grafiek toe; er
     kunnen meerdere grafieken op de pagina staan. Elke grafiek heeft één bron en een
     eigen venster of periode.
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
   - **Database**: de opgeslagen rijen (per 5 minuten, §5.3.2), over 1 uur, 6 uur, 24 uur,
     7 dagen of 30 dagen, met bladeren naar eerdere perioden. Een interval zonder rij
     blijft in de grafiek open.

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
  die nu actief zijn, en de opgetreden alarmen uit de tabel `alarm`. *Nog niet gebouwd.*
- In de grafieken de grens (streeplijn) en herstelgrens (stippellijn) in de kleur van de
  reeks, en de alarmperioden gearceerd in de strook van die reeks. *Gebouwd (v0.33).* Een
  grens buiten het bereik van de as wordt niet getekend: de as volgt de metingen. De
  perioden komen van `/api/alarmen`; een live-grafiek ververst ze elke 30 s.
- Het web leest de regels uit `brms.toml` en de alarmen uit de database; de schakelaar
  gaat als opdracht over MQTT (punt 3).

**[OPEN-15]** Is authenticatie nodig naast Tailscale? Met de schakelaar (v0.31) kan iemand
met toegang tot het web een alarmregel uitzetten.

### 5.7 Camera

*Alle stappen van punt 17 zijn gebouwd: de camera-reader met een foto per interval en op
verzoek (poll-nu), de tabel `opname` en het opruimen van oude foto's (v0.38), video op
verzoek met de maximale ruimte (v0.39), foto en video bij een alarm (v0.40, punt 15), en
bekijken en verwijderen in het web (v0.41, §5.6 punt 2). Foto's, video's en de foto bij
een alarm zijn getest met de testcamera (Foscam) en Telegram; de opnamepagina alleen op
de pc.*

**Redenering.** De camera bekijkt het dek. Een regelmatige foto toont de toestand van de
boot; een video legt een gebeurtenis vast (iemand stapt aan boord, storm). Een IP-camera
heeft na inschakelen ~30–60 s nodig om op te starten; een video bij een bewegingsalarm
komt dan te laat. Met 450 Ah accucapaciteit is er bij walstroomuitval ruim tijd om in te
grijpen, dus de camera kan altijd aan staan.

Foto's: één per 3 minuten ≈ 480 per dag × ~300 KB ≈ 150 MB/dag, ≈ 2 GB per twee weken.
Video's: ~30–60 MB per clip van 120 s. Video's worden bewust bewaard en verdwijnen niet
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

**Specificatie.**
1. IP-camera (wifi 2,4 GHz, RTSP/ONVIF, 12V), **altijd aan**.
2. De camera-reader is een pollende reader (§5.2.1) met twee soorten opname:

   | | Foto | Video |
   |---|---|---|
   | Wanneer | elke **3 min**, plus poll-nu | alleen **poll-nu** (gebruiker, of alarmlaag bij beweging) |
   | Duur | — | vast, **120 s** |
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
   | `video_duur` | standaard `"120s"` |

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
    bestand (v0.39).
12. **Poll-nu** (§5.2.1 punt 6): opdracht `brms/opdracht/camera/poll-nu` met
    `{"camera_id": ..., "type": "foto"|"video", "aanleiding": ...}`. Zonder `camera_id`:
    alle camera's.
13. Na een geslaagde opname publiceert de reader de metadata op `brms/opname/<camera_id>`
    (§3.3). Een **mislukte** opname (camera onbereikbaar, geen JPEG, `ffmpeg` geeft een
    fout) geeft één waarschuwing in het log en geen bericht; de reader probeert het niet
    opnieuw, de volgende intervalfoto komt vanzelf. Mislukken de intervalfoto's keer op
    keer, dan komt alleen de eerste keer een waarschuwing en één regel bij herstel
    (v0.38, zoals bij de Shelly); een mislukte foto op verzoek wordt altijd gemeld. Zolang de klok niet gesynchroniseerd
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
    camera. De testcamera thuis is een Foscam HD-camera (192.168.178.202, webpoort 89),
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
    (§5.6).

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
| Router-DO → relais → Pi-herstart | Pi onbereikbaar terwijl router werkt |
| Teltonika RMS | Router beheren/herstarten op afstand |
| SQLite WAL | Datacorruptie bij stroomuitval |
| Wekelijkse back-up naar OneDrive (§7.1) | Verlies van de opslag |

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
- Mobiel internet zit achter CGNAT → Tailscale op de Pi (key expiry uit), met subnet-route
  naar router en camera. Niets open op internet.
- Noodingang: Teltonika RMS + Pi-herstart via router-DO.

---

## 10. Ontwikkeling en uitrol

- Ontwikkelen in PyCharm op Windows; testen op de test-Pi 4 thuis (zelfde OS).
- Uitrol als systemd-services uit `deploy/systemd/`.

### 10.1 Uitrol en updates (besluit OPEN-19)

**Aanleiding.** Vastgesteld moest worden hoe de code op de (test-)Pi komt, hoe updates aan
boord worden uitgerold, en hoe de installatie op de Pi is ingedeeld.

**Redenering.** De code staat in een privérepo op GitHub. De boot-Pi draait onbeheerd en is
alleen via Tailscale bereikbaar; een update mag niets breken en moet terug te draaien zijn.
Met git op de Pi hoort elke installatie bij één commit en is teruggaan één commando. Een
deploy key geeft de Pi alleen leesrechten op deze ene repo; bij diefstal van de Pi is
alleen de BRMS-code leesbaar.

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

**Specificatie.**
1. De Pi haalt de code met **git** uit de GitHub-repo, met een **deploy key** (alleen
   leesrechten, alleen deze repo).
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
   `~/BRMS/data`. git en de deploy key horen ook bij `jandirk`; alleen pakketten
   installeren en services (her)starten vraagt `sudo`.
6. Systeempakketten via apt (Mosquitto, rclone, later ffmpeg; als beheerhulpmiddelen
   `mosquitto-clients`, `sqlite3` en `tmux`). Ze staan in één lijst,
   **`deploy/pakketten.txt`** (v0.17); `install.sh` en `update.sh` installeren wat daarvan
   ontbreekt, zodat een nieuw pakket bij de eerstvolgende update vanzelf meekomt. Een
   eenmalig **`deploy/install.sh`** richt de Pi in:
   pakketten, deploy key, clone, venv, configuratie, Mosquitto en systemd-units.
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
   loopt, vraagt om akkoord (Enter = ja) en vraagt hoe lang de simulatie moet lopen:
   standaard **10 minuten**, hooguit 60 minuten. Daarna begint de simulatie meteen. Er
   wordt niet meer eerst geluisterd of de echte sensor zendt (v0.25): dat kostte 15 s
   wachttijd.
3. **Startsein:** opdracht `brms/opdracht/simulatie/aan` met
   `{"sensor_id": ..., "duur": 60}` (seconden); eerder stoppen met
   `brms/opdracht/simulatie/uit` (`{"sensor_id": ...}`). Elke reader luistert mee en
   voert alleen de opdrachten voor zijn eigen sensoren uit.
4. Zolang de simulatie loopt:
   - maakt de reader elke **2 s** een advertentie aan in het formaat van de sensor en
     verwerkt die via dezelfde ingang als een advertentie van bleak, met het MAC-adres van
     de sensor;
   - **negeert** de reader de echte advertenties van die sensor (bypass). Andere sensoren
     lopen gewoon door.

   Na `duur` stopt de simulatie vanzelf en telt de echte sensor weer mee.

   Bij een **Shelly** (v0.29) maakt de Shelly-reader bij elke poll zelf het antwoord aan,
   in de vorm van `Shelly.GetStatus`, en verwerkt dat als een echt antwoord. De echte
   Shelly wordt zolang niet gepolld; hij hoeft er dus niet te zijn.
5. **Testreeks:** elke grootheid loopt lineair van begin- naar eindwaarde **en weer
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
6. In de trace (§10.2) begint de stap `ruw` van een gesimuleerde advertentie met
   `SIMULATIE`.
7. Gesimuleerde waarden zijn voor de rest van het systeem **gewone metingen**: ze komen
   onder het ID van de sensor in de database en laten (later) alarmen afgaan.
8. De opdracht geeft alleen het startsein en is dan klaar (v0.25): de shell is meteen weer
   vrij, bijvoorbeeld voor een tweede `brms sim` voor een andere sensor terwijl de eerste
   nog loopt. Elke simulatie stopt vanzelf na haar eigen duur. Eerder stoppen:
   **`brms sim stop <sensor ...>`** of **`brms sim stop all`**.
9. Draait de reader van een sensor niet (volgens systemd), dan meldt de opdracht dat er
   voor die sensor niets gesimuleerd wordt.

---

## 11. Open vragen

| # | Vraag | Sectie |
|---|---|---|
| 1 | ~~Eén proces of aparte services~~ – **besloten in v0.4**: losse services, lokale Mosquitto ertussen | §3.1 |
| 2 | Welke USB-BLE-dongle | §4.1 |
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
| 15 | Authenticatie op webinterface (weegt zwaarder sinds de alarmschakelaar, v0.31) | §5.6 |
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

Daarnaast uit `CLAUDE.md`: accutype (LiFePO4 of niet) en keuze data-abonnement/eSIM.

---

## Revisiegeschiedenis

| Versie | Datum | Auteur | Wijziging |
|---|---|---|---|
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
| **0.43** | 2026-11-10 | Claude Code, Fable 5.2 | Dagoverzicht gebouwd (§5.6 punt 2): tweede weergave van de opnamepagina, met afspelen, pauze, "verder" bij een video en een tijdbalk van de dag. |
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
