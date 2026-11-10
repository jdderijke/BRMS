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

Daarnaast legt de opslag de opgetreden alarmen vast (§5.5.3 punt 8): de alarmlaag meldt
begin en einde van een alarm op de bus (brms/alarm/<alarm_id>), en de opslag schrijft die
direct in de tabel `alarm` – niet per 5 minuten, want het precieze tijdstip telt en het
zijn er weinig.

En de opnames van de camera's (§5.7 punt 14): de camera-reader schrijft het bestand en
meldt de gegevens ervan op de bus (brms/opname/<camera_id>); de opslag legt die vast in de
tabel `opname` en verwijdert eens per uur de foto's die ouder zijn dan de bewaartermijn.
Video's blijven staan tot de gebruiker ze verwijdert; alleen als ze samen meer ruimte
innemen dan `video_ruimte`, verwijdert de opslag de oudste (§5.7 punt 5). Het web vraagt
het verwijderen van opnames aan met een opdracht op de bus (§5.6 punt 2); ook dan is het
de opslag die bestand en rij verwijdert.
"""

import asyncio
import json
import logging
import os
import re
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

-- Opgetreden alarmen (§5.5.3 punt 8): één rij per alarm, met begin en einde.
CREATE TABLE IF NOT EXISTS alarm (
    alarm_id   TEXT    NOT NULL,  -- id van de alarmregel uit brms.toml, bijv. 'vorst'
    sensor_id  TEXT    NOT NULL,
    begin      INTEGER NOT NULL,  -- Unix-tijd UTC, seconden
    grootheid  TEXT,              -- NULL bij een regel van soort 'stil'
    waarde     REAL,              -- de waarde waarmee het alarm afging
    einde      INTEGER,           -- NULL zolang het alarm actief is
    reden      TEXT,              -- 'herstel', 'herstart' of 'uitgezet'
    PRIMARY KEY (alarm_id, sensor_id, begin)
) WITHOUT ROWID;

-- Alarmregels die via het web zijn uitgezet (§5.5.3 punt 7). Nog niet in gebruik: de
-- schakelaar volgt in een latere bouwstap.
CREATE TABLE IF NOT EXISTS alarm_uit (
    alarm_id   TEXT    PRIMARY KEY,
    sinds      INTEGER NOT NULL
) WITHOUT ROWID;
"""

# Topics van de alarmlaag (zie brms/alarms.py).
TOPIC_ALARM = "brms/alarm"
TOPIC_HERSTART = "brms/alarm/herstart"

# Topic van de camera-reader (zie brms/readers/camera.py).
TOPIC_OPNAME = "brms/opname"
# Opdracht van het web om opnames te verwijderen (§5.6 punt 2):
# {"opnames": [[camera_id, ts], ...]}.
TOPIC_VERWIJDER = "brms/opdracht/opslag/verwijder"
# Zoveel opnames mag één opdracht hooguit noemen.
MAX_VERWIJDER = 1000
# Om de hoeveel seconden oude foto's worden opgeruimd (§5.7 punt 14).
OPRUIM_INTERVAL = 3600
# Het pad van een opname binnen de opnamemap: <camera_id>/JJJJ/MM/DD/UUMMSS.jpg of .mp4.
# Het pad komt van de bus; alleen deze vorm wordt aangenomen, zodat het opruimen nooit
# een bestand buiten de opnamemap kan raken.
_BESTAND = re.compile(r"^[A-Za-z0-9_-]+/\d{4}/\d{2}/\d{2}/\d{6}\.(jpg|mp4)$")

# Eén rij in de tabel meting: (ts, sensor_id, grootheid, waarde).
Rij = tuple[int, str, str, float]


