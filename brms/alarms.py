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
- notificaties via de meldingskanalen (brms/melding.py, §5.5.4).

Nog niet gebouwd (§5.5.3 punt 11): de aan/uit-schakelaar via het web, acties (relais,
video) en een foto bij de melding.

Dit module bevat:
- Bewaker: de toestand van één regel voor één sensor (zuiver, zonder klok of netwerk);
- Alarmlaag: alle bewakers bij elkaar; maakt van hun uitkomsten berichten en meldingen;
- draai(): de service.
"""

import asyncio
import json
import logging
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


class Alarmlaag:
    """Alle alarmregels bij elkaar. Geen netwerk: bij_meting() en tik() geven terug wat er
    moet gebeuren, als een lijst van Gebeurtenis (voor de bus) en Melding (voor de
    meldingskanalen)."""

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

    def _verwerk(self, bewaker: Bewaker, wat: str | None, nu: float, uit: list) -> None:
        """Zet de uitkomst van een bewaker om in een gebeurtenis en eventueel een melding."""
        if wat is None:
            return
        regel, sensor = bewaker.regel, bewaker.sensor
        uit.append(Gebeurtenis(int(self._tijd()), regel.id, sensor.id, regel.grootheid, wat,
                               bewaker.laatste))
        if wat == BEGIN:
            log.info("ALARM %s voor %s (%s)", regel.id, sensor.naam, voorwaardetekst(regel))
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
        return Melding(f"{_TEKEN[wat]} {voor}{kop}: {tekst}", prioriteit=prioriteit)


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
            else:
                verzender.voeg_toe(item)

    # Na een herstart is er geen actief alarm (§5.5.2 punt 6). Dat wordt één keer op de bus
    # gemeld, zodra de broker bereikbaar is; de opslag sluit dan de alarmen die nog open
    # staan in de database (§5.5.3 punt 8).
    herstart_gemeld = False
    laatste_tik = 0.0
    try:
        while not stop.is_set():
            if not herstart_gemeld and bus.verbonden:
                herstart_gemeld = bus.publiceer(
                    TOPIC_HERSTART, json.dumps({"ts": int(time.time())}), qos=1)

            # Ongeveer elke seconde: wat door de tijd verandert (stil, vensters, herinneringen).
            nu = time.monotonic()
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
        # Bij een gewoon stopverzoek stopt de verzender zelf; bij een fout hierboven niet.
        if not stop.is_set():
            verzendtaak.cancel()
        try:
            await verzendtaak
        except asyncio.CancelledError:
            pass
        log.info("alarmlaag gestopt")
