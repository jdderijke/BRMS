# BRMS – Architectuur

**Versie 0.5 – concept** · 8 november 2026

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
7. **[OPEN-22]** MQTT-clientbibliotheek (de standaardbibliotheek heeft er geen; voor de
   hand ligt `paho-mqtt`).
8. **[OPEN-23]** Topic-indeling en berichtformaat (bijv. `brms/<sensor_id>/<grootheid>`, JSON).

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
- *Threads in plaats van asyncio.* `bleak` vereist asyncio toch al.
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
   MQTT-client ([OPEN-22]).
6. Elke **pollende** reader (Shelly, camera) heeft een **poll-nu**-ingang: een MQTT-opdracht
   die direct een poll uitvoert, los van het interval. Alarmlaag, web en gebruiker kunnen
   die opdracht geven. De BLE-reader heeft geen poll-nu.

| Reader | Bron | Methode | Status |
|---|---|---|---|
| BTHome v2 | Xiaomi Monitor 3 (pvvx) | BLE-advertenties, `bleak` | Eerste stap |
| Ruuvi | RuuviTag (Pro) | BLE-advertenties; **[OPEN-5]** `ruuvitag-sensor` of eigen decoder op `bleak` | Gepland |
| Victron | BMV-700 | **[OPEN-6]** BLE Instant Readout (`victron-ble`) of VE.Direct-USB (tekstprotocol) | Gepland |
| Shelly | Walstroom, bilge | Lokale HTTP-API | Gepland |
| Laadregelaar | Zonnepaneel 50 Wp | **[OPEN-7]** afhankelijk van type | Open |
| Camera | IP-camera dek | Foto per interval, video op poll-nu (§5.7) | Later |
| GPIO | Waar nodig | — | Gepland |

**Walstroom.** De Shelly zit aan de 230V-kant: als hij offline is, is de walstroom weg.
**[OPEN-8]** Hoe "offline" wordt vastgesteld (aantal gemiste polls / tijdsduur; poll-interval 30 s, §5.3.2).

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
    sensor_id  TEXT    NOT NULL,  -- vaste ID (bijv. MAC of 'shelly-walstroom'), niet de naam
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
bij walstroom betekent 0 "walstroom weg", wat iets anders is dan "geen bericht van de Shelly".

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

Wijzigen gaat via SSH; de betreffende service leest het bestand opnieuw in.
**[OPEN-12]** Bestandsformaat. *(Het projectskelet bevat een `brms.example.toml`; dat is
een voorstel, geen besluit.)*

### 5.5 Alarmen en meldingen

**Redenering.** Het doel van het systeem (§1) is op tijd ingrijpen.
**Specificatie.** Meldingen via ntfy (push naar telefoon). Alarmen:

| Alarm | Voorwaarde |
|---|---|
| Walstroom weg | Shelly walstroom offline (§5.2) |
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
| Periodieke back-up SQLite naar thuis via Tailscale | Verlies van de opslag |