class Database:
    """Dunne laag om sqlite3 heen."""

    def __init__(self, pad: str):
        # Map aanmaken als die nog niet bestaat (normaal bestaat data/ al: hij staat in git
        # met een leeg .gitkeep-bestand).
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

    # --- alarmen (§5.5.3 punt 8) ---------------------------------------------------
    def alarm_begin(self, ts: int, alarm_id: str, sensor_id: str, grootheid: str | None,
                    waarde: float | None) -> None:
        """Legt het begin van een alarm vast. Staat er voor dezelfde regel en sensor nog
        een alarm open (het einde is nooit gemeld, bijv. door een herstart), dan wordt dat
        eerst afgesloten: er is per regel en sensor hooguit één alarm actief."""
        with self._con:
            self._con.execute(
                "UPDATE alarm SET einde = ?, reden = 'herstart' "
                "WHERE alarm_id = ? AND sensor_id = ? AND einde IS NULL", (ts, alarm_id, sensor_id))
            self._con.execute(
                "INSERT OR REPLACE INTO alarm (alarm_id, sensor_id, begin, grootheid, waarde) "
                "VALUES (?, ?, ?, ?, ?)", (alarm_id, sensor_id, ts, grootheid, waarde))

    def alarm_einde(self, ts: int, alarm_id: str, sensor_id: str, reden: str) -> None:
        """Sluit het openstaande alarm van deze regel en sensor af."""
        with self._con:
            self._con.execute(
                "UPDATE alarm SET einde = ?, reden = ? "
                "WHERE alarm_id = ? AND sensor_id = ? AND einde IS NULL",
                (ts, reden, alarm_id, sensor_id))

    def alarm_herstart(self, ts: int, actief=()) -> int:
        """Sluit de alarmen af die in de database open staan terwijl de alarmlaag ze niet
        (meer) actief heeft: na een herstart van de alarmlaag is geen enkel alarm nog
        actief (§5.5.2 punt 6). `actief` zijn de paren (alarm_id, sensor_id) die volgens
        de alarmlaag nu actief zijn; die blijven open. Geeft het aantal afgesloten alarmen."""
        blijft = {tuple(paar) for paar in actief}
        with self._con:
            open_ = self._con.execute(
                "SELECT alarm_id, sensor_id FROM alarm WHERE einde IS NULL").fetchall()
            weg = [paar for paar in open_ if tuple(paar) not in blijft]
            # Alleen schrijven als er iets af te sluiten is (weinig schrijven, §3.2).
            self._con.executemany(
                "UPDATE alarm SET einde = ?, reden = 'herstart' "
                "WHERE alarm_id = ? AND sensor_id = ? AND einde IS NULL",
                [(ts, *paar) for paar in weg])
        return len(weg)

    # --- opnames van de camera's (§5.7 punt 14) --------------------------------------
    def opname(self, ts: int, camera_id: str, type_: str, duur: int | None, bestand: str,
               bytes_: int, aanleiding: str) -> None:
        """Legt de gegevens van één opname vast. Twee opnames van dezelfde camera in
        dezelfde seconde hebben dezelfde bestandsnaam; de laatste geldt."""
        with self._con:
            self._con.execute(
                "INSERT OR REPLACE INTO opname (ts, camera_id, type, duur, bestand, bytes, aanleiding) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)", (ts, camera_id, type_, duur, bestand, bytes_, aanleiding))

    def oude_fotos(self, voor: int) -> list[tuple[str, int, str]]:
        """De foto's van vóór het tijdstip `voor`: (camera_id, ts, bestand)."""
        return self._con.execute(
            "SELECT camera_id, ts, bestand FROM opname WHERE type = 'foto' AND ts < ?",
            (voor,)).fetchall()

    def videos(self) -> list[tuple[str, int, str, int]]:
        """Alle video's, de oudste eerst: (camera_id, ts, bestand, bytes)."""
        return self._con.execute(
            "SELECT camera_id, ts, bestand, bytes FROM opname WHERE type = 'video' "
            "ORDER BY ts, camera_id").fetchall()

    def bestand_van(self, camera_id: str, ts: int) -> str | None:
        """Het pad van één opname binnen de opnamemap, of None als de opname niet bestaat."""
        rij = self._con.execute("SELECT bestand FROM opname WHERE camera_id = ? AND ts = ?",
                                (camera_id, ts)).fetchone()
        return rij[0] if rij else None

    def verwijder_opnames(self, sleutels: list[tuple[str, int]]) -> None:
        """Verwijdert de rijen van de opnames met deze (camera_id, ts)."""
        with self._con:
            self._con.executemany("DELETE FROM opname WHERE camera_id = ? AND ts = ?", sleutels)

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
        # Toename: lopende som en aantal (het aantal alleen voor trace).
        self._toename: dict[tuple[str, str], float] = {}
        self._toename_n: dict[tuple[str, str], int] = {}
        # Toestand: laatst bekende waarde. Blijft bewaard over intervallen heen, zodat een
        # verandering ook herkend wordt als die in een later interval valt.
        self._toestand: dict[tuple[str, str], float] = {}
        # Toestanden waarvan in dit interval iets is ontvangen.
        self._toestand_gezien: set[tuple[str, str]] = set()
        # Veranderingsrijen van toestanden, met het exacte tijdstip.
        self._wijzigingen: list[Rij] = []

    def voeg_toe(self, m: Meting) -> bool:
        """Neemt een meting op in het lopende interval.

        Geeft True terug als het een toestand is die veranderde (dan komt er een rij met
        het exacte tijdstip); anders False. Alleen gebruikt voor trace.
        """
        sleutel = (m.sensor_id, m.grootheid)
        # De soort komt uit de tabel in de code, niet uit het bericht (§5.3.2).
        soort = grootheden.soort(m.grootheid)
        if soort == grootheden.DOORLOPEND:
            s = self._som.setdefault(sleutel, [0.0, 0])
            s[0] += m.waarde
            s[1] += 1
        elif soort == grootheden.TOENAME:
            self._toename[sleutel] = self._toename.get(sleutel, 0.0) + m.waarde
            self._toename_n[sleutel] = self._toename_n.get(sleutel, 0) + 1
        else:  # toestand
            self._toestand_gezien.add(sleutel)
            # De eerste waarde na het starten telt ook als verandering.
            if self._toestand.get(sleutel) != m.waarde:
                self._wijzigingen.append((m.ts, m.sensor_id, m.grootheid, m.waarde))
                self._toestand[sleutel] = m.waarde
                return True
        return False

    def beschrijf(self, m: Meting, veranderd: bool) -> str:
        """Tussenstand voor deze grootheid in het lopende interval (voor trace, §10.2)."""
        sleutel = (m.sensor_id, m.grootheid)
        soort = grootheden.soort(m.grootheid)
        if soort == grootheden.DOORLOPEND:
            som, n = self._som[sleutel]
            return f"{m.grootheid:<17} {m.waarde:g} → nu {n} waarden, gemiddelde {som / n:.4g}"
        if soort == grootheden.TOENAME:
            return (f"{m.grootheid:<17} +{m.waarde:g} → nu {self._toename_n[sleutel]} waarden, "
                    f"som {self._toename[sleutel]:g}")
        if veranderd:
            return f"{m.grootheid:<17} {m.waarde:g} → VERANDERD: rij met exact tijdstip {m.ts}"
        return f"{m.grootheid:<17} {m.waarde:g} → ongewijzigd"

    def aantallen(self) -> dict[tuple[str, str], int]:
        """Aantal opgenomen waarden per (sensor_id, grootheid) in het lopende interval,
        voor doorlopende grootheden en toenames (voor trace bij het opslagmoment)."""
        n = {k: int(v[1]) for k, v in self._som.items()}
        n.update(self._toename_n)
        return n

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
        self._toename_n.clear()
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


