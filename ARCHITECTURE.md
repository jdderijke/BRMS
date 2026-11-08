# BRMS – Architectuur

**Versie 0.9 – concept** · 8 november 2026

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
 BLE-sensoren ──> BLE-reader ─────┐                       ┌──> alarmen ──> ntfy ──> telefoon
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
| Opdracht | `brms/opdracht/<doel>/<actie>`, bijv. `brms/opdracht/camera/poll-nu`, `brms/opdracht/shelly/poll-nu`, `brms/opdracht/opslag/verwijder` | afhankelijk van de actie, bijv. `{"type": "video", "aanleiding": "alarm:beweging"}` | 1 |

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

### 4.2 Klok: DS3231 RTC

**Redenering.** De Pi 4 heeft geen RTC. Na stroomuitval zonder internet is de tijd fout,
en daarmee de tijdstempels van metingen.
**Specificatie.** DS3231 via I2C. Alle tijdstempels in UTC. **[OPEN-3]** Gedrag zolang de
tijd na opstarten nog niet betrouwbaar is.

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
2. **Eén Shelly-reader** die alle Shelly's elke 30 s pollt, via de standaardbibliotheek
   (`urllib`, uitgevoerd in een thread vanuit asyncio).
3. Readers verwerken **alleen apparaten die in de configuratie staan**; al het andere wordt
   genegeerd.
4. Readers passen de **correcties uit de configuratie** toe en publiceren daarna elke
   ontvangen waarde via MQTT (§3.1) als
   `Meting(ts, sensor_id, grootheid, waarde, soort)`:
   - `ts`: ontvangsttijd op de Pi, Unix-tijd UTC (een advertentie heeft zelf geen tijd; zie [OPEN-3]);
   - `soort`: doorlopend, toestand of toename, vast per grootheid (§5.3.2);
   - alleen eenvoudige velden (getal, tekst), zodat het bericht direct als JSON te versturen is.
5. Readers zijn asyncio-gebaseerd, **zonder extra bibliotheken** naast `bleak` en de
   `paho-mqtt` (§3.2).
6. Elke **pollende** reader (Shelly, camera) heeft een **poll-nu**-ingang: een MQTT-opdracht
   die direct een poll uitvoert, los van het interval. Alarmlaag, web en gebruiker kunnen
   die opdracht geven. De BLE-reader heeft geen poll-nu.

| Reader | Bron | Methode | Status |
|---|---|---|---|
| BTHome v2 | Xiaomi Monitor 3 (pvvx) | BLE-advertenties, `bleak` | Eerste stap |
| Ruuvi | RuuviTag (Pro) | BLE-advertenties; **[OPEN-5]** `ruuvitag-sensor` of eigen decoder op `bleak` | Gepland |
| Victron | BMV-700, 3× MPPT, MultiPlus | BLE Instant Readout, decoder in de BLE-reader (§5.2.2) | Gepland |
| Shelly | Bilge | Lokale HTTP-API | Gepland |
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
64 mG, vaste firmwaredrempel) springt na 255 terug naar 0. Een gemiddelde van de ruwe
teller is betekenisloos (254, 255, 0, 1 → ~127). Wat telt is de *toename*: die is per
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
1. Readers ontvangen zo vaak als de bron levert. Shelly's worden elke **30 s** gepolld.
2. De alarmlaag beoordeelt **elke ontvangen waarde**, onafhankelijk van de opslag.
3. Elke grootheid heeft een vaste soort, vastgelegd in de reader:

   | Soort | Voorbeelden | Opslag |
   |---|---|---|
   | Doorlopend | temperatuur, vocht, spanning, stroom, SoC, RSSI, sensorbatterij, versnelling X/Y/Z | gemiddelde van de ontvangen waarden over **5 minuten** |
   | Toestand (0/1) | bilgevlotter, walstroom aanwezig | nooit gemiddeld; een rij bij **elke verandering** met het exacte tijdstip, plus elke **5 minuten** de actuele toestand op dat moment |
   | Toename | beweging (toename van de Ruuvi-bewegingsteller) | **som** over 5 minuten |

   De ruwe bewegingsteller wordt niet opgeslagen; de Ruuvi-decoder levert de toename.

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
| `versnelling_x`, `versnelling_y`, `versnelling_z` | mG | doorlopend | Ruuvi | gemiddelde ≈ helling van de boot |
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
| `bilge` | 0/1 | toestand | Shelly Plus Uni | 1 = vlotter actief |

