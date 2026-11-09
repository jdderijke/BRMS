"""Een meting zoals readers die publiceren (ARCHITECTURE.md §5.2.1, §3.3)."""

import json
import math
from dataclasses import asdict, dataclass

from brms import grootheden

TOPIC_PREFIX = "brms/meting"


@dataclass(frozen=True)
class Meting:
    ts: int          # ontvangsttijd op de Pi, Unix-seconden UTC
    sensor_id: str   # vaste ID uit de configuratie (bij BLE: MAC-adres)
    grootheid: str   # naam uit brms.grootheden
    waarde: float
    soort: str       # vast per grootheid

    def topic(self) -> str:
        return f"{TOPIC_PREFIX}/{self.sensor_id}/{self.grootheid}"

    def naar_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def van_json(cls, data: bytes | str) -> "Meting":
        """Leest een bericht van de bus; ValueError als het geen geldige meting is."""
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
    """Maakt een gecontroleerde Meting; de soort volgt uit de tabel van grootheden.

    Een meegegeven soort moet daarmee overeenkomen. ValueError bij ongeldige invoer.
    """
    if grootheid not in grootheden.GROOTHEDEN:
        raise ValueError(f"onbekende grootheid: {grootheid!r}")
    verwacht = grootheden.soort(grootheid)
    if soort is not None and soort != verwacht:
        raise ValueError(f"soort {soort!r} past niet bij {grootheid!r} ({verwacht})")
    if isinstance(ts, bool) or not isinstance(ts, int):
        raise ValueError(f"ts is geen geheel getal: {ts!r}")
    if not isinstance(sensor_id, str) or not sensor_id:
        raise ValueError(f"ongeldige sensor_id: {sensor_id!r}")
    if isinstance(waarde, bool) or not isinstance(waarde, (int, float)) or not math.isfinite(waarde):
        raise ValueError(f"ongeldige waarde: {waarde!r}")
    return Meting(ts, sensor_id, grootheid, float(waarde), verwacht)
