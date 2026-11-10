"""Alarmlaag: elke meting beoordelen tegen de alarmregels (ARCHITECTURE.md §5.5).

De alarmlaag is een eigen service (`brms alarm`, systemd-unit brms-alarm). Hij luistert
naar alle metingen op de bus en houdt per alarmregel en per sensor bij of het alarm
actief is. Een alarm, een notificatie en een actie zijn drie dingen (§5.5.3): het alarm
stelt vast dat een voorwaarde geldt; of daar een melding op volgt, staat in de regel.

De regels staan in brms.toml (blokken [[alarm]], zie config.Alarmregel). Drie soorten:

- **grens**   de waarde ligt boven of onder een grens;
- **aantal**  een aantal binnen een tijdvenster (bewegingen, pompbeurten);
- **stil**    de sensor levert geen waarden meer.

Voor alle soorten geldt dezelfde werking (§5.5.2):

1. Het alarm gaat af als de voorwaarde de ingestelde **duur** onafgebroken geldt; één
   waarde die niet voldoet, zet de duur terug.
2. Een actief alarm herstelt pas als de **herstelvoorwaarde** de ingestelde herstelduur
   onafgebroken geldt. Bij een grens is dat de herstelgrens; het verschil met de grens is
   de hysterese, die pendelen rond de grens voorkomt.
3. **Geen data is geen herstel:** zonder nieuwe waarde verandert er niets (behalve bij
   'stil', waar het uitblijven van waarden juist het alarm is).
4. De toestand wordt **niet bewaard over een herstart**: daarna begint elke duur opnieuw.

Alle tijdsduren lopen op de monotone klok (§4.2): een sprong van de systeemklok bij de
tijdsynchronisatie mag geen alarm geven of onderdrukken.

Wat de alarmlaag naar buiten brengt:
- begin en einde van elk alarm op de bus (brms/alarm/<alarm_id>); de opslag-service legt
  ze vast in de tabel `alarm` (§5.5.3 punt 8);
- notificaties via de meldingskanalen (brms/melding.py, §5.5.4);
- opdrachten aan de camera-reader (§5.5.3 punt 5 en 6, §5.7 punt 15): een video of foto
  als het alarm afgaat (`actie`), en een foto die met de notificatie meegaat (`foto`).

Een foto bij de notificatie werkt zo: de alarmlaag vraagt de camera-reader om een foto
(poll-nu) met de regel als aanleiding ("alarm:<alarm_id>"), wacht hooguit 15 s op het
bericht van die opname op de bus, leest het bestand uit de opnamemap en stuurt het mee.
Komt de foto niet op tijd, dan gaat de melding zonder foto: een melding is belangrijker
dan het plaatje erbij.

Nog niet gebouwd (§5.5.3 punt 11): de aan/uit-schakelaar via het web en het relais.

Dit module bevat:
- Bewaker: de toestand van één regel voor één sensor (zuiver, zonder klok of netwerk);
- Alarmlaag: alle bewakers bij elkaar; maakt van hun uitkomsten berichten, meldingen en
  opdrachten;
- Fotograaf: vraagt een foto aan de camera-reader en wacht erop;
- draai(): de service.
"""

import asyncio
import dataclasses
import json
import logging
import os
import re
import time
from collections import deque
from dataclasses import dataclass

from brms import config, grootheden
from brms.config import Alarmregel, Config, Sensor
from brms.melding import Melding
from brms.meting import TOPIC_PREFIX, Meting

log = logging.getLogger(__name__)

TOPIC_ALARM = "brms/alarm"                 # brms/alarm/<alarm_id>
TOPIC_HERSTART = f"{TOPIC_ALARM}/herstart"
# Om de hoeveel seconden de alarmlaag opnieuw meldt welke alarmen actief zijn (§5.5.3 punt 8).
STAND_INTERVAL = 60.0

