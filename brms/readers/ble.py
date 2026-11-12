"""BLE-reader: één scanner voor alle Bluetooth-sensoren (ARCHITECTURE.md §5.2.1).

Werking:
1. bleak (Bluetooth-bibliotheek, asyncio) scant continu en roept voor elke ontvangen
   advertentie `bij_advertentie` aan.
2. Alleen apparaten die in de configuratie staan worden verwerkt. De scanner ziet ook
   sensoren van andere boten in de haven; die worden genegeerd.
3. Het sensortype uit de configuratie bepaalt welke decoder de bytes omzet in waarden:
   BTHome (Xiaomi met pvvx), Ruuvi (formaat 5) of Victron (Instant Readout, versleuteld;
   §5.2.2, §5.2.3).
   Bij een Ruuvi wordt de bewegingsteller hier omgezet in een toename (§5.2.3 punt 5).
4. De correcties uit de configuratie (offsets) worden toegepast.
5. De signaalsterkte (RSSI) gaat mee als eigen grootheid: handig om het bereik van de
   Bluetooth-ontvangst aan boord te beoordelen (OPEN-2).
6. Elke waarde gaat als Meting naar de MQTT-bus; de alarmlaag en de opslag luisteren mee.

Er is maar één Bluetooth-adapter; meerdere scanners naast elkaar zitten elkaar in de weg.
Daarom is er één scanner die per advertentie de juiste decoder kiest.

Simulatie (§10.3): met `brms sim <sensor>` maakt de reader zelf advertenties aan en
verwerkt die via dezelfde ingang (Ontvanger) als de advertenties van bleak. De echte
advertenties van die sensor worden zolang genegeerd.
"""

import asyncio
import logging
import time

from brms import meting
from brms.config import Config, Sensor
from brms.meting import Meting
from brms.readers import bthome, ruuvi, victron

log = logging.getLogger(__name__)

# Victron-sensoren waarvoor al een waarschuwing in het log staat (verkeerde sleutel of
# verkeerd type). Zo'n fout komt elke seconde terug; één logregel per sensor is genoeg.
_gewaarschuwd: set[str] = set()

# Sensortypen die deze reader verwerkt (zie config.BLE_TYPES).
_TYPES = ("bthome", "ruuvi", *victron.LEVERT)