def _trace_opslag(tracer, rijen: list[Rij], aantallen: dict, moment: int, resultaat: str) -> None:
    """Meldt per getraceerde sensor welke rijen bij dit opslagmoment zijn weggeschreven
    (of weggegooid), met het aantal samengevoegde waarden (§10.2, stap 'opgeslagen')."""
    tijd = time.strftime("%H:%M:%S", time.localtime(moment))
    per_sensor: dict[str, list[str]] = {}
    for ts, sid, g, w in rijen:
        if not tracer.actief(sid):
            continue
        n = aantallen.get((sid, g))
        if ts != moment:
            uitleg = "toestandsverandering (exact tijdstip)"
        elif n is None:
            uitleg = "actuele toestand"
        elif grootheden.soort(g) == grootheden.TOENAME:
            uitleg = f"som van {n} waarden"
        else:
            uitleg = f"gemiddelde van {n} waarden"
        regel_tijd = time.strftime("%H:%M:%S", time.localtime(ts))
        per_sensor.setdefault(sid, []).append(f"{regel_tijd}  {g:<17} {w:g}   ({uitleg})")
    for sid, regels in per_sensor.items():
        tekst = f"opslagmoment {tijd}: {resultaat}\n" + "\n".join(regels)
        tracer.stuur(sid, "opgeslagen", tekst)


