"""Shelly-reader: pollt alle Shelly's via hun lokale HTTP-API (ARCHITECTURE.md §5.2.1
punt 2, §5.2.4).

Anders dan de Bluetooth-sensoren zendt een Shelly zijn toestand niet zelf uit; de reader
vraagt hem op ("pollen"). Werking:

1. Voor elke Shelly uit de configuratie loopt een eigen taak die om de `interval` seconden
   http://<adres>/rpc/Shelly.GetStatus opvraagt (standaard 30 s; de bilge 2 s, omdat een
   pompbeurt korter kan zijn dan 30 s).
2. De decoder (readers/shelly_uni.py) zet het antwoord om in waarden, volgens 'bron',
   'drempel' en 'omgekeerd' uit de configuratie.
3. De signaalsterkte van de wifi (rssi) gaat mee als eigen grootheid.
4. Grootheden-filter en correcties uit de configuratie (§5.4.2), en dan gaat elke waarde
   als Meting naar de MQTT-bus.

Is een Shelly niet bereikbaar, dan wordt er niets gepubliceerd: stilte betekent "geen
meting". De reader blijft het elk interval opnieuw proberen.

Poll-nu (§5.2.1 punt 6): de opdracht

    brms/opdracht/shelly/poll-nu     {"sensor_id": "shelly-bilge"}   (of leeg: alle)

laat de reader direct pollen, los van het interval.

Het ophalen gebeurt met urllib uit de standaardbibliotheek (geen extra afhankelijkheid,
P3), in een thread: urllib blokkeert, en de asyncio-loop moet door kunnen lopen voor de
andere Shelly's.

Simulatie (§10.3): met `brms sim <sensor>` maakt de reader zelf het antwoord van de Shelly
aan en verwerkt dat op dezelfde manier. De echte Shelly wordt zolang niet gepolld.
"""

import asyncio
import http.client
import json
import logging
import random
import time
import urllib.request

from brms import meting
from brms.config import Config, Sensor
from brms.meting import Meting
from brms.readers import shelly_uni

log = logging.getLogger(__name__)

# Sensortypen die deze reader verwerkt (zie config.SHELLY_TYPES).
_TYPES = ("shelly-uni",)

TOPIC_POLL_NU = "brms/opdracht/shelly/poll-nu"

# Langste wachttijd op een antwoord, in seconden. Een Shelly op het lokale netwerk
# antwoordt binnen een fractie van een seconde. Bij een kort interval geldt het interval
# als grens, zodat een trage Shelly de volgende poll niet ophoudt.
TIMEOUT = 5.0

# Een antwoord is enkele kB; meer lezen we niet (vangnet tegen een verkeerd adres waarop
# iets heel anders antwoordt).
MAX_ANTWOORD = 256 * 1024


def haal_status(adres: str, timeout: float) -> dict:
    """Vraagt de toestand van één Shelly op en geeft het antwoord als dict.

    Blokkeert tot het antwoord er is (of de timeout); de reader voert dit daarom in een
    thread uit. OSError, ValueError of http.client.HTTPException als het niet lukt.
    """
    with urllib.request.urlopen(f"http://{adres}/rpc/Shelly.GetStatus", timeout=timeout) as r:
        return json.loads(r.read(MAX_ANTWOORD))


def _kort(status: dict, sensor: Sensor) -> str:
    """De delen van het antwoord die BRMS gebruikt, als tekst voor de trace. Het hele
    antwoord is tientallen regels (cloud, bluetooth, systeem); dat past niet in de monitor."""
    delen = {c: status.get(c) for c in sensor.bron.values()}
    wifi = status.get("wifi")
    delen["wifi"] = {"rssi": wifi.get("rssi")} if isinstance(wifi, dict) else None
    return json.dumps(delen, ensure_ascii=False)


