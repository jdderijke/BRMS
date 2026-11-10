"""Simulatie: een testreeks van gesimuleerde sensordata door de keten sturen
(ARCHITECTURE.md §10.3).

    brms sim kajuit              (ook: meerdere sensoren, of 'all')
    brms sim stop kajuit         (ook: 'all')

Werking:
1. De opdracht `brms sim` waarschuwt, vraagt om akkoord en om de duur, en stuurt dan per
   sensor alleen een startsein naar de readers (de reader van die sensor voert het uit):

       brms/opdracht/simulatie/aan   {"sensor_id": "A4:C1:38:...", "duur": 60}

2. De BLE-reader maakt daarna zelf elke STAP seconden een advertentie aan in het echte
   formaat van die sensor (BTHome, Ruuvi of Victron) en verwerkt die via dezelfde ingang
   als een advertentie van de Bluetooth-adapter. De testdata zelf gaat dus niet over MQTT.
   Een Victron-bericht wordt versleuteld met de sleutel van het apparaat uit de
   configuratie, zodat ook het ontsleutelen in de reader getest wordt.
3. Zolang de simulatie loopt, negeert de reader de echte advertenties van die sensor
   (bypass). Na `duur` seconden stopt de simulatie vanzelf en telt de echte sensor weer
   mee; eerder stoppen kan met brms/opdracht/simulatie/uit.
4. Een Shelly wordt op dezelfde manier gesimuleerd door de Shelly-reader: die maakt bij
   elke poll zelf het antwoord van de Shelly aan (shelly_status) en slaat de echte over.

Wat wordt getest: decoder, grootheden-filter, correctie, bus, opslag (en later de alarmen).
Wat buiten de test blijft: de radio, BlueZ en bleak.

Let op: de gesimuleerde waarden zijn voor de rest van het systeem gewone metingen. Ze
komen onder het ID van de sensor in de database (§10.3 punt 7).

Dit module bevat drie delen:
- de testreeks en het omzetten daarvan naar bytes (zuivere functies);
- de Simulator, die in de BLE-reader en in de Shelly-reader draait;
- de opdracht `brms sim` voor de beheerder.
"""

import asyncio
import json
import logging
import math
import os
import random
import struct
import subprocess
import threading
import time
from dataclasses import dataclass

from brms.config import Config, Sensor
from brms.readers import bthome, ruuvi, victron

log = logging.getLogger(__name__)

TOPIC_AAN = "brms/opdracht/simulatie/aan"
TOPIC_UIT = "brms/opdracht/simulatie/uit"

STANDAARD_DUUR = 600  # seconden; de standaard van 10 minuten (§10.3 punt 2)
MAX_DUUR = 3600       # langer dan een uur simuleren is altijd een vergissing
STAP = 2.0            # seconden tussen twee gesimuleerde advertenties

# Signaalsterkte van een gesimuleerde advertentie (er is geen adapter die hem meet), en de
# ruis daarop: de standaardafwijking in dBm.
RSSI = -60
RSSI_RUIS = 2

# Sensortypen die gesimuleerd kunnen worden, per reader, en de systemd-service van die
# reader.
BLE_TYPES = ("bthome", "ruuvi", *victron.LEVERT)   # victron-bmv, -mppt, -multiplus
SHELLY_TYPES = ("shelly-uni",)
TYPES = BLE_TYPES + SHELLY_TYPES
SERVICE = {**{t: "brms-ble-reader" for t in BLE_TYPES},
           **{t: "brms-shelly-reader" for t in SHELLY_TYPES}}

# Signaalsterkte van de wifi in een gesimuleerd antwoord van een Shelly (dBm).
WIFI_RSSI = -55
# Spanning op de bilgepomp als hij loopt (V), en de ruis op de gemeten spanning.
POMPSPANNING = 12.6
SPANNING_RUIS = 0.05


# =============================================================================
# Testreeks en bytes (zuivere functies, getest in tests/test_simulatie.py)
# =============================================================================