def verwerk_alarm(db: Database, topic: str, payload: bytes) -> str | None:
    """Legt één bericht van de alarmlaag vast in de tabel `alarm`. Geeft een korte
    omschrijving terug (voor het log), of None als er niets te doen was; ValueError bij
    een ongeldig bericht."""
    try:
        d = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError(f"geen geldige JSON: {e}") from None
    if not isinstance(d, dict):
        raise ValueError("bericht is geen JSON-object")
    ts = d.get("ts")
    if isinstance(ts, bool) or not isinstance(ts, int):
        raise ValueError(f"ts is geen geheel getal: {ts!r}")
    if topic == TOPIC_HERSTART:
        # De stand van de alarmlaag: komt bij zijn start en daarna elke minuut. Zonder
        # veld `actief` is er geen alarm actief.
        actief = d.get("actief", [])
        if not isinstance(actief, list) or not all(
                isinstance(p, list) and len(p) == 2 and all(isinstance(x, str) for x in p)
                for p in actief):
            raise ValueError("actief is geen lijst van paren [alarm_id, sensor_id]")
        aantal = db.alarm_herstart(ts, actief)
        if not aantal:
            return None
        return f"alarmlaag herstart: {aantal} openstaande alarmen afgesloten"
    alarm_id, sensor_id, wat = d.get("alarm_id"), d.get("sensor_id"), d.get("gebeurtenis")
    if not isinstance(alarm_id, str) or not alarm_id or not isinstance(sensor_id, str) or not sensor_id:
        raise ValueError("alarm_id of sensor_id ontbreekt")
    if wat == "begin":
        grootheid, waarde = d.get("grootheid"), d.get("waarde")
        if grootheid is not None and grootheid not in grootheden.GROOTHEDEN:
            raise ValueError(f"onbekende grootheid: {grootheid!r}")
        if isinstance(waarde, bool) or not isinstance(waarde, (int, float, type(None))):
            raise ValueError(f"ongeldige waarde: {waarde!r}")
        db.alarm_begin(ts, alarm_id, sensor_id, grootheid, waarde)
    elif wat in ("herstel", "uitgezet"):
        db.alarm_einde(ts, alarm_id, sensor_id, wat)
    else:
        raise ValueError(f"onbekende gebeurtenis: {wat!r}")
    return f"alarm {alarm_id} voor {sensor_id}: {wat}"


def verwerk_opname(db: Database, payload: bytes) -> str:
    """Legt één bericht van de camera-reader vast in de tabel `opname`. Geeft een korte
    omschrijving terug (voor het log); ValueError bij een ongeldig bericht."""
    try:
        d = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError(f"geen geldige JSON: {e}") from None
    if not isinstance(d, dict):
        raise ValueError("bericht is geen JSON-object")

    def geheel(naam, leeg_mag=False):
        w = d.get(naam)
        if w is None and leeg_mag:
            return None
        if isinstance(w, bool) or not isinstance(w, int) or w < 0:
            raise ValueError(f"{naam} is geen geheel getal: {w!r}")
        return w

    ts, bytes_, duur = geheel("ts"), geheel("bytes"), geheel("duur", leeg_mag=True)
    camera_id, type_, bestand, aanleiding = (d.get(k) for k in ("camera_id", "type", "bestand", "aanleiding"))
    if type_ not in ("foto", "video"):
        raise ValueError(f"onbekend type: {type_!r}")
    if not isinstance(bestand, str) or not _BESTAND.match(bestand):
        raise ValueError(f"ongeldige bestandsnaam: {bestand!r}")
    # De map van het bestand is die van de camera.
    if not isinstance(camera_id, str) or bestand.split("/")[0] != camera_id:
        raise ValueError(f"camera_id {camera_id!r} hoort niet bij bestand {bestand!r}")
    if not isinstance(aanleiding, str) or not aanleiding:
        raise ValueError("aanleiding ontbreekt")
    db.opname(ts, camera_id, type_, duur, bestand, bytes_, aanleiding)
    return f"{type_} van {camera_id}: {bestand} ({bytes_} bytes, {aanleiding})"