# De camera-reader (zie brms/readers/camera.py): opdracht voor een opname, en het bericht
# van een gemaakte opname.
TOPIC_POLL_NU = "brms/opdracht/camera/poll-nu"
TOPIC_OPNAME = "brms/opname"
# Zo lang wacht een melding hooguit op haar foto, in seconden (§5.7 punt 15).
FOTO_WACHTTIJD = 15.0
# Een foto groter dan dit gaat niet mee: Telegram neemt hooguit 10 MB aan.
MAX_FOTO = 10 * 1024 * 1024
# Het pad van een foto binnen de opnamemap (zie brms/storage.py); alleen deze vorm wordt
# gelezen, zodat een bericht op de bus nooit naar een ander bestand kan wijzen.
_FOTOBESTAND = re.compile(r"^[A-Za-z0-9_-]+/\d{4}/\d{2}/\d{2}/\d{6}\.jpg$")

# Wat een bewaker kan melden.
BEGIN, HERSTEL, HERINNERING = "begin", "herstel", "herinnering"

# Teken vóór de titel van een melding, zodat in de lijst met berichten direct te zien is
# wat voor bericht het is.
_TEKEN = {BEGIN: "⚠️", HERINNERING: "🔁", HERSTEL: "✅"}


# =============================================================================
# Tekst voor meldingen
# =============================================================================

def getal(w: float) -> str:
    """Een getal zoals het in een melding staat: Nederlands, zonder overbodige nullen."""
    return f"{round(w, 2):g}".replace(".", ",")


def waardetekst(grootheid: str, w: float) -> str:
    """Waarde met eenheid, bijv. '1,8 °C'. Een toestand (0/1) heeft geen eenheid."""
    eenheid = grootheden.GROOTHEDEN[grootheid].eenheid
    return getal(w) if eenheid in ("0/1", "aantal") else f"{getal(w)} {eenheid}"


def duurtekst(seconden: float) -> str:
    """Een tijdsduur in woorden: '45 s', '12 min', '3 u 12 min', '2 d 4 u'."""
    s = int(round(seconden))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min"
    if s < 86400:
        minuten = s % 3600 // 60
        return f"{s // 3600} u" + (f" {minuten} min" if minuten else "")
    uren = s % 86400 // 3600
    return f"{s // 86400} d" + (f" {uren} u" if uren else "")


def voorwaardetekst(regel: Alarmregel) -> str:
    """De voorwaarde van een regel in woorden, bijv. 'onder 2 °C'."""
    if regel.soort == config.GRENS:
        if regel.onder is not None:
            return f"onder {waardetekst(regel.grootheid, regel.onder)}"
        return f"boven {waardetekst(regel.grootheid, regel.boven)}"
    if regel.soort == config.AANTAL:
        return f"{regel.aantal} of meer binnen {duurtekst(regel.venster)}"
    return f"geen waarden gedurende {duurtekst(regel.duur)}"


# =============================================================================
# Bewaker: één regel, één sensor
# =============================================================================

