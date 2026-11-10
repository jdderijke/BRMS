"""Inlezen en controleren van de configuratie (ARCHITECTURE.md §5.4, §5.4.1).

- Eén TOML-bestand voor alle services: config/brms.toml in de projectmap (op de Pi
  ~/BRMS/config/brms.toml, op de pc in de PyCharm-projectmap). Het staat in .gitignore,
  zodat een code-update (git pull) de configuratie nooit overschrijft (§10.1).
- Gelezen met tomllib uit de standaardbibliotheek (geen extra afhankelijkheid, P3).
- Elke service leest het hele bestand en gebruikt zijn eigen deel.
- Een wijziging wordt doorgevoerd door de service te herstarten.
- Een tikfout mag een service (en daarmee bijv. de alarmen) niet onverwacht laten
  uitvallen. Daarom wordt alles gecontroleerd en worden **alle** fouten tegelijk gemeld;
  `brms check-config` doet dat vóór een herstart.

Opbouw van het bestand (zie config/brms.example.toml):

    [mqtt]      host, port              – de lokale broker
    [opslag]    database                – pad van de SQLite-database; een relatief pad
                                          geldt vanaf de projectmap (bijv. data/brms.db)
    [web]       host, port              – waar de webinterface luistert (§5.6)
    [[sensor]]  id, type, naam, grootheden, correctie – één blok per sensor (§5.4.2);
                bij een Victron-apparaat ook 'sleutel' (§5.2.2);
                bij een Shelly ook 'adres', 'interval', 'bron', 'drempel', 'omgekeerd'
                (§5.2.4)
"""

import os
import re
import tomllib
from dataclasses import dataclass, field

from brms import grootheden
from brms.readers import bthome, ruuvi, shelly_uni, victron

# Projectmap: de map waarin de map 'brms' (dit pakket) staat. Op de Pi ~/BRMS.
# Alle standaardpaden zijn hiervan afgeleid, zodat dezelfde code op de pc en op de Pi werkt.
PROJECTMAP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

STANDAARD_PAD = os.path.join(PROJECTMAP, "config", "brms.toml")

# Sensortypen die via Bluetooth binnenkomen (hun id is een MAC-adres). Victron komt erbij
# zodra die decoder er is.
BLE_TYPES = ("bthome", "ruuvi", *victron.LEVERT)   # victron-bmv, -mppt, -multiplus
# Sensortypen waarvan de gegevens versleuteld zijn; die hebben een 'sleutel' nodig (§5.2.2).
SLEUTEL_TYPES = tuple(victron.LEVERT)
# Sensortypen die de Shelly-reader via het netwerk pollt (§5.2.4). Hun id is een vrij
# gekozen vaste naam, bijv. "shelly-bilge".
SHELLY_TYPES = ("shelly-uni",)
SENSOR_TYPES = BLE_TYPES + SHELLY_TYPES

# Poll-interval van een Shelly in seconden (§5.3.2): de standaard en de grenzen.
STANDAARD_INTERVAL = 30.0
MIN_INTERVAL, MAX_INTERVAL = 1.0, 3600.0

# Welke grootheden elk sensortype kan leveren (§5.4.2 punt 3). De decoder legt dat vast;
# BLE-readers voegen zelf de signaalsterkte (rssi) toe. Gebruikt voor de controle van
# 'grootheden' en 'correctie', en door `brms grootheden <type>`.
LEVERT: dict[str, tuple[str, ...]] = {
    "bthome": bthome.LEVERT + ("rssi",),
    "ruuvi": ruuvi.LEVERT + ("rssi",),
    **{type_: levert + ("rssi",) for type_, levert in victron.LEVERT.items()},
    "shelly-uni": shelly_uni.LEVERT + ("rssi",),   # rssi = signaalsterkte van de wifi
}

# Versleutelingssleutel van een Victron-apparaat: 32 hexadecimale tekens.
_SLEUTEL = re.compile(r"^[0-9a-fA-F]{32}$")

