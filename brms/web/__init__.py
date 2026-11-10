"""Webinterface: Flask + Chart.js, alleen bereikbaar via Tailscale/LAN (ARCHITECTURE.md §5.6).

Nu één pagina met grafieken (§5.6 punt 4). De gebruiker voegt zelf grafieken toe en kiest
per grafiek welke reeksen (sensor + grootheid) erin staan. Een grafiek heeft één bron:

- **live**      de waarden zoals ze nu over de MQTT-bus gaan (elke ontvangen meting);
- **database**  wat de opslag-service heeft weggeschreven (per 5 minuten), ook historisch.

Opbouw:
- De webservice abonneert zich op brms/meting/# en bewaart de metingen van de laatste 24
  uur per sensor en grootheid in het geheugen (LiveBuffer). De pagina vraagt elke paar
  seconden, in één aanvraag voor alle live-grafieken, wat er nieuw is. Niets hiervan
  wordt opgeslagen: na een herstart begint het geheugen leeg.
- De database wordt alleen gelezen, met een alleen-lezen verbinding per aanvraag. De
  opslag-service blijft de enige schrijver (§3.1).
- Chart.js staat als bestand in static/ (geen CDN): de pagina werkt ook als de boot geen
  internet heeft en je via het lokale netwerk kijkt.

Adressen:
    /                 de pagina
    /api/live         (POST) metingen van de bus voor een lijst reeksen, zie api_live
    /api/historie     ?sensor=<id>&grootheid=<naam>&van=<ts>&tot=<ts>  rijen uit de database
    /api/alarmen      ?sensor=<id>&grootheid=<naam>&van=<ts>&tot=<ts>  alarmperioden (§5.5.3)
    /opnames          de pagina met foto's en video's van de camera's (§5.6 punt 2)
    /api/opnames      ?van=<ts>&tot=<ts>[&camera=<id>][&type=foto|video][&interval=0]
                      [&alles=1]  (alles=1: de hele dag, voor het dagoverzicht)
    /opname/<pad>     het bestand van één opname (JPEG of MP4)
    /api/opnames/verwijder   (POST) opnames verwijderen; gaat als opdracht naar de opslag
    /api/opnames/nu          (POST) nu een foto of video laten maken (poll-nu)
    /live/<camera_id>        het live-beeld van een camera (MJPEG-stroom, §5.7 punt 18)

Er is geen aanmelding: de pagina is alleen via Tailscale of het lokale netwerk bereikbaar
(OPEN-15).
"""

import array
import bisect
import logging
import json
import pathlib
import re
import sqlite3
import threading
import time
import urllib.request

from flask import Flask, Response, abort, jsonify, render_template, request, send_from_directory

from brms import VERSION, config, grootheden
from brms.config import Config
from brms.meting import TOPIC_PREFIX, Meting

log = logging.getLogger(__name__)

# Opdrachten die het web over de bus geeft (§5.6 punt 3); zie brms/storage.py en
# brms/readers/camera.py.
TOPIC_VERWIJDER = "brms/opdracht/opslag/verwijder"
TOPIC_POLL_NU = "brms/opdracht/camera/poll-nu"
# Zoveel opnames toont de pagina hooguit in één keer, en zoveel mag één verwijderopdracht
# noemen.
MAX_OPNAMES = 500
# Het dagoverzicht (§5.6 punt 2) speelt alle opnames van een dag af: 480 intervalfoto's
# bij één foto per 3 minuten, meer bij een korter interval of meer camera's. Dit is het
# vangnet.
MAX_DAG = 5000
# Zo lang mag de browser het bestand van een opname bewaren zonder het opnieuw te vragen
# (seconden). Een opname verandert nooit meer; bij terugspoelen in het dagoverzicht hoeft
# de foto dan niet opnieuw over de (mobiele) verbinding.
BEWAAR_OPNAME = 24 * 3600
# Het pad van een opname binnen de opnamemap (zie brms/storage.py); alleen deze vorm wordt
# uitgeleverd.
_BESTAND = re.compile(r"^[A-Za-z0-9_-]+/\d{4}/\d{2}/\d{2}/\d{6}\.(jpg|mp4)$")

# --- live-beeld (§5.7 punt 18) ---
# Na zoveel seconden stopt een live-beeld vanzelf: een vergeten tabblad mag de databundel
# van de boot niet leegtrekken.
MAX_LIVE = 600
# Zoveel kijkers tegelijk: elke kijker is een eigen verbinding met de camera.
MAX_KIJKERS = 3
# Langste wachttijd op de camera (verbinden, en op het volgende stuk van de stroom).
LIVE_TIMEOUT = 10.0
# Een beeld groter dan dit is geen beeld (vangnet tegen een adres waarop iets anders
# antwoordt).
MAX_BEELD = 5 * 1024 * 1024
# De grens tussen twee beelden in de stroom naar de browser.
LIVE_GRENS = "brmsbeeld"
# Begin en einde van een JPEG.
_SOI, _EOI = b"\xff\xd8", b"\xff\xd9"
_LENGTE = re.compile(rb"content-length:\s*(\d+)", re.IGNORECASE)