**[OPEN-21]** Maatregelen op OS-niveau tegen flashslijtage (o.a. journald-logs, swap,
caches). Het schrijfvolume van de database zelf is verwaarloosbaar (§5.3.2).
Of foto's en video's in de back-up worden meegenomen is instelbaar in de configuratie
(§5.4), omdat foto's alleen al ~4,5 GB per maand aan mobiele data kunnen kosten.
**[OPEN-16]** Back-up: frequentie, methode, bestemming.
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
| 6 | Victron: BLE of VE.Direct-USB | §5.2 |
| 7 | Type laadregelaar zonnepaneel | §5.2 |
| 8 | Wanneer geldt de walstroom-Shelly als offline | §5.2 |
| 9 | ~~Datamodel SQLite~~ – **besloten in v0.2**: generieke tabel `meting`, stills als bestand + tabel `still` (sinds v0.5 `opname`) | §5.3.1 |
| 10 | ~~Meetinterval en schrijffrequentie~~ – **besloten in v0.3**: opslag per 5 min (gemiddelde; toestanden bij verandering), alarm op elke waarde, Shelly-poll 30 s | §5.3.2 |
| 11 | Bewaartermijn / downsampling | §5.3 |
| 12 | Formaat configuratiebestand | §5.4 |
| 13 | Alarmdetails: drempels, duur, hysterese, herhaling, herstel | §5.5 |
| 14 | ntfy.sh of zelf gehost | §5.5 |
| 15 | Authenticatie op webinterface | §5.6 |
| 16 | Back-up: frequentie, methode, bestemming | §7 |
| 17 | Reikwijdte van de watchdog | §7 |
| 18 | Detectie thuis als BRMS zelf uitvalt | §8 |
| 19 | Uitrol van code en updates | §10 |
| 20 | ~~Bewaartermijn van stills~~ – **besloten in v0.5**: foto's 2 weken, video's tot de gebruiker ze verwijdert, met maximale ruimte | §5.7 |
| 21 | Maatregelen op OS-niveau tegen flashslijtage | §7 |
| 22 | MQTT-clientbibliotheek | §3.1 |
| 23 | MQTT-topic-indeling en berichtformaat | §3.1 |
| 24 | Opnamemethode camera (snapshot/RTSP, ffmpeg) | §5.7 |

Daarnaast uit `CLAUDE.md`: accutype (LiFePO4 of niet) en keuze data-abonnement/eSIM.

---

## Revisiegeschiedenis

| Versie | Datum | Auteur | Wijziging |
|---|---|---|---|
| 0.1 | 2026-11-08 | Claude Code, Opus 5.5 | Eerste concept, opgesteld uit `CLAUDE.md`; open vragen geïnventariseerd. |
| 0.2 | 2026-11-08 | Claude Code, Opus 5.5 | OPEN-9 besloten: generieke tabel `meting`; stills als bestand met tabel `still` (§5.3.1), met aanleiding, redenering en verworpen alternatieven. OPEN-20 toegevoegd; OPEN-16 uitgebreid met stills. |
| 0.3 | 2026-11-08 | Claude Code, Opus 5.5 | OPEN-10 besloten (§5.3.2): ontvangen, alarmeren en opslaan losgekoppeld; opslag per 5 min als gemiddelde voor doorlopende grootheden, toestanden nooit gemiddeld maar bij verandering; Shelly-poll 30 s. OPEN-21 toegevoegd (flashslijtage door OS). §5.2, §6 bijgewerkt. |
| 0.4 | 2026-11-08 | Claude Code, Opus 5.5 | OPEN-1 besloten (§3.1): losse services met lokale Mosquitto-broker (geen persistence, QoS 1 toestanden / 0 doorlopend, weggooien bij onbereikbare broker). OPEN-4 besloten (§5.2.1): één BLE-reader met decoders, één Shelly-reader, alleen geconfigureerde apparaten, asyncio. OPEN-22 en OPEN-23 toegevoegd. Systeemoverzicht (§3) en §6 bijgewerkt. |
| **0.5** | 2026-11-08 | Claude Code, Opus 5.5 | Poll-nu voor pollende readers, niet voor BLE (§5.2.1). Camera uitgewerkt (§5.7): altijd aan (relais vervalt), foto elke 3 min / 2 weken, video 120 s op poll-nu / tot handmatig verwijderd, maximale videoruimte; OPEN-20 besloten, OPEN-24 toegevoegd. Tabel `still` → `opname` (§5.3.1). Derde soort "toename" voor de Ruuvi-bewegingsteller; versnelling X/Y/Z opgeslagen (§5.3.2). Bewegingsalarm met rustperiode 10 min (§5.5.1). Drempels en instellingen in configuratiebestand (§5.4). Verwijderen van opnames via web → opslag-service (§5.6). Back-up van opnames instelbaar (§7). |