# De testreeks: per grootheid (beginwaarde, eindwaarde). De waarde loopt lineair van begin
# naar eind over de duur van de simulatie (§10.3 punt 5). Gekozen om door de alarmgrenzen
# heen te lopen: vorst, hoge luchtvochtigheid, lege sensorbatterij, een dalende luchtdruk
# en een boot die scheef gaat liggen.
REEKS: dict[str, tuple[float, float]] = {
    "temperatuur": (25.0, -5.0),        # °C
    "vocht": (40.0, 95.0),              # %
    "batterijspanning": (3.0, 2.2),     # V
    "luchtdruk": (1020.0, 980.0),       # hPa   (alleen Ruuvi)
    "versnelling_x": (0, 200),          # mG    (alleen Ruuvi)
    "versnelling_y": (0, -100),         # mG    (alleen Ruuvi)
    "versnelling_z": (1000, 975),       # mG    (alleen Ruuvi)
}


# Ruis op de testreeks: per grootheid de standaardafwijking, in de eenheid van de grootheid.
# Een echte sensor levert nooit een strakke lijn; met wat ruis ziet een gesimuleerde reeks
# er in de grafieken uit als een echte (ook voor demonstraties). De waarden zijn zo gekozen
# dat het verloop van de testreeks goed zichtbaar blijft.
RUIS: dict[str, float] = {
    "temperatuur": 0.15,        # °C
    "vocht": 0.5,               # %
    "batterijspanning": 0.005,  # V
    "luchtdruk": 0.15,          # hPa
    "versnelling_x": 8,         # mG
    "versnelling_y": 8,         # mG
    "versnelling_z": 8,         # mG
}


def waarden(fractie: float, toeval: random.Random | None = None) -> dict[str, float]:
    """De waarden van de testreeks op een punt in de simulatie.

    fractie: 0.0 = begin, 1.0 = einde.
    toeval:  bron van toevalsgetallen voor de ruis (RUIS). Zonder: de strakke testreeks
             zonder ruis, zodat tests exacte waarden kunnen controleren.
    """
    w = {g: begin + (eind - begin) * fractie for g, (begin, eind) in REEKS.items()}
    if toeval is not None:
        for g, afwijking in RUIS.items():
            w[g] += toeval.gauss(0, afwijking)
        # Binnen wat een sensor kan uitzenden blijven: vocht is 0–100 %, en de spanning
        # moet in het bericht passen (Ruuvi: 1,6–3,6 V).
        w["vocht"] = min(100.0, max(0.0, w["vocht"]))
        w["batterijspanning"] = min(3.6, max(1.6, w["batterijspanning"]))
    return w


def bthome_bytes(w: dict[str, float], n: int) -> bytes:
    """Maakt BTHome v2 service data zoals pvvx-firmware die uitzendt (zie readers/bthome.py
    voor de opbouw). n is het volgnummer van de advertentie (pakket-ID)."""
    # pvvx zendt ook een batterijpercentage; BRMS gebruikt het niet, maar de decoder moet
    # het wel overslaan. Hier grof afgeleid uit de spanning (2,0 V = 0 %, 3,0 V = 100 %).
    procent = max(0, min(100, round((w["batterijspanning"] - 2.0) * 100)))
    return (bytes([0x40,                 # BTHome v2, onversleuteld
                   0x00, n & 0xFF,       # pakket-ID
                   0x01, procent])       # batterijpercentage
            + b"\x02" + round(w["temperatuur"] * 100).to_bytes(2, "little", signed=True)
            + b"\x03" + round(w["vocht"] * 100).to_bytes(2, "little")
            + b"\x0c" + round(w["batterijspanning"] * 1000).to_bytes(2, "little"))


