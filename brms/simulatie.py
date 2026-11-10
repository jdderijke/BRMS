"""Simulatie: een testreeks van gesimuleerde sensordata door de keten sturen
(ARCHITECTURE.md §10.3).

    brms simuleer kajuit

Werking:
1. De opdracht `brms simuleer` vraagt (zo nodig) om bevestiging en om de duur, en stuurt
   dan alleen een startsein naar de BLE-reader:

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
- de opdracht `brms simuleer` voor de beheerder.
"""

import asyncio
import json
import logging
import os
import queue
import threading
import time
from dataclasses import dataclass

from brms.config import Config, Sensor
from brms.readers import bthome, ruuvi

log = logging.getLogger(__name__)

TOPIC_AAN = "brms/opdracht/simulatie/aan"
TOPIC_UIT = "brms/opdracht/simulatie/uit"

STANDAARD_DUUR = 60   # seconden; de standaard van 1 minuut (§10.3 punt 2)
MAX_DUUR = 3600       # langer dan een uur simuleren is altijd een vergissing
STAP = 2.0            # seconden tussen twee gesimuleerde advertenties
LUISTERTIJD = 15      # zo lang luistert `brms simuleer` of de echte sensor metingen levert

# Signaalsterkte van een gesimuleerde advertentie. Er is geen adapter die hem meet; een
# vaste waarde maakt in de database herkenbaar dat het om simulatie ging.
RSSI = -60

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


def waarden(fractie: float) -> dict[str, float]:
    """De waarden van de testreeks op een punt in de simulatie.

    fractie: 0.0 = begin, 1.0 = einde.
    """
    return {g: begin + (eind - begin) * fractie for g, (begin, eind) in REEKS.items()}


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


def advertentie(sensor: Sensor, fractie: float, n: int) -> tuple[dict, dict]:
    """Eén gesimuleerde advertentie voor deze sensor, in de vorm waarin bleak hem aanlevert:
    (service data {UUID: bytes}, manufacturer data {fabrikant-ID: bytes})."""
    w = waarden(fractie)
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
                service_data, fabrikant_data = advertentie(sensor, fractie, n)
                ontvang(sensor.id, service_data, fabrikant_data, RSSI, True)
            try:
                await asyncio.wait_for(stop.wait(), STAP)
            except asyncio.TimeoutError:
                pass


# =============================================================================
# De opdracht `brms simuleer <sensor>` (voor de beheerder)
# =============================================================================

def vraag_duur(vraag=input) -> int:
    """Vraagt hoe lang de simulatie moet lopen; geeft de duur in seconden.

    Alleen Enter = de standaard van 1 minuut. Bij ongeldige invoer wordt opnieuw gevraagd.
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


def opdracht(cfg: Config, namen: list[str], vraag=input) -> int:
    """`brms simuleer <sensor>` (§10.3). Exitcode 0 = simulatie gelopen."""
    from brms import monitor
    from brms.bus import Bus

    if len(namen) != 1:
        print("gebruik: brms simuleer <sensor>   (naam of ID uit de configuratie)")
        return 1
    try:
        sensor = monitor.zoek_sensoren(namen, cfg)[0]
    except ValueError as e:
        print(e)
        return 1
    if sensor.type not in TYPES:
        print(f"{sensor.naam}: sensortype {sensor.type} kan niet gesimuleerd worden "
              f"(wel: {', '.join(TYPES)})")
        return 1

    # Alle metingen van deze sensor komen in `binnen`: eerst om te zien of de echte sensor
    # zendt, daarna om de simulatie te volgen.
    binnen: queue.Queue = queue.Queue()
    bus = Bus(f"simuleer-{os.getpid()}", cfg.mqtt.host, cfg.mqtt.port)
    bus.abonneer(f"brms/meting/{sensor.id}/#", 0, lambda t, p: binnen.put(t))
    bus.start()
    gestart = False

    def stuur(topic: str, **inhoud) -> bool:
        return bus.publiceer(topic, json.dumps({"sensor_id": sensor.id, **inhoud}), qos=1)

    try:
        # De bus verbindt op de achtergrond; even wachten tot dat gelukt is.
        wacht_tot = time.monotonic() + 5
        while not bus.verbonden and time.monotonic() < wacht_tot:
            time.sleep(0.1)
        if not bus.verbonden:
            print(f"De broker ({cfg.mqtt.host}:{cfg.mqtt.port}) is niet bereikbaar.")
            return 1

        # 1. Levert de echte sensor nu metingen? Dan eerst bevestiging vragen.
        print(f"Luisteren of {sensor.naam} ({sensor.id}) nu metingen levert "
              f"(hooguit {LUISTERTIJD} s) …")
        try:
            binnen.get(timeout=LUISTERTIJD)
            aanwezig = True
        except queue.Empty:
            aanwezig = False
        if aanwezig:
            print(f"{sensor.naam} levert op dit moment metingen.")
            antwoord = vraag("Weet je zeker dat je de sensor wilt by-passen met "
                             "gesimuleerde data? [j/N] ")
            if antwoord.strip().lower() not in ("j", "ja"):
                print("Afgebroken; er is niets gesimuleerd.")
                return 1
        else:
            print(f"Geen metingen van {sensor.naam} ontvangen; er valt niets te by-passen.")

        # 2. Altijd de duur vragen.
        duur = vraag_duur(vraag)

        # 3. Startsein naar de BLE-reader.
        while not binnen.empty():   # metingen van de echte sensor van vóór de start
            binnen.get_nowait()
        if not stuur(TOPIC_AAN, duur=duur):
            print("Het startsein kon niet verstuurd worden (broker niet bereikbaar).")
            return 1
        gestart = True
        print(f"Simulatie gestart voor {sensor.naam}, {duur} s. De waarden komen als gewone "
              f"metingen in de database.")
        print("Meekijken: brms monitor " + sensor.naam + "     Eerder stoppen: Ctrl-c")

        # 4. Volgen tot de simulatie afgelopen is. De reader stopt zelf na `duur`; dit is
        # alleen om de voortgang te tonen.
        begin = time.monotonic()
        einde = begin + duur + STAP + 1
        aantal = 0
        while time.monotonic() < einde:
            try:
                binnen.get(timeout=1)
                aantal += 1
                while True:
                    binnen.get_nowait()
                    aantal += 1
            except queue.Empty:
                pass
            if aantal == 0 and time.monotonic() - begin > 5 * STAP:
                print("\nGeen gesimuleerde metingen ontvangen. Draait de BLE-reader? "
                      "(brms start)")
                stuur(TOPIC_UIT)
                time.sleep(0.3)
                return 1
            rest = max(0, round(begin + duur - time.monotonic()))
            print(f"\r  nog {rest // 60}:{rest % 60:02d}   {aantal} metingen ontvangen ",
                  end="", flush=True)
        print(f"\nSimulatie afgelopen; {sensor.naam} telt weer gewoon mee.")
        return 0
    except (KeyboardInterrupt, EOFError):
        # Ctrl-c, of geen toetsenbord om de vragen te beantwoorden.
        if gestart:
            stuur(TOPIC_UIT)
            time.sleep(0.3)  # paho de tijd geven om de opdracht te versturen
            print(f"\nSimulatie gestopt; {sensor.naam} telt weer gewoon mee.")
        else:
            print("\nAfgebroken; er is niets gesimuleerd.")
        return 1
    finally:
        bus.stop()