# Naam van de kop waarin de webservice bij elk antwoord zijn starttijdstip meestuurt. De
# pagina ziet daaraan dat de webservice herstart is en laadt zichzelf dan opnieuw (§5.6).
KOP_GESTART = "X-BRMS-Gestart"

# Aantal seconden dat metingen van de bus in het geheugen blijven voor de live-grafiek:
# het langste venster dat de pagina aanbiedt (24 uur).
BEWAAR = 24 * 3600

# Bovengrens per sensor en grootheid, als vangnet voor het geheugen. Een RuuviTag zendt
# ongeveer elke seconde: 86.400 metingen per dag. Een meting kost 24 bytes; drie Xiaomi's
# (4 grootheden, elke 2,5 s) komen daarmee op ongeveer 10 MB.
MAX_PUNTEN = 100_000

# Grenzen voor wat de pagina in één keer mag opvragen.
MAX_AANTAL = 1000   # "laatste N metingen"
MAX_OVERSLAAN = MAX_PUNTEN   # terugbladeren kan niet verder dan wat er bewaard wordt
MAX_STAP = 3600     # s; breedste tijdvak bij samenvatten
MAX_VRAGEN = 64     # reeksen in één aanvraag aan /api/live

# Langste periode die in één keer uit de database wordt gelezen (in seconden). 31 dagen is
# bijna 9000 rijen per grootheid; meer wordt traag in de browser.
MAX_PERIODE = 31 * 24 * 3600


class _Reeks:
    """De metingen van één sensor en grootheid, compact opgeslagen in drie rijen getallen
    (24 bytes per meting; een lijst van Python-objecten kost ruim vier keer zoveel)."""

    def __init__(self):
        self.nr = array.array("q")      # volgnummer (oplopend)
        self.ts = array.array("q")      # ontvangsttijd, Unix-seconden
        self.waarde = array.array("d")


