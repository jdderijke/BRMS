"""BLE-reader: één scanner voor alle BLE-sensoren (ARCHITECTURE.md §5.2.1).

Elke advertentie van een geconfigureerd apparaat gaat naar de decoder van dat sensortype;
daarna worden de correcties uit de configuratie toegepast en gaat elke waarde (plus de
RSSI) als Meting naar de bus. Alle andere apparaten worden genegeerd.
"""

import asyncio
import logging
import time

from brms import meting
from brms.config import Config, Sensor
from brms.meting import Meting
from brms.readers import bthome

log = logging.getLogger(__name__)


def verwerk(adres: str, service_data: dict[str, bytes], rssi: int | None, ts: int,
            sensoren: dict[str, Sensor]) -> list[Meting]:
    """Zet één advertentie om in metingen. Zuivere functie (testbaar zonder Bluetooth)."""
    sensor = sensoren.get(adres.upper())
    if sensor is None:
        return []

    if sensor.type == "bthome":
        data = service_data.get(bthome.FCD2_UUID)
        if data is None:
            return []
        try:
            waarden = bthome.decodeer(bytes(data))
        except bthome.BTHomeFout as e:
            log.debug("%s (%s): %s", sensor.naam, sensor.id, e)
            return []
    else:
        return []

    if not waarden:
        return []
    for g, offset in sensor.correctie.items():
        if g in waarden:
            waarden[g] = round(waarden[g] + offset, 3)
    if rssi is not None:
        waarden["rssi"] = rssi
    return [meting.maak(ts, sensor.id, g, w) for g, w in waarden.items()]


async def draai(config: Config, bus, stop: asyncio.Event) -> None:
    """Scant tot `stop` gezet wordt. Een Bluetooth-fout beëindigt de service (systemd herstart)."""
    from bleak import BleakScanner  # pas hier: tests van verwerk() hebben geen bleak nodig

    sensoren = {s.id: s for s in config.sensoren.values() if s.type in ("bthome",)}
    if not sensoren:
        log.warning("geen BLE-sensoren geconfigureerd")

    def bij_advertentie(device, adv):
        for m in verwerk(device.address, adv.service_data, adv.rssi, int(time.time()), sensoren):
            bus.publiceer_meting(m)

    log.info("BLE-reader gestart; %d sensoren: %s", len(sensoren),
             ", ".join(f"{s.naam} ({s.id})" for s in sensoren.values()))
    async with BleakScanner(detection_callback=bij_advertentie):
        await stop.wait()
    log.info("BLE-reader gestopt")