**[OPEN-11]** Bewaartermijn / downsampling van oude data.

### 5.4 Configuratie

**Redenering (v0.5).** Drempels en instellingen moeten zonder codewijziging aan te passen
zijn. Eén configuratiebestand naast de sensorconfig is de eenvoudigste plek (P1); instellen
via het web vraagt schrijfrechten, invoercontrole en een extra tabel.

**Verworpen alternatieven.** *Instellingen via de webinterface (opgeslagen in SQLite).*
Complexer; kan later alsnog.

**Specificatie.** Eén configuratiebestand, buiten git (bevat de Victron-sleutel), met:
- sensorconfiguratie (MAC → naam/locatie, correcties/offsets);
- alarmdrempels en -instellingen, per alarm en waar nodig per sensor (§5.5);
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
2. **Eén bestand** voor alle services: **`/etc/brms/brms.toml`** op de Pi, buiten git en
   los van de code. Elke service leest zijn eigen deel. Een voorbeeldbestand
   (`config/brms.example.toml`) staat wel in git.
3. Geheimen van rclone (OneDrive-token) staan niet in dit bestand maar in rclone's eigen
   configuratie.
4. Een wijziging wordt doorgevoerd door de betreffende service te **herstarten**
   (`systemctl restart`).
5. Elke service **controleert de configuratie bij het starten** en meldt fouten duidelijk
   in het log. Met **`brms check-config`** is de configuratie vooraf te controleren, vóór
   een herstart.

### 5.5 Alarmen en meldingen

**Redenering.** Het doel van het systeem (§1) is op tijd ingrijpen.
**Specificatie.** Meldingen via ntfy (push naar telefoon). Alarmen:

| Alarm | Voorwaarde |
|---|---|
| Walstroom weg | `walstroom` = 0 (MultiPlus, §5.2.2) |
| Accu laag | SoC onder drempel, bijv. 50 % |
| Vocht | Luchtvochtigheid langdurig > 80 % |
| Vorst | Temperatuur < 2 °C |
| Bilge | Vlotter actief |
| Sensor stil | Sensor levert geen data meer |
| Beweging | Toename van de Ruuvi-bewegingsteller ≥ N binnen T seconden (instelbaar) |

Alle drempels en instellingen staan in het configuratiebestand (§5.4).

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

**[OPEN-13]** Per alarm: exacte drempel, duur ("langdurig"), hysterese, herhaling van
meldingen, en melding bij herstel.
**[OPEN-14]** ntfy-server: publieke ntfy.sh of zelf gehost. (Ntfy.sh vereist internet;
bij uitval van mobiel internet komt geen melding aan – zie ook §8.)

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
3. Het web leest uit SQLite en geeft opdrachten via MQTT (poll-nu, verwijderen); het
   schrijft niet zelf naar database of opnamemap.

**[OPEN-15]** Is authenticatie nodig naast Tailscale?

### 5.7 Camera (later)

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

**Specificatie.**
1. IP-camera (wifi 2,4 GHz, RTSP/ONVIF, 12V), **altijd aan**.
2. De camera-reader is een pollende reader (§5.2.1) met twee soorten opname:

   | | Foto | Video |
   |---|---|---|
   | Wanneer | elke **3 min**, plus poll-nu | alleen **poll-nu** (gebruiker, of alarmlaag bij beweging) |
   | Duur | — | vast, **120 s** |
   | Bewaren | **2 weken**, daarna automatisch verwijderd; eerder handmatig mogelijk | tot de gebruiker hem verwijdert |

   Alle waarden instelbaar in de configuratie (§5.4).
3. Een poll-nu voor video **tijdens een lopende video wordt genegeerd**.
4. De camera-reader schrijft het bestand naar de opnamemap en publiceert de metadata via
   MQTT; de opslag-service legt die vast in de tabel `opname` (§5.3.1). Het beeld zelf gaat
   niet via MQTT.
5. Voor video's geldt een **maximale ruimte** (instelbaar). Dreigt die vol te raken, dan
   verwijdert de opslag-service de **oudste video's** tot er weer ruimte is.
6. **[OPEN-24]** Opnamemethode (snapshot-URL / RTSP; voor video in de praktijk `ffmpeg`
   via apt) – afhankelijk van het cameratype.

