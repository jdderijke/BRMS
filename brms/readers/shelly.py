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

Het relais (§5.2.6): een sensor van het type `shelly-relais` (de tussenstekker van de
kachel) wordt op dezelfde manier gepolld, met de decoder readers/shelly_relais.py. De
reader is ook de enige die het relais schakelt (§5.5.3: per apparaat één component die
ermee praat). De opdracht

    brms/opdracht/relais/zet     {"sensor_id": "kachel", "aan": true, "duur": 120}

komt van de schakellaag (brms/schakel.py, §5.8), die beslist in welke stand een relais
hoort. Elk relais heeft een veilige stand (`veilige_stand` in brms.toml). De andere,
actieve stand gaat altijd met een terugschakeltijd (`duur`, seconden) die de Shelly zelf
bewaakt, en de schakellaag herhaalt die opdracht zolang de stand geldt; de veilige stand
gaat zonder tijd. Een opdracht naar de actieve stand zonder terugschakeltijd weigert de
reader, van wie hij ook komt (§5.8 punt 5). Direct na het schakelen pollt de reader, zodat
de nieuwe stand meteen op de bus staat. Lukt het schakelen niet, dan komt er een
waarschuwing in het log: de actieve stand wordt vanzelf herhaald, en na een mislukte
opdracht naar de veilige stand schakelt de Shelly zelf terug als de tijd om is.

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
from brms.readers import shelly_relais, shelly_uni

log = logging.getLogger(__name__)

# Sensortypen die deze reader verwerkt (zie config.SHELLY_TYPES).
RELAIS_TYPE = "shelly-relais"
_TYPES = ("shelly-uni", RELAIS_TYPE)

TOPIC_POLL_NU = "brms/opdracht/shelly/poll-nu"
# Schakelopdracht voor een relais (§5.2.6 punt 3): {"sensor_id", "aan", "duur"}.
TOPIC_RELAIS_ZET = "brms/opdracht/relais/zet"
# Hierop vraagt de reader bij zijn start om de stand van de relais (§5.2.6 punt 6, v0.88).
# Sinds v0.29.0 antwoordt de schakellaag (§5.8 punt 7), eerder de alarmlaag; het antwoord
# komt als gewone opdrachten op TOPIC_RELAIS_ZET.
TOPIC_RELAIS_STAND_VRAAG = "brms/opdracht/relais/stand"

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


def schakel(adres: str, aan: bool, duur: float | None, timeout: float,
            veilig_aan: bool = False) -> dict:
    """Zet een relais aan of uit (§5.2.6 punt 3) en geeft het antwoord van de Shelly als
    dict (bijv. {"was_on": false}).

    veilig_aan: de veilige stand van dit relais (False = uit, True = aan). Blokkeert zoals
    haal_status, met dezelfde fouten. ValueError ook bij de actieve stand zonder
    terugschakeltijd (zie shelly_relais.zet_url).
    """
    url = shelly_relais.zet_url(adres, aan, duur, veilig_aan)
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read(MAX_ANTWOORD))


def lees_zet(payload: bytes) -> tuple[str, bool, float | None]:
    """Leest een opdracht van brms/opdracht/relais/zet: geeft (sensor_id, aan, duur).
    ValueError bij een ongeldig bericht.

    `duur` is de terugschakeltijd in seconden, of None als hij er niet bij staat. Of een
    opdracht zonder tijd mag, hangt af van de veilige stand van het relais (§5.8 punt 5)
    en wordt bij het schakelen beoordeeld (Poller.zet): dat weet dit bericht niet. Een
    duur die er wél staat, moet een getal groter dan 0 zijn."""
    try:
        d = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError(f"geen geldige JSON: {e}") from None
    if not isinstance(d, dict) or not isinstance(d.get("sensor_id"), str) or not d["sensor_id"]:
        raise ValueError("sensor_id ontbreekt")
    if not isinstance(d.get("aan"), bool):
        raise ValueError("'aan' moet true of false zijn")
    duur = d.get("duur")
    if duur is None:
        return d["sensor_id"], d["aan"], None
    if isinstance(duur, bool) or not isinstance(duur, (int, float)) or duur <= 0:
        raise ValueError("'duur' (terugschakeltijd in seconden) moet een getal groter dan 0 zijn")
    return d["sensor_id"], d["aan"], float(duur)