# MAC-adres in de vorm AA:BB:CC:DD:EE:FF (na omzetten naar hoofdletters).
_MAC = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$")

# Vrij gekozen ID van een niet-Bluetooth-sensor (Shelly). Het ID komt in MQTT-topics en in
# de database: alleen letters, cijfers, '-' en '_' (geen '/', '+', '#' of spaties).
_VRIJ_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

# Netwerkadres van een Shelly: IP-adres of hostnaam, eventueel met poort.
_ADRES = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?(:\d{1,5})?$")


class ConfigFout(Exception):
    """De configuratie is ongeldig; `fouten` bevat alle gevonden fouten als tekst."""

    def __init__(self, fouten: list[str]):
        self.fouten = fouten
        super().__init__("\n".join(fouten))


@dataclass(frozen=True)
class MqttConfig:
    host: str = "localhost"
    port: int = 1883


@dataclass(frozen=True)
class OpslagConfig:
    database: str = os.path.join(PROJECTMAP, "data", "brms.db")


@dataclass(frozen=True)
class WebConfig:
    # 0.0.0.0 = alle netwerkinterfaces: het lokale netwerk van de boot én Tailscale. De
    # router laat van buitenaf niets door (CGNAT, geen poorten open), dus bereikbaar is de
    # pagina alleen via Tailscale of aan boord (§5.6 punt 1).
    host: str = "0.0.0.0"
    port: int = 8080


@dataclass(frozen=True)
class Correctie:
    """Lineaire correctie: gecorrigeerd = gemeten × factor + offset (§5.4.2 punt 2)."""
    factor: float = 1.0
    offset: float = 0.0

    def toepassen(self, waarde: float) -> float:
        return round(waarde * self.factor + self.offset, 4)

    def __str__(self) -> str:
        # Leesbaar voor de trace, bijv. "× 1.02 − 1.5" of "− 0.3".
        delen = []
        if self.factor != 1.0:
            delen.append(f"× {self.factor:g}")
        if self.offset != 0.0 or not delen:
            delen.append(f"{'+' if self.offset >= 0 else '−'} {abs(self.offset):g}")
        return " ".join(delen)


@dataclass(frozen=True)
class Sensor:
    # Vaste ID; bij BLE het MAC-adres in hoofdletters met dubbele punten. Deze ID staat in
    # de database, niet de naam: hernoemen laat de historie intact (§5.3.1).
    id: str
    type: str
    # Naam/locatie voor weergave en logregels, bijv. "kajuit".
    naam: str
    # Correctie per doorlopende grootheid (§5.4.2 punt 2), bijv. een sensor die 0,3 °C te
    # hoog meet: temperatuur = Correctie(offset=-0.3).
    correctie: dict[str, Correctie] = field(default_factory=dict)
    # Alleen deze grootheden worden gepubliceerd; None = alles wat de decoder levert
    # (§5.4.2 punt 1).
    grootheden: tuple[str, ...] | None = None
    # Versleutelingssleutel (32 hexadecimale tekens), alleen bij Victron-apparaten (§5.2.2
    # punt 2). Een geheim: komt niet in logregels of traceberichten.
    sleutel: str | None = None
    # --- alleen bij een Shelly (§5.2.4) ---
    # Netwerkadres (IP-adres of hostnaam) waarop de Shelly gepolld wordt.
    adres: str | None = None
    # Seconden tussen twee polls.
    interval: float = STANDAARD_INTERVAL
    # Welke component van de Shelly welke grootheid levert, bijv.
    # {"bilge": "input:0", "bilgepomp": "voltmeter:100"}.
    bron: dict[str, str] = field(default_factory=dict)
    # Spanning in volt vanaf waar een voltmeter-bron 1 geeft (de pomp krijgt spanning).
    drempel: float | None = None
    # Grootheden waarvan 0 en 1 verwisseld worden (een contact dat opent in plaats van sluit).
    omgekeerd: tuple[str, ...] = ()


