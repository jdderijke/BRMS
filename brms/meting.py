"""Een meting zoals readers die publiceren (ARCHITECTURE.md §5.2.1, §3.3).

Een Meting is het enige wat over de bus gaat tussen readers en de afnemers (alarmlaag,
opslag, later eventueel Home Assistant). Op die plek is de meting compleet: juiste
sensor-ID, grootheid, gecorrigeerde waarde en soort (§3.1).

Op de bus:
    topic   brms/meting/<sensor_id>/<grootheid>
    inhoud  {"ts": ..., "sensor_id": ..., "grootheid": ..., "waarde": ..., "soort": ...}

Het bericht is compleet: ook wat in de topic staat, staat in de inhoud (§3.3 punt 1).
Er gaat geen eenheid mee; elke grootheid heeft één vaste eenheid (§5.3.3).
"""

import json
import math
from dataclasses import asdict, dataclass

from brms import grootheden

# Alle metingen vallen onder deze topic; abonneren op alles = "brms/meting/#".
TOPIC_PREFIX = "brms/meting"


@dataclass(frozen=True)
class Meting:
    ts: int          # ontvangsttijd op de Pi, Unix-seconden UTC (geheel getal, §3.3 punt 3)
    sensor_id: str   # vaste ID uit de configuratie (bij BLE: MAC-adres)
    grootheid: str   # naam uit brms.grootheden
    waarde: float
    soort: str       # doorlopend / toestand / toename; vast per grootheid

    def topic(self) -> str:
        return f"{TOPIC_PREFIX}/{self.sensor_id}/{self.grootheid}"

    def naar_json(self) -> str:
        # Compact (geen spaties) – scheelt bytes bij elke meting.
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def van_json(cls, data: bytes | str) -> "Meting":
        """Leest een bericht van de bus; ValueError als het geen geldige meting is.

        Een afnemer vertrouwt de inhoud niet blind: alles gaat opnieuw door `maak`.
        """
        try:
            d = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise ValueError(f"geen geldige JSON: {e}") from None
        if not isinstance(d, dict):
            raise ValueError("bericht is geen JSON-object")
        try:
            return maak(d["ts"], d["sensor_id"], d["grootheid"], d["waarde"], d["soort"])
        except KeyError as e:
            raise ValueError(f"veld ontbreekt: {e}") from None


def maak(ts, sensor_id, grootheid, waarde, soort=None) -> Meting:
    """Maakt een gecontroleerde Meting. ValueError bij ongeldige invoer.

    - De grootheid moet in de tabel van grootheden staan (§5.3.3).
    - De soort volgt uit die tabel. Wordt een soort meegegeven (bij een bericht van de
      bus), dan moet die overeenkomen.
    - ts moet een geheel getal zijn; waarde een eindig getal (geen NaN of oneindig).
    """
    if grootheid not in grootheden.GROOTHEDEN:
        raise ValueError(f"onbekende grootheid: {grootheid!r}")
    verwacht = grootheden.soort(grootheid)
    if soort is not None and soort != verwacht:
        raise ValueError(f"soort {soort!r} past niet bij {grootheid!r} ({verwacht})")
    # bool is in Python een subtype van int; True als tijdstip of waarde is altijd een fout.
    if isinstance(ts, bool) or not isinstance(ts, int):
        raise ValueError(f"ts is geen geheel getal: {ts!r}")
    if not isinstance(sensor_id, str) or not sensor_id:
        raise ValueError(f"ongeldige sensor_id: {sensor_id!r}")
    if isinstance(waarde, bool) or not isinstance(waarde, (int, float)) or not math.isfinite(waarde):
        raise ValueError(f"ongeldige waarde: {waarde!r}")
    return Meting(ts, sensor_id, grootheid, float(waarde), verwacht)