def verwerk(sensor: Sensor, status: dict, ts: int, tracer=None,
            gesimuleerd: bool = False) -> list[Meting]:
    """Zet één antwoord van een Shelly om in een lijst metingen.

    Geen netwerk, zodat hij te testen is met een opgeschreven antwoord (tests/test_shelly.py).

    sensor       de Shelly uit de configuratie
    status       het antwoord van Shelly.GetStatus, als dict
    ts           ontvangsttijd (Unix-seconden UTC)
    tracer       optioneel (brms.trace.Tracer): staat trace voor deze sensor aan, dan wordt
                 elke stap gemeld (§10.2)
    gesimuleerd  True als het antwoord van de simulator komt (§10.3); alleen de trace
                 vermeldt het
    """
    # stap(naam, tekst) als trace voor deze sensor aanstaat, anders None.
    stap = tracer.voor(sensor.id) if tracer else None
    bron = "SIMULATIE " if gesimuleerd else ""

    try:
        velden = shelly_uni.decodeer_velden(status, sensor.bron, sensor.drempel, sensor.omgekeerd)
    except shelly_uni.ShellyFout as e:
        log.debug("%s (%s): %s", sensor.naam, sensor.id, e)
        if stap:
            stap("ruw", f"{bron}Shelly {sensor.adres}: FOUT: {e}")
        return []
    if stap:
        stap("ruw", f"{bron}Shelly {sensor.adres}  {_kort(status, sensor)}")

    # Wat niet te bepalen was (waarde None) wordt niet gepubliceerd (§5.2.3 punt 4).
    waarden = {v.grootheid: v.waarde for v in velden if v.waarde is not None}
    regels = [f"{v.grootheid:<17} {v.bron} = {json.dumps(v.ruw)}  ({v.uitleg}) = "
              + ("onbekend (niet gepubliceerd)" if v.waarde is None else f"{v.waarde:g}")
              for v in velden]

    # De signaalsterkte van de wifi, zoals de Shelly die zelf meet.
    rssi = shelly_uni.rssi(status)
    if rssi is not None:
        waarden["rssi"] = rssi
        regels.append(f"{'rssi':<17} wifi.rssi = {rssi} dBm")
    if stap:
        stap("gedecodeerd", "\n".join(regels) or "niets bruikbaars in het antwoord")
    if not waarden:
        return []

    # Alleen de grootheden uit de lijst van de sensor (§5.4.2 punt 1). Zonder lijst gaat
    # alles door.
    regels = []
    if sensor.grootheden is not None:
        weg = [g for g in waarden if g not in sensor.grootheden]
        for g in weg:
            del waarden[g]
        if weg:
            regels.append(f"weggelaten (niet in 'grootheden'): {', '.join(weg)}")

    # Correcties uit de configuratie (§5.4.2 punt 2). Bij een Shelly kan dat alleen op de
    # signaalsterkte: bilge en bilgepomp zijn toestanden.
    for g, c in sensor.correctie.items():
        if g in waarden:
            nieuw = c.toepassen(waarden[g])
            regels.append(f"{g:<17} {waarden[g]:g} {c} = {nieuw:g}")
            waarden[g] = nieuw
    if stap:
        stap("gecorrigeerd", "\n".join(regels) or "geen correcties ingesteld")

    # meting.maak controleert de grootheid en zet de vaste soort erbij (§5.3.3).
    return [meting.maak(ts, sensor.id, g, w) for g, w in waarden.items()]