@dataclass(frozen=True)
class Config:
    mqtt: MqttConfig
    opslag: OpslagConfig
    sensoren: dict[str, Sensor]  # id -> Sensor
    web: WebConfig = WebConfig()


def levert(sensor: Sensor) -> tuple[str, ...]:
    """De grootheden die deze sensor kan publiceren: wat zijn type levert, bij een Shelly
    beperkt tot wat een 'bron' heeft, en verder beperkt tot de lijst 'grootheden' als die
    er is (§5.4.2). Gebruikt door de webpagina voor de keuzelijst."""
    alles = LEVERT.get(sensor.type, ())
    if sensor.type in SHELLY_TYPES:
        alles = tuple(g for g in alles if g in sensor.bron or g == "rssi")
    return tuple(g for g in alles if sensor.grootheden is None or g in sensor.grootheden)


def lees(pad: str = STANDAARD_PAD) -> Config:
    """Leest en controleert het configuratiebestand; ConfigFout bij fouten."""
    try:
        with open(pad, "rb") as f:  # tomllib wil het bestand binair
            data = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigFout([f"configuratiebestand niet gevonden: {pad}"]) from None
    except tomllib.TOMLDecodeError as e:
        # Syntaxfout in de TOML zelf; de melding bevat regel en kolom.
        raise ConfigFout([f"{pad}: geen geldige TOML: {e}"]) from None
    return controleer(data)


def _onbekend(fouten, plek, d, toegestaan):
    """Meldt sleutels die niet bestaan. Vangt tikfouten af als 'naaam' of 'corectie',
    die anders stil genegeerd zouden worden."""
    for k in d:
        if k not in toegestaan:
            fouten.append(f"{plek}: onbekende sleutel '{k}'")


def _tabel(fouten, data, naam) -> dict:
    """Haalt een [tabel] op; ontbreekt hij, dan gelden de standaardwaarden."""
    d = data.get(naam, {})
    if not isinstance(d, dict):
        fouten.append(f"[{naam}] moet een tabel zijn")
        return {}
    return d


def _getal(w) -> bool:
    # bool is in Python een subtype van int; 'true' als getal is altijd een tikfout.
    return isinstance(w, (int, float)) and not isinstance(w, bool)


def _correctie(fouten, plek, w) -> "Correctie | None":
    """Zet een correctie uit de TOML om: een getal (= offset) of een tabel met factor
    en/of offset. Meldt fouten en geeft dan None."""
    if _getal(w):
        return Correctie(offset=float(w))
    if isinstance(w, dict):
        _onbekend(fouten, plek, w, ("factor", "offset"))
        factor, offset = w.get("factor", 1.0), w.get("offset", 0.0)
        if not _getal(factor) or not _getal(offset):
            fouten.append(f"{plek}: factor en offset moeten getallen zijn")
            return None
        if factor == 0:
            fouten.append(f"{plek}: factor 0 maakt elke waarde 0")
            return None
        return Correctie(float(factor), float(offset))
    fouten.append(f"{plek}: moet een getal (offset) of een tabel "
                  f"{{ factor = ..., offset = ... }} zijn")
    return None