def _kort(status: dict, sensor: Sensor) -> str:
    """De delen van het antwoord die BRMS gebruikt, als tekst voor de trace. Het hele
    antwoord is tientallen regels (cloud, bluetooth, systeem); dat past niet in de monitor."""
    if sensor.type == RELAIS_TYPE:
        # Van de uitgang alleen de vier velden die een grootheid worden.
        deel = status.get(shelly_relais.COMPONENT)
        delen = {shelly_relais.COMPONENT: {k: deel.get(k) for k in ("output", "apower", "voltage", "current")}
                 if isinstance(deel, dict) else None}
    else:
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
        # De decoder hangt af van het sensortype: het relais (§5.2.6) of de Shelly Plus
        # Uni met 'bron', 'drempel' en 'omgekeerd' uit de configuratie (§5.2.4).
        if sensor.type == RELAIS_TYPE:
            velden = shelly_relais.decodeer_velden(status)
        else:
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

    # Het relais levert ook 'walstroom', afgeleid van de spanning (§5.2.6 punt 5). Zoals
    # elke afleiding rekent zij met de gecorrigeerde waarde (§5.2.5 punt 4), en ook als
    # 'spanning' zelf niet in de lijst 'grootheden' staat.
    if sensor.type == RELAIS_TYPE and "spanning" in waarden:
        spanning = waarden["spanning"]
        if "spanning" in sensor.correctie:
            spanning = sensor.correctie["spanning"].toepassen(spanning)
        waarden["walstroom"], uitleg = shelly_relais.walstroom(spanning)
        if stap:
            stap("afgeleid", f"{'walstroom':<17} {uitleg} = {waarden['walstroom']:g}")

    # Alleen de grootheden uit de lijst van de sensor (§5.4.2 punt 1). Zonder lijst gaat
    # alles door.
    regels = []
    if sensor.grootheden is not None:
        weg = [g for g in waarden if g not in sensor.grootheden]
        for g in weg:
            del waarden[g]
        if weg:
            regels.append(f"weggelaten (niet in 'grootheden'): {', '.join(weg)}")

    # Correcties uit de configuratie (§5.4.2 punt 2). Bij de bilge-Shelly kan dat alleen op
    # de signaalsterkte (bilge en bilgepomp zijn toestanden); bij het relais ook op
    # vermogen, spanning en stroom.
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

    def __init__(self, sensor: Sensor, bus, tracer, simulator, haal=haal_status,
                 schakel=schakel):
        """haal: de functie die het antwoord ophaalt; schakel: de functie die een relais
        zet. Beide te vervangen in tests."""
        self.sensor = sensor
        self._bus = bus
        self._tracer = tracer
        self._simulator = simulator
        self._haal = haal
        self._schakel = schakel
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

    async def zet(self, aan: bool, duur: float | None) -> bool:
        """Zet het relais aan of uit (§5.2.6 punt 3) en laat direct daarna pollen, zodat
        de nieuwe stand meteen op de bus staat. Geeft terug of het schakelen gelukt is.

        De veilige stand van dit relais (`veilige_stand` in brms.toml) gaat zonder tijd,
        ook als er een `duur` bij de opdracht staat. De andere, actieve stand gaat met de
        terugschakeltijd `duur`; zonder wordt de opdracht geweigerd (§5.8 punt 5).

        Eén poging: de actieve stand wordt door de schakellaag elke minuut herhaald, en na
        een mislukte opdracht naar de veilige stand schakelt de Shelly zelf terug zodra
        de terugschakeltijd om is.
        """
        sensor = self.sensor
        stap = self._tracer.voor(sensor.id) if self._tracer else None
        veilig_aan = sensor.veilige_stand == "aan"
        if aan == veilig_aan:
            duur = None          # de veilige stand: daar blijft het relais staan
        wat = ("aan" if aan else "uit") + (f" voor {duur:g} s" if duur is not None else "")
        try:
            # urllib blokkeert; in een thread, net als het pollen. De actieve stand zonder
            # duur geeft hier een ValueError (zie shelly_relais.zet_url).
            antwoord = await asyncio.to_thread(self._schakel, sensor.adres, aan, duur, TIMEOUT,
                                               veilig_aan)
        except (OSError, ValueError, http.client.HTTPException) as e:
            log.warning("relais %s (%s) op %s niet geschakeld (%s): %s", sensor.naam, sensor.id,
                        sensor.adres, wat, e)
            if stap:
                stap("opdracht", f"relais {wat}: MISLUKT ({e})")
            return False
        # De Shelly meldt de vorige stand. Alleen een verandering komt in het log: de
        # actieve stand wordt elke minuut herhaald en zou het log anders vullen.
        was_aan = antwoord.get("was_on") if isinstance(antwoord, dict) else None
        if was_aan != aan:
            log.info("relais %s (%s) is %s gezet", sensor.naam, sensor.id, wat)
        if stap:
            stap("opdracht", f"relais {wat}; antwoord van de Shelly: {json.dumps(antwoord)}")
        # Direct pollen (zoals poll-nu): de wachttijd tot de volgende poll afbreken.
        self.wek.set()
        return True

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
    simulator = Simulator(bus, sensoren, types=SHELLY_TYPES, dienst="shelly-reader")
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

    # Schakelopdrachten voor een relais (§5.2.6 punt 3), van de schakellaag. Ook deze komen
    # binnen in paho's netwerkthread; het schakelen zelf loopt als taak in de asyncio-loop.
    # De taken worden vastgehouden tot ze klaar zijn (anders kan Python ze opruimen).
    schakeltaken: set[asyncio.Task] = set()

    def start_zet(poller: Poller, aan: bool, duur: float | None) -> None:
        taak = asyncio.create_task(poller.zet(aan, duur))
        schakeltaken.add(taak)
        taak.add_done_callback(schakeltaken.discard)

    def bij_relais_zet(topic: str, payload: bytes) -> None:
        try:
            sensor_id, aan, duur = lees_zet(payload)
        except ValueError as e:
            log.warning("ongeldige opdracht op %s genegeerd: %s", topic, e)
            return
        poller = pollers.get(sensor_id)
        if poller is None or poller.sensor.type != RELAIS_TYPE:
            # Alleen een relais uit de configuratie wordt geschakeld (§5.2.1 punt 3).
            log.warning("opdracht op %s voor %r genegeerd: geen relais in de configuratie",
                        topic, sensor_id)
            return
        loop.call_soon_threadsafe(start_zet, poller, aan, duur)

    bus.abonneer(TOPIC_RELAIS_ZET, 1, bij_relais_zet)

    async def vraag_relais_stand() -> None:
        """Vraagt de schakellaag bij de start om de stand van de relais (§5.2.6 punt 6,
        §5.8 punt 7). Bij een herstart van alle services geeft de schakellaag zijn eerste
        opdrachten vaak net voordat deze reader luistert; zonder deze vraag bleef een
        relais dan in de oude stand tot de Shelly zelf terugschakelde. Herstart alleen
        deze reader terwijl een relais in zijn actieve stand staat, dan hoort hij zo ook
        direct dat het daar moet blijven.

        Eén keer, zodra de broker bereikbaar is (publiceer geeft dan True): het
        abonnement op de schakelopdrachten hierboven is dan al aangemeld, dus het
        antwoord wordt niet gemist. Draait de schakellaag nog niet, dan geeft hij de
        stand zelf bij zijn start."""
        while not stop.is_set():
            if bus.publiceer(TOPIC_RELAIS_STAND_VRAAG, json.dumps({"ts": int(time.time())}), qos=1):
                return
            await asyncio.sleep(1.0)

    log.info("Shelly-reader gestart; %d Shelly's: %s", len(sensoren),
             ", ".join(f"{s.naam} ({s.id}) op {s.adres} elke {s.interval:g} s"
                       for s in sensoren.values()))
    taken = [asyncio.create_task(p.draai(stop)) for p in pollers.values()]
    # Meldt op de bus welke Shelly's gesimuleerd worden (ook als dat er geen zijn); het web
    # toont daarmee "Actief", "Simulatie" of "Demo" (§10.3 punt 10).
    taken.append(asyncio.create_task(simulator.meld_steeds(stop)))
    # Alleen als er een relais is: zonder relais valt er niets te vragen.
    if any(s.type == RELAIS_TYPE for s in sensoren.values()):
        taken.append(asyncio.create_task(vraag_relais_stand()))
    await stop.wait()
    # De pollers wachten op hun volgende poll; wekken, zodat ze het stopsein zien.
    for p in pollers.values():
        p.wek.set()
    await asyncio.gather(*taken)
    # Een schakelopdracht die nog loopt, mag afmaken (hooguit TIMEOUT seconden).
    await asyncio.gather(*schakeltaken, return_exceptions=True)
    log.info("Shelly-reader gestopt")