def verwerk(adres: str, service_data: dict[str, bytes], fabrikant_data: dict[int, bytes],
            rssi: int | None, ts: int, sensoren: dict[str, Sensor], tracer=None,
            tellers: dict[str, int] | None = None, gesimuleerd: bool = False) -> list[Meting]:
    """Zet één advertentie om in een lijst metingen.

    Geen Bluetooth en geen netwerk, zodat hij te testen is met opgenomen advertenties
    (tests/test_ble.py). Het enige wat tussen twee aanroepen onthouden wordt, staat in
    `tellers`.

    adres           MAC-adres van de zender, zoals bleak het aanlevert
    service_data    {UUID: bytes} uit de advertentie (BTHome)
    fabrikant_data  {fabrikant-ID: bytes} uit de advertentie ("manufacturer data"; Ruuvi)
    rssi            signaalsterkte in dBm, of None als onbekend
    ts              ontvangsttijd (Unix-seconden UTC); een advertentie bevat zelf geen tijd
    sensoren        geconfigureerde sensoren, {MAC in hoofdletters: Sensor}
    tracer          optioneel (brms.trace.Tracer): staat trace voor deze sensor aan, dan
                    wordt elke stap gemeld (§10.2)
    tellers         optioneel: {sensor-ID: laatst ontvangen stand van de Ruuvi-
                    bewegingsteller}. Wordt hier bijgewerkt. Zonder dit woordenboek wordt
                    er geen `beweging` gepubliceerd.
    gesimuleerd     True als de advertentie van de simulator komt (§10.3). De verwerking
                    is precies gelijk; alleen de trace vermeldt het.
    """
    # Alleen geconfigureerde apparaten (§5.2.1 punt 3).
    sensor = sensoren.get(adres.upper())
    if sensor is None:
        return []

    # stap(naam, tekst) als trace voor deze sensor aanstaat, anders None. Alle tracetekst
    # wordt alleen opgebouwd als 'stap' bestaat, zodat trace niets kost als hij uit staat.
    stap = tracer.voor(sensor.id) if tracer else None
    # In de trace is zichtbaar dat een advertentie gesimuleerd is (§10.3 punt 6).
    bron = "SIMULATIE " if gesimuleerd else ""

    # Decoder kiezen op basis van het sensortype uit de configuratie.
    if sensor.type == "bthome":
        data = service_data.get(bthome.FCD2_UUID)
        if data is None:
            return []  # advertentie zonder BTHome-gegevens (komt voor bij pvvx)
        data = bytes(data)
        if stap:
            stap("ruw", f"{bron}BTHome {data.hex(' ')}   rssi {rssi} dBm")
        try:
            objecten = bthome.decodeer_objecten(data)
        except bthome.BTHomeFout as e:
            # Een kapot bericht is geen reden om te stoppen; het volgende komt zo.
            log.debug("%s (%s): %s", sensor.naam, sensor.id, e)
            if stap:
                stap("gedecodeerd", f"FOUT: {e} – advertentie overgeslagen")
            return []
        if stap:
            stap("gedecodeerd", "\n".join(
                f"{o.grootheid:<17} 0x{o.object_id:02X}: {o.ruw.hex(' ')} = {o.getal}"
                f" × {o.factor:g} = {o.waarde:g}" for o in objecten) or "geen bruikbare objecten")
        waarden = {o.grootheid: o.waarde for o in objecten}
    elif sensor.type == "ruuvi":
        data = fabrikant_data.get(ruuvi.FABRIKANT_ID)
        if data is None:
            return []  # advertentie zonder Ruuvi-gegevens
        data = bytes(data)
        if stap:
            stap("ruw", f"{bron}Ruuvi {data.hex(' ')}   rssi {rssi} dBm")
        try:
            velden = ruuvi.decodeer_velden(data)
        except ruuvi.RuuviFout as e:
            # Een kapot bericht is geen reden om te stoppen; het volgende komt zo.
            log.debug("%s (%s): %s", sensor.naam, sensor.id, e)
            if stap:
                stap("gedecodeerd", f"FOUT: {e} – advertentie overgeslagen")
            return []
        # Wat de sensor als "ongeldig" uitzendt (waarde None) wordt niet gepubliceerd
        # (§5.2.3 punt 4), bijv. luchtdruk bij een RuuviTag Pro 3-in-1.
        waarden = {v.grootheid: v.waarde for v in velden if v.waarde is not None}
        regels = [f"{v.grootheid:<17} {v.ruw.hex(' ')} = {v.getal} {v.uitleg} = "
                  + ("ongeldig (niet gepubliceerd)" if v.waarde is None else f"{v.waarde:g}")
                  for v in velden]

        # Bewegingsteller -> toename (§5.2.3 punt 5, §5.3.2). De ruwe tellerstand is geen
        # grootheid en gaat niet de bus op. Bij de eerste ontvangst na een (her)start is er
        # nog geen vorige stand; dan is de toename onbekend en wordt er niets gepubliceerd.
        teller = waarden.pop(ruuvi.TELLER, None)
        if teller is not None and tellers is not None:
            vorige = tellers.get(sensor.id)
            tellers[sensor.id] = teller
            if vorige is None:
                regels.append(f"{'beweging':<17} eerste tellerstand {teller}; nog geen toename")
            else:
                waarden["beweging"] = ruuvi.toename(vorige, teller)
                regels.append(f"{'beweging':<17} teller {vorige} -> {teller}"
                              f" = toename {waarden['beweging']}")
        if stap:
            stap("gedecodeerd", "\n".join(regels))
    elif sensor.type in victron.LEVERT:
        data = fabrikant_data.get(victron.FABRIKANT_ID)
        if data is None or not victron.is_instant_readout(data):
            return []  # een andere advertentie van het apparaat (bijv. voor VictronConnect)
        data = bytes(data)
        if stap:
            stap("ruw", f"{bron}Victron {data.hex(' ')}   rssi {rssi} dBm")
        try:
            ontsleuteld, velden = victron.decodeer_velden(data, sensor.sleutel, sensor.type)
        except victron.VictronFout as e:
            # Een verkeerde sleutel of een verkeerd type is een fout in de configuratie en
            # komt elke seconde terug: één keer als waarschuwing, daarna alleen in debug.
            niveau = logging.DEBUG if sensor.id in _gewaarschuwd else logging.WARNING
            _gewaarschuwd.add(sensor.id)
            log.log(niveau, "%s (%s): %s", sensor.naam, sensor.id, e)
            if stap:
                stap("gedecodeerd", f"FOUT: {e} – advertentie overgeslagen")
            return []
        # Wat het apparaat als "onbekend" uitzendt (waarde None) wordt niet gepubliceerd
        # (§5.2.3 punt 4), bijv. de resterende tijd van de BMV tijdens laden.
        waarden = {v.grootheid: v.waarde for v in velden if v.waarde is not None}
        if stap:
            stap("gedecodeerd", "\n".join(
                [f"ontsleuteld: {ontsleuteld.hex(' ')}"]
                + [f"{v.grootheid:<17} {v.bron} = {v.ruw}  ({v.uitleg}) = "
                   + ("onbekend (niet gepubliceerd)" if v.waarde is None else f"{v.waarde:g}")
                   for v in velden]))
    else:
        return []

    if not waarden:
        return []

    # RSSI meten we zelf (de scanner), los van wat de sensor uitzendt.
    if rssi is not None:
        waarden["rssi"] = rssi

    # Alleen de grootheden uit de lijst van de sensor (§5.4.2 punt 1); de rest wordt hier
    # weggegooid en komt niet op de bus. Zonder lijst gaat alles door.
    regels = []
    if sensor.grootheden is not None:
        weg = [g for g in waarden if g not in sensor.grootheden]
        for g in weg:
            del waarden[g]
        if weg:
            regels.append(f"weggelaten (niet in 'grootheden'): {', '.join(weg)}")

    # Correcties uit de configuratie: gemeten × factor + offset (§5.4.2 punt 2),
    # bijv. temperatuur − 0,3 °C.
    for g, c in sensor.correctie.items():
        if g in waarden:
            nieuw = c.toepassen(waarden[g])
            regels.append(f"{g:<17} {waarden[g]:g} {c} = {nieuw:g}")
            waarden[g] = nieuw
    if stap:
        stap("gecorrigeerd", "\n".join(regels) or "geen correcties ingesteld")

    # meting.maak controleert de grootheid en zet de vaste soort erbij (§5.3.3).
    return [meting.maak(ts, sensor.id, g, w) for g, w in waarden.items()]