class LiveBuffer:
    """De metingen van de bus van de laatste 24 uur, per sensor en grootheid.

    Elke meting krijgt een oplopend volgnummer. De pagina onthoudt het laatste volgnummer
    dat ze gezien heeft en vraagt daarna alleen wat nieuwer is; zo komt er niets dubbel en
    wordt er niets gemist, ook als twee metingen in dezelfde seconde vallen.

    Alles staat alleen in het geheugen: na een herstart van de webservice is het leeg.

    Metingen komen binnen in paho's netwerkthread en worden gelezen in de threads van de
    webserver; daarom een lock.
    """

    def __init__(self, bewaar: int = BEWAAR, maximum: int = MAX_PUNTEN):
        self._bewaar = bewaar
        self._maximum = maximum
        self._lock = threading.Lock()
        self._nr = 0
        self._reeksen: dict[tuple[str, str], _Reeks] = {}

    def bij_bericht(self, topic: str, payload: bytes) -> None:
        """Callback voor de bus (brms/meting/#)."""
        try:
            self.voeg_toe(Meting.van_json(payload))
        except ValueError as e:
            log.debug("ongeldig bericht op %s genegeerd: %s", topic, e)

    def voeg_toe(self, m: Meting) -> None:
        with self._lock:
            self._nr += 1
            r = self._reeksen.setdefault((m.sensor_id, m.grootheid), _Reeks())
            r.nr.append(self._nr)
            r.ts.append(m.ts)
            r.waarde.append(m.waarde)
            # Opruimen: wat ouder is dan `bewaar`, of meer dan `maximum`. Vooraan uit een rij
            # verwijderen verschuift de hele rij; daarom niet bij elke meting, maar pas als
            # er een flink stuk (1/8 extra) weg kan.
            marge = self._bewaar // 8
            if r.ts[0] < m.ts - self._bewaar - marge or len(r.ts) > self._maximum + self._maximum // 8:
                weg = max(bisect.bisect_left(r.ts, m.ts - self._bewaar), len(r.ts) - self._maximum)
                del r.nr[:weg], r.ts[:weg], r.waarde[:weg]

    def lees(self, vragen: list[dict]) -> tuple[int, list[list], list[bool]]:
        """Beantwoordt een lijst vragen, elk over één reeks (sensor + grootheid).

        Een vraag is een dict met:
            sensor_id, grootheid   welke reeks
            na      alleen metingen met een volgnummer groter dan dit (standaard 0)
            vanaf   alleen metingen met ts >= vanaf (optioneel)
            aantal  hooguit de laatste zoveel metingen (optioneel)
            tot     alleen metingen met ts < tot (optioneel). Voor een eerder venster
                    (terugbladeren, §5.6 punt 4): een vast eindpunt, zodat het venster
                    niet verschuift terwijl er nieuwe metingen binnenkomen.
            overslaan  sla de laatste zoveel metingen (vóór `tot`) over (optioneel).
                    "De 50 metingen daarvoor" is aantal=50, overslaan=50.
            stap    0 = elke meting; anders samengevat per tijdvak van `stap` seconden
                    (zie _vat_samen). Voor lange vensters: 24 uur aan losse metingen is te
                    veel voor de browser en voor de mobiele verbinding.

        Geeft (hoogste volgnummer tot nu toe, per vraag een lijst [[ts, waarde], ...],
        per vraag of er in het geheugen nog metingen vóór de eerste geleverde staan).
        Alle vragen worden op hetzelfde moment beantwoord, zodat dat ene volgnummer voor
        alle reeksen klopt.
        """
        # Onder de lock alleen de benodigde stukken kopiëren (snel); het samenvatten
        # gebeurt daarna, zodat de bus niet hoeft te wachten.
        stukken = []
        with self._lock:
            hoogste = self._nr
            for v in vragen:
                r = self._reeksen.get((v["sensor_id"], v["grootheid"]))
                if r is None:
                    stukken.append(None)
                    continue
                # Het stuk [i:j] van de reeks. Eerst het einde j: alles, of tot een vast
                # tijdstip, en daarvan eventueel de laatste `overslaan` niet.
                j = len(r.ts) if v.get("tot") is None else bisect.bisect_left(r.ts, v["tot"])
                j = max(0, j - (v.get("overslaan") or 0))
                # Dan het begin i: de strengste van de gevraagde grenzen.
                i = bisect.bisect_right(r.nr, v["na"]) if v.get("na") else 0
                if v.get("vanaf") is not None:
                    i = max(i, bisect.bisect_left(r.ts, v["vanaf"]))
                if v.get("aantal") is not None:
                    i = max(i, j - v["aantal"])
                i = min(i, j)
                stukken.append((r.ts[i:j], r.waarde[i:j], i > 0))

        antwoorden = []
        for v, stuk in zip(vragen, stukken):
            if stuk is None:
                antwoorden.append([])
            elif v.get("stap"):
                antwoorden.append(_vat_samen(stuk[0], stuk[1], v["stap"],
                                             grootheden.soort(v["grootheid"])))
            else:
                antwoorden.append([[t, w] for t, w in zip(stuk[0], stuk[1])])
        return hoogste, antwoorden, [bool(stuk and stuk[2]) for stuk in stukken]


def _vat_samen(ts, waarden, stap: int, soort: str) -> list:
    """Vat metingen samen per tijdvak van `stap` seconden: [[begin van het vak, waarde], ...].

    De vakken liggen op vaste kloktijden (veelvouden van `stap`), zodat een volgende vraag
    dezelfde vakken oplevert. De waarde hangt af van de soort (zoals in de opslag, §5.3.2):
    - doorlopend: het gemiddelde;
    - toename:    de som;
    - toestand:   het maximum – een korte 1 (bilgepomp die even loopt) blijft zo zichtbaar.
    """
    uit = []
    vak, som, n, hoogste = None, 0.0, 0, 0.0

    def sluit():
        if soort == grootheden.DOORLOPEND:
            uit.append([vak, round(som / n, 4)])
        elif soort == grootheden.TOENAME:
            uit.append([vak, som])
        else:
            uit.append([vak, hoogste])

    for t, w in zip(ts, waarden):
        v = t // stap * stap
        if v != vak:
            if vak is not None:
                sluit()
            vak, som, n, hoogste = v, 0.0, 0, w
        som += w
        n += 1
        hoogste = max(hoogste, w)
    if vak is not None:
        sluit()
    return uit