def ruuvi_bytes(w: dict[str, float], n: int, mac: str) -> bytes:
    """Maakt een Ruuvi formaat 5-bericht (zie readers/ruuvi.py voor de opbouw).

    n is het volgnummer van de advertentie. De bewegingsteller loopt per advertentie één
    op, zodat de reader per stap een `beweging` van 1 publiceert.
    """
    # Bovenste 11 bits: mV boven 1600; onderste 5 bits: zendvermogen (22 = +4 dBm).
    voeding = ((round(w["batterijspanning"] * 1000) - 1600) << 5) | 22
    return (b"\x05"
            + round(w["temperatuur"] * 200).to_bytes(2, "big", signed=True)   # x 0,005 °C
            + round(w["vocht"] * 400).to_bytes(2, "big")                      # x 0,0025 %
            + (round(w["luchtdruk"] * 100) - 50000).to_bytes(2, "big")        # Pa - 50000
            + round(w["versnelling_x"]).to_bytes(2, "big", signed=True)
            + round(w["versnelling_y"]).to_bytes(2, "big", signed=True)
            + round(w["versnelling_z"]).to_bytes(2, "big", signed=True)
            + voeding.to_bytes(2, "big")
            + bytes([n % 255])                     # bewegingsteller: 0 t/m 254
            + (n % 65535).to_bytes(2, "big")       # volgnummer van de meting
            + bytes.fromhex(mac.replace(":", "")))


# --- Victron (accu, zonnepaneel, walstroom) ---------------------------------------------
#
# Testreeks per sensortype (§10.3 punt 5):
# - victron-bmv:        een accu die leegloopt, steeds sneller;
# - victron-mppt:       een dag zon: het vermogen loopt op tot halverwege en weer terug;
# - victron-multiplus:  de walstroom valt halverwege weg.

VICTRON_REEKS: dict[str, tuple[float, float]] = {
    "spanning": (12.8, 11.6),            # V
    "stroom": (-2.0, -12.0),             # A, negatief = ontladen
    "soc": (100.0, 20.0),                # %
    "verbruikt": (0.0, 160.0),           # Ah
    "resterende_tijd": (1200.0, 60.0),   # min
}
ZON_PIEK = 45.0           # W, het hoogste zonnevermogen (halverwege de simulatie)
LAADSPANNING = 13.0       # V; laadstroom = vermogen / laadspanning

# Ruis (standaardafwijking). Laadtoestand en verbruik krijgen geen ruis: een accumonitor
# telt die, en een tellerstand springt niet heen en weer.
VICTRON_RUIS: dict[str, float] = {
    "spanning": 0.02,          # V
    "stroom": 0.15,            # A
    "resterende_tijd": 5.0,    # min
    "zonnevermogen": 1.5,      # W
}


def victron_waarden(type_: str, fractie: float,
                    toeval: random.Random | None = None) -> dict[str, float]:
    """De waarden van de Victron-testreeks voor dit sensortype op een punt in de simulatie
    (fractie: 0.0 = begin, 1.0 = einde). toeval: bron voor de ruis; zonder: geen ruis."""
    ruis = ((lambda g: toeval.gauss(0, VICTRON_RUIS[g])) if toeval is not None
            else (lambda g: 0.0))
    if type_ == "victron-bmv":
        w = {g: begin + (eind - begin) * fractie for g, (begin, eind) in VICTRON_REEKS.items()}
        for g in ("spanning", "stroom", "resterende_tijd"):
            w[g] += ruis(g)
        w["resterende_tijd"] = max(1.0, w["resterende_tijd"])
        return w
    if type_ == "victron-mppt":
        # Een halve sinus: 0 W aan het begin, de piek halverwege, 0 W aan het einde.
        vermogen = max(0.0, ZON_PIEK * math.sin(math.pi * fractie) + ruis("zonnevermogen"))
        return {"zonnevermogen": vermogen, "laadstroom_zon": vermogen / LAADSPANNING}
    if type_ == "victron-multiplus":
        return {"walstroom": 1.0 if fractie < 0.5 else 0.0}
    raise ValueError(f"sensortype {type_!r} is geen Victron-type")


