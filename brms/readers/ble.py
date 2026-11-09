"""BLE-reader: één scanner voor alle Bluetooth-sensoren (ARCHITECTURE.md §5.2.1).

Werking:
1. bleak (Bluetooth-bibliotheek, asyncio) scant continu en roept voor elke ontvangen
   advertentie `bij_advertentie` aan.
2. Alleen apparaten die in de configuratie staan worden verwerkt. De scanner ziet ook
   sensoren van andere boten in de haven; die worden genegeerd.
3. Het sensortype uit de configuratie bepaalt welke decoder de bytes omzet in waarden.
   Nu alleen BTHome (Xiaomi met pvvx); Ruuvi en Victron volgen later (§5.2.3).
4. De correcties uit de configuratie (offsets) worden toegepast.
5. De signaalsterkte (RSSI) gaat mee als eigen grootheid: handig om het bereik van de
   Bluetooth-ontvangst aan boord te beoordelen (OPEN-2).
6. Elke waarde gaat als Meting naar de MQTT-bus; de alarmlaag en de opslag luisteren mee.

Er is maar één Bluetooth-adapter; meerdere scanners naast elkaar zitten elkaar in de weg.
Daarom is er één scanner die per advertentie de juiste decoder kiest.
"""

import asyncio
import logging
import time

from brms import meting
from brms.config import Config, Sensor
from brms.meting import Meting
from brms.readers import bthome

log = logging.getLogger(__name__)

# Sensortypen die deze reader verwerkt (zie config.BLE_TYPES).
_TYPES = ("bthome",)


def verwerk(adres: str, service_data: dict[str, bytes], rssi: int | None, ts: int,
            sensoren: dict[str, Sensor], tracer=None) -> list[Meting]:
    """Zet één advertentie om in een lijst metingen.

    Zuivere functie (geen Bluetooth, geen netwerk), zodat hij te testen is met opgenomen
    advertenties (tests/test_ble.py).

    adres         MAC-adres van de zender, zoals bleak het aanlevert
    service_data  {UUID: bytes} uit de advertentie
    rssi          signaalsterkte in dBm, of None als onbekend
    ts            ontvangsttijd (Unix-seconden UTC); een advertentie bevat zelf geen tijd
    sensoren      geconfigureerde sensoren, {MAC in hoofdletters: Sensor}
    tracer        optioneel (brms.trace.Tracer): staat trace voor deze sensor aan, dan
                  wordt elke stap gemeld (§10.2)
    """
    # Alleen geconfigureerde apparaten (§5.2.1 punt 3).
    sensor = sensoren.get(adres.upper())
    if sensor is None:
        return []

    # stap(naam, tekst) als trace voor deze sensor aanstaat, anders None. Alle tracetekst
    # wordt alleen opgebouwd als 'stap' bestaat, zodat trace niets kost als hij uit staat.
    stap = tracer.voor(sensor.id) if tracer else None

    # Decoder kiezen op basis van het sensortype uit de configuratie.
    if sensor.type == "bthome":
        data = service_data.get(bthome.FCD2_UUID)
        if data is None:
            return []  # advertentie zonder BTHome-gegevens (komt voor bij pvvx)
        data = bytes(data)
        if stap:
            stap("ruw", f"BTHome {data.hex(' ')}   rssi {rssi} dBm")
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
    else:
        return []

    if not waarden:
        return []

    # Correcties (offsets) uit de configuratie toepassen, bijv. temperatuur -0,3 °C.
    regels = []
    for g, offset in sensor.correctie.items():
        if g in waarden:
            nieuw = round(waarden[g] + offset, 3)
            regels.append(f"{g:<17} {waarden[g]:g} {'+' if offset >= 0 else '−'} "
                          f"{abs(offset):g} = {nieuw:g}")
            waarden[g] = nieuw
    if stap:
        stap("gecorrigeerd", "\n".join(regels) or "geen correcties ingesteld")

    # RSSI meten we zelf (de scanner), los van wat de sensor uitzendt.
    if rssi is not None:
        waarden["rssi"] = rssi

    # meting.maak controleert de grootheid en zet de vaste soort erbij (§5.3.3).
    return [meting.maak(ts, sensor.id, g, w) for g, w in waarden.items()]


async def draai(config: Config, bus, stop: asyncio.Event) -> None:
    """Scant tot `stop` gezet wordt (bij SIGTERM van systemd).

    Gaat er iets mis met Bluetooth, dan loopt de exceptie door en stopt de service;
    systemd start hem daarna opnieuw (Restart=always, §3.1).
    """
    # bleak pas hier importeren: de tests van verwerk() hebben dan geen bleak nodig.
    from bleak import BleakScanner
    from brms.trace import Tracer

    # De sensoren die deze reader moet verwerken, op MAC-adres.
    sensoren = {s.id: s for s in config.sensoren.values() if s.type in _TYPES}
    if not sensoren:
        log.warning("geen BLE-sensoren geconfigureerd")

    # Luistert naar trace-opdrachten van de monitor (§10.2); standaard staat trace uit.
    tracer = Tracer(bus, "ble-reader")
    tracer.start()

    def bij_advertentie(device, adv):
        # Wordt door bleak aangeroepen in de asyncio-loop, voor elke advertentie van elk
        # apparaat in de buurt. De ontvangsttijd is "nu" (§5.2.1 punt 4).
        metingen = verwerk(device.address, adv.service_data, adv.rssi, int(time.time()),
                           sensoren, tracer)
        regels = []
        for m in metingen:
            # Is de broker onbereikbaar, dan gooit de bus de meting weg (§3.1 punt 5).
            gelukt = bus.publiceer_meting(m)
            regels.append(f"{m.topic()}  {m.naar_json()}" if gelukt
                          else f"{m.topic()}  WEGGEGOOID (broker niet bereikbaar)")
        # Eén tracebericht voor alle metingen uit deze advertentie.
        stap = tracer.voor(metingen[0].sensor_id) if metingen else None
        if stap:
            stap("gepubliceerd", "\n".join(regels))

    log.info("BLE-reader gestart; %d sensoren: %s", len(sensoren),
             ", ".join(f"{s.naam} ({s.id})" for s in sensoren.values()))
    # De scanner loopt zolang dit async-with-blok open is.
    async with BleakScanner(detection_callback=bij_advertentie):
        await stop.wait()
    log.info("BLE-reader gestopt")
