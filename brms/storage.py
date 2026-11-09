"""Opslag-service: metingen van de bus naar SQLite (ARCHITECTURE.md §5.3).

De readers leveren veel vaker waarden dan nodig (pvvx elke paar seconden). Opslaan van elke
waarde zou tienduizenden rijen per sensor per dag opleveren. Daarom verzamelt deze service
de metingen en schrijft ze eens per 5 minuten weg (§5.3.2), afhankelijk van de soort:

- Doorlopend (temperatuur, vocht, ...): het **gemiddelde** over het interval.
- Toename (Ruuvi-bewegingsteller): de **som** over het interval.
- Toestand (0/1: walstroom, bilge): nooit gemiddeld (0,5 bestaat niet). Een rij bij
  **elke verandering** met het exacte tijdstip, plus per interval de actuele toestand.

Verder:
- Een interval zonder ontvangen data levert géén rij op (en zeker geen 0: "walstroom 0"
  betekent iets anders dan "geen bericht").
- Eén commit per opslagmoment, zonder extra buffering: bij stroomuitval gaat hooguit het
  lopende interval verloren.
- Er wordt pas geschreven als de systeemklok gesynchroniseerd is (§4.2). Daarvóór zouden
  tijdstempels fout zijn; die metingen worden weggegooid.
- De opslag-service is de enige die naar de database schrijft (§3.1).
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

# Lengte van een opslaginterval in seconden (5 minuten, §5.3.2).
INTERVAL = 300

# Databaseschema (§5.3.1). Ook de tabel voor camera-opnames wordt alvast aangemaakt.
# WITHOUT ROWID met de primaire sleutel (sensor_id, grootheid, ts) slaat elke reeks
# gesorteerd op: een grafiek is één aaneengesloten leesactie, en dubbele rijen zijn onmogelijk.
SCHEMA = """
CREATE TABLE IF NOT EXISTS meting (
    ts         INTEGER NOT NULL,  -- Unix-tijd UTC, seconden
    sensor_id  TEXT    NOT NULL,  -- vaste ID uit de configuratie (bij BLE het MAC-adres)
    grootheid  TEXT    NOT NULL,  -- naam uit brms.grootheden
    waarde     REAL    NOT NULL,  -- aan/uit als 0/1
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

# Eén rij in de tabel meting: (ts, sensor_id, grootheid, waarde).
Rij = tuple[int, str, str, float]


class Database:
    """Dunne laag om sqlite3 heen."""

    def __init__(self, pad: str):
        # Map aanmaken als die nog niet bestaat (handig bij testen; op de Pi maakt
        # install.sh /var/lib/brms aan).
        map_ = os.path.dirname(pad)
        if map_:
            os.makedirs(map_, exist_ok=True)
        self._con = sqlite3.connect(pad)
        # WAL: bestand blijft consistent bij stroomuitval (P6) en lezers (web) blokkeren de
        # schrijver niet.
        self._con.execute("PRAGMA journal_mode=WAL")
        # NORMAL is de gangbare instelling bij WAL: minder vaak fsync (minder schrijven naar
        # flash, P7). Bij stroomuitval kan hooguit de laatste commit verloren gaan; de
        # database raakt niet beschadigd.
        self._con.execute("PRAGMA synchronous=NORMAL")
        self._con.executescript(SCHEMA)

    def schrijf(self, rijen: list[Rij]) -> None:
        """Schrijft alle rijen in één transactie (één commit per opslagmoment)."""
        # "with self._con" = één transactie: commit bij succes, rollback bij een fout.
        # OR REPLACE: valt een veranderingsrij van een toestand precies op het opslagmoment,
        # dan bestaat dezelfde sleutel twee keer; de waarde is dan gelijk, dus vervangen is veilig.
        with self._con:
            self._con.executemany(
                "INSERT OR REPLACE INTO meting (ts, sensor_id, grootheid, waarde) VALUES (?, ?, ?, ?)",
                rijen)

    def sluit(self) -> None:
        self._con.close()


class Aggregator:
    """Verzamelt metingen tussen twee opslagmomenten.

    Zuiver (geen database, geen netwerk, geen klok), zodat de opslagregels los te testen
    zijn (tests/test_storage.py). De sleutel is steeds (sensor_id, grootheid).
    """

    def __init__(self):
        # Doorlopend: lopende som en aantal, voor het gemiddelde.
        self._som: dict[tuple[str, str], list[float]] = {}
        # Toename: lopende som.
        self._toename: dict[tuple[str, str], float] = {}
        # Toestand: laatst bekende waarde. Blijft bewaard over intervallen heen, zodat een
        # verandering ook herkend wordt als die in een later interval valt.
        self._toestand: dict[tuple[str, str], float] = {}
        # Toestanden waarvan in dit interval iets is ontvangen.
        self._toestand_gezien: set[tuple[str, str]] = set()
        # Veranderingsrijen van toestanden, met het exacte tijdstip.
        self._wijzigingen: list[Rij] = []

    def voeg_toe(self, m: Meting) -> None:
        sleutel = (m.sensor_id, m.grootheid)
        # De soort komt uit de tabel in de code, niet uit het bericht (§5.3.2).
        soort = grootheden.soort(m.grootheid)
        if soort == grootheden.DOORLOPEND:
            s = self._som.setdefault(sleutel, [0.0, 0])
            s[0] += m.waarde
            s[1] += 1
        elif soort == grootheden.TOENAME:
            self._toename[sleutel] = self._toename.get(sleutel, 0.0) + m.waarde
        else:  # toestand
            # De eerste waarde na het starten telt ook als verandering.
            if self._toestand.get(sleutel) != m.waarde:
                self._wijzigingen.append((m.ts, m.sensor_id, m.grootheid, m.waarde))
                self._toestand[sleutel] = m.waarde
            self._toestand_gezien.add(sleutel)

    def leeg(self, ts: int) -> list[Rij]:
        """Geeft alle rijen voor opslagmoment `ts` en begint een nieuw interval.

        Gemiddelden, sommen en actuele toestanden krijgen tijdstip `ts` (het einde van het
        interval); veranderingsrijen houden hun eigen tijdstip.
        """
        rijen: list[Rij] = list(self._wijzigingen)
        rijen += [(ts, s, g, round(som / n, 4)) for (s, g), (som, n) in self._som.items()]
        rijen += [(ts, s, g, w) for (s, g), w in self._toename.items()]
        # Actuele toestand alleen voor toestanden waarvan in dit interval iets binnenkwam.
        rijen += [(ts, s, g, self._toestand[(s, g)]) for (s, g) in self._toestand_gezien]
        self._som.clear()
        self._toename.clear()
        self._toestand_gezien.clear()
        self._wijzigingen.clear()
        return rijen


def klok_gesynchroniseerd() -> bool:
    """True als systemd meldt dat de systeemklok gesynchroniseerd is (§4.2).

    Dit is dezelfde status als "System clock synchronized: yes" in `timedatectl`.
    Op een systeem zonder timedatectl (bijv. Windows) is het antwoord False.
    """
    try:
        r = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.stdout.strip() == "yes"


def volgend_moment(nu: float) -> int:
    """Eerstvolgend opslagmoment: een veelvoud van INTERVAL in Unix-tijd.

    Daardoor vallen de opslagmomenten op vaste kloktijden (…:00, …:05, …:10) en liggen
    alle reeksen in de database op dezelfde tijdstippen.
    """
    return (int(nu) // INTERVAL + 1) * INTERVAL


async def draai(config: Config, bus, stop: asyncio.Event, klok_ok=klok_gesynchroniseerd) -> None:
    """Hoofdlus van de opslag-service; loopt tot `stop` gezet wordt.

    klok_ok is te vervangen in tests (op Windows is er geen timedatectl).
    """
    db = Database(config.opslag.database)
    agg = Aggregator()
    # Alle metingen van alle readers. QoS 1, zodat toestanden (met QoS 1 gepubliceerd)
    # ook met QoS 1 worden afgeleverd.
    q = bus.wachtrij(f"{TOPIC_PREFIX}/#", qos=1)
    moment = volgend_moment(time.time())
    log.info("opslag gestart; database %s", config.opslag.database)
    try:
        while not stop.is_set():
            nu = time.time()

            # Is de klok teruggesprongen (bijv. bij de eerste tijdsynchronisatie), dan ligt
            # het geplande moment ver in de toekomst; opnieuw plannen.
            if moment - nu > INTERVAL:
                moment = volgend_moment(nu)

            # Opslagmoment bereikt: verzamelde rijen wegschrijven.
            if nu >= moment:
                rijen = agg.leeg(moment)
                if rijen:
                    if klok_ok():
                        try:
                            db.schrijf(rijen)
                            log.debug("%d rijen opgeslagen", len(rijen))
                        except sqlite3.Error:
                            # Bijv. schijf vol. Loggen en doorgaan; de volgende keer
                            # lukt het misschien wel.
                            log.exception("schrijven naar database mislukt; %d rijen verloren", len(rijen))
                    else:
                        log.warning("klok niet gesynchroniseerd; %d rijen weggegooid", len(rijen))
                moment = volgend_moment(nu)
                continue

            # Wachten op het volgende bericht, maar hooguit tot het opslagmoment en
            # hooguit 1 s, zodat een stopverzoek snel wordt opgemerkt.
            try:
                topic, payload = await asyncio.wait_for(q.get(), timeout=min(moment - nu, 1.0))
            except TimeoutError:
                continue

            # Bericht controleren en verzamelen. Een ongeldig bericht wordt gelogd en
            # overgeslagen; het mag de service niet laten stoppen.
            try:
                agg.voeg_toe(Meting.van_json(payload))
            except ValueError as e:
                log.warning("ongeldig bericht op %s genegeerd: %s", topic, e)
    finally:
        db.sluit()
        log.info("opslag gestopt")
