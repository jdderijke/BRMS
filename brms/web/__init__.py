"""Webinterface: Flask + Chart.js, alleen bereikbaar via Tailscale/LAN (ARCHITECTURE.md §5.6).

Nu één testpagina (§5.6 punt 4): een sensor kiezen en de waarden als grafiek zien, uit één
van twee bronnen:

- **live**      de waarden zoals ze nu over de MQTT-bus gaan (elke ontvangen meting);
- **database**  wat de opslag-service heeft weggeschreven (per 5 minuten), ook historisch.

Opbouw:
- De webservice abonneert zich op brms/meting/# en bewaart de laatste metingen per sensor
  en grootheid in het geheugen (LiveBuffer). De pagina vraagt elke paar seconden wat er
  nieuw is. Niets hiervan wordt opgeslagen: na een herstart begint het geheugen leeg.
- De database wordt alleen gelezen, met een alleen-lezen verbinding per aanvraag. De
  opslag-service blijft de enige schrijver (§3.1).
- Chart.js staat als bestand in static/ (geen CDN): de pagina werkt ook als de boot geen
  internet heeft en je via het lokale netwerk kijkt.

Adressen:
    /                 de testpagina
    /api/live         ?sensor=<id>&na=<volgnummer>   nieuwe metingen van de bus
    /api/historie     ?sensor=<id>&van=<ts>&tot=<ts> rijen uit de database

Er is geen aanmelding: de pagina is alleen via Tailscale of het lokale netwerk bereikbaar
(OPEN-15).
"""

import collections
import logging
import pathlib
import sqlite3
import threading

from flask import Flask, jsonify, render_template, request

from brms import VERSION, grootheden
from brms.config import Config
from brms.meting import TOPIC_PREFIX, Meting

log = logging.getLogger(__name__)

# Aantal metingen dat per sensor en grootheid in het geheugen blijft voor de live-grafiek.
# Een Xiaomi zendt ongeveer elke 2,5 s: 600 punten is dan ruim 20 minuten.
LIVE_PUNTEN = 600

# Langste periode die in één keer uit de database wordt gelezen (in seconden). 31 dagen is
# bijna 9000 rijen per grootheid; meer wordt traag in de browser.
MAX_PERIODE = 31 * 24 * 3600


class LiveBuffer:
    """De laatste metingen van de bus, per sensor en grootheid.

    Elke meting krijgt een oplopend volgnummer. De pagina onthoudt het laatste volgnummer
    dat ze gezien heeft en vraagt daarna alleen wat nieuwer is; zo komt er niets dubbel en
    wordt er niets gemist, ook als twee metingen in dezelfde seconde vallen.

    Metingen komen binnen in paho's netwerkthread en worden gelezen in de threads van de
    webserver; daarom een lock.
    """

    def __init__(self, punten: int = LIVE_PUNTEN):
        self._punten = punten
        self._lock = threading.Lock()
        self._nr = 0
        # (sensor-ID, grootheid) -> rij van (volgnummer, ts, waarde); de oudste valt eraf.
        self._reeksen: dict[tuple[str, str], collections.deque] = {}

    def bij_bericht(self, topic: str, payload: bytes) -> None:
        """Callback voor de bus (brms/meting/#)."""
        try:
            self.voeg_toe(Meting.van_json(payload))
        except ValueError as e:
            log.debug("ongeldig bericht op %s genegeerd: %s", topic, e)

    def voeg_toe(self, m: Meting) -> None:
        with self._lock:
            self._nr += 1
            reeks = self._reeksen.setdefault((m.sensor_id, m.grootheid),
                                             collections.deque(maxlen=self._punten))
            reeks.append((self._nr, m.ts, m.waarde))

    def sinds(self, sensor_id: str, na: int = 0) -> tuple[int, dict[str, list]]:
        """De metingen van deze sensor met een volgnummer groter dan `na`.

        Geeft (hoogste volgnummer tot nu toe, {grootheid: [[ts, waarde], ...]}).
        """
        with self._lock:
            reeksen = {}
            for (sid, g), reeks in self._reeksen.items():
                if sid != sensor_id:
                    continue
                punten = [[ts, w] for nr, ts, w in reeks if nr > na]
                if punten:
                    reeksen[g] = punten
            return self._nr, reeksen


