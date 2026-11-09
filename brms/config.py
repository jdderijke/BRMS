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
    [[sensor]]  id, type, naam, grootheden, correctie – één blok per sensor (§5.4.2)
"""

import os
import re
import tomllib
from dataclasses import dataclass, field

from brms import grootheden
from brms.readers import bthome

# Projectmap: de map waarin de map 'brms' (dit pakket) staat. Op de Pi ~/BRMS.
# Alle standaardpaden zijn hiervan afgeleid, zodat dezelfde code op de pc en op de Pi werkt.
PROJECTMAP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

STANDAARD_PAD = os.path.join(PROJECTMAP, "config", "brms.toml")

# Sensortypen die via Bluetooth binnenkomen (hun id is een MAC-adres). Ruuvi en Victron
# komen erbij zodra hun decoders er zijn.
BLE_TYPES = ("bthome",)
SENSOR_TYPES = BLE_TYPES

# Welke grootheden elk sensortype kan leveren (§5.4.2 punt 3). De decoder legt dat vast;
# BLE-readers voegen zelf de signaalsterkte (rssi) toe. Gebruikt voor de controle van
# 'grootheden' en 'correctie', en door `brms grootheden <type>`.
LEVERT: dict[str, tuple[str, ...]] = {
    "bthome": bthome.LEVERT + ("rssi",),
}

# MAC-adres in de vorm AA:BB:CC:DD:EE:FF (na omzetten naar hoofdletters).
_MAC = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$")


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


@dataclass(frozen=True)
class Config:
    mqtt: MqttConfig
    opslag: OpslagConfig
    sensoren: dict[str, Sensor]  # id -> Sensor


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


def controleer(data: dict) -> Config:
    """Controleert een ingelezen TOML-structuur en zet die om in een Config.

    Alle fouten worden verzameld en samen gemeld (ConfigFout), zodat in één keer
    zichtbaar is wat er gerepareerd moet worden.
    """
    fouten: list[str] = []
    _onbekend(fouten, "bovenste niveau", data, ("mqtt", "opslag", "sensor"))

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
        _onbekend(fouten, plek, s, ("id", "type", "naam", "grootheden", "correctie"))
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

        sensoren[sid] = Sensor(sid, stype, snaam if isinstance(snaam, str) else "", correcties,
                               tuple(lijst_g) if lijst_g is not None else None)

    if fouten:
        raise ConfigFout(fouten)
    return Config(MqttConfig(host, port), OpslagConfig(database), sensoren)
