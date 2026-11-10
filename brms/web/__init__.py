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

Er is geen aanmelding: de pagina is alleen via Tailscale of het lokale netwerk bereikbaar
(OPEN-15).
"""

import array
import bisect
import logging
import pathlib
import sqlite3
import threading
import time

from flask import Flask, jsonify, render_template, request

from brms import VERSION, config, grootheden
from brms.config import Config
from brms.meting import TOPIC_PREFIX, Meting

log = logging.getLogger(__name__)

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


def _levert(sensor) -> list[str]:
    """De grootheden die deze sensor kan publiceren: wat zijn type levert (§5.4.2 punt 3),
    beperkt tot de lijst 'grootheden' uit de configuratie als die er is. Dit is de keuze
    in de pagina."""
    levert = config.LEVERT.get(sensor.type, ())
    return [g for g in levert if sensor.grootheden is None or g in sensor.grootheden]


def maak_app(cfg: Config, live: LiveBuffer, bus=None, klok=time.time) -> Flask:
    """Maakt de Flask-toepassing. `bus` is alleen nodig om te melden of de broker
    bereikbaar is; in tests mag hij ontbreken. `klok` is te vervangen in tests."""
    app = Flask(__name__)
    # Flask sorteert JSON standaard op sleutel (alfabetisch); de volgorde van de tabel van
    # grootheden (§5.3.3) moet blijven. Twee instellingen: één voor de antwoorden
    # (jsonify), één voor 'tojson' in de pagina.
    app.json.sort_keys = False
    app.jinja_env.policies["json.dumps_kwargs"] = {"sort_keys": False}

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
            "index.html", versie=VERSION,
            sensoren=[{"id": s.id, "naam": s.naam, "type": s.type, "grootheden": _levert(s)}
                      for s in cfg.sensoren.values()],
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
            sensor = cfg.sensoren.get(str(v.get("sensor", "")).upper())
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
        sensor = cfg.sensoren.get(request.args.get("sensor", "").upper())
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
