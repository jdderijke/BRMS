"""Opslag-service: metingen van de bus naar SQLite (ARCHITECTURE.md §5.3).

- Doorlopend: gemiddelde per interval van 5 minuten.
- Toestand: een rij bij elke verandering (exact tijdstip), plus per interval de actuele
  toestand – alleen als er in dat interval iets van die sensor binnenkwam.
- Toename: som per interval.
Een interval zonder data levert geen rij op. Eén commit per opslagmoment. Er wordt pas
geschreven als de systeemklok gesynchroniseerd is (§4.2); daarvóór wordt weggegooid.
De opslag-service is de enige schrijver van de database.
"""

import asyncio
import logging
import os
import sqlite3
import subprocess
import time

from brms import grootheden
from brms.config import Config
from brms.meting import TOPIC_PREFIX, Meting

log = logging.getLogger(__name__)

INTERVAL = 300  # seconden

SCHEMA = """
CREATE TABLE IF NOT EXISTS meting (
    ts         INTEGER NOT NULL,
    sensor_id  TEXT    NOT NULL,
    grootheid  TEXT    NOT NULL,
    waarde     REAL    NOT NULL,
    PRIMARY KEY (sensor_id, grootheid, ts)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS opname (
    ts          INTEGER NOT NULL,
    camera_id   TEXT    NOT NULL,
    type        TEXT    NOT NULL,
    duur        INTEGER,
    bestand     TEXT    NOT NULL,
    bytes       INTEGER NOT NULL,
    aanleiding  TEXT    NOT NULL,
    PRIMARY KEY (camera_id, ts)
) WITHOUT ROWID;
"""

Rij = tuple[int, str, str, float]  # (ts, sensor_id, grootheid, waarde)


class Database:
    def __init__(self, pad: str):
        map_ = os.path.dirname(pad)
        if map_:
            os.makedirs(map_, exist_ok=True)
        self._con = sqlite3.connect(pad)
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.execute("PRAGMA synchronous=NORMAL")
        self._con.executescript(SCHEMA)

    def schrijf(self, rijen: list[Rij]) -> None:
        """Schrijft alle rijen in één transactie."""
        with self._con:
            self._con.executemany(
                "INSERT OR REPLACE INTO meting (ts, sensor_id, grootheid, waarde) VALUES (?, ?, ?, ?)",
                rijen)

    def sluit(self) -> None:
        self._con.close()


class Aggregator:
    """Verzamelt metingen tussen twee opslagmomenten. Zuiver; geen I/O."""

    def __init__(self):
        self._som: dict[tuple[str, str], list[float]] = {}     # doorlopend: [som, aantal]
        self._toename: dict[tuple[str, str], float] = {}
        self._toestand: dict[tuple[str, str], float] = {}      # laatst bekende, blijft bewaard
        self._toestand_gezien: set[tuple[str, str]] = set()
        self._wijzigingen: list[Rij] = []

    def voeg_toe(self, m: Meting) -> None:
        sleutel = (m.sensor_id, m.grootheid)
        soort = grootheden.soort(m.grootheid)
        if soort == grootheden.DOORLOPEND:
            s = self._som.setdefault(sleutel, [0.0, 0])
            s[0] += m.waarde
            s[1] += 1
        elif soort == grootheden.TOENAME:
            self._toename[sleutel] = self._toename.get(sleutel, 0.0) + m.waarde
        else:
            if self._toestand.get(sleutel) != m.waarde:
                self._wijzigingen.append((m.ts, m.sensor_id, m.grootheid, m.waarde))
                self._toestand[sleutel] = m.waarde
            self._toestand_gezien.add(sleutel)

    def leeg(self, ts: int) -> list[Rij]:
        """Geeft de rijen voor opslagmoment `ts` en begint een nieuw interval."""
        rijen: list[Rij] = list(self._wijzigingen)
        rijen += [(ts, s, g, round(som / n, 4)) for (s, g), (som, n) in self._som.items()]
        rijen += [(ts, s, g, w) for (s, g), w in self._toename.items()]
        rijen += [(ts, s, g, self._toestand[(s, g)]) for (s, g) in self._toestand_gezien]
        self._som.clear()
        self._toename.clear()
        self._toestand_gezien.clear()
        self._wijzigingen.clear()
        return rijen


def klok_gesynchroniseerd() -> bool:
    """True als systemd de systeemklok gesynchroniseerd meldt (timedatectl, §4.2)."""
    try:
        r = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.stdout.strip() == "yes"


def volgend_moment(nu: float) -> int:
    """Eerstvolgend opslagmoment: een veelvoud van INTERVAL in Unix-tijd."""
    return (int(nu) // INTERVAL + 1) * INTERVAL


async def draai(config: Config, bus, stop: asyncio.Event, klok_ok=klok_gesynchroniseerd) -> None:
    db = Database(config.opslag.database)
    agg = Aggregator()
    q = bus.wachtrij(f"{TOPIC_PREFIX}/#", qos=1)
    moment = volgend_moment(time.time())
    log.info("opslag gestart; database %s", config.opslag.database)
    try:
        while not stop.is_set():
            nu = time.time()
            if moment - nu > INTERVAL:  # klok is teruggesprongen
                moment = volgend_moment(nu)
            if nu >= moment:
                rijen = agg.leeg(moment)
                if rijen:
                    if klok_ok():
                        try:
                            db.schrijf(rijen)
                            log.debug("%d rijen opgeslagen", len(rijen))
                        except sqlite3.Error:
                            log.exception("schrijven naar database mislukt; %d rijen verloren", len(rijen))
                    else:
                        log.warning("klok niet gesynchroniseerd; %d rijen weggegooid", len(rijen))
                moment = volgend_moment(nu)
                continue
            try:
                topic, payload = await asyncio.wait_for(q.get(), timeout=min(moment - nu, 1.0))
            except TimeoutError:
                continue
            try:
                agg.voeg_toe(Meting.van_json(payload))
            except ValueError as e:
                log.warning("ongeldig bericht op %s genegeerd: %s", topic, e)
    finally:
        db.sluit()
        log.info("opslag gestopt")