def ruim_fotos(db: Database, map_: str, bewaren: float, nu: float) -> int:
    """Verwijdert de foto's die ouder zijn dan de bewaartermijn (§5.7 punt 14): het bestand
    en de rij in de tabel. Geeft het aantal verwijderde bestanden.

    Ook een bestand zonder rij wordt verwijderd als het oud genoeg is: het bericht van de
    camera-reader kan verloren zijn gegaan (§3.3), of er is een tijdelijk bestand
    achtergebleven. Video's blijven staan: die verdwijnen niet vanzelf (§5.7 punt 2).
    """
    grens = nu - bewaren
    aantal = 0

    # 1. Wat in de database staat.
    oud = db.oude_fotos(int(grens))
    weg = []
    for camera_id, ts, bestand in oud:
        # Alleen een pad van de bekende vorm; een rij met iets anders blijft staan.
        if not _BESTAND.match(bestand):
            log.warning("opname %s van %s heeft een onverwacht pad (%r); niet opgeruimd",
                        ts, camera_id, bestand)
            continue
        try:
            os.remove(os.path.join(map_, *bestand.split("/")))
            aantal += 1
        except FileNotFoundError:
            pass                # al weg (bijv. met de hand verwijderd): de rij mag ook weg
        except OSError as e:
            log.warning("foto %s niet te verwijderen: %s", bestand, e)
            continue            # rij laten staan; de volgende keer opnieuw proberen
        weg.append((camera_id, ts))
    if weg:
        db.verwijder_opnames(weg)

    # 2. Wat alleen op schijf staat: oude foto's en tijdelijke bestanden zonder rij. Van
    # onder naar boven, zodat mappen die leeg raken meteen weg kunnen.
    for pad, _mappen, bestanden in os.walk(map_, topdown=False):
        for naam in bestanden:
            if not naam.endswith((".jpg", ".tmp")):
                continue
            volledig = os.path.join(pad, naam)
            try:
                if os.path.getmtime(volledig) < grens:
                    os.remove(volledig)
                    aantal += 1
            except OSError:
                pass
        if pad != map_:
            try:
                os.rmdir(pad)   # lukt alleen als de map leeg is
            except OSError:
                pass
    return aantal


def verwerk_verwijder(db: Database, map_: str, payload: bytes) -> int:
    """Voert een verwijderopdracht van het web uit (§5.6 punt 2): van elke genoemde opname
    het bestand en de rij. Geeft het aantal verwijderde opnames; ValueError bij een
    ongeldige opdracht (dan wordt er niets verwijderd)."""
    try:
        d = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError(f"geen geldige JSON: {e}") from None
    lijst = d.get("opnames") if isinstance(d, dict) else None
    if not isinstance(lijst, list) or len(lijst) > MAX_VERWIJDER:
        raise ValueError(f"'opnames' moet een lijst van hooguit {MAX_VERWIJDER} zijn")
    for paar in lijst:
        if (not isinstance(paar, list) or len(paar) != 2 or not isinstance(paar[0], str)
                or isinstance(paar[1], bool) or not isinstance(paar[1], int)):
            raise ValueError("elke opname moet een paar [camera_id, ts] zijn")

    weg = []
    for camera_id, ts in lijst:
        bestand = db.bestand_van(camera_id, ts)
        if bestand is None:
            continue            # bestaat niet (meer): niets te doen
        # Alleen een pad van de bekende vorm, zoals bij het opruimen.
        if not _BESTAND.match(bestand):
            log.warning("opname %s van %s heeft een onverwacht pad (%r); niet verwijderd",
                        ts, camera_id, bestand)
            continue
        try:
            os.remove(os.path.join(map_, *bestand.split("/")))
        except FileNotFoundError:
            pass                # al weg: de rij mag ook weg
        except OSError as e:
            log.warning("opname %s niet te verwijderen: %s", bestand, e)
            continue
        weg.append((camera_id, ts))
    if weg:
        db.verwijder_opnames(weg)
    return len(weg)