class Poller:
    """Het pollen van één Shelly: ophalen (of simuleren), verwerken en publiceren.

    Wordt alleen in de asyncio-loop gebruikt; een lock is niet nodig.
    """

    def __init__(self, sensor: Sensor, bus, tracer, simulator, haal=haal_status):
        """haal: de functie die het antwoord ophaalt; te vervangen in tests."""
        self.sensor = sensor
        self._bus = bus
        self._tracer = tracer
        self._simulator = simulator
        self._haal = haal
        # Bron voor de ruis in gesimuleerde antwoorden.
        self._toeval = random.Random()
        # Aantal mislukte polls op rij. Een onbereikbare Shelly faalt elk interval opnieuw;
        # alleen de eerste keer komt er een waarschuwing in het log, en één regel bij herstel.
        self._mislukt = 0
        # Wordt gezet om de wachttijd tot de volgende poll af te breken (poll-nu, stoppen).
        self.wek = asyncio.Event()

    async def poll(self) -> list[Meting]:
        """Eén poll. Geeft de gepubliceerde metingen terug (leeg als de Shelly niet
        bereikbaar is)."""
        from brms import simulatie

        sensor = self.sensor
        stap = self._tracer.voor(sensor.id) if self._tracer else None

        # Simulatie (§10.3): het antwoord zelf maken en de echte Shelly overslaan (bypass).
        sim = self._simulator.stap(sensor.id) if self._simulator else None
        if sim is not None:
            status = simulatie.shelly_status(sensor, sim[0], self._toeval)
        else:
            try:
                # urllib blokkeert; in een thread, zodat de andere Shelly's doorlopen.
                status = await asyncio.to_thread(self._haal, sensor.adres,
                                                 min(TIMEOUT, sensor.interval))
            except (OSError, ValueError, http.client.HTTPException) as e:
                # Niet bereikbaar, timeout of geen geldig antwoord: niets publiceren.
                if self._mislukt == 0:
                    log.warning("%s (%s) op %s niet bereikbaar: %s", sensor.naam, sensor.id,
                                sensor.adres, e)
                self._mislukt += 1
                if stap:
                    stap("ruw", f"Shelly {sensor.adres}: NIET BEREIKBAAR ({e}); "
                                f"{self._mislukt} keer op rij")
                return []
            if self._mislukt:
                log.info("%s (%s) weer bereikbaar na %d mislukte polls", sensor.naam,
                         sensor.id, self._mislukt)
                self._mislukt = 0

        # De ontvangsttijd is "nu" (§5.2.1 punt 4).
        metingen = verwerk(sensor, status, int(time.time()), self._tracer, sim is not None)
        regels = []
        for m in metingen:
            # Is de broker onbereikbaar, dan gooit de bus de meting weg (§3.1 punt 5).
            gelukt = self._bus.publiceer_meting(m)
            regels.append(f"{m.topic()}  {m.naar_json()}" if gelukt
                          else f"{m.topic()}  WEGGEGOOID (broker niet bereikbaar)")
        if stap and metingen:
            stap("gepubliceerd", "\n".join(regels))
        return metingen

    async def draai(self, stop: asyncio.Event) -> None:
        """Pollt tot `stop` gezet wordt: elke `interval` seconden, en direct als `wek`
        gezet wordt (poll-nu)."""
        interval = self.sensor.interval
        # Vaste tijdstippen op de monotone klok (§4.2), zodat het interval niet oploopt met
        # de duur van een poll.
        volgende = time.monotonic()
        while not stop.is_set():
            try:
                await self.poll()
            except Exception:
                # Een onverwachte fout bij één Shelly mag de andere niet laten stoppen.
                log.exception("fout bij pollen van %s (%s)", self.sensor.naam, self.sensor.id)
            nu = time.monotonic()
            volgende += interval
            if volgende < nu:
                # De poll duurde langer dan het interval: niet inhalen, gewoon verder.
                volgende = nu
            try:
                await asyncio.wait_for(self.wek.wait(), volgende - nu)
            except asyncio.TimeoutError:
                continue
            # Gewekt door poll-nu (of door stoppen): nu pollen, en vanaf nu weer tellen.
            self.wek.clear()
            volgende = time.monotonic()


async def draai(config: Config, bus, stop: asyncio.Event) -> None:
    """Pollt alle Shelly's tot `stop` gezet wordt (bij SIGTERM van systemd)."""
    from brms.simulatie import SHELLY_TYPES, Simulator
    from brms.trace import Tracer

    sensoren = {s.id: s for s in config.sensoren.values() if s.type in _TYPES}
    if not sensoren:
        # Geen fout: de service draait dan zonder iets te doen, zodat `brms start` en
        # `brms update` voor alle services gelijk werken.
        log.warning("geen Shelly's geconfigureerd")

    # Luistert naar trace-opdrachten van de monitor (§10.2); standaard staat trace uit.
    tracer = Tracer(bus, "shelly-reader")
    tracer.start()

    # Luistert naar simulatie-opdrachten (`brms sim`, §10.3); standaard loopt er geen.
    simulator = Simulator(bus, sensoren, types=SHELLY_TYPES)
    simulator.start()

    pollers = {s.id: Poller(s, bus, tracer, simulator) for s in sensoren.values()}

    # Poll-nu (§5.2.1 punt 6). De opdracht komt binnen in paho's netwerkthread; het wekken
    # moet in de asyncio-loop gebeuren.
    loop = asyncio.get_running_loop()

    def bij_poll_nu(topic: str, payload: bytes) -> None:
        try:
            d = json.loads(payload) if payload.strip() else {}
            sensor_id = d.get("sensor_id")
        except (ValueError, AttributeError) as e:
            log.warning("ongeldige opdracht op %s: %s", topic, e)
            return
        # Zonder sensor_id: alle Shelly's. Een ID van een andere reader wordt genegeerd.
        for p in pollers.values():
            if sensor_id is None or sensor_id == p.sensor.id:
                loop.call_soon_threadsafe(p.wek.set)

    bus.abonneer(TOPIC_POLL_NU, 1, bij_poll_nu)

    log.info("Shelly-reader gestart; %d Shelly's: %s", len(sensoren),
             ", ".join(f"{s.naam} ({s.id}) op {s.adres} elke {s.interval:g} s"
                       for s in sensoren.values()))
    taken = [asyncio.create_task(p.draai(stop)) for p in pollers.values()]
    await stop.wait()
    # De pollers wachten op hun volgende poll; wekken, zodat ze het stopsein zien.
    for p in pollers.values():
        p.wek.set()
    await asyncio.gather(*taken)
    log.info("Shelly-reader gestopt")
