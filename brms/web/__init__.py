"""Webinterface: Flask + Chart.js, alleen bereikbaar via Tailscale/LAN (ARCHITECTURE.md §5.6).

Vier pagina's met een gedeelde bovenbalk en een zijbalk als menu (sjabloon basis.html,
stijl static/brms.css): Grafieken, Opnames, Alarmen en Systeem (§5.6, v0.52). Staat er
een blok [boot] in brms.toml, dan is er een vijfde, de eerste pagina: Boot, een tekening
van de boot met de actuele waarden op hun plek (§5.6 "Bootpagina", v0.76).

Grafieken (§5.6 punt 4): de gebruiker voegt zelf grafieken toe en kiest per grafiek welke
reeksen (sensor + grootheid) erin staan. Een grafiek heeft één bron:

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
    /                 de bootpagina als er een blok [boot] is, anders de grafiekenpagina
    /grafieken        de grafiekenpagina; met ?sensor=<id>&grootheid=<naam> komt die reeks
                      in de bootgrafiek bovenaan (een klik op de bootpagina)
    /boot/tekening.svg   de SVG-tekening van de bootpagina, zoals hij nu op schijf staat
    /api/boot         (POST) de actuele waarde en de alarmstand van de waarden in de
                      tekening, zie api_boot
    /api/live        (POST) metingen van de bus voor een lijst reeksen, zie api_live
    /api/historie     ?sensor=<id>&grootheid=<naam>&van=<ts>&tot=<ts>  rijen uit de database
    /api/alarmen      ?sensor=<id>&grootheid=<naam>&van=<ts>&tot=<ts>  alarmperioden (§5.5.3)
    /opnames          de pagina met foto's en video's van de camera's (§5.6 punt 2)
    /api/opnames      ?van=<ts>&tot=<ts>[&camera=<id>][&type=foto|video][&interval=0]
                      [&alles=1]  (alles=1: de hele dag, voor het dagoverzicht)
    /opname/<pad>     het bestand van één opname (JPEG of MP4)
    /api/opnames/verwijder   (POST) opnames verwijderen; gaat als opdracht naar de opslag
    /api/opnames/nu          (POST) nu een foto of video laten maken (poll-nu)
    /live/<camera_id>        (POST) het live-beeld van een camera aanvragen (WebRTC via
                             go2rtc, §5.7 punt 18)
    /richt/<camera_id>       (POST) {"richting": "op"|"neer"|"links"|"rechts"}: een camera
                             die kan bewegen één stap richten; opdracht over de bus
                             (§5.7 punt 19)
    /alarmen               de pagina met de alarmregels, hun schakelaars en de historie
    /api/alarmen/overzicht   regels (met aan/uit), actieve en opgetreden alarmen;
                             ?van=&tot= (periode) of ?aantal=&overslaan= (de laatste N)
    /api/alarmen/schakel     (POST) een alarmregel aan- of uitzetten; opdracht over de bus
    /systeem                 de pagina met services, herstarten, logboek en schijfruimte
    /api/systeem             status van de services en de schijfruimte
    /api/logboek             ?fouten=1&service=<naam> en ?van=&tot= (periode) of
                             ?aantal=&overslaan= (de laatste N): regels uit journald (§5.5.5)
    /api/systeem/herstart    (POST) {"wat": "brms"|"pi"}: de services of de Pi herstarten
    /api/status              kort: aantal actieve alarmen, services in orde, broker bereikbaar,
                             en of er gesimuleerd wordt (actief, simulatie of demo)

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

from flask import (Flask, Response, abort, jsonify, render_template, request, send_file,
                   send_from_directory)

from brms import VERSION, alarms, config, go2rtc, grootheden, systeem
from brms.config import Config
from brms.meting import TOPIC_PREFIX, Meting

log = logging.getLogger(__name__)

# Opdrachten die het web over de bus geeft (§5.6 punt 3); zie brms/storage.py en
# brms/readers/camera.py.
TOPIC_VERWIJDER = "brms/opdracht/opslag/verwijder"
TOPIC_POLL_NU = "brms/opdracht/camera/poll-nu"
TOPIC_RICHT = "brms/opdracht/camera/richt"
TOPIC_SCHAKEL = "brms/opdracht/alarm/schakel"
# Hierop melden de readers welke sensoren ze simuleren (§10.3 punt 10); zie
# brms/simulatie.py. Elke reader meldt zich elke 30 s; een melding die ouder is dan
# SIMULATIE_GELDIG seconden telt niet meer (de reader is dan gestopt of herstart).
TOPIC_SIMULATIE = "brms/simulatie"
SIMULATIE_GELDIG = 90.0
# Zoveel opgetreden alarmen toont de alarmenpagina hooguit van één periode (dag, week,
# maand of jaar).
MAX_HISTORIE = 200
# De keuze "laatste 10" van de alarmenpagina (§5.6): zoveel alarmen per blad, en het
# grootste aantal dat een aanvraag in één keer mag vragen.
STANDAARD_AANTAL = 10
MAX_AANTAL = 100
# Zo lang (seconden) geldt een opgevraagde status van de services voor /api/status: elke
# open pagina vraagt die elke 30 s, en elke vraag aan systemd is een apart proces.
STATUS_BEWAREN = 10.0
# Zo lang na het antwoord op "Herstart BRMS" herstart de webservice zichzelf (seconden):
# het antwoord moet eerst de deur uit.
HERSTART_WACHT = 1.0
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
# van de boot niet leegtrekken (het volle beeld is ongeveer 14 MB per minuut). De pagina
# bewaakt deze tijd zelf: de video loopt rechtstreeks tussen de browser en go2rtc, dus de
# webservice kan hem niet afbreken.
MAX_LIVE = 600
# Zoveel kijkers tegelijk per camera. Ze delen één verbinding met de camera, maar elke
# kijker kost data.
MAX_KIJKERS = 3

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

# --- bootpagina (§5.6 "Bootpagina") ---
# Is de laatste meting van een waarde ouder dan dit (seconden), dan geldt ze als stil en
# toont de bootpagina "--" (besluit OPEN-32): 15 minuten, drie opslagintervallen, zodat
# een enkele gemiste meting nog geen "--" geeft.
BOOT_STIL = 15 * 60


class _Reeks:
    """De metingen van één sensor en grootheid, compact opgeslagen in drie rijen getallen
    (24 bytes per meting; een lijst van Python-objecten kost ruim vier keer zoveel)."""

    def __init__(self):
        self.nr = array.array("q")      # volgnummer (oplopend)
        self.ts = array.array("q")      # ontvangsttijd, Unix-seconden
        self.waarde = array.array("d")


class SimulatieStand:
    """Onthoudt wat de readers melden over lopende simulaties (§10.3 punt 10), voor het
    tegeltje bovenaan elke pagina: "Actief", "Simulatie" of "Demo".

    De meldingen komen binnen in paho's netwerkthread en worden gelezen in de threads van
    de webserver; daarom een lock. Alles staat alleen in het geheugen: na een herstart van
    de webservice is de stand binnen 30 s weer bekend (zo vaak meldt elke reader zich).
    """

    def __init__(self, klok=time.monotonic):
        self._klok = klok
        self._lock = threading.Lock()
        # Per reader: (tijdstip van ontvangst op de monotone klok, de gemelde sensoren).
        self._gemeld: dict[str, tuple[float, list[dict]]] = {}

    def bij_bericht(self, topic: str, payload: bytes) -> None:
        """Callback voor de bus (brms/simulatie/#)."""
        try:
            d = json.loads(payload)
            dienst, sensoren = d["dienst"], d["sensoren"]
            if not isinstance(dienst, str) or not isinstance(sensoren, list):
                raise ValueError("dienst of sensoren heeft de verkeerde vorm")
            sensoren = [{"sensor_id": str(s["sensor_id"]), "soort": str(s["soort"]),
                         "einde": s.get("einde")} for s in sensoren]
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            log.debug("ongeldig bericht op %s genegeerd: %s", topic, e)
            return
        with self._lock:
            self._gemeld[dienst] = (self._klok(), sensoren)

    def lees(self) -> list[dict]:
        """De sensoren die nu gesimuleerd worden, van alle readers samen: per sensor
        {"sensor_id", "soort" ("test" of "demo"), "einde"}. Meldingen die te oud zijn
        tellen niet mee."""
        grens = self._klok() - SIMULATIE_GELDIG
        with self._lock:
            return [s for ontvangen, sensoren in self._gemeld.values() if ontvangen >= grens
                    for s in sensoren]


def bedrijf(gesimuleerd: list[dict], cfg: Config) -> dict:
    """Wat het tegeltje bovenaan toont (§5.6): {"stand", "sensoren"}.

    stand:     "simulatie" als er een test loopt (dan kunnen er alarmen afgaan die niet
               echt zijn; dat gaat voor), anders "demo" als er een demo loopt, anders
               "actief": alle sensoren zijn echt.
    sensoren:  de namen van de gesimuleerde sensoren, voor de uitleg bij het tegeltje.
    """
    soorten = {s["soort"] for s in gesimuleerd}
    stand = "simulatie" if "test" in soorten else "demo" if "demo" in soorten else "actief"
    namen = [cfg.sensoren[s["sensor_id"]].naam if s["sensor_id"] in cfg.sensoren
             else s["sensor_id"] for s in gesimuleerd]
    return {"stand": stand, "sensoren": namen}


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


def lees_laatste(pad: str, reeksen: list[tuple[str, str]], vanaf: int) -> dict:
    """De laatste opgeslagen rij per reeks (sensor-ID, grootheid), niet ouder dan `vanaf`:
    {(sensor_id, grootheid): (ts, waarde)}. Een reeks zonder zo'n rij staat er niet in.

    Voor de bootpagina (§5.6 punt 7): na een herstart van de webservice is het
    live-geheugen leeg en toont de pagina de laatste rij uit de database, tot er weer
    metingen over de bus komen. Bestaat de database of de tabel (nog) niet: leeg.
    """
    if not reeksen or not pathlib.Path(pad).exists():
        return {}
    con = sqlite3.connect(pathlib.Path(pad).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        uit = {}
        for sensor_id, grootheid in reeksen:
            # De tabel is gesorteerd op (sensor_id, grootheid, ts): de laatste rij van een
            # reeks is één stap in de index (§5.3.1).
            rij = con.execute(
                "SELECT ts, waarde FROM meting WHERE sensor_id = ? AND grootheid = ? AND ts >= ? "
                "ORDER BY ts DESC LIMIT 1", (sensor_id, grootheid, vanaf)).fetchone()
            if rij is not None:
                uit[(sensor_id, grootheid)] = (rij[0], rij[1])
        return uit
    except sqlite3.OperationalError as e:
        log.debug("laatste waarden niet leesbaar: %s", e)
        return {}
    finally:
        con.close()


def lees_actieve_alarmen(pad: str) -> set:
    """De alarmen die nu actief zijn, als verzameling van (sensor-ID, grootheid). Bij een
    regel van soort 'stil' is de grootheid None. Voor de bootpagina (§5.6 punt 4): een
    waarde met een actief alarm is rood en vet. Bestaat de database of de tabel (nog)
    niet: leeg."""
    if not pathlib.Path(pad).exists():
        return set()
    con = sqlite3.connect(pathlib.Path(pad).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        return set(con.execute("SELECT sensor_id, grootheid FROM alarm WHERE einde IS NULL"))
    except sqlite3.OperationalError as e:
        log.debug("alarmen niet leesbaar: %s", e)
        return set()
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


def lees_alarmoverzicht(pad: str, van: int | None = None, tot: int | None = None,
                        aantal: int = STANDAARD_AANTAL, overslaan: int = 0) -> dict:
    """Leest voor de alarmenpagina (§5.6) uit de database:

      actief   de alarmen die nu actief zijn (zonder einde), de nieuwste eerst. Altijd
               allemaal: dit deel hangt niet af van de gekozen periode;
      alarmen  de opgetreden alarmen van de keuze op de pagina, de nieuwste eerst:
                 - met `van` en `tot`: de alarmen die in die periode begonnen
                   (van <= begin < tot), hooguit MAX_HISTORIE;
                 - zonder: de laatste `aantal` alarmen, na het overslaan van de
                   `overslaan` nieuwste (bladeren: 10 eerder, nog 10 eerder, ...);
      eerder   of er nog oudere alarmen zijn dan de getoonde (voor de knop "eerder");
      vol      of de periode meer alarmen heeft dan er getoond worden;
      uit      de regels die uit staan, als {alarm_id: sinds}.

    Waarom niet de hele tabel: die groeit de hele winter door, en op een telefoon over
    4G is een lijst van honderden regels onbruikbaar (v0.56).

    Bestaat de database of een tabel (nog) niet, dan is dat deel leeg.
    """
    uitkomst = {"actief": [], "alarmen": [], "eerder": False, "vol": False, "uit": {}}
    if not pathlib.Path(pad).exists():
        return uitkomst
    # Alleen-lezen openen: de opslag-service is de enige schrijver (§3.1).
    con = sqlite3.connect(pathlib.Path(pad).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    sleutels = ("alarm_id", "sensor_id", "begin", "grootheid", "waarde", "einde", "reden")
    kies = f"SELECT {', '.join(sleutels)} FROM alarm "

    def rijen(sql: str, waarden: tuple = ()) -> list[dict]:
        return [dict(zip(sleutels, rij)) for rij in con.execute(kies + sql, waarden)]

    try:
        try:
            uitkomst["actief"] = rijen("WHERE einde IS NULL ORDER BY begin DESC, alarm_id, sensor_id")
            if van is not None and tot is not None:
                # Een periode. Eén rij meer vragen dan er getoond wordt: zo is zonder
                # tweede vraag bekend of de periode meer heeft dan het maximum.
                gevonden = rijen("WHERE begin >= ? AND begin < ? ORDER BY begin DESC, alarm_id, sensor_id "
                                 "LIMIT ?", (van, tot, MAX_HISTORIE + 1))
                uitkomst["vol"] = len(gevonden) > MAX_HISTORIE
                uitkomst["alarmen"] = gevonden[:MAX_HISTORIE]
                # "eerder" heeft zin zolang er vóór deze periode nog een alarm begon.
                uitkomst["eerder"] = con.execute(
                    "SELECT 1 FROM alarm WHERE begin < ? LIMIT 1", (van,)).fetchone() is not None
            else:
                # De laatste N. Ook hier één rij extra: is die er, dan bestaat er een
                # volgend (ouder) blad.
                gevonden = rijen("ORDER BY begin DESC, alarm_id, sensor_id LIMIT ? OFFSET ?",
                                 (aantal + 1, overslaan))
                uitkomst["eerder"] = len(gevonden) > aantal
                uitkomst["alarmen"] = gevonden[:aantal]
        except sqlite3.OperationalError as e:
            log.debug("alarmen niet leesbaar: %s", e)
        try:
            uitkomst["uit"] = dict(con.execute("SELECT alarm_id, sinds FROM alarm_uit"))
        except sqlite3.OperationalError as e:
            log.debug("schakelaars niet leesbaar: %s", e)
    finally:
        con.close()
    return uitkomst


def tel_actieve_alarmen(pad: str) -> int | None:
    """Het aantal alarmen dat nu actief is (voor de statusstip), of None als de database
    niet te lezen is."""
    if not pathlib.Path(pad).exists():
        return None
    con = sqlite3.connect(pathlib.Path(pad).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        return con.execute("SELECT COUNT(*) FROM alarm WHERE einde IS NULL").fetchone()[0]
    except sqlite3.OperationalError:
        return None
    finally:
        con.close()


def _bronnaam(cfg: Config, bron_id: str) -> str:
    """De naam van een sensor of camera bij zijn ID (het ID zelf als hij niet meer in de
    configuratie staat)."""
    bron = cfg.sensoren.get(bron_id) or cfg.cameras.get(bron_id)
    return bron.naam if bron is not None else bron_id


def _regeloverzicht(cfg: Config, uit: dict) -> list[dict]:
    """De alarmregels voor de alarmenpagina, in de volgorde van brms.toml: per regel de
    voorwaarde in woorden, de sensoren bij naam, de melding en of de regel aan staat."""
    regels = []
    for r in cfg.alarmen:
        voorwaarde = alarms.voorwaardetekst(r)
        if r.grootheid is not None:
            voorwaarde = f"{r.grootheid} {voorwaarde}"
        elif all(s in cfg.cameras for s in r.sensoren):
            # Een regel 'stil' met alleen camera's: een camera levert opnames, geen waarden.
            voorwaarde = voorwaarde.replace("geen waarden", "geen opnames")
        if r.duur and r.soort != config.STIL:
            voorwaarde += f", {alarms.duurtekst(r.duur)} lang"
        melding = {"uit": "geen melding", "eenmalig": "één melding"}.get(
            r.melding, f"melding, herhaald elke {alarms.duurtekst(r.herhaling or 0)}")
        regels.append({"id": r.id, "naam": r.naam, "voorwaarde": voorwaarde,
                       "sensoren": [_bronnaam(cfg, s) for s in r.sensoren],
                       "melding": melding, "prioriteit": r.prioriteit,
                       "aan": r.id not in uit, "uit_sinds": uit.get(r.id)})
    return regels


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


def maak_app(cfg: Config, live: LiveBuffer, bus=None, klok=time.time,
             config_pad: str | None = None, simulatie: SimulatieStand | None = None) -> Flask:
    """Maakt de Flask-toepassing. `bus` is nodig voor opdrachten en om te melden of de
    broker bereikbaar is; in tests mag hij ontbreken. `klok` is te vervangen in tests.
    `config_pad` is het pad van brms.toml: "Herstart BRMS" controleert dat bestand eerst.
    `simulatie` onthoudt wat de readers over simulaties melden; zonder: altijd "actief"."""
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

    @app.context_processor
    def _voor_elke_pagina():
        # Wat het gedeelde sjabloon (basis.html) op elke pagina nodig heeft. `boot`: of
        # er een bootpagina is; dan staat "Boot" bovenaan in het menu en wijst
        # "Grafieken" naar /grafieken in plaats van naar / (§5.6 "Bootpagina" punt 1).
        return {"versie": VERSION, "boot": cfg.boot is not None}

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

    def _sensoren_en_grootheden() -> dict:
        """Wat de grafiekenpagina en de bootpagina uit de configuratie nodig hebben."""
        return {
            "sensoren": [{"id": s.id, "naam": s.naam, "type": s.type, "grootheden": _levert(s)}
                         for s in cfg.sensoren.values()],
            # Naam -> eenheid en soort, voor de assen en de vorm van de lijnen.
            # `decimalen`: met hoeveel decimalen de pagina een waarde toont (§5.3.3).
            # `zijden`: alleen bij een grootheid die met een kant getoond wordt in plaats
            # van een teken, bijv. ["sb", "bb"] bij de slagzij (§5.2.5 punt 10); anders null.
            "grootheden": {g.naam: {"eenheid": g.eenheid, "soort": g.soort,
                                    "decimalen": g.decimalen,
                                    "zijden": grootheden.ZIJDEN.get(g.naam)}
                           for g in grootheden.GROOTHEDEN.values()}}

    def _pagina_grafieken():
        return render_template(
            "index.html", pagina="grafieken", titel="Grafieken",
            gestart=gestart, kop_gestart=KOP_GESTART, regels=_regels(cfg),
            **_sensoren_en_grootheden())

    @app.get("/")
    def pagina():
        # De eerste pagina (§5.6 "Bootpagina" punt 1): de bootpagina als er een blok
        # [boot] in brms.toml staat, anders de grafiekenpagina, zoals vóór v0.26.0.
        if cfg.boot is None:
            return _pagina_grafieken()
        return render_template(
            "boot.html", pagina="boot", titel="Boot",
            gestart=gestart, kop_gestart=KOP_GESTART,
            # Alleen de bestandsnaam, voor de melding als de tekening ontbreekt.
            tekening=pathlib.Path(cfg.boot.tekening).name,
            # Voor de uitleg bij een "--": na hoeveel seconden een waarde als stil geldt.
            stil_na=BOOT_STIL,
            **_sensoren_en_grootheden())

    @app.get("/grafieken")
    def pagina_grafieken():
        # Werkt met en zonder bootpagina; de bootpagina verwijst hierheen bij een klik op
        # een waarde (?sensor=<id>&grootheid=<naam>, door de pagina zelf verwerkt).
        return _pagina_grafieken()

    # --- bootpagina (§5.6 "Bootpagina") -------------------------------------------------
    @app.get("/boot/tekening.svg")
    def boot_tekening():
        """De tekening van de boot, zoals hij nu op schijf staat (§5.6 punt 2).

        Het bestand wordt bij elke aanvraag van schijf gelezen: een nieuwe versie is te
        zien na het verversen van de pagina, zonder herstart. max_age=0 met conditional:
        de browser bewaart het bestand, maar vraagt elke keer of het gewijzigd is; is dat
        niet zo, dan is het antwoord een korte "304" en gaat de tekening (enkele honderden
        kB, de boot zit erin) niet opnieuw over de mobiele verbinding.
        """
        if cfg.boot is None or not pathlib.Path(cfg.boot.tekening).is_file():
            abort(404)
        return send_file(cfg.boot.tekening, mimetype="image/svg+xml", conditional=True,
                         max_age=0)

    @app.post("/api/boot")
    def api_boot():
        """De actuele waarden voor de bootpagina; één aanvraag voor alle waarden in de
        tekening (§5.6 punt 7).

        Aanvraag (JSON):  {"waarden": [{"sensor": <id>, "grootheid": <naam>}, ...]}
        Antwoord:  {"nu": tijd van de Pi, "broker": bereikbaar,
                    "waarden": per gevraagde waarde
                       {"waarde": het getal, of null als de sensor stil is of ontbreekt,
                        "ts":     tijdstip van die meting (null zonder waarde),
                        "alarm":  true als er een alarm actief is op deze sensor en deze
                                  grootheid, of – zonder waarde – op het stilvallen van
                                  deze sensor}}

        De waarde komt uit het live-geheugen (de laatste meting van de bus); is dat leeg
        (kort na een herstart van de webservice), dan uit de laatste rij in de database.
        Een meting ouder dan BOOT_STIL telt niet: de sensor geldt dan als stil. Dat geldt
        ook, en direct, zolang er een alarm "sensor stil" actief is voor de sensor.
        Een onbekende sensor of grootheid geeft een waarde null zonder alarm.
        """
        inhoud = request.get_json(silent=True)
        lijst = inhoud.get("waarden") if isinstance(inhoud, dict) else None
        if not isinstance(lijst, list) or len(lijst) > MAX_VRAGEN:
            return jsonify(fout=f"'waarden' moet een lijst van hooguit {MAX_VRAGEN} zijn"), 400
        nu = int(klok())
        grens = nu - BOOT_STIL
        # Per gevraagde waarde de reeks (sensor-ID, grootheid), of None als hij niet bestaat.
        reeksen = []
        for v in lijst:
            v = v if isinstance(v, dict) else {}
            sensor = _sensor(cfg, v.get("sensor"))
            grootheid = v.get("grootheid")
            bestaat = sensor is not None and grootheid in grootheden.GROOTHEDEN
            reeksen.append((sensor.id, grootheid) if bestaat else None)
        # De laatste meting van de bus, voor alle reeksen in één keer.
        _, punten, _ = live.lees([
            {"sensor_id": r[0], "grootheid": r[1], "aantal": 1} if r else
            {"sensor_id": "", "grootheid": ""} for r in reeksen])
        laatste = {r: tuple(p[-1]) for r, p in zip(reeksen, punten) if r and p}
        # Wat de bus (nog) niet heeft, uit de database.
        ontbreekt = [r for r in dict.fromkeys(reeksen) if r and r not in laatste]
        laatste.update(lees_laatste(cfg.opslag.database, ontbreekt, grens))
        actief = lees_actieve_alarmen(cfg.opslag.database)
        waarden = []
        for r in reeksen:
            ts, waarde = laatste.get(r, (None, None))
            # Een actief alarm "sensor stil" voor deze sensor (grootheid None in de tabel).
            stil_alarm = r is not None and (r[0], None) in actief
            if ts is None or ts < grens or stil_alarm:
                # Stil of ontbrekend: geen waarde. Rood alleen als het alarm "sensor stil"
                # actief is. Dat alarm maakt de sensor ook direct stil (v0.83): de regel
                # kan een kortere duur hebben dan BOOT_STIL, en de laatste waarde van een
                # sensor die als stil gemeld is, hoort er niet meer als actueel te staan.
                waarden.append({"waarde": None, "ts": None, "alarm": stil_alarm})
            else:
                waarden.append({"waarde": waarde, "ts": ts, "alarm": r in actief})
        return jsonify(nu=nu, waarden=waarden,
                       broker=bus.verbonden if bus is not None else None)

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
            "opnames.html", pagina="opnames", titel="Opnames",
            cameras=[{"id": c.id, "naam": c.naam, "video": c.video_url is not None,
                      "video_duur": c.video_duur,
                      # Live-beeld heeft elke camera met een videostroom (§5.7 punt 18).
                      "live": c.video_url is not None,
                      # Richtknoppen alleen bij een camera die kan bewegen (§5.7 punt 19).
                      # De pagina krijgt niet het merk, alleen of het kan, en de duur van
                      # een stap: zo lang zijn de knoppen na een klik uitgeschakeld.
                      "richten": c.richten is not None, "richt_stap": c.richt_stap}
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
    @app.post("/live/<camera_id>")
    def live_beeld(camera_id):
        """Vraagt het live-beeld van een camera aan.

        De pagina stuurt haar WebRTC-aanbod (SDP, tekst); de webservice geeft dat door aan
        go2rtc op de Pi en stuurt het antwoord terug. Daarna loopt de video rechtstreeks
        tussen de browser en go2rtc (poort 8555). De browser praat zo nooit met de camera
        en niet met het beheer van go2rtc: het wachtwoord van de camera blijft op de Pi.
        """
        camera = cfg.cameras.get(camera_id)
        if camera is None or camera.video_url is None:
            return jsonify(fout="deze camera heeft geen live-beeld (geen video_url)"), 404
        aanbod = request.get_data(cache=False, as_text=True)
        # Een aanbod is enkele kB tekst en begint altijd met "v=0".
        if not aanbod.startswith("v=0") or len(aanbod) > go2rtc.MAX_SDP:
            return jsonify(fout="de aanvraag is geen WebRTC-aanbod"), 400
        try:
            if go2rtc.kijkers(camera.id) >= MAX_KIJKERS:
                return jsonify(fout=f"er kijken al {MAX_KIJKERS} mensen live; probeer het later"), 503
            antwoord = go2rtc.webrtc(camera.id, aanbod)
        except go2rtc.Go2rtcFout as e:
            log.warning("live-beeld van %s (%s) niet gestart: %s", camera.naam, camera.id, e)
            return jsonify(fout=str(e)), 502
        return Response(antwoord, mimetype="application/sdp",
                        headers={"Cache-Control": "no-store"})

    # --- richten van de camera (§5.7 punt 19) -----------------------------------------
    @app.post("/richt/<camera_id>")
    def richt(camera_id):
        """Eén klik op een richtknop in het live-beeld: de camera één stap bewegen.

        De webservice praat niet zelf met de camera: de opdracht gaat over de bus naar de
        camera-reader, die het commando stuurt en de camera na de stap weer stopt. Het
        wachtwoord van de camera blijft zo bij de camera-reader. Het antwoord zegt alleen
        dat de opdracht verstuurd is; of de camera beweegt, laat het live-beeld zien.
        """
        camera = cfg.cameras.get(camera_id)
        if camera is None or camera.richten is None:
            return jsonify(fout="deze camera kan niet gericht worden (geen 'richten' in brms.toml)"), 404
        inhoud = request.get_json(silent=True)
        richting = inhoud.get("richting") if isinstance(inhoud, dict) else None
        if richting not in config.RICHTINGEN:
            return jsonify(fout=f"'richting' moet een van {', '.join(config.RICHTINGEN)} zijn"), 400
        return (_opdracht(TOPIC_RICHT, {"camera_id": camera.id, "richting": richting})
                or jsonify(gevraagd=richting))

    # --- alarmen (§5.6 alarmenpagina, §5.5.3 punt 7) -----------------------------------
    @app.get("/alarmen")
    def pagina_alarmen():
        return render_template("alarmen.html", pagina="alarmen", titel="Alarmen")

    @app.get("/api/alarmen/overzicht")
    def api_alarmoverzicht():
        """De regels (met de stand van hun schakelaar), de alarmen die nu actief zijn en
        de opgetreden alarmen van de keuze op de pagina (v0.56), de nieuwste eerst.

        De keuze staat in de aanvraag:
          ?van=<ts>&tot=<ts>          een periode (vandaag, deze week, ...). De pagina
                                      rekent de grenzen uit: een dag of week begint in de
                                      tijdzone van de kijker, en die kent alleen de browser;
          ?aantal=<n>&overslaan=<m>   de laatste n alarmen, na de m nieuwste (bladeren).
        Zonder iets: de laatste 10.
        """
        van = _getal(request.args.get("van"), laagste=0)
        tot = _getal(request.args.get("tot"), laagste=0)
        if van is None or tot is None or tot <= van:
            van = tot = None        # geen (geldige) periode: de laatste N
        d = lees_alarmoverzicht(
            cfg.opslag.database, van, tot,
            aantal=_getal(request.args.get("aantal"), STANDAARD_AANTAL, 1, MAX_AANTAL),
            overslaan=_getal(request.args.get("overslaan"), 0, 0))
        namen = {r.id: r.naam for r in cfg.alarmen}
        # Een actief alarm kan in beide lijsten staan; elke rij krijgt zijn namen.
        for a in d["actief"] + d["alarmen"]:
            a["naam"] = namen.get(a["alarm_id"], a["alarm_id"])
            a["sensor"] = _bronnaam(cfg, a["sensor_id"])
            # De waarde waarmee het alarm afging, met eenheid.
            a["waardetekst"] = (alarms.waardetekst(a["grootheid"], a["waarde"])
                                if a["waarde"] is not None
                                and a["grootheid"] in grootheden.GROOTHEDEN else None)
        return jsonify(nu=int(klok()), regels=_regeloverzicht(cfg, d["uit"]),
                       actief=d["actief"], alarmen=d["alarmen"], eerder=d["eerder"],
                       vol=d["vol"], broker=bus.verbonden if bus is not None else None)

    @app.post("/api/alarmen/schakel")
    def api_schakel():
        inhoud = request.get_json(silent=True)
        inhoud = inhoud if isinstance(inhoud, dict) else {}
        alarm_id, aan = inhoud.get("alarm_id"), inhoud.get("aan")
        if alarm_id not in {r.id for r in cfg.alarmen} or not isinstance(aan, bool):
            return jsonify(fout="onbekende alarmregel, of 'aan' is geen true/false"), 400
        # Het web schrijft niet zelf: de opslag legt de stand vast en de alarmlaag voert
        # hem uit (§5.5.3 punt 7).
        log.warning("alarmregel %s %s gezet via het web", alarm_id, "aan" if aan else "uit")
        return (_opdracht(TOPIC_SCHAKEL, {"alarm_id": alarm_id, "aan": aan})
                or jsonify(gevraagd=alarm_id, aan=aan))

    # --- systeem (§5.6 systeempagina, §5.5.5) -------------------------------------------
    # De laatst opgevraagde status van de services, voor /api/status (zie STATUS_BEWAREN).
    status_slot = threading.Lock()
    status_bewaard = {"tot": 0.0, "lijst": [], "uitleg": None}

    def _services(vers: bool = False):
        with status_slot:
            if vers or time.monotonic() >= status_bewaard["tot"]:
                status_bewaard["lijst"], status_bewaard["uitleg"] = systeem.services()
                status_bewaard["tot"] = time.monotonic() + STATUS_BEWAREN
            return status_bewaard["lijst"], status_bewaard["uitleg"]

    @app.get("/systeem")
    def pagina_systeem():
        return render_template("systeem.html", pagina="systeem", titel="Systeem",
                               gestart=gestart, kop_gestart=KOP_GESTART)

    @app.get("/api/status")
    def api_status():
        """Kort, voor de statusstip en de teller in het menu op elke pagina (§5.6):
        hoeveel alarmen er actief zijn, of alle services draaien (None = onbekend, bijv.
        op de ontwikkel-pc) en of de broker bereikbaar is. En voor het tegeltje ernaast:
        of de sensoren echt zijn ("actief") of gesimuleerd ("simulatie", "demo")."""
        lijst, uitleg = _services()
        return jsonify(alarmen=tel_actieve_alarmen(cfg.opslag.database),
                       services=None if uitleg else all(s["actief"] for s in lijst),
                       broker=bus.verbonden if bus is not None else None,
                       bedrijf=bedrijf(simulatie.lees() if simulatie else [], cfg))

    @app.get("/api/systeem")
    def api_systeem():
        lijst, uitleg = _services(vers=True)
        schijf = systeem.schijf(cfg)
        # De omvang van de opnames komt uit de tabel (de bestanden tellen is traag).
        _, totalen = lees_opnames(cfg.opslag.database, 0, 0, limiet=0)
        schijf["opnames"] = (totalen["foto"][1] or 0) + (totalen["video"][1] or 0)
        return jsonify(nu=int(klok()), services=lijst, uitleg=uitleg, schijf=schijf)

    @app.get("/api/logboek")
    def api_logboek():
        """Het logboek voor de systeempagina (§5.6 systeempagina punt 3, v0.61).

        Filters:   ?fouten=1 (geen waarschuwingen) en ?service=<naam>.
        Welk deel: ?van=<ts>&tot=<ts>          de regels in die periode (Unix-seconden);
                   ?aantal=<n>&overslaan=<m>   de laatste n regels, na de m nieuwste.
        Zonder iets: de laatste 10. De pagina rekent de periode zelf uit (laatste 3, 6, 24
        of 48 uur, en bij het bladeren het venster daarvoor).
        """
        service = request.args.get("service") or None
        van = _getal(request.args.get("van"), laagste=0)
        tot = _getal(request.args.get("tot"), laagste=0)
        # Een halve of omgekeerde periode is geen periode: dan gelden aantal en overslaan.
        if van is None or tot is None or tot <= van:
            van = tot = None
        regels, eerder, vol, uitleg = systeem.logboek(
            alleen_fouten=request.args.get("fouten") == "1", service=service,
            aantal=_getal(request.args.get("aantal"), systeem.STANDAARD_AANTAL, 1, systeem.PER_PAGINA),
            overslaan=_getal(request.args.get("overslaan"), 0, 0), van=van, tot=tot)
        # De services voor de keuzelijst: alle van BRMS, Mosquitto en de kernel.
        namen = systeem.brms_services() + list(systeem.ANDERE_EENHEDEN) + [systeem.KERNEL]
        return jsonify(regels=regels, eerder=eerder, vol=vol, uitleg=uitleg, services=namen)

    @app.post("/api/systeem/herstart")
    def api_herstart():
        """Herstart alle BRMS-services, of de hele Pi (§5.6 systeempagina punt 2). De
        pagina vraagt eerst om bevestiging; hier wordt niets meer gevraagd."""
        inhoud = request.get_json(silent=True)
        wat = inhoud.get("wat") if isinstance(inhoud, dict) else None
        if wat == "brms":
            # Eerst de configuratie controleren, zoals `brms herstart`: met een fout erin
            # zouden de services na de herstart niet meer starten.
            try:
                config.lees(config_pad or config.STANDAARD_PAD)
            except config.ConfigFout as e:
                return jsonify(fout="De configuratie bevat fouten; er is niets herstart.",
                               fouten=e.fouten), 409
            # Een waarschuwing, zodat de herstart in het logboek terug te vinden is.
            log.warning("herstart van alle BRMS-services gevraagd via het web")
            fout = systeem.herstart_services(behalve="brms-web")
            if fout:
                return jsonify(fout=f"Herstarten is niet gelukt: {fout}"), 500
            # De webservice zelf als laatste, kort na dit antwoord.
            threading.Timer(HERSTART_WACHT, systeem.herstart_service, ("brms-web",)).start()
            return jsonify(gevraagd="brms")
        if wat == "pi":
            log.warning("herstart van de Pi gevraagd via het web")
            fout = systeem.herstart_pi()
            if fout:
                return jsonify(fout=f"Herstarten is niet gelukt: {fout}"), 500
            return jsonify(gevraagd="pi")
        return jsonify(fout="'wat' moet \"brms\" of \"pi\" zijn"), 400

    return app


def draai(cfg: Config, config_pad: str | None = None) -> None:
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
    # Wat de readers melden over simulaties, voor het tegeltje bovenaan (§10.3 punt 10).
    simulatie = SimulatieStand()
    bus.abonneer(f"{TOPIC_SIMULATIE}/#", 1, simulatie.bij_bericht)
    bus.start()

    # De ingebouwde server van Flask/Werkzeug, met een thread per aanvraag. Voldoende voor
    # één of enkele gebruikers achter Tailscale; er staat niets open op internet (§5.6).
    server = make_server(cfg.web.host, cfg.web.port,
                         maak_app(cfg, live, bus, config_pad=config_pad,
                                  simulatie=simulatie), threaded=True)
    log.info("webservice gestart op http://%s:%d", cfg.web.host, cfg.web.port)
    try:
        server.serve_forever()
    finally:
        bus.stop()
        log.info("webservice gestopt")