async def _verwijderen(db: Database, q: asyncio.Queue, stop: asyncio.Event, map_: str) -> None:
    """Voert de verwijderopdrachten van het web uit. Loopt naast de hoofdlus in dezelfde
    asyncio-loop (en dus in dezelfde thread als de database)."""
    while not stop.is_set():
        try:
            topic, payload = await asyncio.wait_for(q.get(), timeout=1.0)
        except TimeoutError:
            continue
        try:
            log.info("%d opnames verwijderd op verzoek van het web", verwerk_verwijder(db, map_, payload))
        except ValueError as e:
            log.warning("ongeldige opdracht op %s genegeerd: %s", topic, e)
        except sqlite3.Error:
            log.exception("verwijderen van opnames mislukt")


def ruim_videos(db: Database, map_: str, ruimte: float) -> int:
    """Houdt de video's binnen de maximale ruimte (§5.7 punt 5): nemen ze samen meer dan
    `ruimte` bytes in, dan gaan de oudste weg (bestand en rij) tot het weer past. Geeft het
    aantal verwijderde video's. De grootte komt uit de tabel."""
    videos = db.videos()
    totaal = sum(v[3] for v in videos)
    weg = []
    for camera_id, ts, bestand, bytes_ in videos:
        if totaal <= ruimte:
            break
        if not _BESTAND.match(bestand):
            log.warning("opname %s van %s heeft een onverwacht pad (%r); niet opgeruimd",
                        ts, camera_id, bestand)
            continue
        try:
            os.remove(os.path.join(map_, *bestand.split("/")))
        except FileNotFoundError:
            pass                # al weg: de rij mag ook weg
        except OSError as e:
            log.warning("video %s niet te verwijderen: %s", bestand, e)
            continue
        weg.append((camera_id, ts))
        totaal -= bytes_
        log.info("video %s verwijderd: de ruimte voor video's is vol", bestand)
    if weg:
        db.verwijder_opnames(weg)
    return len(weg)


async def _opnames(db: Database, q: asyncio.Queue, stop: asyncio.Event, klok_ok, map_: str,
                   bewaren: float, video_ruimte: float) -> None:
    """Legt de berichten van de camera-reader vast en ruimt eens per uur oude foto's op.
    Loopt naast de hoofdlus in dezelfde asyncio-loop (en dus in dezelfde thread als de
    database)."""
    # De eerste keer kort na de start, daarna elk uur.
    opruimen = time.monotonic() + 60
    while not stop.is_set():
        if time.monotonic() >= opruimen:
            opruimen = time.monotonic() + OPRUIM_INTERVAL
            # Zonder goede klok is niet te zeggen wat oud is (§4.2).
            if klok_ok() and os.path.isdir(map_):
                try:
                    aantal = ruim_fotos(db, map_, bewaren, time.time())
                    if aantal:
                        log.info("%d oude foto's opgeruimd", aantal)
                    ruim_videos(db, map_, video_ruimte)
                except (sqlite3.Error, OSError):
                    log.exception("opruimen van oude foto's mislukt")
        try:
            topic, payload = await asyncio.wait_for(q.get(), timeout=1.0)
        except TimeoutError:
            continue
        try:
            tekst = verwerk_opname(db, payload)
            log.debug(tekst)
            # Een nieuwe video kan de ruimte vol maken: meteen kijken (§5.7 punt 5).
            if tekst.startswith("video"):
                ruim_videos(db, map_, video_ruimte)
        except ValueError as e:
            log.warning("ongeldig bericht op %s genegeerd: %s", topic, e)
        except sqlite3.Error:
            log.exception("vastleggen van opname mislukt (%s)", topic)