class Bewaker:
    """De toestand van één alarmregel voor één sensor.

    Zuiver: geen klok, geen bus. De tijd komt als argument `nu` binnen (monotone klok,
    seconden), zodat de werking los te testen is (tests/test_alarms.py).

    meting() en tik() geven BEGIN, HERSTEL of None terug.
    """

    def __init__(self, regel: Alarmregel, sensor: Sensor, nu: float):
        self.regel = regel
        self.sensor = sensor
        self.actief = False
        # Monotone tijd waarop het alarm afging (alleen geldig als actief).
        self.begin = 0.0
        # De laatst ontvangen waarde (voor herinneringen), en bij 'aantal' de laatste telling.
        self.laatste: float | None = None
        # Sinds wanneer de voorwaarde (in rust) of de herstelvoorwaarde (actief) onafgebroken
        # geldt; None = geldt nu niet.
        self._sinds: float | None = None
        # 'stil': tijdstip van de laatst ontvangen waarde. Bij de start telt de start zelf,
        # zodat een sensor die nooit iets levert ook als stil wordt gemeld.
        self._gezien = nu
        # 'aantal': de gebeurtenissen binnen het venster als (tijdstip, aantal), en de
        # vorige waarde van een toestand (om een overgang 0 -> 1 te herkennen).
        self._tellingen: deque[tuple[float, float]] = deque()
        self._vorige: float | None = None

    # --- een ontvangen waarde ---------------------------------------------------------
    def meting(self, grootheid: str, waarde: float, nu: float) -> str | None:
        regel = self.regel
        if regel.soort == config.STIL:
            # Elke waarde van de sensor telt, van welke grootheid ook.
            self._gezien = nu
            if self.actief:
                self.actief = False
                return HERSTEL
            return None
        if grootheid != regel.grootheid:
            return None
        if regel.soort == config.GRENS:
            self.laatste = waarde
            if not self.actief:
                voldoet = waarde < regel.onder if regel.onder is not None else waarde > regel.boven
                return self._wissel(voldoet, regel.duur, nu)
            # Zonder herstelgrens herstelt het alarm zodra de waarde de grens weer passeert.
            if regel.onder is not None:
                hersteld = waarde >= (regel.herstel if regel.herstel is not None else regel.onder)
            else:
                hersteld = waarde <= (regel.herstel if regel.herstel is not None else regel.boven)
            return self._wissel(hersteld, regel.herstelduur, nu)
        # --- aantal ---
        if grootheden.soort(grootheid) == grootheden.TOENAME:
            if waarde > 0:
                self._tellingen.append((nu, waarde))      # bijv. 3 bewegingen sinds de vorige
        else:
            # Toestand: een overgang 0 -> 1 is één beurt. De eerste waarde na de start is
            # geen overgang (de vorige stand is onbekend).
            if self._vorige == 0 and waarde == 1:
                self._tellingen.append((nu, 1))
            self._vorige = waarde
        return self._tel(nu)

    # --- de tijd verstrijkt ------------------------------------------------------------
    def tik(self, nu: float) -> str | None:
        """Wat alleen door het verstrijken van de tijd verandert: een sensor wordt stil, en
        tellingen schuiven het venster uit. Een grens wordt alleen bij een nieuwe waarde
        beoordeeld (geen data is geen herstel, en ook geen alarm)."""
        regel = self.regel
        if regel.soort == config.STIL:
            if not self.actief and nu - self._gezien >= regel.duur:
                self.actief, self.begin = True, nu
                return BEGIN
            return None
        if regel.soort == config.AANTAL:
            return self._tel(nu)
        return None

    # --- hulpfuncties -------------------------------------------------------------------
    def _wissel(self, geldt: bool, duur: float, nu: float) -> str | None:
        """Wisselt tussen rust en actief als `geldt` de `duur` onafgebroken waar is."""
        if not geldt:
            self._sinds = None      # één waarde die niet voldoet, zet de duur terug
            return None
        if self._sinds is None:
            self._sinds = nu
        if nu - self._sinds < duur:
            return None
        self._sinds = None
        self.actief = not self.actief
        if self.actief:
            self.begin = nu
            return BEGIN
        return HERSTEL

    def _tel(self, nu: float) -> str | None:
        """Soort 'aantal': wat buiten het venster valt vergeten, en de telling beoordelen."""
        regel = self.regel
        while self._tellingen and self._tellingen[0][0] <= nu - regel.venster:
            self._tellingen.popleft()
        self.laatste = sum(n for _, n in self._tellingen)
        if not self.actief:
            return self._wissel(self.laatste >= regel.aantal, 0.0, nu)
        return self._wissel(self.laatste < regel.aantal, regel.herstelduur, nu)


# =============================================================================
# Alarmlaag: alle bewakers
# =============================================================================

@dataclass(frozen=True)
class Gebeurtenis:
    """Begin of einde van een alarm, voor de bus (§3.3) en de tabel `alarm`."""
    ts: int                      # Unix-seconden UTC
    alarm_id: str
    sensor_id: str
    grootheid: str | None
    gebeurtenis: str             # begin of herstel
    waarde: float | None

    def topic(self) -> str:
        return f"{TOPIC_ALARM}/{self.alarm_id}"

    def naar_json(self) -> str:
        return json.dumps({"ts": self.ts, "alarm_id": self.alarm_id, "sensor_id": self.sensor_id,
                           "grootheid": self.grootheid, "gebeurtenis": self.gebeurtenis,
                           "waarde": self.waarde}, ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True)