def _bits(*velden: tuple[int, int]) -> bytes:
    """Pakt (waarde, aantal bits)-paren achter elkaar in, minst significante bit eerst –
    zoals Victron het doet. Negatieve waarden in two's complement."""
    getal, plaats = 0, 0
    for waarde, bits in velden:
        getal |= (waarde & ((1 << bits) - 1)) << plaats
        plaats += bits
    return getal.to_bytes((plaats + 7) // 8, "little")


def victron_bytes(type_: str, w: dict[str, float], n: int, sleutel: str) -> bytes:
    """Maakt Instant Readout manufacturer data zoals een Victron-apparaat die uitzendt:
    een kop, en daarna de meetwaarden, versleuteld met de sleutel van het apparaat.

    n is het volgnummer van de advertentie; het apparaat gebruikt dat als beginwaarde van
    de versleuteling, zodat elk bericht anders versleuteld is.

    De velden die BRMS niet gebruikt (alarm, laadfase, accuspanning van de lader enz.)
    krijgen een vaste, geldige waarde.
    """
    # AES komt uit pycryptodome, dat de Victron-decoder ook gebruikt. Pas hier importeren:
    # alleen de simulatie versleutelt.
    from Crypto.Cipher import AES
    from Crypto.Util import Counter

    if type_ == "victron-bmv":
        soort = 0x02   # accumonitor
        inhoud = _bits((round(w["resterende_tijd"]), 16),     # min
                       (round(w["spanning"] * 100), 16),      # x 0,01 V
                       (0, 16),                               # alarm: geen
                       (0, 16), (3, 2),                       # hulpingang: niet in gebruik
                       (round(w["stroom"] * 1000), 22),       # mA, met teken
                       (round(w["verbruikt"] * 10), 20),      # x 0,1 Ah
                       (round(w["soc"] * 10), 10))            # x 0,1 %
    elif type_ == "victron-mppt":
        soort = 0x01   # zonnelader
        inhoud = _bits((3, 8), (0, 8),                        # laadfase bulk, geen fout
                       (round(LAADSPANNING * 100), 16),       # accuspanning, x 0,01 V
                       (round(w["laadstroom_zon"] * 10), 16),  # x 0,1 A
                       (0, 16),                               # opbrengst vandaag
                       (round(w["zonnevermogen"]), 16),       # W
                       (0x1FF, 9))                            # belastingsuitgang: onbekend
    elif type_ == "victron-multiplus":
        soort = 0x0C   # VE.Bus (MultiPlus)
        # AC-ingang: 0 = ingang 1 actief, 2 = niet aangesloten.
        ac_in = 0 if w["walstroom"] else 2
        inhoud = _bits((3, 8), (0, 8),                        # toestand, geen fout
                       (0, 16), (round(LAADSPANNING * 100), 14),   # accustroom, accuspanning
                       (ac_in, 2),
                       (0, 19), (0, 19),                      # vermogen in en uit
                       (0, 2), (60, 7), (80, 7))              # alarm, temperatuur, laadtoestand
    else:
        raise ValueError(f"sensortype {type_!r} is geen Victron-type")

    sleutel_b = bytes.fromhex(sleutel)
    iv = n & 0xFFFF
    teller = Counter.new(128, initial_value=iv, little_endian=True)
    versleuteld = AES.new(sleutel_b, AES.MODE_CTR, counter=teller).encrypt(inhoud)
    # Kop: Instant Readout (0x10), model, soort apparaat, beginwaarde van de versleuteling,
    # en de eerste byte van de sleutel (daaraan ziet de ontvanger of zijn sleutel past).
    return (b"\x10\x02" + struct.pack("<H", 0xA389) + bytes([soort]) + struct.pack("<H", iv)
            + sleutel_b[:1] + versleuteld)


def advertentie(sensor: Sensor, fractie: float, n: int,
                toeval: random.Random | None = None) -> tuple[dict, dict]:
    """Eén gesimuleerde advertentie voor deze sensor, in de vorm waarin bleak hem aanlevert:
    (service data {UUID: bytes}, manufacturer data {fabrikant-ID: bytes}).

    toeval: bron voor de ruis op de waarden (zie waarden); zonder: geen ruis."""
    if sensor.type in victron.LEVERT:
        w = victron_waarden(sensor.type, fractie, toeval)
        return {}, {victron.FABRIKANT_ID: victron_bytes(sensor.type, w, n, sensor.sleutel)}
    w = waarden(fractie, toeval)
    if sensor.type == "bthome":
        return {bthome.FCD2_UUID: bthome_bytes(w, n)}, {}
    if sensor.type == "ruuvi":
        return {}, {ruuvi.FABRIKANT_ID: ruuvi_bytes(w, n, sensor.id)}
    raise ValueError(f"sensortype {sensor.type!r} kan niet gesimuleerd worden")


# --- Shelly (bilge) ------------------------------------------------------------------
#
# Testreeks voor de bilge (§10.3 punt 5): een lek dat erger wordt.
# - De duur is verdeeld in 10 gelijke perioden. In elke periode loopt de bilgepomp één
#   keer; de beurt wordt steeds langer: van 10 % van de periode aan het begin tot 50 % aan
#   het einde. (Bij 10 minuten: elke minuut een beurt, van 6 s oplopend tot 30 s.)
# - In het laatste vijfde deel van de duur staat ook de hoogwatervlotter aan: de pomp
#   houdt het niet meer bij.

def bilge_toestand(fractie: float) -> dict[str, bool]:
    """De toestand van pomp en hoogwatervlotter op een punt in de simulatie
    (fractie: 0.0 = begin, 1.0 = einde)."""
    return {"bilgepomp": (fractie * 10) % 1 < 0.1 + 0.4 * fractie,
            "bilge": fractie >= 0.8}


def shelly_status(sensor: Sensor, fractie: float, toeval: random.Random | None = None) -> dict:
    """Eén gesimuleerd antwoord van Shelly.GetStatus voor deze Shelly, in de vorm waarin
    de echte Shelly antwoordt (zie readers/shelly_uni.py): voor elke component uit 'bron'
    een onderdeel, en de wifi.

    toeval: bron voor de ruis op spanning en signaalsterkte; zonder: geen ruis."""
    ruis = toeval.gauss if toeval is not None else (lambda gemiddelde, afwijking: 0.0)
    status: dict = {"wifi": {"rssi": WIFI_RSSI + round(ruis(0, RSSI_RUIS))}}
    for grootheid, aan in bilge_toestand(fractie).items():
        component = sensor.bron.get(grootheid)
        if component is None:
            continue
        # Bij 'omgekeerd' meldt de echte Shelly het tegenovergestelde; de reader draait
        # het weer om.
        if grootheid in sensor.omgekeerd:
            aan = not aan
        soort, nummer = component.split(":")
        if soort == "input":
            status[component] = {"id": int(nummer), "state": aan}
        else:
            # Ruim boven de drempel als de pomp loopt, anders (bijna) 0 V.
            hoog = max(POMPSPANNING, 1.5 * (sensor.drempel or 0))
            spanning = (hoog if aan else 0.0) + ruis(0, SPANNING_RUIS)
            status[component] = {"id": int(nummer), "voltage": round(max(0.0, spanning), 2)}
    return status


# =============================================================================
# De Simulator (draait in de BLE-reader en in de Shelly-reader)
# =============================================================================

@dataclass
class _Loop:
    """Eén lopende simulatie."""
    begin: float   # monotone klok
    einde: float   # monotone klok
    n: int = 0     # volgnummer van de volgende advertentie


class Simulator:
    """Houdt bij voor welke sensoren een simulatie loopt en maakt de advertenties aan.

    De opdrachten komen binnen in paho's netwerkthread, het simuleren gebeurt in de
    asyncio-loop van de reader; daarom een lock (zoals bij de Tracer).
    """

    def __init__(self, bus, sensoren: dict[str, Sensor], klok=time.monotonic,
                 types: tuple[str, ...] = BLE_TYPES):
        """sensoren: de sensoren van de reader, {sensor-ID: Sensor}.
        klok: monotone klok (§4.2); te vervangen in tests.
        types: de sensortypen die deze reader kan simuleren."""
        self._bus = bus
        self._sensoren = sensoren
        self._types = types
        self._klok = klok
        self._lock = threading.Lock()
        self._lopend: dict[str, _Loop] = {}
        # Bron voor de ruis op de testreeks; elke start van de reader geeft andere ruis.
        self._toeval = random.Random()

    def start(self) -> None:
        """Luistert naar simulatie-opdrachten op de bus."""
        self._bus.abonneer(TOPIC_AAN, 1, self.bij_opdracht)
        self._bus.abonneer(TOPIC_UIT, 1, self.bij_opdracht)

    # --- opdrachten (draait in paho's thread) ---------------------------------------
    def bij_opdracht(self, topic: str, payload: bytes) -> None:
        try:
            d = json.loads(payload)
            sensor_id = d.get("sensor_id")
            if not isinstance(sensor_id, str):
                raise ValueError("sensor_id ontbreekt of is geen tekst")
        except (ValueError, AttributeError) as e:
            log.warning("ongeldige simulatie-opdracht op %s: %s", topic, e)
            return
        # Een MAC-adres staat in hoofdletters in de configuratie; een vrij gekozen ID
        # (Shelly) staat er zoals het getypt is.
        sensor = self._sensoren.get(sensor_id) or self._sensoren.get(sensor_id.upper())
        if sensor is None or sensor.type not in self._types:
            # Alleen sensoren uit de configuratie (§5.2.1 punt 3), ook bij simulatie. Geen
            # waarschuwing: elke reader krijgt alle opdrachten, ook die voor een sensor
            # van een andere reader.
            log.debug("simulatie-opdracht voor %s is niet voor deze reader", sensor_id)
            return
        sensor_id = sensor.id
        with self._lock:
            if topic == TOPIC_AAN:
                duur = d.get("duur", STANDAARD_DUUR)
                if isinstance(duur, bool) or not isinstance(duur, (int, float)) or duur <= 0:
                    duur = STANDAARD_DUUR
                duur = min(duur, MAX_DUUR)
                nu = self._klok()
                self._lopend[sensor_id] = _Loop(nu, nu + duur)
                log.info("simulatie aan voor %s (%s), %d s; de echte sensor wordt genegeerd",
                         sensor.naam, sensor_id, duur)
            elif self._lopend.pop(sensor_id, None) is not None:
                log.info("simulatie uit voor %s (%s)", sensor.naam, sensor_id)

    # --- simuleren (draait in de asyncio-loop) ---------------------------------------
    def actief(self, sensor_id: str) -> bool:
        """True als voor deze sensor een simulatie loopt; de reader negeert dan de echte
        advertenties van die sensor."""
        with self._lock:
            loop = self._lopend.get(sensor_id)
            if loop is None:
                return False
            # Vangnet: normaal ruimt stappen() een afgelopen simulatie op. Mocht dat niet
            # gebeuren, dan mag de echte sensor toch nooit genegeerd blijven.
            if self._klok() > loop.einde + 3 * STAP:
                del self._lopend[sensor_id]
                log.warning("simulatie voor %s niet netjes afgesloten; opgeruimd", sensor_id)
                return False
            return True

    def stappen(self) -> list[tuple[Sensor, float, int]]:
        """De advertenties die nu aangemaakt moeten worden: (sensor, fractie, volgnummer).

        Is de duur van een simulatie om, dan volgt nog één laatste advertentie met de
        eindwaarden (fractie 1.0) en stopt de simulatie.
        """
        uit = []
        with self._lock:
            nu = self._klok()
            for sensor_id, loop in list(self._lopend.items()):
                sensor = self._sensoren[sensor_id]
                if nu >= loop.einde:
                    fractie = 1.0
                    del self._lopend[sensor_id]
                    log.info("simulatie afgelopen voor %s (%s); echte sensor telt weer mee",
                             sensor.naam, sensor_id)
                else:
                    fractie = (nu - loop.begin) / (loop.einde - loop.begin)
                uit.append((sensor, fractie, loop.n))
                loop.n += 1
        return uit

    def stap(self, sensor_id: str) -> tuple[float, int] | None:
        """Voor een pollende reader (Shelly): (fractie, volgnummer) als voor deze sensor een
        simulatie loopt, anders None. De reader vraagt dit bij elke poll en maakt dan zelf
        het antwoord aan in plaats van de echte sensor te pollen.

        Is de duur om, dan volgt nog één laatste stap met fractie 1.0 en stopt de simulatie.
        """
        with self._lock:
            loop = self._lopend.get(sensor_id)
            if loop is None:
                return None
            nu = self._klok()
            if nu >= loop.einde:
                fractie = 1.0
                del self._lopend[sensor_id]
                sensor = self._sensoren[sensor_id]
                log.info("simulatie afgelopen voor %s (%s); echte sensor telt weer mee",
                         sensor.naam, sensor_id)
            else:
                fractie = (nu - loop.begin) / (loop.einde - loop.begin)
            loop.n += 1
            return fractie, loop.n - 1

    async def draai(self, stop: asyncio.Event, ontvang) -> None:
        """Maakt elke STAP seconden de gesimuleerde advertenties aan en geeft ze aan
        `ontvang` – dezelfde functie waaraan de reader de advertenties van bleak geeft.

        ontvang(adres, service_data, fabrikant_data, rssi, gesimuleerd)
        """
        while not stop.is_set():
            for sensor, fractie, n in self.stappen():
                # Met ruis op de waarden en op de signaalsterkte, zoals bij een echte sensor.
                service_data, fabrikant_data = advertentie(sensor, fractie, n, self._toeval)
                rssi = RSSI + round(self._toeval.gauss(0, RSSI_RUIS))
                ontvang(sensor.id, service_data, fabrikant_data, rssi, True)
            try:
                await asyncio.wait_for(stop.wait(), STAP)
            except asyncio.TimeoutError:
                pass


# =============================================================================
# De opdracht `brms sim ...` (voor de beheerder)
# =============================================================================
#
#     brms sim kajuit                 één sensor
#     brms sim kajuit vooronder       meerdere (ook met komma's: kajuit, vooronder)
#     brms sim all                    alle sensoren die gesimuleerd kunnen worden
#     brms sim stop kajuit            stoppen (ook: brms sim stop all)
#
# De opdracht geeft alleen het startsein en is daarna klaar: de shell is meteen weer vrij,
# bijvoorbeeld voor een tweede `brms sim` voor een andere sensor. De simulatie loopt in de
# reader van de sensor (BLE-reader of Shelly-reader) en stopt vanzelf na de opgegeven duur.

def kies_sensoren(cfg: Config, namen: list[str]) -> list[Sensor]:
    """De sensoren die bij de opgegeven namen horen; ValueError met uitleg als het niet kan.

    - Namen mogen los of met komma's gescheiden zijn: ["kajuit,", "vooronder"].
    - "all" = alle sensoren uit de configuratie die gesimuleerd kunnen worden.
    - Elke sensor hooguit één keer, in de opgegeven volgorde.
    """
    from brms import monitor

    # Komma's zijn alleen scheidingstekens: "kajuit, vooronder" en "kajuit,vooronder".
    namen = [n for deel in namen for n in deel.split(",") if n]
    kan = [s for s in cfg.sensoren.values() if s.type in TYPES]
    if not namen:
        bekend = ", ".join(s.naam for s in kan) or "(geen)"
        raise ValueError(f"geef één of meer sensoren, of 'all'; bekend: {bekend}")
    if any(n.lower() == "all" for n in namen):
        if not kan:
            raise ValueError("er staan geen sensoren in de configuratie die gesimuleerd kunnen worden")
        return kan
    gekozen: list[Sensor] = []
    for sensor in monitor.zoek_sensoren(namen, cfg):   # ValueError bij een onbekende naam
        if sensor.type not in TYPES:
            raise ValueError(f"{sensor.naam}: sensortype {sensor.type} kan niet gesimuleerd "
                             f"worden (wel: {', '.join(TYPES)})")
        if sensor not in gekozen:
            gekozen.append(sensor)
    return gekozen


def vraag_duur(vraag=input) -> int:
    """Vraagt hoe lang de simulatie moet lopen; geeft de duur in seconden.

    Alleen Enter = de standaard van 10 minuten. Bij ongeldige invoer wordt opnieuw gevraagd.
    """
    maximum = MAX_DUUR // 60
    while True:
        antwoord = vraag(f"Hoe lang moet de simulatie lopen, in minuten? "
                         f"[{STANDAARD_DUUR // 60}] ").strip()
        if not antwoord:
            return STANDAARD_DUUR
        try:
            minuten = float(antwoord.replace(",", "."))  # ook 0,5 met een komma
        except ValueError:
            minuten = 0
        if 0 < minuten <= maximum:
            return max(1, round(minuten * 60))
        print(f"Geef een getal groter dan 0 en hooguit {maximum} (minuten).")


def _reader_draait(service: str = "brms-ble-reader") -> bool | None:
    """True/False als systemd weet of deze reader draait; None als dat niet te zeggen is
    (geen systemd, bijv. op de pc)."""
    try:
        r = subprocess.run(["systemctl", "is-active", "--quiet", service], timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.returncode == 0


def _stuur(cfg: Config, topic: str, sensoren: list[Sensor], **inhoud) -> bool:
    """Stuurt per sensor één opdracht naar de readers. False als de broker niet
    bereikbaar is."""
    from brms.bus import Bus

    bus = Bus(f"simuleer-{os.getpid()}", cfg.mqtt.host, cfg.mqtt.port)
    bus.start()
    try:
        # De bus verbindt op de achtergrond; even wachten tot dat gelukt is.
        wacht_tot = time.monotonic() + 5
        while not bus.verbonden and time.monotonic() < wacht_tot:
            time.sleep(0.05)
        if not bus.verbonden:
            print(f"De broker ({cfg.mqtt.host}:{cfg.mqtt.port}) is niet bereikbaar.")
            return False
        gelukt = all([bus.publiceer(topic, json.dumps({"sensor_id": s.id, **inhoud}), qos=1)
                      for s in sensoren])
        time.sleep(0.3)  # paho de tijd geven om de opdrachten te versturen
        return gelukt
    finally:
        bus.stop()


def opdracht(cfg: Config, argumenten: list[str], vraag=input) -> int:
    """`brms sim <sensor ...>` en `brms sim stop <sensor ...>` (§10.3). Exitcode 0 = gelukt."""
    stoppen = bool(argumenten) and argumenten[0].lower() == "stop"
    try:
        sensoren = kies_sensoren(cfg, argumenten[1:] if stoppen else argumenten)
    except ValueError as e:
        print(e)
        print("gebruik: brms sim <sensor ...> | all        brms sim stop <sensor ...> | all")
        return 1
    namen = ", ".join(s.naam for s in sensoren)

    if stoppen:
        if not _stuur(cfg, TOPIC_UIT, sensoren):
            return 1
        print(f"Simulatie gestopt voor: {namen}. De echte sensoren tellen weer mee.")
        return 0

    # Geen controle of de echte sensor zendt (dat kostte een wachttijd): altijd waarschuwen
    # en om akkoord vragen (§10.3 punt 2).
    print(f"Simuleren: {namen}")
    print("Let op: zolang de simulatie loopt, worden de echte sensoren genegeerd (bypass). "
          "De gesimuleerde\nwaarden komen als gewone metingen in de database.")
    try:
        if vraag("Doorgaan? [J/n] ").strip().lower() not in ("", "j", "ja"):
            print("Afgebroken; er is niets gesimuleerd.")
            return 1
        duur = vraag_duur(vraag)
    except (KeyboardInterrupt, EOFError):
        # Ctrl-c, of geen toetsenbord om de vragen te beantwoorden.
        print("\nAfgebroken; er is niets gesimuleerd.")
        return 1

    if not _stuur(cfg, TOPIC_AAN, sensoren, duur=duur):
        return 1
    einde = time.strftime("%H:%M:%S", time.localtime(time.time() + duur))
    print(f"Simulatie gestart ({duur // 60}:{duur % 60:02d} min); loopt vanzelf af om {einde}.")
    print(f"  meekijken:      brms monitor {sensoren[0].naam}")
    print(f"  eerder stoppen: brms sim stop {'all' if len(sensoren) > 1 else sensoren[0].naam}")
    # De opdracht is verstuurd; draait de reader van een sensor niet, dan voert niemand
    # hem uit.
    gelukt = 0
    for service in sorted({SERVICE[s.type] for s in sensoren}):
        if _reader_draait(service) is False:
            wie = ", ".join(s.naam for s in sensoren if SERVICE[s.type] == service)
            print(f"LET OP: {service} draait niet (brms start); voor {wie} wordt nu niets "
                  f"gesimuleerd.")
            gelukt = 1
    return gelukt