async def _alarmen(db: Database, q: asyncio.Queue, stop: asyncio.Event, klok_ok) -> None:
    """Legt de berichten van de alarmlaag vast, direct bij ontvangst. Loopt naast de
    hoofdlus in dezelfde asyncio-loop (en dus in dezelfde thread als de database)."""
    while not stop.is_set():
        try:
            topic, payload = await asyncio.wait_for(q.get(), timeout=1.0)
        except TimeoutError:
            continue
        # Net als metingen: pas schrijven als de klok klopt (§4.2).
        if not klok_ok():
            log.warning("klok niet gesynchroniseerd; alarmbericht op %s niet vastgelegd", topic)
            continue
        try:
            tekst = verwerk_alarm(db, topic, payload)
            if tekst:
                log.info(tekst)
        except ValueError as e:
            log.warning("ongeldig bericht op %s genegeerd: %s", topic, e)
        except sqlite3.Error:
            log.exception("vastleggen van alarm mislukt (%s)", topic)


async def draai(config: Config, bus, stop: asyncio.Event, klok_ok=klok_gesynchroniseerd) -> None:
    """Hoofdlus van de opslag-service; loopt tot `stop` gezet wordt.

    klok_ok is te vervangen in tests (op Windows is er geen timedatectl).
    """
    from brms.trace import Tracer

    db = Database(config.opslag.database)
    agg = Aggregator()
    # Luistert naar trace-opdrachten van de monitor (§10.2); standaard staat trace uit.
    tracer = Tracer(bus, "opslag")
    tracer.start()
    # Alle metingen van alle readers. QoS 1, zodat toestanden (met QoS 1 gepubliceerd)
    # ook met QoS 1 worden afgeleverd.
    q = bus.wachtrij(f"{TOPIC_PREFIX}/#", qos=1)
    # De alarmen van de alarmlaag (§5.5.3 punt 8), in een eigen taak naast deze lus.
    alarmtaak = asyncio.create_task(
        _alarmen(db, bus.wachtrij(f"{TOPIC_ALARM}/#", qos=1), stop, klok_ok))
    # De opnames van de camera's (§5.7 punt 14), ook in een eigen taak.
    opnametaak = asyncio.create_task(
        _opnames(db, bus.wachtrij(f"{TOPIC_OPNAME}/#", qos=1), stop, klok_ok,
                 config.opnames.map, config.opnames.foto_bewaren,
                 config.opnames.video_ruimte * 1e9))
    # Verwijderopdrachten van het web (§5.6 punt 2).
    verwijdertaak = asyncio.create_task(
        _verwijderen(db, bus.wachtrij(TOPIC_VERWIJDER, qos=1), stop, config.opnames.map))
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
                aantallen = agg.aantallen()  # vóór leeg(), alleen nodig voor trace
                rijen = agg.leeg(moment)
                if rijen:
                    if klok_ok():
                        try:
                            db.schrijf(rijen)
                            log.debug("%d rijen opgeslagen", len(rijen))
                            resultaat = "weggeschreven naar de database"
                        except sqlite3.Error:
                            # Bijv. schijf vol. Loggen en doorgaan; de volgende keer
                            # lukt het misschien wel.
                            log.exception("schrijven naar database mislukt; %d rijen verloren", len(rijen))
                            resultaat = "FOUT bij schrijven – rijen verloren"
                    else:
                        log.warning("klok niet gesynchroniseerd; %d rijen weggegooid", len(rijen))
                        resultaat = "WEGGEGOOID (klok niet gesynchroniseerd, §4.2)"
                    _trace_opslag(tracer, rijen, aantallen, moment, resultaat)
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
                m = Meting.van_json(payload)
            except ValueError as e:
                log.warning("ongeldig bericht op %s genegeerd: %s", topic, e)
                continue
            veranderd = agg.voeg_toe(m)

            # Trace (§10.2, stap 'opgenomen'): de tussenstand in het lopende interval.
            stap = tracer.voor(m.sensor_id)
            if stap:
                tot = time.strftime("%H:%M", time.localtime(moment))
                # Eén bericht per grootheid; 'deel' geeft elk een eigen plaats in de monitor.
                stap("opgenomen", f"interval tot {tot}: {agg.beschrijf(m, veranderd)}",
                     m.grootheid)
    finally:
        # Eerst de neventaken laten stoppen; die gebruiken de database nog.
        for taak in (alarmtaak, opnametaak, verwijdertaak):
            if not stop.is_set():
                taak.cancel()
            try:
                await taak
            except asyncio.CancelledError:
                pass
        db.sluit()
        log.info("opslag gestopt")