class Opdracht:
    """Een opdracht aan de camera-reader (§5.5.3 punt 6): maak nu een foto of video."""
    camera_id: str
    type: str                    # foto of video
    aanleiding: str              # "alarm:<alarm_id>"

    def naar_json(self) -> str:
        return json.dumps({"camera_id": self.camera_id, "type": self.type,
                           "aanleiding": self.aanleiding}, separators=(",", ":"))


@dataclass(frozen=True)
class MetFoto:
    """Een melding waar een foto bij hoort (§5.5.3 punt 5): eerst de foto laten maken, dan
    versturen."""
    melding: Melding
    camera_id: str
    aanleiding: str


def aanleiding(regel: Alarmregel) -> str:
    """De aanleiding van een opname bij een alarm, zoals die in de tabel `opname` komt."""
    return f"alarm:{regel.id}"


class Alarmlaag:
    """Alle alarmregels bij elkaar. Geen netwerk: bij_meting() en tik() geven terug wat er
    moet gebeuren, als een lijst van Gebeurtenis (voor de bus), Melding of MetFoto (voor de
    meldingskanalen) en Opdracht (voor de camera-reader)."""

    def __init__(self, cfg: Config, klok=time.monotonic, tijd=time.time):
        """klok: monotone klok voor tijdsduren; tijd: systeemklok voor de tijdstempels op
        de bus. Beide te vervangen in tests."""
        self._klok = klok
        self._tijd = tijd
        nu = klok()
        # Per sensor de bewakers die naar zijn metingen kijken.
        self._per_sensor: dict[str, list[Bewaker]] = {}
        # Per bewaker het tijdstip van de volgende herinnering (alleen bij melding 'herhaald').
        self._herinnering: dict[Bewaker, float] = {}
        for regel in cfg.alarmen:
            for sensor_id in regel.sensoren:
                bewaker = Bewaker(regel, cfg.sensoren[sensor_id], nu)
                self._per_sensor.setdefault(sensor_id, []).append(bewaker)

    @property
    def bewakers(self) -> list[Bewaker]:
        return [b for lijst in self._per_sensor.values() for b in lijst]

    def actief(self) -> list[list[str]]:
        """De alarmen die nu actief zijn, als paren [alarm_id, sensor_id]. Dit gaat
        geregeld naar de opslag, die daarmee vergeten alarmen afsluit (§5.5.3 punt 8)."""
        return [[b.regel.id, b.sensor.id] for b in self.bewakers if b.actief]

    def bij_meting(self, m: Meting) -> list:
        """Beoordeelt één meting; geeft de gebeurtenissen en meldingen die eruit volgen."""
        nu = self._klok()
        uit: list = []
        for bewaker in self._per_sensor.get(m.sensor_id, ()):
            self._verwerk(bewaker, bewaker.meting(m.grootheid, m.waarde, nu), nu, uit)
        return uit

    def tik(self) -> list:
        """Wat door het verstrijken van de tijd verandert; ongeveer elke seconde aanroepen."""
        nu = self._klok()
        uit: list = []
        for bewaker in self.bewakers:
            self._verwerk(bewaker, bewaker.tik(nu), nu, uit)
            # Herinnering zolang het alarm actief is (§5.5.2 punt 4).
            volgende = self._herinnering.get(bewaker)
            if bewaker.actief and volgende is not None and nu >= volgende:
                self._herinnering[bewaker] = nu + bewaker.regel.herhaling
                uit.append(self._melding(bewaker, HERINNERING, nu))
        return uit

    def _met_foto(self, regel: Alarmregel, melding: Melding, wat: str):
        """Hoort er bij deze melding een foto (§5.5.3 punt 5)? Bij 'begin' alleen als het
        alarm afgaat, bij 'elke' ook bij elke herinnering. Een herstelmelding krijgt er
        nooit een."""
        if regel.foto is None or wat == HERSTEL or (wat == HERINNERING and regel.foto != "elke"):
            return melding
        return MetFoto(melding, regel.camera, aanleiding(regel))

    def _verwerk(self, bewaker: Bewaker, wat: str | None, nu: float, uit: list) -> None:
        """Zet de uitkomst van een bewaker om in een gebeurtenis en eventueel een melding."""
        if wat is None:
            return
        regel, sensor = bewaker.regel, bewaker.sensor
        uit.append(Gebeurtenis(int(self._tijd()), regel.id, sensor.id, regel.grootheid, wat,
                               bewaker.laatste))
        if wat == BEGIN:
            log.info("ALARM %s voor %s (%s)", regel.id, sensor.naam, voorwaardetekst(regel))
            # Acties (§5.5.3 punt 6): los van de notificatie, ook bij melding 'uit'.
            for type_ in regel.acties:
                uit.append(Opdracht(regel.camera, type_, aanleiding(regel)))
            if regel.melding != "uit":
                uit.append(self._melding(bewaker, BEGIN, nu))
            if regel.melding == "herhaald":
                self._herinnering[bewaker] = nu + regel.herhaling
        else:
            log.info("alarm %s voor %s hersteld na %s", regel.id, sensor.naam,
                     duurtekst(nu - bewaker.begin))
            self._herinnering.pop(bewaker, None)
            if regel.melding != "uit" and regel.herstelmelding:
                uit.append(self._melding(bewaker, HERSTEL, nu))

    def _melding(self, bewaker: Bewaker, wat: str, nu: float) -> Melding:
        """De tekst van een melding bij begin, herinnering of herstel."""
        regel, sensor = bewaker.regel, bewaker.sensor
        kop = f"{regel.naam} – {sensor.naam}"
        actief = duurtekst(nu - bewaker.begin)
        if regel.soort == config.STIL:
            if wat == BEGIN:
                tekst = f"geen waarden ontvangen gedurende {duurtekst(regel.duur)}"
            elif wat == HERINNERING:
                tekst = f"nog steeds geen waarden; stil sinds {duurtekst(nu - bewaker.begin + regel.duur)}"
            else:
                tekst = f"levert weer waarden (was {duurtekst(nu - bewaker.begin + regel.duur)} stil)"
        else:
            if regel.soort == config.AANTAL:
                waarde = f"{regel.grootheid} {getal(bewaker.laatste)} keer binnen {duurtekst(regel.venster)}"
            else:
                waarde = f"{regel.grootheid} {waardetekst(regel.grootheid, bewaker.laatste)}"
            if wat == BEGIN:
                tekst = f"{waarde} ({voorwaardetekst(regel)}"
                tekst += f", al {duurtekst(regel.duur)})" if regel.duur else ")"
            elif wat == HERINNERING:
                tekst = f"{waarde}; alarm actief sinds {actief}"
            else:
                tekst = f"{waarde} (alarm was {actief} actief)"
        voor = {HERINNERING: "Herinnering: ", HERSTEL: "Hersteld: "}.get(wat, "")
        # Een herstelmelding is nooit dringend: ingrijpen is niet meer nodig.
        prioriteit = "normaal" if wat == HERSTEL else regel.prioriteit
        return self._met_foto(
            regel, Melding(f"{_TEKEN[wat]} {voor}{kop}: {tekst}", prioriteit=prioriteit), wat)


