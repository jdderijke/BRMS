"""Simulatie: een testreeks van gesimuleerde sensordata door de keten sturen
(ARCHITECTURE.md §10.3).

    brms sim kajuit              (ook: meerdere sensoren, of 'all')
    brms sim stop kajuit         (ook: 'all')

Er zijn twee soorten (§10.3 punt 2):
- een TEST: de gebruiker geeft een duur op (hooguit 60 minuten). De testreeks loopt binnen
  die duur twee keer heen en weer, dwars door de alarmgrenzen, en stopt vanzelf. Bedoeld
  om alarmen te testen;
- een DEMO: de gebruiker geeft geen duur op (alleen Enter). De waarden schommelen rond
  een normale waarde, met een dag- en nachtritme, elke sensor onafhankelijk van de
  andere, tot `brms sim stop`. Bedoeld om te laten zien hoe BRMS eruitziet met sensoren
  die er (nog) niet zijn; er gaat in principe geen alarm af.

Werking:
1. De opdracht `brms sim` waarschuwt, vraagt om akkoord en om de duur, en stuurt dan per
   sensor alleen een startsein naar de readers (de reader van die sensor voert het uit):

       brms/opdracht/simulatie/aan   {"sensor_id": "A4:C1:38:...", "duur": 60}
       brms/opdracht/simulatie/aan   {"sensor_id": "A4:C1:38:...", "demo": true}

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

5. Elke reader meldt op de bus welke sensoren hij simuleert (brms/simulatie/<dienst>);
   het web toont daarmee bovenaan elke pagina "Actief", "Simulatie" of "Demo".

Dit module bevat vier delen:
- de testreeks en het omzetten daarvan naar bytes (zuivere functies);
- de demo: waarden rond normaal, met dag- en nachtritme (klasse Demo);
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
# Hierop meldt elke reader welke sensoren hij simuleert: brms/simulatie/<dienst>, bijv.
# brms/simulatie/ble-reader (§10.3 punt 10). Bij zijn start, bij elke verandering en
# daarna elke MELD_INTERVAL seconden: er is geen retain op de bus (§3.2 punt 2), dus wie
# later begint te luisteren (het web na een herstart) hoort het binnen die tijd.
TOPIC_STAND = "brms/simulatie"
MELD_INTERVAL = 30.0
# De twee soorten simulatie, zoals ze in de melding staan.
TEST = "test"
DEMO = "demo"

STANDAARD_DUUR = 600  # seconden; de standaard van 10 minuten (§10.3 punt 2)
MAX_DUUR = 3600       # een test van langer dan een uur is altijd een vergissing
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
# Hoe vaak de testreeks binnen de duur van een simulatie heen en weer doorlopen wordt
# (§10.3 punt 5): van begin naar eind en weer terug. Door het teruglopen is ook het
# herstellen van een alarm te testen.
RONDES = 2


def verloop(voortgang: float) -> float:
    """Zet de voortgang van een simulatie (0.0 = start, 1.0 = duur om) om in de plaats in
    de testreeks (fractie: 0.0 = beginwaarde, 1.0 = eindwaarde).

    De reeks wordt RONDES keer heen en weer doorlopen, in een zaagtand: bij twee rondes
    is de fractie 0 → 1 → 0 → 1 → 0. Aan het einde staat alles dus weer op de beginwaarde.
    """
    in_ronde = (min(max(voortgang, 0.0), 1.0) * RONDES) % 1.0
    return 2 * in_ronde if in_ronde < 0.5 else 2 * (1 - in_ronde)


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
    op, zodat de reader per stap een `beweging` van 1 publiceert. In een demo staat de
    stand van de teller in w["bewegingsteller"]: daar beweegt de boot maar af en toe.
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
            + bytes([int(w.get("bewegingsteller", n)) % 255])   # bewegingsteller: 0 t/m 254
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


def advertentie(sensor: Sensor, fractie: float | dict, n: int,
                toeval: random.Random | None = None) -> tuple[dict, dict]:
    """Eén gesimuleerde advertentie voor deze sensor, in de vorm waarin bleak hem aanlevert:
    (service data {UUID: bytes}, manufacturer data {fabrikant-ID: bytes}).

    fractie: bij een test de plaats in de testreeks (0.0 = begin, 1.0 = einde); bij een
             demo de waarden zelf, als dict (van Demo.waarden; de ruis zit er al in).
    toeval:  bron voor de ruis op de waarden van de testreeks (zie waarden); zonder: geen
             ruis."""
    demo = isinstance(fractie, dict)
    if sensor.type in victron.LEVERT:
        w = fractie if demo else victron_waarden(sensor.type, fractie, toeval)
        return {}, {victron.FABRIKANT_ID: victron_bytes(sensor.type, w, n, sensor.sleutel)}
    w = fractie if demo else waarden(fractie, toeval)
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


def shelly_status(sensor: Sensor, fractie: float | dict,
                  toeval: random.Random | None = None) -> dict:
    """Eén gesimuleerd antwoord van Shelly.GetStatus voor deze Shelly, in de vorm waarin
    de echte Shelly antwoordt (zie readers/shelly_uni.py): voor elke component uit 'bron'
    een onderdeel, en de wifi.

    fractie: bij een test de plaats in de testreeks (0.0 = begin, 1.0 = einde); bij een
             demo de toestand van pomp en vlotter zelf, als dict (van Demo.bilge).
    toeval:  bron voor de ruis op spanning en signaalsterkte; zonder: geen ruis."""
    ruis = toeval.gauss if toeval is not None else (lambda gemiddelde, afwijking: 0.0)
    status: dict = {"wifi": {"rssi": WIFI_RSSI + round(ruis(0, RSSI_RUIS))}}
    toestand = fractie if isinstance(fractie, dict) else bilge_toestand(fractie)
    for grootheid, aan in toestand.items():
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
# De demo: waarden rond normaal, met dag- en nachtritme (§10.3 punt 5a)
# =============================================================================
#
# Een demo loopt door tot de gebruiker hem stopt. Er is dus geen begin en einde waartussen
# een reeks kan lopen. In plaats daarvan is elke waarde de som van:
#   - een normale waarde voor een boot in de winter (DEMO_NORMAAL);
#   - een vaste afwijking per sensor (de ene hut is wat warmer dan de andere);
#   - het dag- en nachtritme, volgens de echte klok (dagritme, daglicht);
#   - een langzame willekeurige afwijking, per sensor en per grootheid apart (_Zwerver);
#   - dezelfde ruis als in de testreeks (RUIS, VICTRON_RUIS).
# Elke sensor heeft zijn eigen bron van toeval: twee sensoren tekenen nooit dezelfde lijn.
# De waarden blijven binnen normale grenzen: een demo laat in principe geen alarm afgaan.

# Per grootheid: (normale waarde, grootste vaste afwijking per sensor, uitslag van het
# dagritme, spreiding van de langzame afwijking, traagheid daarvan in seconden).
# Uitslag van het dagritme: zoveel boven normaal op het warmste moment (15 uur) en zoveel
# eronder op het koudste (6 uur). Negatief = loopt tegen de temperatuur in (vocht).
DEMO_NORMAAL: dict[str, tuple[float, float, float, float, float]] = {
    "temperatuur": (8.0, 2.0, 2.5, 0.8, 2 * 3600),          # °C
    "vocht": (70.0, 5.0, -6.0, 3.0, 2 * 3600),              # %
    "batterijspanning": (2.95, 0.05, 0.02, 0.0, 3600),      # V; iets hoger als het warmer is
    "luchtdruk": (1013.0, 0.0, 0.0, 8.0, 24 * 3600),        # hPa; het weer, over dagen
    "versnelling_x": (0.0, 20.0, 0.0, 15.0, 600),           # mG; de boot beweegt wat
    "versnelling_y": (0.0, 20.0, 0.0, 15.0, 600),           # mG
    "versnelling_z": (1000.0, 0.0, 0.0, 5.0, 600),          # mG
}
# Vocht blijft in een demo tussen deze grenzen (%).
DEMO_VOCHT = (30.0, 90.0)
# De Ruuvi telt een beweging gemiddeld eens per zoveel seconden.
DEMO_BEWEGING = 600.0

# Accu (victron-bmv): een accu van DEMO_CAPACITEIT Ah met een klein vast verbruik, die
# overdag door de zon wordt bijgeladen. Over een etmaal is dat ongeveer in evenwicht.
DEMO_CAPACITEIT = 200.0     # Ah
DEMO_VERBRUIK = -0.5        # A, dag en nacht
DEMO_ZONSTROOM = 2.4        # A extra op het lichtste moment van de dag
DEMO_SOC = (50.0, 100.0)    # % ; de laadtoestand blijft hiertussen
DEMO_TIJD_MAX = 14400.0     # min; "resterende tijd" als de accu niet ontlaadt (10 dagen)
# Zon (victron-mppt): het aandeel van de piek dat door de bewolking komt: (normaal,
# spreiding, traagheid in seconden); het blijft tussen 5 % en 100 %.
DEMO_BEWOLKING = (0.65, 0.25, 1800)
# Bilge (Shelly): de pomp loopt af en toe kort. De eerste keer vrij snel na de start, zodat
# er wat te zien is; daarna met een pauze van uren. De hoogwatervlotter blijft uit.
DEMO_POMP_EERSTE = (120.0, 600.0)        # s na de start: tussen 2 en 10 minuten
DEMO_POMP_PAUZE = (3600.0, 3 * 3600.0)   # s tussen twee beurten
DEMO_POMP_BEURT = (10.0, 25.0)           # s dat de pomp loopt


def dagritme(uur: float) -> float:
    """Het dag- en nachtritme van de temperatuur: -1.0 op het koudste moment (6 uur),
    +1.0 op het warmste (15 uur), vloeiend daartussen. uur: plaatselijke tijd, 0–24.

    Opwarmen gaat sneller (6 → 15 uur, 9 uur) dan afkoelen (15 → 6 uur, 15 uur), zoals
    buiten; daarom twee halve cosinussen van ongelijke lengte."""
    uur %= 24
    if 6 <= uur < 15:
        return -math.cos(math.pi * (uur - 6) / 9)
    return math.cos(math.pi * ((uur - 15) % 24) / 15)


def daglicht(uur: float) -> float:
    """Hoeveel zon er is: 0.0 in het donker, 1.0 midden op de dag. Een winterdag: licht
    van 8.30 tot 16.30 uur, de piek om 12.30 uur. uur: plaatselijke tijd, 0–24."""
    uur %= 24
    return math.sin(math.pi * (uur - 8.5) / 8) if 8.5 < uur < 16.5 else 0.0


class _Zwerver:
    """Een waarde die langzaam en willekeurig rond nul zwerft en er vanzelf naar terugkeert
    (een Ornstein-Uhlenbeck-proces).

    spreiding:  de standaardafwijking op de lange duur;
    traagheid:  na ongeveer zoveel seconden is de waarde "vergeten" waar hij was.
    De stap is exact voor elke tijdsduur: het maakt niet uit of er elke 2 s (Bluetooth) of
    elke minuut (Shelly) een waarde gevraagd wordt.
    """

    def __init__(self, spreiding: float, traagheid: float, toeval: random.Random):
        self._spreiding, self._traagheid, self._toeval = spreiding, traagheid, toeval
        # Ergens binnen de spreiding beginnen, niet precies op normaal.
        self.x = toeval.gauss(0, spreiding)

    def stap(self, dt: float) -> float:
        """Zet de waarde dt seconden verder en geeft hem terug."""
        rest = math.exp(-max(0.0, dt) / self._traagheid)
        self.x = self.x * rest + self._toeval.gauss(0, self._spreiding * math.sqrt(1 - rest * rest))
        return self.x


class Demo:
    """De demo van één sensor: onthoudt de langzame afwijkingen en tellers, en geeft op
    verzoek de waarden van dit moment (§10.3 punt 5a).

    Wordt alleen gebruikt binnen het lock van de Simulator.
    """

    def __init__(self, type_: str, nu: float, toeval: random.Random | None = None):
        """type_: het sensortype; nu: monotone klok; toeval: eigen bron van toeval voor
        deze sensor (te vervangen in tests)."""
        self._type = type_
        self._toeval = t = toeval or random.Random()
        self._vorige = nu
        # Per grootheid de vaste afwijking van deze sensor en de langzame afwijking.
        self._vast = {g: t.uniform(-v[1], v[1]) for g, v in DEMO_NORMAAL.items()}
        self._zwerf = {g: _Zwerver(v[3], v[4], t) for g, v in DEMO_NORMAAL.items()}
        self._zwerf["stroom"] = _Zwerver(0.25, 1800, t)
        self._zwerf["bewolking"] = _Zwerver(DEMO_BEWOLKING[1], DEMO_BEWOLKING[2], t)
        self._soc = t.uniform(80.0, 95.0)      # laadtoestand van de accu, %
        self._bewegingen = 0                   # stand van de bewegingsteller (Ruuvi)
        # Bilgepomp: het tijdstip (monotone klok) van de volgende beurt, en tot wanneer
        # een lopende beurt duurt (None = de pomp staat uit).
        self._pomp_aan = nu + t.uniform(*DEMO_POMP_EERSTE)
        self._pomp_tot: float | None = None

    def waarden(self, nu: float, uur: float) -> dict[str, float]:
        """De waarden van dit moment voor een Bluetooth-sensor, in de vorm die
        bthome_bytes, ruuvi_bytes en victron_bytes verwachten.

        nu: monotone klok (voor de verstreken tijd); uur: plaatselijke tijd, 0–24."""
        dt, self._vorige = max(0.0, nu - self._vorige), nu
        t = self._toeval
        if self._type == "victron-bmv":
            stroom = (DEMO_VERBRUIK + DEMO_ZONSTROOM * daglicht(uur)
                      + self._zwerf["stroom"].stap(dt))
            if self._soc >= DEMO_SOC[1] and stroom > 0:
                stroom = 0.05      # de accu is vol: er loopt alleen nog een druppellading
            # De accumonitor telt de stroom: Ah erbij of eraf, omgerekend naar procenten.
            self._soc = min(DEMO_SOC[1], max(DEMO_SOC[0],
                            self._soc + stroom * dt / 3600 / DEMO_CAPACITEIT * 100))
            stroom += t.gauss(0, VICTRON_RUIS["stroom"])
            return {
                # Een vollere accu en een accu die geladen wordt, hebben een hogere spanning.
                "spanning": 11.9 + 0.008 * self._soc + 0.04 * stroom
                            + t.gauss(0, VICTRON_RUIS["spanning"]),
                "stroom": stroom,
                "soc": self._soc,
                "verbruikt": (100 - self._soc) / 100 * DEMO_CAPACITEIT,
                # Alleen bij ontladen is er een resterende tijd: de inhoud gedeeld door de stroom.
                "resterende_tijd": (min(DEMO_TIJD_MAX, self._soc / 100 * DEMO_CAPACITEIT
                                        / -stroom * 60)
                                    if stroom < -0.05 else DEMO_TIJD_MAX),
            }
        if self._type == "victron-mppt":
            licht = daglicht(uur)
            door = min(1.0, max(0.05, DEMO_BEWOLKING[0] + self._zwerf["bewolking"].stap(dt)))
            # In het donker precies 0 W: ruis zou daar een zonnetje in de nacht geven.
            vermogen = (max(0.0, ZON_PIEK * licht * door + t.gauss(0, VICTRON_RUIS["zonnevermogen"]))
                        if licht > 0 else 0.0)
            return {"zonnevermogen": vermogen, "laadstroom_zon": vermogen / LAADSPANNING}
        if self._type == "victron-multiplus":
            return {"walstroom": 1.0}      # de walstroom blijft aan
        # BTHome en Ruuvi.
        dag = dagritme(uur)
        w = {g: normaal + self._vast[g] + uitslag * dag + self._zwerf[g].stap(dt)
                + t.gauss(0, RUIS[g])
             for g, (normaal, _, uitslag, _, _) in DEMO_NORMAAL.items()}
        w["vocht"] = min(DEMO_VOCHT[1], max(DEMO_VOCHT[0], w["vocht"]))
        w["batterijspanning"] = min(3.6, max(1.6, w["batterijspanning"]))
        # Af en toe een beweging: de kans in deze stap volgt uit de verstreken tijd.
        if t.random() < dt / DEMO_BEWEGING:
            self._bewegingen += 1
        w["bewegingsteller"] = self._bewegingen
        return w

    def bilge(self, nu: float) -> dict[str, bool]:
        """De toestand van bilgepomp en hoogwatervlotter op dit moment (nu: monotone klok),
        in de vorm van bilge_toestand.

        Een beurt is altijd bij minstens één poll te zien: de pomp gaat pas uit bij de
        eerste poll ná het einde van de beurt."""
        if self._pomp_tot is not None and nu >= self._pomp_tot:
            self._pomp_tot = None
            self._pomp_aan = nu + self._toeval.uniform(*DEMO_POMP_PAUZE)
        elif self._pomp_tot is None and nu >= self._pomp_aan:
            self._pomp_tot = nu + self._toeval.uniform(*DEMO_POMP_BEURT)
        return {"bilgepomp": self._pomp_tot is not None, "bilge": False}


# =============================================================================
# De Simulator (draait in de BLE-reader en in de Shelly-reader)
# =============================================================================

@dataclass
class _Loop:
    """Eén lopende simulatie."""
    begin: float          # monotone klok
    einde: float | None   # monotone klok; None = een demo, loopt tot hij gestopt wordt
    n: int = 0            # volgnummer van de volgende advertentie
    demo: Demo | None = None      # de demo van deze sensor (alleen bij een demo)
    einde_ts: int | None = None   # het einde als Unix-tijd, voor de melding op de bus


class Simulator:
    """Houdt bij voor welke sensoren een simulatie loopt en maakt de advertenties aan.

    De opdrachten komen binnen in paho's netwerkthread, het simuleren gebeurt in de
    asyncio-loop van de reader; daarom een lock (zoals bij de Tracer).
    """

    def __init__(self, bus, sensoren: dict[str, Sensor], klok=time.monotonic,
                 types: tuple[str, ...] = BLE_TYPES, dienst: str = "ble-reader",
                 tijd=time.time):
        """sensoren: de sensoren van de reader, {sensor-ID: Sensor}.
        klok: monotone klok (§4.2); te vervangen in tests.
        types: de sensortypen die deze reader kan simuleren.
        dienst: de naam van de reader, voor de melding op de bus (TOPIC_STAND).
        tijd: de gewone klok (Unix-seconden): voor het dag- en nachtritme van een demo en
              voor de tijdstippen in de melding; te vervangen in tests."""
        self._bus = bus
        self._sensoren = sensoren
        self._types = types
        self._klok = klok
        self._dienst = dienst
        self._tijd = tijd
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
            nu = self._klok()
            if topic == TOPIC_AAN and d.get("demo") is True:
                # Een demo: geen einde; loopt tot `brms sim stop` of tot de reader herstart.
                self._lopend[sensor_id] = _Loop(nu, None, demo=Demo(sensor.type, nu))
                log.info("demo aan voor %s (%s); de echte sensor wordt genegeerd tot "
                         "'brms sim stop'", sensor.naam, sensor_id)
            elif topic == TOPIC_AAN:
                duur = d.get("duur", STANDAARD_DUUR)
                if isinstance(duur, bool) or not isinstance(duur, (int, float)) or duur <= 0:
                    duur = STANDAARD_DUUR
                duur = min(duur, MAX_DUUR)
                self._lopend[sensor_id] = _Loop(nu, nu + duur,
                                                einde_ts=int(self._tijd() + duur))
                log.info("simulatie aan voor %s (%s), %d s; de echte sensor wordt genegeerd",
                         sensor.naam, sensor_id, duur)
            elif self._lopend.pop(sensor_id, None) is not None:
                log.info("simulatie uit voor %s (%s)", sensor.naam, sensor_id)
        # Het web meteen laten weten wat er nu gesimuleerd wordt (buiten het lock).
        self.meld()

    # --- melden wat er gesimuleerd wordt (§10.3 punt 10) --------------------------------
    def stand(self) -> list[dict]:
        """De lopende simulaties: per sensor {"sensor_id", "soort" (TEST of DEMO), "einde"
        (Unix-seconden; None bij een demo)}."""
        with self._lock:
            return [{"sensor_id": sensor_id, "soort": DEMO if loop.einde is None else TEST,
                     "einde": loop.einde_ts}
                    for sensor_id, loop in self._lopend.items()]

    def meld(self) -> None:
        """Zet op de bus welke sensoren deze reader nu simuleert (ook als dat er geen zijn:
        zo ziet het web dat een simulatie gestopt is)."""
        self._bus.publiceer(f"{TOPIC_STAND}/{self._dienst}", json.dumps(
            {"ts": int(self._tijd()), "dienst": self._dienst, "sensoren": self.stand()}), qos=1)

    async def meld_steeds(self, stop: asyncio.Event) -> None:
        """Meldt de stand bij de start van de reader en daarna elke MELD_INTERVAL seconden,
        tot `stop` gezet wordt. De reader start dit als taak."""
        while not stop.is_set():
            self.meld()
            try:
                await asyncio.wait_for(stop.wait(), MELD_INTERVAL)
            except asyncio.TimeoutError:
                pass

    def _uur(self) -> float:
        """De plaatselijke tijd als uur van de dag (0–24), voor het ritme van een demo."""
        t = time.localtime(self._tijd())
        return t.tm_hour + t.tm_min / 60 + t.tm_sec / 3600

    # --- simuleren (draait in de asyncio-loop) ---------------------------------------
    def actief(self, sensor_id: str) -> bool:
        """True als voor deze sensor een simulatie loopt; de reader negeert dan de echte
        advertenties van die sensor."""
        with self._lock:
            loop = self._lopend.get(sensor_id)
            if loop is None:
                return False
            if loop.einde is None:
                return True      # een demo heeft geen einde
            # Vangnet: normaal ruimt stappen() een afgelopen simulatie op. Mocht dat niet
            # gebeuren, dan mag de echte sensor toch nooit genegeerd blijven.
            if self._klok() > loop.einde + 3 * STAP:
                del self._lopend[sensor_id]
                log.warning("simulatie voor %s niet netjes afgesloten; opgeruimd", sensor_id)
                return False
            return True

    def stappen(self) -> list[tuple[Sensor, float | dict, int]]:
        """De advertenties die nu aangemaakt moeten worden: (sensor, fractie, volgnummer).

        Bij een test loopt de fractie binnen de duur heen en weer (zie verloop). Is de
        duur om, dan volgt nog één laatste advertentie, weer met de beginwaarden (fractie
        0.0), en stopt de simulatie. Bij een demo staan op de plaats van de fractie de
        waarden zelf (een dict, van Demo.waarden); zie advertentie().
        """
        uit = []
        afgelopen = False
        with self._lock:
            nu = self._klok()
            for sensor_id, loop in list(self._lopend.items()):
                sensor = self._sensoren[sensor_id]
                if loop.demo is not None:
                    fractie = loop.demo.waarden(nu, self._uur())
                elif nu >= loop.einde:
                    fractie = verloop(1.0)
                    del self._lopend[sensor_id]
                    afgelopen = True
                    log.info("simulatie afgelopen voor %s (%s); echte sensor telt weer mee",
                             sensor.naam, sensor_id)
                else:
                    fractie = verloop((nu - loop.begin) / (loop.einde - loop.begin))
                uit.append((sensor, fractie, loop.n))
                loop.n += 1
        if afgelopen:
            self.meld()
        return uit

    def stap(self, sensor_id: str) -> tuple[float | dict, int] | None:
        """Voor een pollende reader (Shelly): (fractie, volgnummer) als voor deze sensor een
        simulatie loopt, anders None. De reader vraagt dit bij elke poll en maakt dan zelf
        het antwoord aan in plaats van de echte sensor te pollen.

        Bij een test loopt de fractie binnen de duur heen en weer (zie verloop). Is de
        duur om, dan volgt nog één laatste stap, weer met fractie 0.0, en stopt de
        simulatie. Bij een demo staat op de plaats van de fractie de toestand van pomp en
        vlotter zelf (een dict, van Demo.bilge); zie shelly_status().
        """
        afgelopen = False
        with self._lock:
            loop = self._lopend.get(sensor_id)
            if loop is None:
                return None
            nu = self._klok()
            if loop.demo is not None:
                fractie = loop.demo.bilge(nu)
            elif nu >= loop.einde:
                fractie = verloop(1.0)
                del self._lopend[sensor_id]
                afgelopen = True
                sensor = self._sensoren[sensor_id]
                log.info("simulatie afgelopen voor %s (%s); echte sensor telt weer mee",
                         sensor.naam, sensor_id)
            else:
                fractie = verloop((nu - loop.begin) / (loop.einde - loop.begin))
            loop.n += 1
            uit = fractie, loop.n - 1
        if afgelopen:
            self.meld()
        return uit

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
# reader van de sensor (BLE-reader of Shelly-reader). Een test stopt vanzelf na de
# opgegeven duur; een demo (geen duur opgegeven) loopt tot `brms sim stop`.

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


def vraag_duur(vraag=input) -> int | None:
    """Vraagt hoe lang de simulatie moet lopen; geeft de duur in seconden, of None voor
    een demo.

    Een getal = een test van zoveel minuten (§10.3 punt 2). Alleen Enter = een demo, die
    doorloopt tot `brms sim stop`. Bij ongeldige invoer wordt opnieuw gevraagd.
    """
    maximum = MAX_DUUR // 60
    print(f"Hoe lang moet de simulatie lopen?\n"
          f"  een getal    = test: de testreeks loopt in zoveel minuten twee keer heen en "
          f"weer (hooguit {maximum})\n"
          f"  alleen Enter = demo: waarden rond normaal, loopt door tot 'brms sim stop'")
    while True:
        antwoord = vraag("Minuten, of Enter voor een demo: ").strip()
        if not antwoord:
            return None
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

    stop = f"brms sim stop {'all' if len(sensoren) > 1 else sensoren[0].naam}"
    if duur is None:
        # Een demo: geen duur, wel het kenmerk "demo" (zie Simulator.bij_opdracht).
        if not _stuur(cfg, TOPIC_AAN, sensoren, demo=True):
            return 1
        print("Demo gestart; loopt door tot je hem stopt (of tot BRMS herstart).")
        print(f"  meekijken: brms monitor {sensoren[0].naam}")
        print(f"  stoppen:   {stop}")
    else:
        if not _stuur(cfg, TOPIC_AAN, sensoren, duur=duur):
            return 1
        einde = time.strftime("%H:%M:%S", time.localtime(time.time() + duur))
        print(f"Simulatie gestart ({duur // 60}:{duur % 60:02d} min); loopt vanzelf af om {einde}.")
        print(f"  meekijken:      brms monitor {sensoren[0].naam}")
        print(f"  eerder stoppen: {stop}")
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
