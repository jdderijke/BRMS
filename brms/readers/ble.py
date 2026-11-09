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
            sensoren: dict[str, Sensor]) -> list[Meting]:
    """Zet één advertentie om in een lijst metingen.

    Zuivere functie (geen Bluetooth, geen netwerk), zodat hij te testen is met opgenomen
    advertenties (tests/test_ble.py).

    adres         MAC-adres van de zender, zoals bleak het aanlevert
    service_data  {UUID: bytes} uit de advertentie
    rssi          signaalsterkte in dBm, of None als onbekend
    ts            ontvangsttijd (Unix-seconden UTC); een advertentie bevat zelf geen tijd
    sensoren      geconfigureerde sensoren, {MAC in hoofdletters: Sensor}
    """
    # Alleen geconfigureerde apparaten (§5.2.1 punt 3).
    sensor = sensoren.get(adres.upper())
    if sensor is None:
        return []

    # Decoder kiezen op basis van het sensortype uit de configuratie.
    if sensor.type == "bthome":
        data = service_data.get(bthome.FCD2_UUID)
        if data is None:
            return []  # advertentie zonder BTHome-gegevens (komt voor bij pvvx)
        try:
            waarden = bthome.decodeer(bytes(data))
        except bthome.BTHomeFout as e:
            # Een kapot bericht is geen reden om te stoppen; het volgende komt zo.
            log.debug("%s (%s): %s", sensor.naam, sensor.id, e)
            return []
    else:
        return []

    if not waarden:
        return []

    # Correcties (offsets) uit de configuratie toepassen, bijv. temperatuur -0,3 °C.
    for g, offset in sensor.correctie.items():
        if g in waarden:
            waarden[g] = round(waarden[g] + offset, 3)

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

    # De sensoren die deze reader moet verwerken, op MAC-adres.
    sensoren = {s.id: s for s in config.sensoren.values() if s.type in _TYPES}
    if not sensoren:
        log.warning("geen BLE-sensoren geconfigureerd")

    def bij_advertentie(device, adv):
        # Wordt door bleak aangeroepen in de asyncio-loop, voor elke advertentie van elk
        # apparaat in de buurt. De ontvangsttijd is "nu" (§5.2.1 punt 4).
        for m in verwerk(device.address, adv.service_data, adv.rssi, int(time.time()), sensoren):
            # Is de broker onbereikbaar, dan gooit de bus de meting weg (§3.1 punt 5).
            bus.publiceer_meting(m)

    log.info("BLE-reader gestart; %d sensoren: %s", len(sensoren),
             ", ".join(f"{s.naam} ({s.id})" for s in sensoren.values()))
    # De scanner loopt zolang dit async-with-blok open is.
    async with BleakScanner(detection_callback=bij_advertentie):
        await stop.wait()
    log.info("BLE-reader gestopt")