def lees_historie(pad: str, sensor_id: str, grootheid: str, van: int, tot: int) -> list:
    """Leest één reeks uit de database: [[ts, waarde], ...], oplopend in de tijd, voor
    van <= ts <= tot.

    Bestaat de database (nog) niet, dan is het resultaat leeg.
    """
    if not pathlib.Path(pad).exists():
        return []
    # mode=ro: alleen lezen; deze verbinding kan de database niet wijzigen (§3.1: de
    # opslag-service is de enige schrijver). as_uri() maakt er ook op Windows een geldig
    # adres van.
    con = sqlite3.connect(pathlib.Path(pad).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        # De tabel is gesorteerd op (sensor_id, grootheid, ts): een reeks is één
        # aaneengesloten stuk (§5.3.1).
        rijen = con.execute(
            "SELECT ts, waarde FROM meting "
            "WHERE sensor_id = ? AND grootheid = ? AND ts >= ? AND ts <= ? ORDER BY ts",
            (sensor_id, grootheid, van, tot)).fetchall()
        return [[ts, w] for ts, w in rijen]
    except sqlite3.OperationalError as e:
        # Bijv. de tabel bestaat nog niet (de opslag heeft nog nooit gedraaid).
        log.warning("database niet leesbaar: %s", e)
        return []
    finally:
        con.close()


def lees_alarmen(pad: str, sensor_id: str, grootheid: str, van: int, tot: int) -> list:
    """Leest de alarmen van één reeks die (deels) tussen `van` en `tot` vallen:
    [[begin, einde, alarm_id], ...], oplopend in de tijd. `einde` is None zolang het alarm
    actief is. Voor het arceren van de alarmperioden in een grafiek (§5.5.3 punt 9).

    Bestaat de database of de tabel (nog) niet, dan is het resultaat leeg.
    """
    if not pathlib.Path(pad).exists():
        return []
    con = sqlite3.connect(pathlib.Path(pad).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        rijen = con.execute(
            "SELECT begin, einde, alarm_id FROM alarm "
            "WHERE sensor_id = ? AND grootheid = ? AND begin <= ? AND (einde IS NULL OR einde >= ?) "
            "ORDER BY begin", (sensor_id, grootheid, tot, van)).fetchall()
        return [[begin, einde, alarm_id] for begin, einde, alarm_id in rijen]
    except sqlite3.OperationalError as e:
        # Bijv. de tabel bestaat nog niet (de opslag heeft nog niet gedraaid sinds de update).
        log.debug("alarmen niet leesbaar: %s", e)
        return []
    finally:
        con.close()


def lees_opnames(pad: str, van: int, tot: int, camera_id: str | None = None,
                 type_: str | None = None, interval: bool = True,
                 limiet: int = MAX_OPNAMES) -> tuple[list, dict]:
    """Leest de opnames tussen `van` en `tot` (de nieuwste eerst, hooguit `limiet`), en
    de totalen van alle opnames per type.

    camera_id, type_  alleen deze camera of dit type (None = alle)
    interval          False = zonder de intervalfoto's: alleen wat op verzoek of bij een
                      alarm is gemaakt
    Geeft (opnames, totalen): opnames als lijst van dicts, totalen als
    {"foto": [aantal, bytes], "video": [aantal, bytes]}. Bestaat de database of de tabel
    (nog) niet, dan is alles leeg.
    """
    totalen = {"foto": [0, 0], "video": [0, 0]}
    if not pathlib.Path(pad).exists():
        return [], totalen
    waar, waarden = ["ts >= ?", "ts < ?"], [van, tot]
    if camera_id is not None:
        waar.append("camera_id = ?")
        waarden.append(camera_id)
    if type_ is not None:
        waar.append("type = ?")
        waarden.append(type_)
    if not interval:
        waar.append("aanleiding != 'interval'")
    con = sqlite3.connect(pathlib.Path(pad).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        rijen = con.execute(
            "SELECT ts, camera_id, type, duur, bestand, bytes, aanleiding FROM opname "
            f"WHERE {' AND '.join(waar)} ORDER BY ts DESC, camera_id LIMIT ?",
            (*waarden, limiet)).fetchall()
        for type__, aantal, bytes_ in con.execute(
                "SELECT type, COUNT(*), SUM(bytes) FROM opname GROUP BY type"):
            if type__ in totalen:
                totalen[type__] = [aantal, bytes_]
    except sqlite3.OperationalError as e:
        log.debug("opnames niet leesbaar: %s", e)
        return [], totalen
    finally:
        con.close()
    sleutels = ("ts", "camera_id", "type", "duur", "bestand", "bytes", "aanleiding")
    return [dict(zip(sleutels, rij)) for rij in rijen], totalen


def beelden(stroom, blok: int = 16384):
    """Haalt de losse JPEG-beelden uit een MJPEG-stroom (multipart/x-mixed-replace).

    stroom: een object met read(n) (het antwoord van de camera). Geeft elk beeld als bytes.
    Stuurt de camera bij een beeld de lengte mee (Content-Length), dan wordt die gebruikt;
    anders loopt een beeld van het begin van een JPEG tot het einde ervan. Stopt als de
    stroom ophoudt; ValueError bij iets dat geen beeld kan zijn.
    """
    buffer = b""
    while True:
        stuk = stroom.read(blok)
        if not stuk:
            return
        buffer += stuk
        while True:
            begin = buffer.find(_SOI)
            if begin < 0:
                # Nog geen begin van een beeld; alleen het staartje bewaren (een begin
                # kan over twee stukken verdeeld zijn, en de kop met de lengte gaat vooraf).
                buffer = buffer[-512:]
                break
            # De kop van dit beeld staat vlak vóór het begin.
            lengte = _LENGTE.search(buffer[max(0, begin - 512):begin])
            if lengte:
                einde = begin + int(lengte.group(1))
                if int(lengte.group(1)) > MAX_BEELD:
                    raise ValueError("beeld groter dan toegestaan")
                if len(buffer) < einde:
                    break                # de rest van het beeld komt nog
            else:
                einde = buffer.find(_EOI, begin + 2)
                if einde < 0:
                    if len(buffer) - begin > MAX_BEELD:
                        raise ValueError("geen einde van het beeld gevonden")
                    break
                einde += 2
            yield buffer[begin:einde]
            buffer = buffer[einde:]


def live_stroom(stroom, per_seconde: float, klok=time.monotonic, max_duur: float = MAX_LIVE):
    """Zet de MJPEG-stroom van een camera om in de stroom voor de browser (§5.7 punt 18).

    Geeft hooguit `per_seconde` beelden per seconde door en slaat de rest over: de camera
    levert er meer dan nodig is, en over de mobiele verbinding telt elke kB. Stopt na
    `max_duur` seconden, of als de camera ophoudt. klok: te vervangen in tests.
    """
    begin = klok()
    volgende = begin
    for beeld in beelden(stroom):
        nu = klok()
        if nu - begin >= max_duur:
            return
        if nu < volgende:
            continue                     # te snel na het vorige: overslaan
        # Vaste tussenpozen; na een hapering niet inhalen.
        volgende = max(volgende + 1.0 / per_seconde, nu)
        yield (f"--{LIVE_GRENS}\r\nContent-Type: image/jpeg\r\n"
               f"Content-Length: {len(beeld)}\r\n\r\n").encode() + beeld + b"\r\n"


def _regels(cfg: Config) -> dict:
    """De alarmregels per sensor en grootheid, voor de grafieken (§5.5.3 punt 9):
    {sensor-ID: {grootheid: [{"id", "naam", "boven"|"onder", "herstel"}, ...]}}.
    Een regel zonder grens (soort 'aantal') staat er ook in: zijn alarmperioden worden wel
    gearceerd, maar er is geen lijn te tekenen. Regels van soort 'stil' horen bij geen
    enkele grootheid en staan er niet in."""
    uit: dict = {}
    for r in cfg.alarmen:
        if r.grootheid is None:
            continue
        regel = {"id": r.id, "naam": r.naam}
        for veld in ("boven", "onder", "herstel"):
            if getattr(r, veld) is not None:
                regel[veld] = getattr(r, veld)
        for sensor_id in r.sensoren:
            uit.setdefault(sensor_id, {}).setdefault(r.grootheid, []).append(regel)
    return uit


def _levert(sensor) -> list[str]:
    """De grootheden die deze sensor kan publiceren (zie config.levert). Dit is de keuze
    in de pagina."""
    return list(config.levert(sensor))


def _sensor(cfg: Config, sensor_id) -> "config.Sensor | None":
    """Zoekt een sensor op ID. Een MAC-adres staat in hoofdletters in de configuratie
    (de pagina mag het in kleine letters sturen); een vrij gekozen ID (Shelly) staat er
    zoals het getypt is."""
    sensor_id = str(sensor_id or "")
    return cfg.sensoren.get(sensor_id) or cfg.sensoren.get(sensor_id.upper())


def maak_app(cfg: Config, live: LiveBuffer, bus=None, klok=time.time) -> Flask:
    """Maakt de Flask-toepassing. `bus` is alleen nodig om te melden of de broker
    bereikbaar is; in tests mag hij ontbreken. `klok` is te vervangen in tests."""
    app = Flask(__name__)
    # Flask sorteert JSON standaard op sleutel (alfabetisch); de volgorde van de tabel van
    # grootheden (§5.3.3) moet blijven. Twee instellingen: één voor de antwoorden
    # (jsonify), één voor 'tojson' in de pagina.
    app.json.sort_keys = False
    app.jinja_env.policies["json.dumps_kwargs"] = {"sort_keys": False}

    # Het starttijdstip van deze webservice, als kenmerk. De configuratie (sensoren,
    # alarmregels) wordt alleen bij de start gelezen (§5.4.1) en staat in de pagina; een
    # pagina die al open stond bij een herstart kent de nieuwe regels dus niet. Daarom
    # gaat het kenmerk met elk antwoord mee en laadt de pagina zichzelf opnieuw als het
    # anders is dan bij het laden (§5.6).
    gestart = f"{klok():.3f}"

    @app.after_request
    def _kenmerk(antwoord):
        antwoord.headers[KOP_GESTART] = gestart
        return antwoord

    def _getal(waarde, standaard=None, laagste=None, hoogste=None):
        """Zet een waarde uit een aanvraag om in een geheel getal binnen de grenzen;
        `standaard` als hij ontbreekt of geen getal is."""
        try:
            getal = int(waarde)
        except (TypeError, ValueError):
            return standaard
        if laagste is not None:
            getal = max(laagste, getal)
        if hoogste is not None:
            getal = min(hoogste, getal)
        return getal

    @app.get("/")
    def pagina():
        return render_template(
            "index.html", versie=VERSION, gestart=gestart, kop_gestart=KOP_GESTART,
            sensoren=[{"id": s.id, "naam": s.naam, "type": s.type, "grootheden": _levert(s)}
                      for s in cfg.sensoren.values()],
            regels=_regels(cfg),
            # Naam -> eenheid en soort, voor de assen en de vorm van de lijnen.
            grootheden={g.naam: {"eenheid": g.eenheid, "soort": g.soort}
                        for g in grootheden.GROOTHEDEN.values()})

    @app.post("/api/live")
    def api_live():
        """Metingen van de bus voor een lijst reeksen; één aanvraag voor alle live-grafieken
        op de pagina.

        Aanvraag (JSON):  {"vragen": [{"sensor": ..., "grootheid": ..., en verder
            aantal=N    de laatste N metingen
            venster=S   de laatste S seconden (eerste vraag; de tijd van de Pi telt, niet
                        die van de browser)
            vanaf=T     vanaf tijdstip T (vervolgvragen bij samenvatten; eerder venster)
            tot=T       tot (niet tot en met) tijdstip T: een eerder venster
            overslaan=M de laatste M metingen (vóór 'tot') overslaan: bladeren per aantal
            na=NR       alleen wat nieuwer is dan volgnummer NR (vervolgvragen)
            stap=S      samenvatten per S seconden; 0 of weggelaten = elke meting }, ...]}
        Antwoord:  {"nu": tijd van de Pi, "nr": hoogste volgnummer, "broker": bereikbaar,
                    "punten": per vraag [[ts, waarde], ...],
                    "eerder": per vraag of er in het geheugen nog oudere metingen staan
                              dan de eerste geleverde (dan kan de pagina terugbladeren)}
        Een vraag over een onbekende sensor of grootheid levert een lege lijst.
        """
        inhoud = request.get_json(silent=True)
        lijst = inhoud.get("vragen") if isinstance(inhoud, dict) else None
        if not isinstance(lijst, list) or len(lijst) > MAX_VRAGEN:
            return jsonify(fout=f"'vragen' moet een lijst van hooguit {MAX_VRAGEN} zijn"), 400
        nu = int(klok())
        vragen = []
        for v in lijst:
            v = v if isinstance(v, dict) else {}
            sensor = _sensor(cfg, v.get("sensor"))
            grootheid = v.get("grootheid")
            if sensor is None or grootheid not in grootheden.GROOTHEDEN:
                vragen.append({"sensor_id": "", "grootheid": ""})   # geeft een lege lijst
                continue
            vanaf = _getal(v.get("vanaf"))
            if "venster" in v:
                vanaf = nu - _getal(v["venster"], BEWAAR, 1, BEWAAR)
            vragen.append({"sensor_id": sensor.id, "grootheid": grootheid,
                           "na": _getal(v.get("na"), 0), "vanaf": vanaf,
                           "aantal": _getal(v.get("aantal"), None, 1, MAX_AANTAL),
                           "tot": _getal(v.get("tot")),
                           "overslaan": _getal(v.get("overslaan"), 0, 0, MAX_OVERSLAAN),
                           "stap": _getal(v.get("stap"), 0, 0, MAX_STAP)})
        nr, punten, eerder = live.lees(vragen)
        return jsonify(nu=nu, nr=nr, punten=punten, eerder=eerder,
                       broker=bus.verbonden if bus is not None else None)

    @app.get("/api/historie")
    def api_historie():
        sensor = _sensor(cfg, request.args.get("sensor"))
        grootheid = request.args.get("grootheid", "")
        if sensor is None or grootheid not in grootheden.GROOTHEDEN:
            return jsonify(fout="onbekende sensor of grootheid"), 404
        van, tot = _getal(request.args.get("van"), 0), _getal(request.args.get("tot"), 0)
        if tot <= van:
            return jsonify(fout="'tot' moet later zijn dan 'van'"), 400
        # Nooit meer dan MAX_PERIODE in één keer: het begin schuift dan op.
        van = max(van, tot - MAX_PERIODE)
        return jsonify(van=van, tot=tot,
                       punten=lees_historie(cfg.opslag.database, sensor.id, grootheid, van, tot))

    @app.get("/api/alarmen")
    def api_alarmen():
        sensor = _sensor(cfg, request.args.get("sensor"))
        grootheid = request.args.get("grootheid", "")
        if sensor is None or grootheid not in grootheden.GROOTHEDEN:
            return jsonify(fout="onbekende sensor of grootheid"), 404
        van, tot = _getal(request.args.get("van"), 0), _getal(request.args.get("tot"), 0)
        if tot <= van:
            return jsonify(fout="'tot' moet later zijn dan 'van'"), 400
        return jsonify(perioden=lees_alarmen(cfg.opslag.database, sensor.id, grootheid, van, tot))

    # --- opnames van de camera's (§5.6 punt 2) ----------------------------------------
    @app.get("/opnames")
    def pagina_opnames():
        return render_template(
            "opnames.html", versie=VERSION,
            cameras=[{"id": c.id, "naam": c.naam, "video": c.video_url is not None,
                      "video_duur": c.video_duur, "live": c.live_url is not None}
                     for c in cfg.cameras.values()],
            max_live=MAX_LIVE,
            video_ruimte=cfg.opnames.video_ruimte, foto_bewaren=cfg.opnames.foto_bewaren)

    @app.get("/api/opnames")
    def api_opnames():
        van, tot = _getal(request.args.get("van"), 0), _getal(request.args.get("tot"), 0)
        if tot <= van:
            return jsonify(fout="'tot' moet later zijn dan 'van'"), 400
        camera = request.args.get("camera") or None
        type_ = request.args.get("type") or None
        if camera is not None and camera not in cfg.cameras:
            return jsonify(fout="onbekende camera"), 404
        if type_ not in (None, "foto", "video"):
            return jsonify(fout="'type' moet foto of video zijn"), 400
        # alles=1: het dagoverzicht wil de hele dag, niet alleen wat in de lijst past.
        limiet = MAX_DAG if request.args.get("alles") == "1" else MAX_OPNAMES
        opnames, totalen = lees_opnames(cfg.opslag.database, van, tot, camera, type_,
                                        interval=request.args.get("interval") != "0",
                                        limiet=limiet)
        # 'vol': er zijn er meer dan de pagina in één keer toont.
        return jsonify(opnames=opnames, totalen=totalen, vol=len(opnames) >= limiet)

    @app.get("/opname/<path:bestand>")
    def opname(bestand):
        # Alleen een pad van de vaste vorm; send_from_directory weigert bovendien alles
        # wat buiten de opnamemap uitkomt. conditional: de browser kan een deel van een
        # video opvragen (spoelen) zonder het hele bestand te halen.
        if not _BESTAND.match(bestand):
            abort(404)
        return send_from_directory(cfg.opnames.map, bestand, conditional=True,
                                   max_age=BEWAAR_OPNAME)

    def _opdracht(topic: str, inhoud: dict):
        """Zet een opdracht op de bus (§5.6 punt 3); een foutantwoord als dat niet kan."""
        if bus is None or not bus.publiceer(topic, json.dumps(inhoud), qos=1):
            return jsonify(fout="de MQTT-broker is niet bereikbaar; probeer het opnieuw"), 503
        return None

    @app.post("/api/opnames/verwijder")
    def api_verwijder():
        inhoud = request.get_json(silent=True)
        lijst = inhoud.get("opnames") if isinstance(inhoud, dict) else None
        if (not isinstance(lijst, list) or not lijst or len(lijst) > MAX_OPNAMES
                or not all(isinstance(p, list) and len(p) == 2 and isinstance(p[0], str)
                           and isinstance(p[1], int) and not isinstance(p[1], bool) for p in lijst)):
            return jsonify(fout=f"'opnames' moet een lijst van 1 tot {MAX_OPNAMES} paren "
                                f"[camera_id, ts] zijn"), 400
        # Het web verwijdert niet zelf: de opslag-service is de enige die schrijft (§3.1).
        return _opdracht(TOPIC_VERWIJDER, {"opnames": lijst}) or jsonify(gevraagd=len(lijst))

    @app.post("/api/opnames/nu")
    def api_opname_nu():
        inhoud = request.get_json(silent=True)
        inhoud = inhoud if isinstance(inhoud, dict) else {}
        camera, type_ = cfg.cameras.get(inhoud.get("camera_id")), inhoud.get("type")
        if camera is None or type_ not in ("foto", "video"):
            return jsonify(fout="onbekende camera of type"), 400
        if type_ == "video" and camera.video_url is None:
            return jsonify(fout="deze camera maakt geen video's (geen video_url)"), 400
        return (_opdracht(TOPIC_POLL_NU, {"camera_id": camera.id, "type": type_,
                                          "aanleiding": "handmatig"})
                or jsonify(gevraagd=type_))

    # --- live-beeld (§5.7 punt 18) ----------------------------------------------------
    # Het aantal kijkers op dit moment; elke aanvraag loopt in een eigen thread.
    kijkers = {"aantal": 0}
    kijkers_slot = threading.Lock()

    @app.get("/live/<camera_id>")
    def live_beeld(camera_id):
        """Geeft de MJPEG-stroom van de camera door aan de browser. De browser praat zo
        niet zelf met de camera: het wachtwoord blijft op de Pi."""
        from brms.readers import camera as camerahulp

        camera = cfg.cameras.get(camera_id)
        if camera is None or camera.live_url is None:
            return jsonify(fout="deze camera heeft geen live-beeld (geen live_url)"), 404
        with kijkers_slot:
            if kijkers["aantal"] >= MAX_KIJKERS:
                return jsonify(fout=f"er kijken al {MAX_KIJKERS} mensen live; probeer het later"), 503
            kijkers["aantal"] += 1

        def klaar():
            with kijkers_slot:
                kijkers["aantal"] -= 1

        try:
            stroom = urllib.request.urlopen(camerahulp.vul_in(camera.live_url, camera),
                                            timeout=LIVE_TIMEOUT)
        except (OSError, ValueError) as e:
            klaar()
            # Het adres (met het wachtwoord) hoort niet in het log of in het antwoord.
            log.warning("live-beeld van %s (%s) op %s niet te openen: %s", camera.naam, camera.id,
                        camerahulp.toon(camera.live_url), camerahulp.schoon(str(e), camera))
            return jsonify(fout="de camera is niet bereikbaar"), 502

        def geef():
            try:
                yield from live_stroom(stroom, camera.live_beelden)
            except (OSError, ValueError) as e:
                # De camera viel weg tijdens het kijken; de browser houdt het laatste beeld.
                log.info("live-beeld van %s (%s) gestopt: %s", camera.naam, camera.id,
                         camerahulp.schoon(str(e), camera))
            finally:
                # Ook als de browser de verbinding verbreekt (de gebruiker stopt of verlaat
                # de pagina): de verbinding met de camera sluiten en de plaats vrijgeven.
                stroom.close()
                klaar()

        antwoord = Response(geef(), mimetype=f"multipart/x-mixed-replace; boundary={LIVE_GRENS}")
        antwoord.headers["Cache-Control"] = "no-store"
        # Sluit Flask het antwoord zonder dat de stroom ooit gelezen is (de browser was al
        # weg), dan loopt 'geef' niet en moet het hier worden opgeruimd.
        gestart = {"ja": False}

        def bewaakt():
            gestart["ja"] = True
            yield from geef()

        antwoord.response = bewaakt()

        @antwoord.call_on_close
        def _sluit():
            if not gestart["ja"]:
                stroom.close()
                klaar()
        return antwoord

    return app


def draai(cfg: Config) -> None:
    """Start de webservice en loopt tot het proces gestopt wordt (door systemd of Ctrl-c)."""
    from werkzeug.serving import make_server

    from brms.bus import Bus

    # De webserver meldt standaard elke aanvraag in het log. De live-pagina vraagt elke
    # paar seconden iets op; dat zou het log (op flash, P7) onnodig vullen.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    live = LiveBuffer()
    bus = Bus("web", cfg.mqtt.host, cfg.mqtt.port)
    # QoS 0: voor een grafiek is een gemiste meting geen probleem.
    bus.abonneer(f"{TOPIC_PREFIX}/#", 0, live.bij_bericht)
    bus.start()

    # De ingebouwde server van Flask/Werkzeug, met een thread per aanvraag. Voldoende voor
    # één of enkele gebruikers achter Tailscale; er staat niets open op internet (§5.6).
    server = make_server(cfg.web.host, cfg.web.port, maak_app(cfg, live, bus), threaded=True)
    log.info("webservice gestart op http://%s:%d", cfg.web.host, cfg.web.port)
    try:
        server.serve_forever()
    finally:
        bus.stop()
        log.info("webservice gestopt")
