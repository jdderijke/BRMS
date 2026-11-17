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
    [boot]      tekening                – de SVG-tekening van de bootpagina (§5.6); een
                                          relatief pad geldt vanaf de map van brms.toml
    [[sensor]]  id, type, naam, grootheden, correctie – één blok per sensor (§5.4.2);
                bij een Victron-apparaat ook 'sleutel' (§5.2.2);
                bij een sensor met versnellingen (Ruuvi) eventueel 'stuurboord', voor de
                slagzij (§5.2.5 punt 10);
                bij een Shelly ook 'adres', 'interval', 'bron', 'drempel', 'omgekeerd'
                (§5.2.4)
    [melding.telegram]  token, chat     – meldingen via een Telegram-bot (§5.5.4)
    [melding.email]     server, poort, gebruiker, wachtwoord, van, aan – tweede kanaal
    [[alarm]]   id, naam, sensoren, grootheid, soort, ... – één blok per alarmregel (§5.5.3)
"""

import dataclasses
import os
import re
import tomllib
from dataclasses import dataclass, field

from brms import afleiden, grootheden
from brms.readers import bthome, ruuvi, shelly_relais, shelly_uni, victron

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
# gekozen vaste naam, bijv. "shelly-bilge". Het relais (§5.2.6) is de tussenstekker van
# de kachel: de reader pollt hem en schakelt hem op opdracht van de alarmlaag.
RELAIS_TYPE = "shelly-relais"
SHELLY_TYPES = ("shelly-uni", RELAIS_TYPE)
SENSOR_TYPES = BLE_TYPES + SHELLY_TYPES

# Poll-interval van een Shelly in seconden (§5.3.2): de standaard en de grenzen.
STANDAARD_INTERVAL = 30.0
MIN_INTERVAL, MAX_INTERVAL = 1.0, 3600.0

# Welke grootheden elk sensortype kan leveren (§5.4.2 punt 3). De decoder legt dat vast;
# BLE-readers voegen zelf de signaalsterkte (rssi) toe. Gebruikt voor de controle van
# 'grootheden' en 'correctie', en door `brms grootheden <type>`.
#
# Daar komen de afgeleide grootheden bij waarvoor het type alle invoer levert (§5.2.5
# punt 3), bijv. 'slagzij' bij een Ruuvi (die levert de versnellingen). Of een bepaalde
# sensor zo'n grootheid ook echt levert, hangt nog af van zijn instelling: zie levert().
LEVERT: dict[str, tuple[str, ...]] = {
    type_: decoder + afleiden.voor_type(decoder) + ("rssi",)
    for type_, decoder in {
        "bthome": bthome.LEVERT,
        "ruuvi": ruuvi.LEVERT,
        **victron.LEVERT,
        "shelly-uni": shelly_uni.LEVERT,   # rssi = hier de signaalsterkte van de wifi
        RELAIS_TYPE: shelly_relais.LEVERT,
    }.items()
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
class BootConfig:
    """Het blok [boot] (§5.6, bootpagina): de tekening van de boot.

    Alleen het pad van het SVG-bestand. Welke waarden op de pagina staan en waar, staat in
    de tekening zelf (de teksten `sensor.grootheid`); de pagina leest dat in de browser.
    Zonder dit blok is er geen bootpagina.
    """
    tekening: str   # volledig pad van het SVG-bestand


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
    # --- alleen bij een sensor die versnellingen levert (Ruuvi; §5.2.5 punt 10) ---
    # De versnelling die positief wordt als de boot over stuurboord helt: "x", "-x", "y"
    # of "-y". Nodig voor de afgeleide grootheid 'slagzij'; zonder dit veld (None) levert
    # de sensor geen slagzij.
    stuurboord: str | None = None
    # --- alleen bij een Shelly-relais (§5.2.6 punt 5) ---
    # Wat moet gelden om het relais aan te zetten: (sensor-ID, grootheid, waarde), bijv.
    # (("D3:00:00:00:00:03", "walstroom", 1.0),). Alle voorwaarden moeten gelden. Verplicht
    # bij een relais (besluit OPEN-34): de kachel gaat nooit aan zonder dat er iets over
    # de walstroom is vastgelegd.
    voorwaarde: tuple[tuple[str, str, float], ...] = ()


@dataclass(frozen=True)
class TelegramConfig:
    """Meldingen via een Telegram-bot (§5.5.4 punt 2). De token is een geheim: hij komt
    niet in logregels of foutmeldingen (repr=False houdt hem ook uit een afgedrukte Config)."""
    token: str = field(repr=False)
    chat: str = ""      # het gesprek waarheen de meldingen gaan (chat-ID)


@dataclass(frozen=True)
class EmailConfig:
    """Meldingen via e-mail (SMTP), het tweede kanaal (§5.5.4 punt 3)."""
    server: str
    poort: int
    gebruiker: str
    wachtwoord: str = field(repr=False)   # een geheim, zie TelegramConfig
    van: str = ""
    aan: tuple[str, ...] = ()


@dataclass(frozen=True)
class MeldingConfig:
    telegram: TelegramConfig | None = None
    email: EmailConfig | None = None
    # Een fout in het logboek (journald) ook als melding versturen (§5.5.5 punt 5).
    systeemfouten: bool = True

    @property
    def ingesteld(self) -> bool:
        return self.telegram is not None or self.email is not None


# Soorten alarmregel (§5.5.3 punt 2).
GRENS, AANTAL, STIL = "grens", "aantal", "stil"
ALARM_SOORTEN = (GRENS, AANTAL, STIL)
# Notificatie bij een alarm (§5.5.3 punt 4).
MELDINGEN = ("uit", "eenmalig", "herhaald")
# Prioriteit van een melding (§5.5.2 punt 8).
PRIORITEITEN = ("urgent", "hoog", "normaal")
# Foto bij een notificatie (§5.5.3 punt 5): bij het begin van het alarm, of ook bij elke
# herinnering.
FOTO_BIJ = ("begin", "elke")
# Acties met de camera (§5.5.3 punt 6). De derde actie, "relais", staat apart in de regel
# (veld `relais`): zij heeft geen camera nodig en werkt anders (§5.2.6).
ACTIES = ("video", "foto")


@dataclass(frozen=True)
class Alarmregel:
    """Eén blok [[alarm]] uit de configuratie (§5.5.3 punt 1). Tijden in seconden."""
    id: str                      # vaste naam; staat in de database
    naam: str                    # tekst voor meldingen
    # De ID's van de sensoren waarvoor de regel geldt. Bij soort 'stil' kan er ook het ID
    # van een camera tussen staan (§5.5.3 punt 2): een geslaagde opname is dan het levensteken.
    sensoren: tuple[str, ...]
    soort: str                   # grens, aantal of stil
    grootheid: str | None = None  # None bij 'stil'
    # --- soort grens: precies één van boven/onder ---
    boven: float | None = None   # alarm als de waarde hierboven ligt
    onder: float | None = None   # alarm als de waarde hieronder ligt
    herstel: float | None = None  # herstelgrens; het verschil met de grens is de hysterese
    # --- soort aantal ---
    aantal: int | None = None    # zoveel (of meer) binnen het venster
    venster: float | None = None
    # --- tijden ---
    duur: float = 0.0            # zo lang moet de voorwaarde onafgebroken gelden
    herstelduur: float = 0.0     # zo lang moet de herstelvoorwaarde onafgebroken gelden
    # --- notificatie ---
    melding: str = "eenmalig"    # uit, eenmalig of herhaald
    herhaling: float | None = None  # tussenpoos van de herinnering bij 'herhaald'
    herstelmelding: bool = True
    prioriteit: str = "normaal"
    # --- camera (§5.5.3 punt 5 en 6, §5.7 punt 15) ---
    foto: str | None = None      # foto bij de notificatie: "begin" of "elke"; None = geen
    acties: tuple[str, ...] = ()  # "video" en/of "foto": opname als het alarm afgaat
    camera: str | None = None    # ID van de camera voor foto en acties
    # --- relais (§5.2.6, §5.5.3 punt 6) ---
    # ID van de sensor van het type shelly-relais die deze regel schakelt: aan zolang het
    # alarm actief is en er walstroom is, uit bij herstel. None = de regel schakelt niets.
    relais: str | None = None


# Grenzen van de camera-instellingen (§5.7), in seconden.
MIN_FOTO_INTERVAL, MAX_FOTO_INTERVAL = 10.0, 86400.0
MIN_VIDEO_DUUR, MAX_VIDEO_DUUR = 5.0, 600.0
# Velden van [[camera]] die niet meer bestaan (§5.7 punt 18, v0.48): het live-beeld kwam
# tot code v0.17 van de MJPEG-stroom van de camera. Staan ze nog in brms.toml, dan is dat
# geen fout maar een waarschuwing: een update mag er niet op stuklopen.
VERVALLEN_CAMERA = ("live_url", "live_beelden")
# De invulplaatsen voor het account in een camera-adres (§5.7 punt 7).
PLAATS_GEBRUIKER, PLAATS_WACHTWOORD = "{gebruiker}", "{wachtwoord}"
# Richten van een camera die kan bewegen (§5.7 punt 19). Het veld 'richten' noemt het merk:
# elk merk heeft zijn eigen commando's, en die staan in de camera-reader
# (brms/readers/camera.py). Een nieuw merk komt hier en daar bij.
RICHT_MERKEN = ("foscam",)
# De vier richtingen van de knoppen in het live-beeld; zo heten ze ook in de opdracht
# brms/opdracht/camera/richt (§3.3).
RICHTINGEN = ("op", "neer", "links", "rechts")
# Grenzen van 'richt_stap' in seconden: hoe lang de camera per klik beweegt.
MIN_RICHT_STAP, MAX_RICHT_STAP = 0.1, 5.0


@dataclass(frozen=True)
class Camera:
    """Eén blok [[camera]] uit de configuratie (§5.7 punt 7). Tijden in seconden."""
    id: str                      # vaste naam; staat in de database en in de bestandsnaam
    naam: str
    # Het snapshot-adres (http/https). Op de plaats van het account staan {gebruiker} en
    # {wachtwoord}; de camera-reader vult die in.
    foto_url: str = field(repr=False)
    # Het adres van de RTSP-stroom voor video's; None = deze camera maakt geen video's.
    video_url: str | None = field(default=None, repr=False)
    gebruiker: str | None = None
    # Een geheim: komt niet in logregels of foutmeldingen (zie TelegramConfig). Ook de
    # adressen staan niet in de repr: iemand kan het wachtwoord er letterlijk in zetten.
    wachtwoord: str | None = field(default=None, repr=False)
    foto_interval: float = 180.0
    video_duur: float = 60.0
    # Het live-beeld in het web (§5.7 punt 18) heeft geen eigen velden: elke camera met een
    # video_url heeft live-beeld, via go2rtc (zie brms/go2rtc.py).
    # Richten (§5.7 punt 19): het merk van een camera die kan bewegen (een van
    # RICHT_MERKEN), of None voor een vaste camera. Alleen een camera met 'richten' krijgt
    # de vier richtknoppen in het live-beeld.
    richten: str | None = None
    # Hoe lang de camera per klik beweegt, in seconden; daarna stopt de camera-reader hem.
    richt_stap: float = 0.5


@dataclass(frozen=True)
class OpnamenConfig:
    """Het blok [opnames] (§5.7 punt 7)."""
    # De opnamemap; een relatief pad geldt vanaf de projectmap (zoals de database).
    map: str = os.path.normpath(os.path.join(PROJECTMAP, "data", "opnames"))
    foto_bewaren: float = 14 * 86400.0   # seconden
    video_ruimte: float = 20.0           # GB


@dataclass(frozen=True)
class Config:
    mqtt: MqttConfig
    opslag: OpslagConfig
    sensoren: dict[str, Sensor]  # id -> Sensor
    web: WebConfig = WebConfig()
    melding: MeldingConfig = MeldingConfig()
    alarmen: tuple[Alarmregel, ...] = ()
    cameras: dict[str, Camera] = field(default_factory=dict)   # id -> Camera
    opnames: OpnamenConfig = OpnamenConfig()
    # De bootpagina (§5.6); None = geen blok [boot], dus geen bootpagina.
    boot: BootConfig | None = None
    # Opmerkingen over de configuratie die geen fout zijn (bijv. een veld dat niet meer
    # bestaat en genegeerd wordt). `brms check-config` toont ze; de services starten gewoon.
    waarschuwingen: tuple[str, ...] = ()


def levert(sensor: Sensor) -> tuple[str, ...]:
    """De grootheden die deze sensor kan publiceren: wat zijn type levert, bij een Shelly
    beperkt tot wat een 'bron' heeft, een afgeleide grootheid alleen als de instelling
    ervoor er staat (bijv. 'stuurboord' voor de slagzij, §5.2.5 punt 3), en verder beperkt
    tot de lijst 'grootheden' als die er is (§5.4.2). Gebruikt door de webpagina voor de
    keuzelijst en door de controle van de alarmregels."""
    alles = LEVERT.get(sensor.type, ())
    if sensor.type == "shelly-uni":
        alles = tuple(g for g in alles if g in sensor.bron or g == "rssi")
    alles = tuple(g for g in alles if afleiden.ontbreekt(g, sensor) is None)
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
    # De map van het bestand gaat mee: de tekening van de bootpagina staat ernaast (§5.6).
    return controleer(data, os.path.dirname(os.path.abspath(pad)))


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


def _shelly(fouten, plek, s: dict, lijst_g, relais: bool = False) -> dict:
    """Controleert de velden van een Shelly (§5.2.4) en geeft ze terug als argumenten voor
    Sensor. lijst_g is de lijst 'grootheden' van de sensor, of None. relais: het is een
    Shelly-relais (§5.2.6 punt 1): alleen 'adres' en 'interval'."""
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

    # Het relais heeft één vaste uitgang (switch:0): er valt niets te kiezen, en een
    # contact of spanningsmeting heeft het niet.
    if relais:
        for k in ("bron", "drempel", "omgekeerd"):
            if k in s:
                fouten.append(f"{plek}: '{k}' hoort bij type shelly-uni, niet bij een relais")
        return {"adres": adres, "interval": float(interval)}

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


# Een tijdsduur in de configuratie: getal met eenheid, bijv. "90s", "15m", "6u", "7d".
_DUUR = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*([smud])\s*$")
_SECONDEN = {"s": 1, "m": 60, "u": 3600, "d": 86400}


def duur(tekst) -> float:
    """Zet een tijdsduur uit de configuratie om in seconden; ValueError als het geen
    geldige tijdsduur is. "90s" -> 90, "15m" -> 900, "6u" -> 21600, "7d" -> 604800."""
    m = _DUUR.match(tekst) if isinstance(tekst, str) else None
    if m is None:
        raise ValueError('moet een tijdsduur zijn als "90s", "15m", "6u" of "7d"')
    return float(m.group(1).replace(",", ".")) * _SECONDEN[m.group(2)]


def _tekst(fouten, plek, d: dict, sleutel: str) -> str | None:
    """Haalt een verplichte, niet-lege tekst op; meldt een fout en geeft None als hij
    ontbreekt. De waarde zelf komt niet in de melding (kan een geheim zijn)."""
    w = d.get(sleutel)
    if not isinstance(w, str) or not w.strip():
        fouten.append(f"{plek}: '{sleutel}' ontbreekt of is leeg")
        return None
    return w.strip()


def _melding(fouten, data: dict) -> MeldingConfig:
    """Controleert [melding.telegram] en [melding.email] (§5.5.4)."""
    m = _tabel(fouten, data, "melding")
    _onbekend(fouten, "[melding]", m, ("telegram", "email", "systeemfouten"))
    systeemfouten = m.get("systeemfouten", MeldingConfig.systeemfouten)
    if not isinstance(systeemfouten, bool):
        fouten.append("[melding]: 'systeemfouten' moet true of false zijn")
        systeemfouten = MeldingConfig.systeemfouten

    telegram = None
    t = m.get("telegram")
    if t is not None:
        plek = "[melding.telegram]"
        if not isinstance(t, dict):
            fouten.append(f"{plek} moet een tabel zijn")
        else:
            _onbekend(fouten, plek, t, ("token", "chat"))
            token = _tekst(fouten, plek, t, "token")
            # Het chat-ID is een getal (soms negatief, bij een groep); als getal of als
            # tekst geschreven is beide goed.
            chat = t.get("chat")
            if isinstance(chat, int) and not isinstance(chat, bool):
                chat = str(chat)
            if not isinstance(chat, str) or not chat.strip():
                fouten.append(f"{plek}: 'chat' ontbreekt (het chat-ID van het gesprek met de bot)")
                chat = None
            if token and chat:
                telegram = TelegramConfig(token, chat.strip())

    email = None
    e = m.get("email")
    if e is not None:
        plek = "[melding.email]"
        if not isinstance(e, dict):
            fouten.append(f"{plek} moet een tabel zijn")
        else:
            _onbekend(fouten, plek, e, ("server", "poort", "gebruiker", "wachtwoord", "van", "aan"))
            server = _tekst(fouten, plek, e, "server")
            gebruiker = _tekst(fouten, plek, e, "gebruiker")
            wachtwoord = _tekst(fouten, plek, e, "wachtwoord")
            poort = e.get("poort", 587)
            if isinstance(poort, bool) or not isinstance(poort, int) or not 1 <= poort <= 65535:
                fouten.append(f"{plek}: 'poort' moet een geheel getal tussen 1 en 65535 zijn")
                poort = None
            # Eén ontvanger als tekst, of meerdere als lijst.
            aan = e.get("aan")
            aan = [aan] if isinstance(aan, str) else aan
            if (not isinstance(aan, list) or not aan
                    or not all(isinstance(a, str) and "@" in a for a in aan)):
                fouten.append(f"{plek}: 'aan' moet een e-mailadres zijn, of een lijst van adressen")
                aan = None
            # Zonder 'van' is de afzender de gebruikersnaam (meestal het mailadres zelf).
            van = e.get("van", gebruiker)
            if not isinstance(van, str) or "@" not in van:
                fouten.append(f"{plek}: 'van' moet een e-mailadres zijn (de afzender)")
                van = None
            if server and gebruiker and wachtwoord and poort and aan and van:
                email = EmailConfig(server, poort, gebruiker, wachtwoord, van, tuple(aan))
    return MeldingConfig(telegram, email, systeemfouten)


def _tijd(fouten, plek, d: dict, sleutel: str, standaard: float, laagste: float,
          hoogste: float) -> float:
    """Haalt een optionele tijdsduur op ("3m", "120s") en controleert de grenzen; bij een
    fout wordt die gemeld en geldt de standaardwaarde."""
    if sleutel not in d:
        return standaard
    try:
        seconden = duur(d[sleutel])
    except ValueError as e:
        fouten.append(f"{plek}: '{sleutel}' {e}")
        return standaard
    if not laagste <= seconden <= hoogste:
        fouten.append(f"{plek}: '{sleutel}' moet tussen {laagste:g} en {hoogste:g} seconden liggen")
        return standaard
    return seconden


def _adres(fouten, plek, c: dict, sleutel: str, schemas: tuple[str, ...]) -> str | None:
    """Controleert een camera-adres. De waarde zelf komt niet in de melding: er kan een
    wachtwoord in staan."""
    w = c.get(sleutel)
    if not isinstance(w, str) or not w.startswith(schemas) or any(t.isspace() for t in w):
        fouten.append(f"{plek}: '{sleutel}' moet een adres zijn dat begint met "
                      f"{' of '.join(schemas)}, zonder spaties")
        return None
    return w


def _cameras(fouten, data: dict, sensoren: dict[str, Sensor],
             waarschuwingen: list[str] | None = None) -> dict[str, Camera]:
    """Controleert de blokken [[camera]] (§5.7 punt 7). In `waarschuwingen` komt wat geen
    fout is maar wel het melden waard (een vervallen veld)."""
    if waarschuwingen is None:
        waarschuwingen = []
    lijst = data.get("camera", [])
    if not isinstance(lijst, list):
        fouten.append("[[camera]] moet een lijst van tabellen zijn")
        return {}
    cameras: dict[str, Camera] = {}
    for n, c in enumerate(lijst, 1):
        plek = f"[[camera]] nr. {n}"
        if not isinstance(c, dict):
            fouten.append(f"{plek}: moet een tabel zijn")
            continue
        _onbekend(fouten, plek, c, ("id", "naam", "foto_url", "video_url", "gebruiker",
                                    "wachtwoord", "foto_interval", "video_duur", "richten",
                                    "richt_stap")
                  + VERVALLEN_CAMERA)
        aantal_fouten = len(fouten)

        cid = c.get("id")
        if not isinstance(cid, str) or not _VRIJ_ID.match(cid):
            fouten.append(f"{plek}: 'id' ontbreekt of bevat andere tekens dan letters, cijfers, "
                          f"'-' en '_' (bijv. \"dek\")")
            cid = None
        else:
            plek = f"{plek} ({cid})"
            # Het ID wordt een mapnaam en een topic; het mag niet botsen met een sensor.
            if cid in cameras or cid in sensoren:
                fouten.append(f"{plek}: id {cid} komt meer dan eens voor")
        # Vervallen velden worden genegeerd, wat er ook in staat (§5.7 punt 18).
        oud = [k for k in VERVALLEN_CAMERA if k in c]
        if oud:
            waarschuwingen.append(
                f"{plek}: {' en '.join(repr(k) for k in oud)} "
                f"{'bestaat' if len(oud) == 1 else 'bestaan'} niet meer en "
                f"{'wordt' if len(oud) == 1 else 'worden'} genegeerd; het live-beeld komt nu "
                f"via go2rtc van de 'video_url'. Haal de regel(s) weg uit brms.toml.")
        # Zonder naam geldt het ID als naam.
        naam = c.get("naam", cid or "?")
        if not isinstance(naam, str) or not naam.strip():
            fouten.append(f"{plek}: 'naam' moet een niet-lege tekst zijn")

        foto_url = _adres(fouten, plek, c, "foto_url", ("http://", "https://"))
        video_url = None
        if "video_url" in c:
            video_url = _adres(fouten, plek, c, "video_url", ("rtsp://", "rtsps://"))

        # Het account: beide of geen van beide. Staat er een invulplaats in een adres, dan
        # moet het account er zijn (anders gaat de tekst {wachtwoord} naar de camera).
        gebruiker, wachtwoord = c.get("gebruiker"), c.get("wachtwoord")
        for sleutel, w in (("gebruiker", gebruiker), ("wachtwoord", wachtwoord)):
            if w is not None and (not isinstance(w, str) or not w):
                fouten.append(f"{plek}: '{sleutel}' moet een niet-lege tekst zijn")
        if (gebruiker is None) != (wachtwoord is None):
            fouten.append(f"{plek}: 'gebruiker' en 'wachtwoord' horen bij elkaar; een van de twee ontbreekt")

        adressen = (foto_url or "") + (video_url or "")
        if gebruiker is None and (PLAATS_GEBRUIKER in adressen or PLAATS_WACHTWOORD in adressen):
            fouten.append(f"{plek}: een adres bevat {PLAATS_GEBRUIKER} of {PLAATS_WACHTWOORD}, "
                          f"maar 'gebruiker' en 'wachtwoord' ontbreken")

        foto_interval = _tijd(fouten, plek, c, "foto_interval", Camera.foto_interval,
                              MIN_FOTO_INTERVAL, MAX_FOTO_INTERVAL)
        video_duur = _tijd(fouten, plek, c, "video_duur", Camera.video_duur,
                           MIN_VIDEO_DUUR, MAX_VIDEO_DUUR)

        # Richten (§5.7 punt 19): het merk moet bekend zijn, want de commando's verschillen
        # per merk. De richtknoppen staan in het live-beeld; zonder videostroom is er geen
        # live-beeld en zou 'richten' stil niets doen.
        richten = c.get("richten")
        if richten is not None:
            if richten not in RICHT_MERKEN:
                fouten.append(f"{plek}: 'richten' moet het merk van de camera zijn; bekend: "
                              f"{', '.join(RICHT_MERKEN)}. Laat het veld weg bij een camera "
                              f"die niet kan bewegen")
            elif "video_url" not in c:
                fouten.append(f"{plek}: 'richten' zonder 'video_url' doet niets: de "
                              f"richtknoppen staan in het live-beeld")
        elif "richt_stap" in c:
            fouten.append(f"{plek}: 'richt_stap' zonder 'richten' doet niets")
        richt_stap = _tijd(fouten, plek, c, "richt_stap", Camera.richt_stap,
                           MIN_RICHT_STAP, MAX_RICHT_STAP)
        if len(fouten) == aantal_fouten:
            cameras[cid] = Camera(cid, naam.strip(), foto_url, video_url, gebruiker, wachtwoord,
                                  foto_interval, video_duur, richten, richt_stap)
    return cameras


def _opnames(fouten, data: dict) -> OpnamenConfig:
    """Controleert het blok [opnames] (§5.7 punt 7)."""
    o = _tabel(fouten, data, "opnames")
    plek = "[opnames]"
    _onbekend(fouten, plek, o, ("map", "foto_bewaren", "video_ruimte"))
    map_ = o.get("map", OpnamenConfig.map)
    if not isinstance(map_, str) or not map_:
        fouten.append(f"{plek}: 'map' moet een niet-leeg pad zijn")
        map_ = OpnamenConfig.map
    elif not os.path.isabs(map_):
        map_ = os.path.normpath(os.path.join(PROJECTMAP, map_))
    # Minstens een uur: korter is altijd een tikfout ("14" in plaats van "14d" is geen duur).
    bewaren = _tijd(fouten, plek, o, "foto_bewaren", OpnamenConfig.foto_bewaren, 3600.0,
                    3650 * 86400.0)
    ruimte = o.get("video_ruimte", OpnamenConfig.video_ruimte)
    if not _getal(ruimte) or ruimte <= 0:
        fouten.append(f"{plek}: 'video_ruimte' moet een getal groter dan 0 zijn (GB)")
        ruimte = OpnamenConfig.video_ruimte
    return OpnamenConfig(map_, bewaren, float(ruimte))


def _relais_bij_alarm(fouten, plek, actie: dict, sensoren: dict[str, Sensor]) -> str | None:
    """Controleert een actie { type = "relais", relais = "<sensor>" } (§5.5.3 punt 6,
    §5.2.6); geeft het ID van het relais, of None na een fout."""
    _onbekend(fouten, f"{plek}: actie 'relais'", actie, ("type", "relais"))
    naam = actie.get("relais")
    if not isinstance(naam, str) or not naam:
        fouten.append(f"{plek}: actie 'relais' mist het veld 'relais' (de naam of het id van "
                      f"de sensor van het type {RELAIS_TYPE})")
        return None
    # Op ID of op naam, zoals bij 'sensoren' van de regel.
    sensor = sensoren.get(naam) or next(
        (x for x in sensoren.values() if x.naam.lower() == naam.lower()), None)
    if sensor is None:
        fouten.append(f"{plek}: actie 'relais': onbekende sensor '{naam}'")
        return None
    if sensor.type != RELAIS_TYPE:
        fouten.append(f"{plek}: actie 'relais': sensor '{naam}' is van type {sensor.type}, "
                      f"niet {RELAIS_TYPE}")
        return None
    # Wanneer het relais aan mag (bijv. alleen met walstroom), staat bij het relais zelf:
    # het veld 'voorwaarde' van die sensor (§5.2.6 punt 5).
    return sensor.id


def _voorwaarde(fouten, plek, w, sensoren: dict[str, Sensor]) -> tuple[tuple[str, str, float], ...]:
    """Controleert het veld 'voorwaarde' van een Shelly-relais (§5.2.6 punt 5) en geeft de
    voorwaarden als (sensor-ID, grootheid, waarde).

    In brms.toml staat  voorwaarde = { multiplus.walstroom = 1 }. TOML leest de punt als
    een tabel in een tabel: {"multiplus": {"walstroom": 1}}. Zo komt het hier binnen.
    Het veld is verplicht (besluit OPEN-34).
    """
    voorbeeld = "bijv. voorwaarde = { multiplus.walstroom = 1 }"
    if w is None:
        fouten.append(f"{plek}: 'voorwaarde' ontbreekt: wat moet gelden om het relais aan te "
                      f"zetten, {voorbeeld}")
        return ()
    if not isinstance(w, dict) or not w or not all(isinstance(v, dict) and v for v in w.values()):
        fouten.append(f"{plek}: 'voorwaarde' moet een tabel <sensor>.<grootheid> = <waarde> "
                      f"zijn, {voorbeeld}")
        return ()
    uit: list[tuple[str, str, float]] = []
    for naam, per_grootheid in w.items():
        # Op ID of op naam, zoals bij 'sensoren' van een alarmregel.
        sensor = (sensoren.get(naam) or sensoren.get(naam.upper())
                  or next((x for x in sensoren.values() if x.naam.lower() == naam.lower()), None))
        if sensor is None:
            fouten.append(f"{plek}: voorwaarde: onbekende sensor '{naam}'")
            continue
        for g, waarde in per_grootheid.items():
            if g not in levert(sensor):
                fouten.append(f"{plek}: voorwaarde: sensor '{sensor.naam}' levert geen '{g}'  "
                              f"(zie: brms grootheden {sensor.type})")
            elif grootheden.soort(g) != grootheden.TOESTAND:
                fouten.append(f"{plek}: voorwaarde: '{g}' is geen toestand (0/1); een "
                              f"voorwaarde kan alleen op een toestand, bijv. walstroom")
            elif not _getal(waarde) or waarde not in (0, 1):
                fouten.append(f"{plek}: voorwaarde: de waarde van '{naam}.{g}' moet 0 of 1 zijn, "
                              f"niet {waarde!r}")
            else:
                uit.append((sensor.id, g, float(waarde)))
    return tuple(uit)


def _camera_bij_alarm(fouten, plek, a: dict, melding: str, cameras: dict[str, Camera],
                      sensoren: dict[str, Sensor]
                      ) -> tuple[str | None, tuple[str, ...], str | None, str | None]:
    """Controleert 'foto', 'actie' en 'camera' van een alarmregel (§5.5.3 punt 5 en 6);
    geeft (foto, acties met de camera, camera-ID, relais-ID)."""
    # --- foto bij de notificatie ---
    foto = a.get("foto")
    if foto == "geen":
        foto = None          # hetzelfde als het veld weglaten
    if foto is not None and foto not in FOTO_BIJ:
        fouten.append(f"{plek}: 'foto' moet \"geen\", \"begin\" of \"elke\" zijn")
        foto = None
    elif foto is not None and melding == "uit":
        fouten.append(f"{plek}: 'foto' gaat mee met de melding, maar melding = \"uit\"; "
                      f"gebruik voor een losse foto: actie = [{{ type = \"foto\" }}]")

    # --- acties: een lijst van tabellen, elk met een type ---
    acties: list[str] = []
    relais: str | None = None
    relais_gezien = False
    lijst = a.get("actie", [])
    if not isinstance(lijst, list) or not all(isinstance(x, dict) for x in lijst):
        fouten.append(f"{plek}: 'actie' moet een lijst van tabellen zijn, "
                      f"bijv. [{{ type = \"video\" }}]")
        lijst = []
    for actie in lijst:
        type_ = actie.get("type")
        if type_ == "relais":
            # Het relais (§5.2.6): een eigen veld in de regel, los van de camera.
            if relais_gezien:
                fouten.append(f"{plek}: actie 'relais' staat er meer dan eens")
            else:
                relais = _relais_bij_alarm(fouten, plek, actie, sensoren)
            relais_gezien = True
        elif type_ not in ACTIES:
            fouten.append(f"{plek}: actie 'type' moet een van {', '.join(ACTIES)}, relais zijn, "
                          f"niet {type_!r}")
        elif type_ in acties:
            fouten.append(f"{plek}: actie '{type_}' staat er meer dan eens")
        else:
            _onbekend(fouten, f"{plek}: actie '{type_}'", actie, ("type",))
            acties.append(type_)

    # --- camera: welke camera de foto of video maakt; zonder dit veld de eerste ---
    # (Een regel met alleen de actie 'relais' heeft geen camera nodig.)
    camera = a.get("camera")
    if foto is None and not acties:
        if camera is not None:
            fouten.append(f"{plek}: 'camera' zonder 'foto' of 'actie' doet niets")
        return None, (), None, relais
    if camera is None:
        if not cameras:
            fouten.append(f"{plek}: 'foto' of 'actie' gevraagd, maar er is geen [[camera]] ingesteld")
            return foto, tuple(acties), None, relais
        camera = next(iter(cameras))
    elif camera not in cameras:
        fouten.append(f"{plek}: onbekende camera {camera!r}"
                      + (f"; ingesteld: {', '.join(cameras)}" if cameras else "; er is geen [[camera]]"))
        return foto, tuple(acties), None, relais
    if "video" in acties and cameras[camera].video_url is None:
        fouten.append(f"{plek}: actie 'video', maar camera {camera} heeft geen 'video_url'")
    return foto, tuple(acties), camera, relais


def _alarmen(fouten, data: dict, sensoren: dict[str, Sensor],
             cameras: dict[str, Camera]) -> tuple[Alarmregel, ...]:
    """Controleert de blokken [[alarm]] (§5.5.3 punt 1) en zet ze om in Alarmregels."""
    lijst = data.get("alarm", [])
    if not isinstance(lijst, list):
        fouten.append("[[alarm]] moet een lijst van tabellen zijn")
        return ()
    regels: list[Alarmregel] = []
    ids: set[str] = set()
    for n, a in enumerate(lijst, 1):
        plek = f"[[alarm]] nr. {n}"
        if not isinstance(a, dict):
            fouten.append(f"{plek}: moet een tabel zijn")
            continue
        _onbekend(fouten, plek, a, (
            "id", "naam", "sensoren", "grootheid", "soort", "boven", "onder", "duur", "herstel",
            "herstelduur", "aantal", "venster", "melding", "herhaling", "herstelmelding",
            "prioriteit", "foto", "actie", "camera"))
        aantal_fouten = len(fouten)

        aid = a.get("id")
        if not isinstance(aid, str) or not _VRIJ_ID.match(aid):
            fouten.append(f"{plek}: 'id' ontbreekt of bevat andere tekens dan letters, cijfers, "
                          f"'-' en '_' (bijv. \"vorst\")")
            aid = None
        else:
            plek = f"{plek} ({aid})"
            if aid in ids:
                fouten.append(f"{plek}: id {aid} komt meer dan eens voor")
            ids.add(aid)
        naam = a.get("naam", aid)
        if not isinstance(naam, str) or not naam:
            fouten.append(f"{plek}: 'naam' moet een niet-lege tekst zijn")

        soort = a.get("soort")
        if soort not in ALARM_SOORTEN:
            fouten.append(f"{plek}: 'soort' moet een van {', '.join(ALARM_SOORTEN)} zijn, "
                          f"niet {soort!r}")
            continue

        # --- sensoren: op naam of ID; elke sensor wordt apart beoordeeld. In een regel van
        # soort 'stil' mag ook een camera staan (§5.5.3 punt 2, v0.51): een camera levert
        # geen metingen, maar elke geslaagde opname telt als levensteken. ---
        gekozen: list[Sensor | Camera] = []
        namen = a.get("sensoren")
        if not isinstance(namen, list) or not namen or not all(isinstance(s, str) for s in namen):
            fouten.append(f"{plek}: 'sensoren' moet een niet-lege lijst van sensornamen zijn")
        else:
            for s in namen:
                sensor = (sensoren.get(s) or sensoren.get(s.upper())
                          or next((x for x in sensoren.values() if x.naam.lower() == s.lower()), None))
                if sensor is None:
                    # Geen sensor met die naam: is het een camera?
                    sensor = cameras.get(s) or next(
                        (c for c in cameras.values() if c.naam.lower() == s.lower()), None)
                    if sensor is not None and soort != STIL:
                        fouten.append(f"{plek}: '{s}' is een camera; een camera kan alleen in "
                                      f"een regel van soort 'stil' staan")
                        continue
                if sensor is None:
                    fouten.append(f"{plek}: onbekende sensor '{s}'")
                elif sensor in gekozen:
                    fouten.append(f"{plek}: sensor '{s}' staat meer dan eens in 'sensoren'")
                else:
                    gekozen.append(sensor)

        # --- grootheid: niet bij 'stil' (elke waarde van de sensor telt dan) ---
        grootheid = a.get("grootheid")
        if soort == STIL:
            if grootheid is not None:
                fouten.append(f"{plek}: bij soort 'stil' hoort geen 'grootheid'")
        elif grootheid not in grootheden.GROOTHEDEN:
            fouten.append(f"{plek}: 'grootheid' ontbreekt of is onbekend: {grootheid!r}  "
                          f"(zie: brms grootheden)")
            grootheid = None
        else:
            for sensor in gekozen:
                if grootheid not in levert(sensor):
                    # Een afgeleide grootheid waarvoor de sensor alleen de instelling
                    # mist: dan zeggen wat er moet komen (§5.2.5 punt 10).
                    veld = afleiden.ontbreekt(grootheid, sensor)
                    if veld and grootheid in LEVERT.get(sensor.type, ()):
                        fouten.append(f"{plek}: sensor '{sensor.naam}' levert geen '{grootheid}' "
                                      f"zolang het veld '{veld}' bij die sensor ontbreekt")
                    else:
                        fouten.append(f"{plek}: sensor '{sensor.naam}' levert geen '{grootheid}'  "
                                      f"(zie: brms grootheden {sensor.type})")

        def tijd(sleutel, standaard=0.0, verplicht=False):
            """Een tijdsduur uit de regel, in seconden."""
            if sleutel not in a:
                if verplicht:
                    fouten.append(f"{plek}: '{sleutel}' ontbreekt")
                return standaard
            try:
                return duur(a[sleutel])
            except ValueError as e:
                fouten.append(f"{plek}: '{sleutel}' {e}")
                return standaard

        def alleen_bij(sleutels, soorten):
            for k in sleutels:
                if k in a and soort not in soorten:
                    fouten.append(f"{plek}: '{k}' hoort niet bij soort '{soort}'")

        alleen_bij(("boven", "onder", "herstel"), (GRENS,))
        alleen_bij(("aantal", "venster"), (AANTAL,))
        alleen_bij(("herstelduur",), (GRENS, AANTAL))

        boven = onder = herstel = aantal = venster = None
        if soort == GRENS:
            boven, onder, herstel = a.get("boven"), a.get("onder"), a.get("herstel")
            if (boven is None) == (onder is None):
                fouten.append(f"{plek}: geef precies één van 'boven' of 'onder' (de grens)")
            for k, w in (("boven", boven), ("onder", onder), ("herstel", herstel)):
                if w is not None and not _getal(w):
                    fouten.append(f"{plek}: '{k}' moet een getal zijn")
            if _getal(herstel) and _getal(boven) and herstel > boven:
                fouten.append(f"{plek}: 'herstel' moet gelijk aan of lager dan 'boven' zijn")
            if _getal(herstel) and _getal(onder) and herstel < onder:
                fouten.append(f"{plek}: 'herstel' moet gelijk aan of hoger dan 'onder' zijn")
        elif soort == AANTAL:
            aantal = a.get("aantal")
            if isinstance(aantal, bool) or not isinstance(aantal, int) or aantal < 1:
                fouten.append(f"{plek}: 'aantal' moet een geheel getal van 1 of meer zijn")
            venster = tijd("venster", None, verplicht=True)
            if venster is not None and venster <= 0:
                fouten.append(f"{plek}: 'venster' moet langer dan 0 zijn")
            # Tellen kan alleen bij een toename (som) of een toestand (overgangen 0 -> 1).
            if grootheid and grootheden.soort(grootheid) == grootheden.DOORLOPEND:
                fouten.append(f"{plek}: soort 'aantal' kan alleen bij een toename of een "
                              f"toestand, niet bij '{grootheid}'")
        regel_duur = tijd("duur", 0.0, verplicht=(soort == STIL))
        if soort == STIL and regel_duur <= 0 and "duur" in a:
            fouten.append(f"{plek}: 'duur' moet langer dan 0 zijn")
        # Een camera meldt zich met elke intervalfoto. Is de duur niet langer dan dat
        # interval, dan gaat het alarm tussen twee foto's af.
        for bron in gekozen:
            if isinstance(bron, Camera) and 0 < regel_duur <= bron.foto_interval:
                fouten.append(f"{plek}: 'duur' moet langer zijn dan het foto-interval van "
                              f"camera {bron.id} ({bron.foto_interval:g} s)")
        herstelduur = tijd("herstelduur")

        # --- notificatie ---
        melding = a.get("melding", "herhaald" if "herhaling" in a else "eenmalig")
        if melding not in MELDINGEN:
            fouten.append(f"{plek}: 'melding' moet een van {', '.join(MELDINGEN)} zijn")
        herhaling = tijd("herhaling", None)
        if melding == "herhaald" and "herhaling" not in a:
            fouten.append(f"{plek}: bij melding 'herhaald' hoort een 'herhaling' (de tussenpoos)")
        elif melding in ("uit", "eenmalig") and "herhaling" in a:
            fouten.append(f"{plek}: 'herhaling' heeft alleen betekenis bij melding 'herhaald'")
        elif herhaling is not None and herhaling < 60:
            fouten.append(f"{plek}: 'herhaling' moet minstens 1 minuut zijn")
        herstelmelding = a.get("herstelmelding", True)
        if not isinstance(herstelmelding, bool):
            fouten.append(f"{plek}: 'herstelmelding' moet true of false zijn")
        prioriteit = a.get("prioriteit", "normaal")
        if prioriteit not in PRIORITEITEN:
            fouten.append(f"{plek}: 'prioriteit' moet een van {', '.join(PRIORITEITEN)} zijn")

        # Foto bij de notificatie en acties met de camera (§5.5.3 punt 5 en 6).
        # en met het relais (§5.2.6).
        foto, acties, camera, relais = _camera_bij_alarm(fouten, plek, a, melding, cameras,
                                                         sensoren)

        if len(fouten) == aantal_fouten:
            regels.append(Alarmregel(
                aid, naam, tuple(s.id for s in gekozen), soort, grootheid,
                float(boven) if boven is not None else None,
                float(onder) if onder is not None else None,
                float(herstel) if herstel is not None else None,
                aantal, venster, regel_duur, herstelduur, melding, herhaling, herstelmelding,
                prioriteit, foto, acties, camera, relais))
    return tuple(regels)


def _boot(fouten, data: dict, config_map: str, waarschuwingen: list[str]) -> "BootConfig | None":
    """Controleert het blok [boot] (§5.6, bootpagina). Zonder blok: None (geen bootpagina).

    Een relatief pad van de tekening geldt vanaf de map van brms.toml: de tekening hoort
    bij de configuratie van deze boot en staat ernaast, buiten git.

    Bestaat het bestand niet, dan is dat een waarschuwing en geen fout: een vergeten of
    verplaatste tekening mag de services (en daarmee de alarmen) niet tegenhouden. De
    bootpagina meldt het dan zelf.
    """
    if "boot" not in data:
        return None
    b = _tabel(fouten, data, "boot")
    plek = "[boot]"
    _onbekend(fouten, plek, b, ("tekening",))
    tekening = b.get("tekening")
    if not isinstance(tekening, str) or not tekening:
        fouten.append(f"{plek}: 'tekening' ontbreekt of is geen tekst (de naam van het "
                      f"SVG-bestand naast brms.toml)")
        return None
    if not tekening.lower().endswith(".svg"):
        fouten.append(f"{plek}: 'tekening' moet een SVG-bestand zijn (naam eindigt op .svg)")
        return None
    if not os.path.isabs(tekening):
        tekening = os.path.normpath(os.path.join(config_map, tekening))
    if not os.path.isfile(tekening):
        waarschuwingen.append(f"{plek}: de tekening {tekening} bestaat niet; de bootpagina "
                              f"blijft leeg tot het bestand er staat")
    return BootConfig(tekening)


def controleer(data: dict, config_map: str | None = None) -> Config:
    """Controleert een ingelezen TOML-structuur en zet die om in een Config.

    `config_map` is de map waarin brms.toml staat (voor een relatief pad in [boot]);
    zonder: de standaardmap `config/` in de projectmap.

    Alle fouten worden verzameld en samen gemeld (ConfigFout), zodat in één keer
    zichtbaar is wat er gerepareerd moet worden.
    """
    fouten: list[str] = []
    if config_map is None:
        config_map = os.path.dirname(STANDAARD_PAD)
    _onbekend(fouten, "bovenste niveau", data,
              ("mqtt", "opslag", "web", "boot", "sensor", "melding", "alarm", "camera", "opnames"))

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
    # Per relais: (plek, het veld 'voorwaarde' zoals het in het bestand staat).
    voorwaarden_in: dict[str, tuple[str, object]] = {}
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
                                    "adres", "interval", "bron", "drempel", "omgekeerd",
                                    "stuurboord", "voorwaarde"))
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

        # --- stuurboord: de versnelling die positief wordt als de boot over stuurboord
        # helt (§5.2.5 punt 10). Alleen bij een sensor die de versnellingen levert; nodig
        # voor de afgeleide grootheid 'slagzij'. ---
        stuurboord = s.get("stuurboord")
        if stuurboord is not None:
            if stuurboord not in afleiden.STUURBOORD:
                fouten.append(f"{plek}: 'stuurboord' moet een van "
                              f"{', '.join(repr(w) for w in afleiden.STUURBOORD)} zijn "
                              f"(als tekst, tussen aanhalingstekens), niet {stuurboord!r}")
                stuurboord = None
            elif levert and "slagzij" not in levert:
                fouten.append(f"{plek}: 'stuurboord' hoort alleen bij een sensor die "
                              f"versnellingen levert (type ruuvi), niet bij type {stype}")
                stuurboord = None

        def mist_instelling(g: str) -> str | None:
            """Het veld dat deze sensor mist om de afgeleide grootheid g te leveren (bijv.
            'stuurboord' voor 'slagzij'), of None als er niets ontbreekt."""
            a = afleiden.AFLEIDINGEN.get(g)
            if a is None or a.instelling is None or g not in levert:
                return None
            return a.instelling if {"stuurboord": stuurboord}.get(a.instelling) is None else None

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
                    elif mist_instelling(g):
                        fouten.append(f"{plek}: '{g}' staat in 'grootheden', maar het veld "
                                      f"'{mist_instelling(g)}' ontbreekt")
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
            elif mist_instelling(g):
                fouten.append(f"{plek}: correctie voor '{g}', maar het veld "
                              f"'{mist_instelling(g)}' ontbreekt")
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
            shelly = _shelly(fouten, plek, s, lijst_g, relais=(stype == RELAIS_TYPE))
        else:
            for k in ("adres", "interval", "bron", "drempel", "omgekeerd"):
                if k in s:
                    fouten.append(f"{plek}: '{k}' hoort alleen bij een Shelly")
        # --- voorwaarde: alleen bij een relais (§5.2.6 punt 5). Hij verwijst naar sensoren
        # (ook naar het relais zelf, of naar een sensor verderop in het bestand) en wordt
        # daarom pas na deze lus gecontroleerd. ---
        if stype == RELAIS_TYPE:
            voorwaarden_in[sid] = (plek, s.get("voorwaarde"))
        elif "voorwaarde" in s:
            fouten.append(f"{plek}: 'voorwaarde' hoort alleen bij een relais (type {RELAIS_TYPE})")

        sensoren[sid] = Sensor(sid, stype, snaam if isinstance(snaam, str) else "", correcties,
                               tuple(lijst_g) if lijst_g is not None else None, sleutel, **shelly,
                               stuurboord=stuurboord)

    # --- de voorwaarden van de relais (§5.2.6 punt 5), nu alle sensoren bekend zijn ---
    for sid, (plek, w) in voorwaarden_in.items():
        sensoren[sid] = dataclasses.replace(
            sensoren[sid], voorwaarde=_voorwaarde(fouten, plek, w, sensoren))

    # --- [[camera]] en [opnames] (§5.7) ---
    waarschuwingen: list[str] = []
    cameras = _cameras(fouten, data, sensoren, waarschuwingen)
    opnames = _opnames(fouten, data)

    # --- [boot] (§5.6, bootpagina) ---
    boot = _boot(fouten, data, config_map, waarschuwingen)

    # --- [melding] en [[alarm]] (§5.5.3, §5.5.4); de regels verwijzen naar de sensoren
    # en de camera's ---
    melding = _melding(fouten, data)
    alarmen = _alarmen(fouten, data, sensoren, cameras)

    if fouten:
        raise ConfigFout(fouten)
    return Config(MqttConfig(host, port), OpslagConfig(database), sensoren,
                  WebConfig(web_host, web_port), melding, alarmen, cameras, opnames,
                  boot, tuple(waarschuwingen))