def lees_historie(pad: str, sensor_id: str, van: int, tot: int) -> dict[str, list]:
    """Leest de rijen van één sensor uit de database: {grootheid: [[ts, waarde], ...]},
    oplopend in de tijd, voor van <= ts <= tot.

    Bestaat de database (nog) niet, dan is het resultaat leeg.
    """
    if not pathlib.Path(pad).exists():
        return {}
    # mode=ro: alleen lezen; deze verbinding kan de database niet wijzigen (§3.1: de
    # opslag-service is de enige schrijver). as_uri() maakt er ook op Windows een geldig
    # adres van.
    con = sqlite3.connect(pathlib.Path(pad).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        reeksen = {}
        # Eén leesactie per grootheid: de tabel is gesorteerd op (sensor_id, grootheid, ts),
        # dus elke reeks is één aaneengesloten stuk (§5.3.1).
        for g in grootheden.GROOTHEDEN:
            rijen = con.execute(
                "SELECT ts, waarde FROM meting "
                "WHERE sensor_id = ? AND grootheid = ? AND ts >= ? AND ts <= ? ORDER BY ts",
                (sensor_id, g, van, tot)).fetchall()
            if rijen:
                reeksen[g] = [[ts, w] for ts, w in rijen]
        return reeksen
    except sqlite3.OperationalError as e:
        # Bijv. de tabel bestaat nog niet (de opslag heeft nog nooit gedraaid).
        log.warning("database niet leesbaar: %s", e)
        return {}
    finally:
        con.close()


def maak_app(cfg: Config, live: LiveBuffer, bus=None) -> Flask:
    """Maakt de Flask-toepassing. `bus` is alleen nodig om te melden of de broker
    bereikbaar is; in tests mag hij ontbreken."""
    app = Flask(__name__)
    # Flask sorteert JSON standaard op sleutel (alfabetisch). De pagina toont de grafieken
    # in de volgorde van de tabel van grootheden (§5.3.3); die volgorde moet dus blijven.
    # Twee instellingen: één voor de antwoorden (jsonify), één voor 'tojson' in de pagina.
    app.json.sort_keys = False
    app.jinja_env.policies["json.dumps_kwargs"] = {"sort_keys": False}

    def _sensor():
        """De sensor uit de aanvraag (?sensor=<id>), of None als die niet bestaat."""
        return cfg.sensoren.get(request.args.get("sensor", "").upper())

    def _getal(naam: str, standaard: int) -> int:
        try:
            return int(request.args.get(naam, standaard))
        except ValueError:
            return standaard

    @app.get("/")
    def pagina():
        return render_template(
            "index.html", versie=VERSION,
            sensoren=[{"id": s.id, "naam": s.naam, "type": s.type}
                      for s in cfg.sensoren.values()],
            # Naam -> eenheid en soort, voor de titels en de vorm van de grafieken.
            grootheden={g.naam: {"eenheid": g.eenheid, "soort": g.soort}
                        for g in grootheden.GROOTHEDEN.values()})

    @app.get("/api/live")
    def api_live():
        sensor = _sensor()
        if sensor is None:
            return jsonify(fout="onbekende sensor"), 404
        nr, reeksen = live.sinds(sensor.id, _getal("na", 0))
        return jsonify(nr=nr, reeksen=reeksen,
                       broker=bus.verbonden if bus is not None else None)

    @app.get("/api/historie")
    def api_historie():
        sensor = _sensor()
        if sensor is None:
            return jsonify(fout="onbekende sensor"), 404
        van, tot = _getal("van", 0), _getal("tot", 0)
        if tot <= van:
            return jsonify(fout="'tot' moet later zijn dan 'van'"), 400
        # Nooit meer dan MAX_PERIODE in één keer: het begin schuift dan op.
        van = max(van, tot - MAX_PERIODE)
        return jsonify(van=van, tot=tot,
                       reeksen=lees_historie(cfg.opslag.database, sensor.id, van, tot))

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
