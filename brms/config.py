"""Inlezen en controleren van de configuratie (ARCHITECTURE.md §5.4, §5.4.1).

Eén TOML-bestand (/etc/brms/brms.toml), gelezen met tomllib. Elke service leest zijn
eigen deel; de controle meldt alle fouten tegelijk, zodat `brms check-config` vóór een
herstart laat zien wat er mis is.
"""

import re
import tomllib
from dataclasses import dataclass, field

from brms import grootheden

STANDAARD_PAD = "/etc/brms/brms.toml"

BLE_TYPES = ("bthome",)
SENSOR_TYPES = BLE_TYPES

_MAC = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$")


class ConfigFout(Exception):
    """De configuratie is ongeldig; `fouten` bevat alle gevonden fouten."""

    def __init__(self, fouten: list[str]):
        self.fouten = fouten
        super().__init__("\n".join(fouten))


@dataclass(frozen=True)
class MqttConfig:
    host: str = "localhost"
    port: int = 1883


@dataclass(frozen=True)
class OpslagConfig:
    database: str = "/var/lib/brms/brms.db"


@dataclass(frozen=True)
class Sensor:
    id: str      # vaste ID; bij BLE het MAC-adres in hoofdletters met dubbele punten
    type: str
    naam: str
    correctie: dict[str, float] = field(default_factory=dict)  # grootheid -> offset


@dataclass(frozen=True)
class Config:
    mqtt: MqttConfig
    opslag: OpslagConfig
    sensoren: dict[str, Sensor]  # id -> Sensor


def lees(pad: str = STANDAARD_PAD) -> Config:
    """Leest en controleert het configuratiebestand; ConfigFout bij fouten."""
    try:
        with open(pad, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigFout([f"configuratiebestand niet gevonden: {pad}"]) from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigFout([f"{pad}: geen geldige TOML: {e}"]) from None
    return controleer(data)


def _onbekend(fouten, plek, d, toegestaan):
    for k in d:
        if k not in toegestaan:
            fouten.append(f"{plek}: onbekende sleutel '{k}'")


def _tabel(fouten, data, naam) -> dict:
    d = data.get(naam, {})
    if not isinstance(d, dict):
        fouten.append(f"[{naam}] moet een tabel zijn")
        return {}
    return d


def controleer(data: dict) -> Config:
    """Controleert een ingelezen TOML-structuur en zet die om in een Config."""
    fouten: list[str] = []
    _onbekend(fouten, "bovenste niveau", data, ("mqtt", "opslag", "sensor"))

    m = _tabel(fouten, data, "mqtt")
    _onbekend(fouten, "[mqtt]", m, ("host", "port"))
    host = m.get("host", MqttConfig.host)
    port = m.get("port", MqttConfig.port)
    if not isinstance(host, str) or not host:
        fouten.append("[mqtt] host moet een niet-lege tekst zijn")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        fouten.append("[mqtt] port moet een geheel getal tussen 1 en 65535 zijn")

    o = _tabel(fouten, data, "opslag")
    _onbekend(fouten, "[opslag]", o, ("database",))
    database = o.get("database", OpslagConfig.database)
    if not isinstance(database, str) or not database:
        fouten.append("[opslag] database moet een niet-leeg pad zijn")

    sensoren: dict[str, Sensor] = {}
    lijst = data.get("sensor", [])
    if not isinstance(lijst, list):
        fouten.append("[[sensor]] moet een lijst van tabellen zijn")
        lijst = []
    for n, s in enumerate(lijst, 1):
        plek = f"[[sensor]] nr. {n}"
        if not isinstance(s, dict):
            fouten.append(f"{plek}: moet een tabel zijn")
            continue
        _onbekend(fouten, plek, s, ("id", "type", "naam", "correctie"))
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
            sid = sid.upper()
            if not _MAC.match(sid):
                fouten.append(f"{plek}: 'id' moet een MAC-adres zijn (AA:BB:CC:DD:EE:FF), niet {s['id']!r}")
        if sid in sensoren:
            fouten.append(f"{plek}: id {sid} komt meer dan eens voor")
        correctie = s.get("correctie", {})
        if not isinstance(correctie, dict):
            fouten.append(f"{plek}: 'correctie' moet een tabel zijn, bijv. {{ temperatuur = -0.3 }}")
            correctie = {}
        for g, w in correctie.items():
            if g not in grootheden.GROOTHEDEN:
                fouten.append(f"{plek}: correctie voor onbekende grootheid '{g}'")
            elif grootheden.soort(g) != grootheden.DOORLOPEND:
                fouten.append(f"{plek}: correctie alleen mogelijk voor doorlopende grootheden, niet '{g}'")
            if isinstance(w, bool) or not isinstance(w, (int, float)):
                fouten.append(f"{plek}: correctie voor '{g}' moet een getal zijn")
        sensoren[sid] = Sensor(sid, stype, snaam if isinstance(snaam, str) else "",
                               {g: float(w) for g, w in correctie.items()
                                if isinstance(w, (int, float)) and not isinstance(w, bool)})

    if fouten:
        raise ConfigFout(fouten)
    return Config(MqttConfig(host, port), OpslagConfig(database), sensoren)