async def draai(config: Config, bus, stop: asyncio.Event) -> None:
    """Scant tot `stop` gezet wordt (bij SIGTERM van systemd).

    Gaat er iets mis met Bluetooth, dan loopt de exceptie door en stopt de service;
    systemd start hem daarna opnieuw (Restart=always, §3.1).
    """
    # bleak pas hier importeren: de tests van verwerk() hebben dan geen bleak nodig.
    from bleak import BleakScanner
    from brms.simulatie import Simulator
    from brms.trace import Tracer

    # De sensoren die deze reader moet verwerken, op MAC-adres.
    sensoren = {s.id: s for s in config.sensoren.values() if s.type in _TYPES}
    if not sensoren:
        log.warning("geen BLE-sensoren geconfigureerd")

    # Luistert naar trace-opdrachten van de monitor (§10.2); standaard staat trace uit.
    tracer = Tracer(bus, "ble-reader")
    tracer.start()

    # Luistert naar simulatie-opdrachten (`brms simuleer`, §10.3); standaard loopt er geen.
    simulator = Simulator(bus, sensoren, dienst="ble-reader")
    simulator.start()

    # De ene ingang voor alle advertenties: van bleak én van de simulator.
    ontvang = Ontvanger(sensoren, bus, tracer, simulator)

    def bij_advertentie(device, adv):
        # Wordt door bleak aangeroepen in de asyncio-loop, voor elke advertentie van elk
        # apparaat in de buurt.
        ontvang(device.address, adv.service_data, adv.manufacturer_data, adv.rssi)

    log.info("BLE-reader gestart; %d sensoren: %s", len(sensoren),
             ", ".join(f"{s.naam} ({s.id})" for s in sensoren.values()))
    # De scanner loopt zolang dit async-with-blok open is.
    async with BleakScanner(detection_callback=bij_advertentie):
        # De simulator draait als taak in dezelfde asyncio-loop; hij doet niets zolang er
        # geen simulatie loopt.
        taak = asyncio.create_task(simulator.draai(stop, ontvang))
        # Meldt op de bus welke sensoren gesimuleerd worden (ook als dat er geen zijn); het
        # web toont daarmee "Actief", "Simulatie" of "Demo" (§10.3 punt 10).
        melder = asyncio.create_task(simulator.meld_steeds(stop))
        await stop.wait()
        await asyncio.gather(taak, melder)
    log.info("BLE-reader gestopt")