---

## 6. Gegevensstroom

Reader ontvangt/leest → decodeert → past correcties uit config toe → publiceert elke
waarde via MQTT (§3.1, §5.2.1) → de alarmlaag beoordeelt elke waarde; de opslag middelt of
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
- Uitrol als systemd-service(s) uit `deploy/systemd/`.
- **[OPEN-19]** Hoe code op de Pi komt (git pull, of iets anders) en hoe updates aan boord
  worden uitgerold.

---

## 11. Open vragen

| # | Vraag | Sectie |
|---|---|---|
| 1 | ~~Eén proces of aparte services~~ – **besloten in v0.4**: losse services, lokale Mosquitto ertussen | §3.1 |
| 2 | Welke USB-BLE-dongle | §4.1 |
| 3 | Gedrag zolang de systeemtijd nog niet betrouwbaar is | §4.2 |
| 4 | ~~Interface van een reader~~ – **besloten in v0.4**: één BLE-reader met decoders, één Shelly-reader, push via MQTT | §5.2.1 |
| 5 | Ruuvi: `ruuvitag-sensor` of eigen decoder | §5.2 |
| 6 | ~~Victron: BLE of VE.Direct-USB~~ – **besloten in v0.9**: BLE Instant Readout | §5.2.2 |
| 7 | ~~Type laadregelaar zonnepaneel~~ – **besloten in v0.9**: 3× Victron MPPT via BLE | §5.2.2 |
| 8 | ~~Wanneer geldt de walstroom-Shelly als offline~~ – **vervallen in v0.9**: walstroom via MultiPlus | §5.2.2 |
| 9 | ~~Datamodel SQLite~~ – **besloten in v0.2**: generieke tabel `meting`, stills als bestand + tabel `still` (sinds v0.5 `opname`) | §5.3.1 |
| 10 | ~~Meetinterval en schrijffrequentie~~ – **besloten in v0.3**: opslag per 5 min (gemiddelde; toestanden bij verandering), alarm op elke waarde, Shelly-poll 30 s | §5.3.2 |
| 11 | Bewaartermijn / downsampling | §5.3 |
| 12 | ~~Formaat configuratiebestand~~ – **besloten in v0.7**: TOML, één bestand `/etc/brms/brms.toml`, herstart bij wijziging, controle bij start | §5.4.1 |
| 13 | Alarmdetails: drempels, duur, hysterese, herhaling, herstel | §5.5 |
| 14 | ntfy.sh of zelf gehost | §5.5 |
| 15 | Authenticatie op webinterface | §5.6 |
| 16 | ~~Back-up~~ – **besloten in v0.6**: wekelijks naar OneDrive met `rclone copy` | §7.1 |
| 17 | Reikwijdte van de watchdog | §7 |
| 18 | Detectie thuis als BRMS zelf uitvalt | §8 |
| 19 | Uitrol van code en updates | §10 |
| 20 | ~~Bewaartermijn van stills~~ – **besloten in v0.5**: foto's 2 weken, video's tot de gebruiker ze verwijdert, met maximale ruimte | §5.7 |
| 21 | Maatregelen op OS-niveau tegen flashslijtage | §7 |
| 22 | ~~MQTT-clientbibliotheek~~ – **besloten in v0.8**: `paho-mqtt==2.1.0` direct, met eigen busmodule | §3.2 |
| 23 | ~~MQTT-topic-indeling en berichtformaat~~ – **besloten in v0.8**: `brms/meting`, `brms/opname`, `brms/opdracht`, complete JSON-berichten, geen retain | §3.3 |
| 24 | Opnamemethode camera (snapshot/RTSP, ffmpeg) | §5.7 |
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
| **0.9** | 2026-11-08 | Claude Code, Opus 5.5 | OPEN-6 en OPEN-7 besloten (§5.2.2): BMV-700, 3× MPPT (2× 75/15 met VE.Direct-dongle, 100/30 SmartSolar) en MultiPlus (VE.Bus Smart dongle) via BLE Instant Readout. Walstroom via MultiPlus in plaats van Shelly; OPEN-8 vervallen; Shelly alleen nog voor bilge. OPEN-25 besloten (§5.3.3): tabel van grootheden. §5.3.2, §5.5 bijgewerkt. |