# =============================================================================
# Fotograaf: een foto bij een melding
# =============================================================================

def lees_foto(map_: str, bestand: str) -> bytes:
    """Leest een foto uit de opnamemap. ValueError bij een onverwacht pad of een te groot
    bestand; OSError als het bestand niet te lezen is."""
    if not _FOTOBESTAND.match(bestand):
        raise ValueError(f"onverwacht pad: {bestand!r}")
    pad = os.path.join(map_, *bestand.split("/"))
    if os.path.getsize(pad) > MAX_FOTO:
        raise ValueError(f"foto is groter dan {MAX_FOTO // (1024 * 1024)} MB")
    with open(pad, "rb") as f:
        return f.read()


class Fotograaf:
    """Vraagt de camera-reader om een foto en wacht op het bericht van die opname (§5.7
    punt 15).

    Meldingen die tegelijk om dezelfde foto vragen (dezelfde regel gaat voor drie sensoren
    tegelijk af) delen één opdracht en één foto. Alles hier draait in de asyncio-loop.
    """

    def __init__(self, bus, map_: str, wachttijd: float = FOTO_WACHTTIJD):
        self._bus = bus
        self._map = map_
        self._wachttijd = wachttijd
        # (camera_id, aanleiding) -> Future die het pad van de foto krijgt.
        self._wachtend: dict[tuple[str, str], asyncio.Future] = {}

    def bij_opname(self, payload: bytes) -> None:
        """Een bericht op brms/opname/<camera_id>: is dit de foto waar iemand op wacht?"""
        try:
            d = json.loads(payload)
            sleutel = (d["camera_id"], d["aanleiding"])
            bestand, type_ = d["bestand"], d["type"]
        except (ValueError, KeyError, TypeError):
            return                       # de opslag meldt een ongeldig bericht al
        toekomst = self._wachtend.get(sleutel) if isinstance(sleutel[1], str) else None
        if type_ == "foto" and toekomst is not None and not toekomst.done():
            toekomst.set_result(bestand)

    async def foto(self, camera_id: str, aanleiding_: str) -> tuple[bytes, str] | None:
        """Laat een foto maken en geeft (bytes, bestandsnaam), of None als de foto er na
        de wachttijd niet is of niet te lezen is (de reden staat dan in het log)."""
        sleutel = (camera_id, aanleiding_)
        toekomst = self._wachtend.get(sleutel)
        if toekomst is None:
            toekomst = asyncio.get_running_loop().create_future()
            self._wachtend[sleutel] = toekomst
            if not self._bus.publiceer(TOPIC_POLL_NU, Opdracht(camera_id, "foto", aanleiding_).naar_json(), qos=1):
                log.warning("broker niet bereikbaar: geen foto van %s gevraagd", camera_id)
        try:
            # shield: verloopt de wachttijd van deze melding, dan blijft de gedeelde
            # Future heel voor de andere wachtenden.
            bestand = await asyncio.wait_for(asyncio.shield(toekomst), self._wachttijd)
        except asyncio.TimeoutError:
            log.warning("geen foto van camera %s binnen %g s (%s); de melding gaat zonder foto",
                        camera_id, self._wachttijd, aanleiding_)
            return None
        finally:
            # Een volgende melding vraagt een nieuwe foto.
            if self._wachtend.get(sleutel) is toekomst:
                del self._wachtend[sleutel]
        try:
            data = await asyncio.to_thread(lees_foto, self._map, bestand)
        except (OSError, ValueError) as e:
            log.warning("foto %s niet te lezen (%s); de melding gaat zonder foto", bestand, e)
            return None
        return data, bestand.replace("/", "-")