def _shelly(fouten, plek, s: dict, lijst_g) -> dict:
    """Controleert de velden van een Shelly (§5.2.4) en geeft ze terug als argumenten voor
    Sensor. lijst_g is de lijst 'grootheden' van de sensor, of None."""
    # --- adres: waar de Shelly te bereiken is ---
    adres = s.get("adres")
    if not isinstance(adres, str) or not _ADRES.match(adres):
        fouten.append(f"{plek}: 'adres' ontbreekt of is geen IP-adres of hostnaam "
                      f"(bijv. \"192.168.1.50\")")
        adres = None

    # --- interval: seconden tussen twee polls ---
    interval = s.get("interval", STANDAARD_INTERVAL)
    if not _getal(interval) or not MIN_INTERVAL <= interval <= MAX_INTERVAL:
        fouten.append(f"{plek}: 'interval' moet een getal tussen {MIN_INTERVAL:g} en "
                      f"{MAX_INTERVAL:g} (seconden) zijn")
        interval = STANDAARD_INTERVAL

    # --- bron: welke component welke grootheid levert ---
    bron_in = s.get("bron")
    bron: dict[str, str] = {}
    if not isinstance(bron_in, dict) or not bron_in:
        fouten.append(f"{plek}: 'bron' ontbreekt; geef per grootheid de component van de Shelly, "
                      f"bijv. {{ bilge = \"input:0\", bilgepomp = \"voltmeter:100\" }}")
        bron_in = {}
    for g, component in bron_in.items():
        if g not in shelly_uni.LEVERT:
            fouten.append(f"{plek}: bron voor '{g}', maar een Shelly levert alleen: "
                          f"{', '.join(shelly_uni.LEVERT)}")
        elif not isinstance(component, str) or not shelly_uni.BRON.match(component):
            fouten.append(f"{plek}: bron voor '{g}' moet \"input:<nummer>\" of "
                          f"\"voltmeter:<nummer>\" zijn, niet {component!r}")
        else:
            bron[g] = component
    if len(set(bron.values())) != len(bron):
        fouten.append(f"{plek}: 'bron' gebruikt dezelfde component voor meer dan één grootheid")
    # Een grootheid in de lijst 'grootheden' moet ook ergens vandaan komen.
    for g in lijst_g or ():
        if g in shelly_uni.LEVERT and g not in bron_in:
            fouten.append(f"{plek}: '{g}' staat in 'grootheden' maar heeft geen 'bron'")

    # --- drempel: nodig zodra een spanningsmeting een 0/1 moet opleveren ---
    drempel = s.get("drempel")
    voltmeter = any(c.startswith("voltmeter:") for c in bron.values())
    if drempel is None:
        if voltmeter:
            fouten.append(f"{plek}: 'drempel' ontbreekt: de spanning (in volt) vanaf waar een "
                          f"voltmeter-bron 1 geeft, bijv. 6.0")
    elif not _getal(drempel) or drempel <= 0:
        fouten.append(f"{plek}: 'drempel' moet een getal groter dan 0 zijn (volt)")
        drempel = None
    elif not voltmeter:
        fouten.append(f"{plek}: 'drempel' heeft alleen betekenis bij een voltmeter-bron")

    # --- omgekeerd: grootheden waarvan 0 en 1 verwisseld worden ---
    omgekeerd = s.get("omgekeerd", [])
    if not isinstance(omgekeerd, list) or not all(isinstance(g, str) for g in omgekeerd):
        fouten.append(f"{plek}: 'omgekeerd' moet een lijst van grootheden zijn, bijv. [\"bilge\"]")
        omgekeerd = []
    for g in omgekeerd:
        if g not in bron_in:
            fouten.append(f"{plek}: '{g}' staat in 'omgekeerd' maar heeft geen 'bron'")

    return {"adres": adres, "interval": float(interval), "bron": bron,
            "drempel": float(drempel) if drempel is not None else None,
            "omgekeerd": tuple(omgekeerd)}


