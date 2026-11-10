"""Simulatie: een testreeks van gesimuleerde sensordata door de keten sturen
(ARCHITECTURE.md §10.3).

    brms sim kajuit              (ook: meerdere sensoren, of 'all')
    brms sim stop kajuit         (ook: 'all')

Werking:
1. De opdracht `brms sim` waarschuwt, vraagt om akkoord en om de duur, en stuurt dan per
   sensor alleen een startsein naar de BLE-reader:

       brms/opdracht/simulatie/aan   {"sensor_id": "A4:C1:38:...", "duur": 60}

2. De BLE-reader maakt daarna zelf elke STAP seconden een advertentie aan in het echte
   formaat van die sensor (BTHome of Ruuvi) en verwerkt die via dezelfde ingang als een
   advertentie van de Bluetooth-adapter. De testdata zelf gaat dus niet over MQTT.
3. Zolang de simulatie loopt, negeert de reader de echte advertenties van die sensor
   (bypass). Na `duur` seconden stopt de simulatie vanzelf en telt de echte sensor weer
   mee; eerder stoppen kan met brms/opdracht/simulatie/uit.

Wat wordt getest: decoder, grootheden-filter, correctie, bus, opslag (en later de alarmen).
Wat buiten de test blijft: de radio, BlueZ en bleak.

Let op: de gesimuleerde waarden zijn voor de rest van het systeem gewone metingen. Ze
komen onder het ID van de sensor in de database (§10.3 punt 7).

Dit module bevat drie delen:
- de testreeks en het omzetten daarvan naar bytes (zuivere functies);
- de Simulator, die in de BLE-reader draait;
- de opdracht `brms sim` voor de beheerder.
"""

import asyncio
import json
import logging
import os
import random
import subprocess
import threading
import time
from dataclasses import dataclass

from brms.config import Config, Sensor
from brms.readers import bthome, ruuvi

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

# Sensortypen die gesimuleerd kunnen worden.
TYPES = ("bthome", "ruuvi")


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


def advertentie(sensor: Sensor, fractie: float, n: int,
                toeval: random.Random | None = None) -> tuple[dict, dict]:
    """Eén gesimuleerde advertentie voor deze sensor, in de vorm waarin bleak hem aanlevert:
    (service data {UUID: bytes}, manufacturer data {fabrikant-ID: bytes}).

    toeval: bron voor de ruis op de waarden (zie waarden); zonder: geen ruis."""
    w = waarden(fractie, toeval)
    if sensor.type == "bthome":
        return {bthome.FCD2_UUID: bthome_bytes(w, n)}, {}
    if sensor.type == "ruuvi":
        return {}, {ruuvi.FABRIKANT_ID: ruuvi_bytes(w, n, sensor.id)}
    raise ValueError(f"sensortype {sensor.type!r} kan niet gesimuleerd worden")


# =============================================================================
# De Simulator (draait in de BLE-reader)
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

    def __init__(self, bus, sensoren: dict[str, Sensor], klok=time.monotonic):
        """sensoren: de sensoren van de reader, {sensor-ID: Sensor}.
        klok: monotone klok (§4.2); te vervangen in tests."""
        self._bus = bus
        self._sensoren = sensoren
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
        sensor_id = sensor_id.upper()
        sensor = self._sensoren.get(sensor_id)
        if sensor is None or sensor.type not in TYPES:
            # Alleen sensoren uit de configuratie (§5.2.1 punt 3), ook bij simulatie.
            log.warning("simulatie-opdracht voor onbekende sensor %s genegeerd", sensor_id)
            return
        with self._lock:
            if topic == TOPIC_AAN:
                duur = d.get("duur", STANDAARD_DUUR)
                if isinstance(duur, bool) or not isinstance(duur, (int, float)) or duur <= 0:
                    duur = STANDAARD_DUUR
                duur = min(duur, MAX_DUUR)
                nu = self._klok()
                self._lopend[sensor_id] = _Loop(nu, nu + duur)
                log.info("simulatie aan voor %s (%s), %d s; echte advertenties worden genegeerd",
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
# BLE-reader en stopt vanzelf na de opgegeven duur.

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


def _reader_draait() -> bool | None:
    """True/False als systemd weet of de BLE-reader draait; None als dat niet te zeggen is
    (geen systemd, bijv. op de pc)."""
    try:
        r = subprocess.run(["systemctl", "is-active", "--quiet", "brms-ble-reader"], timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.returncode == 0


def _stuur(cfg: Config, topic: str, sensoren: list[Sensor], **inhoud) -> bool:
    """Stuurt per sensor één opdracht naar de BLE-reader. False als de broker niet
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
    if _reader_draait() is False:
        # De opdracht is verstuurd, maar er is niemand die hem uitvoert.
        print("LET OP: de BLE-reader draait niet (brms start); er wordt nu niets gesimuleerd.")
        return 1
    return 0