# =============================================================================
# De service
# =============================================================================

async def draai(cfg: Config, bus, stop: asyncio.Event) -> None:
    """Hoofdlus van de alarmlaag; loopt tot `stop` gezet wordt."""
    from brms import melding

    laag = Alarmlaag(cfg)
    kanalen = melding.kanalen(cfg.melding)
    verzender = melding.Verzender(kanalen)
    if not cfg.alarmen:
        # Geen fout: de service draait dan zonder iets te doen, zodat `brms start` en
        # `brms update` voor alle services gelijk werken.
        log.warning("geen alarmregels in de configuratie; er wordt niets bewaakt")
    elif not kanalen and any(r.melding != "uit" for r in cfg.alarmen):
        log.warning("geen meldingskanaal ingesteld ([melding.telegram] of [melding.email]); "
                    "alarmen worden wel vastgelegd, maar er gaat geen melding uit")

    # Alle metingen van alle readers. QoS 1, zodat toestanden ook met QoS 1 aankomen.
    q = bus.wachtrij(f"{TOPIC_PREFIX}/#", qos=1)
    verzendtaak = asyncio.create_task(verzender.draai(stop))

    # Foto's bij meldingen (§5.7 punt 15): alleen meeluisteren met de camera-reader als er
    # een regel is die een foto wil.
    fotograaf = Fotograaf(bus, cfg.opnames.map)
    loop = asyncio.get_running_loop()
    if any(r.foto for r in cfg.alarmen):
        # Het bericht komt binnen in paho's netwerkthread; verwerken in de asyncio-loop.
        bus.abonneer(f"{TOPIC_OPNAME}/#", 1,
                     lambda topic, payload: loop.call_soon_threadsafe(fotograaf.bij_opname, payload))
    # De meldingen die nog op hun foto wachten.
    fototaken: set[asyncio.Task] = set()

    async def met_foto(item: MetFoto) -> None:
        """Wacht op de foto en zet de melding dan in de wachtrij, met of zonder foto."""
        melding_ = item.melding
        try:
            foto = await fotograaf.foto(item.camera_id, item.aanleiding)
            if foto is not None:
                melding_ = dataclasses.replace(melding_, foto=foto[0], foto_naam=foto[1])
        except Exception:
            # Wat er ook misgaat met de foto: de melding gaat uit.
            log.exception("fout bij de foto voor een melding")
        verzender.voeg_toe(melding_)
    log.info("alarmlaag gestart; %d regels, %d bewakers; meldingen via: %s", len(cfg.alarmen),
             len(laag.bewakers), ", ".join(kanalen) or "geen kanaal")

    def voer_uit(uit: list) -> None:
        for item in uit:
            if isinstance(item, Gebeurtenis):
                # Is de broker onbereikbaar, dan gaat het bericht verloren (§3.1 punt 5);
                # de melding hieronder gaat wel uit.
                if not bus.publiceer(item.topic(), item.naar_json(), qos=1):
                    log.warning("broker niet bereikbaar: alarm %s (%s) niet vastgelegd",
                                item.alarm_id, item.gebeurtenis)
            elif isinstance(item, Opdracht):
                if bus.publiceer(TOPIC_POLL_NU, item.naar_json(), qos=1):
                    log.info("%s van camera %s gevraagd (%s)", item.type, item.camera_id,
                             item.aanleiding)
                else:
                    log.warning("broker niet bereikbaar: geen %s van camera %s (%s)",
                                item.type, item.camera_id, item.aanleiding)
            elif isinstance(item, MetFoto):
                # Niet hier wachten: de alarmlaag moet de metingen blijven beoordelen.
                taak = asyncio.create_task(met_foto(item))
                fototaken.add(taak)
                taak.add_done_callback(fototaken.discard)
            else:
                verzender.voeg_toe(item)

    # Na een herstart is er geen actief alarm (§5.5.2 punt 6), maar in de database kan er
    # nog een open staan. Daarom meldt de alarmlaag bij zijn start en daarna elke minuut
    # welke alarmen actief zijn; de opslag sluit de alarmen af die open staan en daar niet
    # bij zijn (§5.5.3 punt 8). Eén melding bij de start is niet genoeg: na een herstart
    # van de Pi starten beide services tegelijk en luistert de opslag nog niet, of de klok
    # is nog niet gelijkgezet en de opslag schrijft nog niet (§4.2).
    stand_gemeld = None        # monotone tijd van de laatste geslaagde melding
    laatste_tik = 0.0
    try:
        while not stop.is_set():
            nu = time.monotonic()
            if bus.verbonden and (stand_gemeld is None or nu - stand_gemeld >= STAND_INTERVAL):
                if bus.publiceer(TOPIC_HERSTART, json.dumps(
                        {"ts": int(time.time()), "actief": laag.actief()}), qos=1):
                    stand_gemeld = nu

            # Ongeveer elke seconde: wat door de tijd verandert (stil, vensters, herinneringen).
            if nu - laatste_tik >= 1.0:
                laatste_tik = nu
                voer_uit(laag.tik())

            # Wachten op de volgende meting, hooguit 1 s (voor de tik en een stopverzoek).
            try:
                topic, payload = await asyncio.wait_for(q.get(), timeout=1.0)
            except TimeoutError:
                continue
            try:
                m = Meting.van_json(payload)
            except ValueError as e:
                log.warning("ongeldig bericht op %s genegeerd: %s", topic, e)
                continue
            voer_uit(laag.bij_meting(m))
    finally:
        # Meldingen die nog op een foto wachten, gaan bij het stoppen verloren.
        for taak in list(fototaken):
            taak.cancel()
        await asyncio.gather(*fototaken, return_exceptions=True)
        # Bij een gewoon stopverzoek stopt de verzender zelf; bij een fout hierboven niet.
        if not stop.is_set():
            verzendtaak.cancel()
        try:
            await verzendtaak
        except asyncio.CancelledError:
            pass
        log.info("alarmlaag gestopt")