class Ontvanger:
    """De ingang van de BLE-reader: elke advertentie komt hier binnen, of hij nu van de
    Bluetooth-adapter (bleak) komt of van de simulator (§10.3).

    Aanroepen als functie:  ontvang(adres, service_data, fabrikant_data, rssi)

    Wordt alleen in de asyncio-loop aangeroepen (door bleak en door de simulator-taak);
    een lock is daarom niet nodig.
    """

    def __init__(self, sensoren: dict[str, Sensor], bus, tracer, simulator):
        self._sensoren = sensoren
        self._bus = bus
        self._tracer = tracer
        self._simulator = simulator
        # Laatst ontvangen stand van de bewegingsteller per Ruuvi (§5.2.3 punt 5). Alleen in
        # het geheugen: na een herstart begint de reader opnieuw en gaat hooguit de beweging
        # tussen de laatste ontvangst vóór en de eerste ontvangst ná de herstart verloren.
        self._tellers: dict[str, int] = {}
        # Per sensor of de laatste advertentie gesimuleerd was (zie __call__).
        self._gesimuleerd: dict[str, bool] = {}

    def __call__(self, adres: str, service_data: dict, fabrikant_data: dict,
                 rssi: int | None, gesimuleerd: bool = False) -> None:
        sensor_id = adres.upper()
        # Alleen geconfigureerde apparaten (§5.2.1 punt 3). verwerk() controleert dit ook;
        # hier staat het vooraan omdat de scanner vooral advertenties van vreemde apparaten
        # ziet, en die zo het snelst afvallen.
        if sensor_id not in self._sensoren:
            return

        # Bypass (§10.3 punt 4): zolang de simulatie voor deze sensor loopt, worden zijn
        # echte advertenties genegeerd.
        if not gesimuleerd and self._simulator.actief(sensor_id):
            return

        # Bij de overgang van echt naar gesimuleerd (en terug) klopt de vorige stand van de
        # Ruuvi-bewegingsteller niet meer: de sprong tussen de echte en de gesimuleerde
        # teller is geen beweging. Vergeten dus; de eerste advertentie daarna geeft dan
        # geen `beweging`, net als na een herstart.
        if self._gesimuleerd.get(sensor_id, False) != gesimuleerd:
            self._gesimuleerd[sensor_id] = gesimuleerd
            self._tellers.pop(sensor_id, None)

        # De ontvangsttijd is "nu" (§5.2.1 punt 4).
        metingen = verwerk(adres, service_data, fabrikant_data, rssi, int(time.time()),
                           self._sensoren, self._tracer, self._tellers, gesimuleerd)
        regels = []
        for m in metingen:
            # Is de broker onbereikbaar, dan gooit de bus de meting weg (§3.1 punt 5).
            gelukt = self._bus.publiceer_meting(m)
            regels.append(f"{m.topic()}  {m.naar_json()}" if gelukt
                          else f"{m.topic()}  WEGGEGOOID (broker niet bereikbaar)")
        # Eén tracebericht voor alle metingen uit deze advertentie.
        stap = self._tracer.voor(sensor_id) if metingen else None
        if stap:
            stap("gepubliceerd", "\n".join(regels))