def controleer(data: dict) -> Config:
    """Controleert een ingelezen TOML-structuur en zet die om in een Config.

    Alle fouten worden verzameld en samen gemeld (ConfigFout), zodat in één keer
    zichtbaar is wat er gerepareerd moet worden.
    """
    fouten: list[str] = []
    _onbekend(fouten, "bovenste niveau", data, ("mqtt", "opslag", "web", "sensor"))

    # --- [mqtt] ---
    m = _tabel(fouten, data, "mqtt")
    _onbekend(fouten, "[mqtt]", m, ("host", "port"))
    host = m.get("host", MqttConfig.host)
    port = m.get("port", MqttConfig.port)
    if not isinstance(host, str) or not host:
        fouten.append("[mqtt] host moet een niet-lege tekst zijn")
    # bool is in Python een subtype van int; 'port = true' moet toch een fout zijn.
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        fouten.append("[mqtt] port moet een geheel getal tussen 1 en 65535 zijn")

    # --- [opslag] ---
    o = _tabel(fouten, data, "opslag")
    _onbekend(fouten, "[opslag]", o, ("database",))
    database = o.get("database", OpslagConfig.database)
    if not isinstance(database, str) or not database:
        fouten.append("[opslag] database moet een niet-leeg pad zijn")
    elif not os.path.isabs(database):
        # Relatief pad (bijv. "data/brms.db") geldt vanaf de projectmap, niet vanaf de map
        # waarin de service toevallig gestart wordt.
        # normpath maakt van "data/brms.db" op Windows "data\brms.db".
        database = os.path.normpath(os.path.join(PROJECTMAP, database))

    # --- [web] ---
    w = _tabel(fouten, data, "web")
    _onbekend(fouten, "[web]", w, ("host", "port"))
    web_host = w.get("host", WebConfig.host)
    web_port = w.get("port", WebConfig.port)
    if not isinstance(web_host, str) or not web_host:
        fouten.append("[web] host moet een niet-lege tekst zijn")
    if isinstance(web_port, bool) or not isinstance(web_port, int) or not 1 <= web_port <= 65535:
        fouten.append("[web] port moet een geheel getal tussen 1 en 65535 zijn")

    # --- [[sensor]] (lijst van tabellen) ---
    sensoren: dict[str, Sensor] = {}
    lijst = data.get("sensor", [])
    if not isinstance(lijst, list):
        fouten.append("[[sensor]] moet een lijst van tabellen zijn")
        lijst = []
    for n, s in enumerate(lijst, 1):
        # 'plek' wordt in elke foutmelding gebruikt, zodat duidelijk is welk blok het is.
        plek = f"[[sensor]] nr. {n}"
        if not isinstance(s, dict):
            fouten.append(f"{plek}: moet een tabel zijn")
            continue
        _onbekend(fouten, plek, s, ("id", "type", "naam", "grootheden", "correctie", "sleutel",
                                    "adres", "interval", "bron", "drempel", "omgekeerd"))
        sid, stype, snaam = s.get("id"), s.get("type"), s.get("naam")
        if not isinstance(snaam, str) or not snaam:
            fouten.append(f"{plek}: 'naam' ontbreekt of is leeg")
        else:
            plek = f"{plek} ({snaam})"
        if stype not in SENSOR_TYPES:
            fouten.append(f"{plek}: 'type' moet een van {', '.join(SENSOR_TYPES)} zijn, niet {stype!r}")
        if not isinstance(sid, str) or not sid:
            fouten.append(f"{plek}: 'id' ontbreekt of is leeg")
            continue
        if stype in BLE_TYPES:
            # bleak levert MAC-adressen in hoofdletters; zo vergelijken we altijd gelijk.
            sid = sid.upper()
            if not _MAC.match(sid):
                fouten.append(f"{plek}: 'id' moet een MAC-adres zijn (AA:BB:CC:DD:EE:FF), niet {s['id']!r}")
        elif stype in SHELLY_TYPES and not _VRIJ_ID.match(sid):
            fouten.append(f"{plek}: 'id' mag alleen letters, cijfers, '-' en '_' bevatten "
                          f"(bijv. \"shelly-bilge\"), niet {s['id']!r}")
        if sid in sensoren:
            fouten.append(f"{plek}: id {sid} komt meer dan eens voor")

        # Wat dit sensortype kan leveren (leeg bij een onbekend type; dat is hierboven al gemeld).
        levert = LEVERT.get(stype, ())
        tip = f"; {stype} levert: {', '.join(levert)}  (zie: brms grootheden {stype})" if levert else ""

        # --- grootheden: optionele lijst van wat wél mee moet (§5.4.2 punt 1) ---
        lijst_g = s.get("grootheden")
        if lijst_g is not None:
            if not isinstance(lijst_g, list) or not all(isinstance(g, str) for g in lijst_g):
                fouten.append(f"{plek}: 'grootheden' moet een lijst van namen zijn, "
                              f"bijv. [\"temperatuur\", \"vocht\"]")
                lijst_g = None
            else:
                for g in lijst_g:
                    if g not in grootheden.GROOTHEDEN:
                        fouten.append(f"{plek}: onbekende grootheid '{g}' in 'grootheden'{tip}")
                    elif levert and g not in levert:
                        fouten.append(f"{plek}: '{g}' wordt door type {stype} niet geleverd{tip}")
                if len(set(lijst_g)) != len(lijst_g):
                    fouten.append(f"{plek}: 'grootheden' bevat een naam meer dan eens")

        # --- correctie: per doorlopende grootheid een getal (offset) of een tabel met
        # factor en/of offset (§5.4.2 punt 2). Een correctie op een toestand (0/1) of een
        # toename heeft geen betekenis. ---
        correctie_in = s.get("correctie", {})
        if not isinstance(correctie_in, dict):
            fouten.append(f"{plek}: 'correctie' moet een tabel zijn, bijv. {{ temperatuur = -0.3 }}")
            correctie_in = {}
        correcties: dict[str, Correctie] = {}
        for g, w in correctie_in.items():
            if g not in grootheden.GROOTHEDEN:
                fouten.append(f"{plek}: correctie voor onbekende grootheid '{g}'{tip}")
                continue
            if grootheden.soort(g) != grootheden.DOORLOPEND:
                fouten.append(f"{plek}: correctie alleen mogelijk voor doorlopende grootheden, niet '{g}'")
                continue
            if levert and g not in levert:
                fouten.append(f"{plek}: correctie voor '{g}', maar type {stype} levert die niet{tip}")
            if lijst_g is not None and g not in lijst_g:
                fouten.append(f"{plek}: correctie voor '{g}', maar '{g}' staat niet in 'grootheden'")
            c = _correctie(fouten, f"{plek}: correctie voor '{g}'", w)
            if c is not None:
                correcties[g] = c

        # --- sleutel: verplicht bij een Victron-apparaat, anders niet toegestaan. De
        # foutmelding noemt de sleutel zelf niet (het is een geheim). ---
        sleutel = s.get("sleutel")
        if stype in SLEUTEL_TYPES:
            if not isinstance(sleutel, str) or not _SLEUTEL.match(sleutel):
                fouten.append(f"{plek}: 'sleutel' ontbreekt of is geen 32 hexadecimale tekens "
                              f"(te vinden in VictronConnect bij Productinfo)")
                sleutel = None
        elif sleutel is not None:
            fouten.append(f"{plek}: 'sleutel' hoort alleen bij een Victron-apparaat")
            sleutel = None

        # --- Shelly: adres, interval, bron, drempel, omgekeerd (§5.2.4) ---
        shelly = {}
        if stype in SHELLY_TYPES:
            shelly = _shelly(fouten, plek, s, lijst_g)
        else:
            for k in ("adres", "interval", "bron", "drempel", "omgekeerd"):
                if k in s:
                    fouten.append(f"{plek}: '{k}' hoort alleen bij een Shelly")

        sensoren[sid] = Sensor(sid, stype, snaam if isinstance(snaam, str) else "", correcties,
                               tuple(lijst_g) if lijst_g is not None else None, sleutel, **shelly)

    if fouten:
        raise ConfigFout(fouten)
    return Config(MqttConfig(host, port), OpslagConfig(database), sensoren,
                  WebConfig(web_host, web_port))
