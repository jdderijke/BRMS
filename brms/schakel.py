"""Schakellaag: beslist in welke stand elk relais hoort te staan (ARCHITECTURE.md §5.8).

De schakellaag is een eigen service (`brms schakel`, systemd-unit brms-schakel). Er zijn
drie rollen, en die staan elk op één plek (§5.8, redenering):

- **vragen** om een stand: een alarmregel (de alarmlaag) of de gebruiker (het web);
- **beslissen** welke stand geldt, en die stand bewaken: deze service;
- **uitvoeren**: de Shelly-reader, de enige die met de Shelly praat (§5.5.3).

Een relais is een sensor van het type `shelly-relais` uit brms.toml. Elk relais heeft een
**veilige stand** (het veld `veilige_stand`: de kachel uit, de walstroom aan); de andere
stand heet de **actieve stand**. Wat de schakellaag doet, alle punten uit §5.8:

- Punt 3, vragers. De alarmlaag stuurt de hele lijst van zijn verzoeken
  (brms/opdracht/schakel/verzoek): bij elke verandering en elke minuut. Blijft dat bericht
  2 minuten uit, dan vervallen zijn verzoeken. Het web stuurt de keuze van de gebruiker
  (brms/opdracht/schakel/hand): AUTO, of de actieve stand van dat relais. Beide alleen
  voor een relais met "relais" in zijn `controls` (§5.4.2 punt 8).
- Punt 4, welke stand geldt:
    1. geldt de `voorwaarde` van het relais niet (of is hij onbekend): de veilige stand.
       Dit gaat voor alles, ook voor de hand;
    2. staat de keuze op de hand: de actieve stand;
    3. staat de keuze op AUTO en is er een verzoek van een alarmregel: de actieve stand;
    4. anders de veilige stand.
- Punt 5, bewaking. De actieve stand gaat naar de Shelly met een terugschakeltijd van
  2 minuten en wordt elke minuut herhaald. Valt de schakellaag of de Pi weg, dan zet de
  Shelly zichzelf terug in de veilige stand. De veilige stand gaat zonder tijd.
- Punt 6 en 7, hand en herstart. Niets wordt bewaard: na een herstart staat elk relais
  op AUTO en krijgt het de opdracht naar zijn veilige stand. De schakellaag vraagt de
  alarmlaag dan om zijn verzoeken, en beantwoordt de vraag van de Shelly-reader naar de
  stand van de relais (bij diens eigen start).
- Punt 9, stand voor het web. Per relais op brms/schakel/<sensor_id>: de keuze, de
  stand die de schakellaag wil en de reden; bij elke verandering en elke minuut.
- Punt 10, log. Elke verandering van keuze of stand met de reden; de herhaling niet.

Een ingevoerde waarde (een thermostaat, §5.6) voert de schakellaag nog niet uit: wat hij
ermee moet doen is OPEN-44, en er is nog geen sensortype dat zo'n waarde aanneemt. Het
bericht wordt herkend en met een regel in het log genegeerd.

Alle tijdsduren lopen op de monotone klok (§4.2), zoals in de alarmlaag.

Dit module bevat:
- Schakellaag: de beslissing (zuiver, zonder netwerk; de klok is te vervangen in tests);
- lees_verzoek, lees_hand: het lezen van de twee opdrachten;
- draai(): de service.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass

from brms import config
from brms.config import Config
from brms.meting import TOPIC_PREFIX, Meting

log = logging.getLogger(__name__)

# De verzoeken van de alarmlaag: {"ts", "bron": "alarm", "verzoeken": [{"sensor_id",
# "alarm_id"}, ...]}; altijd de hele lijst (§3.3, §5.8 punt 3).
TOPIC_VERZOEK = "brms/opdracht/schakel/verzoek"
# De keuze van de gebruiker, van het web: {"ts", "sensor_id", "keuze"} met keuze "auto",
# "aan" of "uit"; of, bij een bedienbare waarde, {"ts", "sensor_id", "grootheid", "waarde"}.
TOPIC_HAND = "brms/opdracht/schakel/hand"
# Hierop meldt de schakellaag per relais zijn stand: brms/schakel/<sensor_id> (§5.8 punt 9).
TOPIC_STAND = "brms/schakel"
# De schakelopdracht aan de Shelly-reader (§5.2.6 punt 3), en de vraag van die reader naar
# de stand van de relais, bij zijn eigen start (§5.2.6 punt 6; §5.8 punt 7).
TOPIC_RELAIS_ZET = "brms/opdracht/relais/zet"
TOPIC_RELAIS_STAND_VRAAG = "brms/opdracht/relais/stand"
# Hierop vraagt de schakellaag bij zijn start de alarmlaag om zijn stand; de alarmlaag
# antwoordt daarop ook met zijn lijst van verzoeken (§5.8 punt 7).
TOPIC_ALARM_STAND_VRAAG = "brms/opdracht/alarm/stand"

# De terugschakeltijd die met de actieve stand meegaat (de Shelly keert de stand na die
# tijd zelf weer om), en de tussenpoos waarmee de opdracht herhaald wordt. De herhaling is
# de helft van de terugschakeltijd: één gemiste herhaling geeft nog geen onderbreking.
# Dezelfde tijden als bij de kachel tot v0.28 (§5.2.6 punt 4, besluit OPEN-36).
TERUGSCHAKELTIJD = 120.0
HERHAAL = 60.0
# Zo lang geldt een waarde waar een voorwaarde naar kijkt (seconden): ouder is "onbekend",
# en dan gaat het relais naar zijn veilige stand (§5.2.6 punt 5).
VOORWAARDE_GELDIG = TERUGSCHAKELTIJD
# Blijft het bericht van de alarmlaag zo lang uit (seconden), dan vervallen zijn verzoeken
# (§5.8 punt 3): de alarmlaag draait dan niet meer, en een kachel hoort niet te blijven
# stoken op een alarm dat niemand meer bewaakt.
VERZOEK_GELDIG = 120.0
# Om de hoeveel seconden de stand van elk relais opnieuw gemeld wordt (§5.8 punt 9). Het
# web ziet aan het uitblijven ervan dat de schakellaag niet draait.
MELD_INTERVAL = 60.0

AUTO = "auto"
# De enige grootheid die de schakellaag nu bedient.
RELAIS = "relais"


# =============================================================================
# Wat de schakellaag naar buiten brengt
# =============================================================================

@dataclass(frozen=True)
class RelaisOpdracht:
    """Een schakelopdracht aan de Shelly-reader (§5.2.6 punt 3): relais aan of uit."""
    sensor_id: str               # het relais (sensor van het type shelly-relais)
    aan: bool
    # De terugschakeltijd in seconden; altijd gezet bij de actieve stand, None bij de
    # veilige stand (§5.8 punt 5).
    duur: float | None = None

    def naar_json(self) -> str:
        d: dict = {"sensor_id": self.sensor_id, "aan": self.aan}
        if self.duur is not None:
            d["duur"] = self.duur
        return json.dumps(d, separators=(",", ":"))


@dataclass(frozen=True)
class Stand:
    """De stand van één relais, voor het web (§5.8 punt 9, §3.3)."""
    ts: int                      # Unix-seconden UTC
    sensor_id: str
    keuze: str                   # "auto", of de actieve stand ("aan" of "uit") = met de hand
    stand: str                   # de stand die de schakellaag wil: "aan" of "uit"
    reden: str                   # waarom, in woorden

    def topic(self) -> str:
        return f"{TOPIC_STAND}/{self.sensor_id}"

    def naar_json(self) -> str:
        return json.dumps({"ts": self.ts, "sensor_id": self.sensor_id, "keuze": self.keuze,
                           "stand": self.stand, "reden": self.reden},
                          ensure_ascii=False, separators=(",", ":"))


# =============================================================================
# De opdrachten lezen
# =============================================================================

def _json(payload: bytes) -> dict:
    """Een bericht als dict; ValueError als het geen JSON-object is."""
    try:
        d = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError(f"geen geldige JSON: {e}") from None
    if not isinstance(d, dict):
        raise ValueError("het bericht is geen JSON-object")
    return d


def lees_verzoek(payload: bytes) -> list[tuple[str, str]]:
    """Leest de lijst van verzoeken van de alarmlaag: geeft [(sensor_id, alarm_id), ...].
    ValueError bij een ongeldig bericht. Een lege lijst is geldig: er is dan geen verzoek."""
    lijst = _json(payload).get("verzoeken")
    if not isinstance(lijst, list):
        raise ValueError("'verzoeken' ontbreekt of is geen lijst")
    uit = []
    for v in lijst:
        if (not isinstance(v, dict) or not isinstance(v.get("sensor_id"), str)
                or not isinstance(v.get("alarm_id"), str)):
            raise ValueError("een verzoek moet 'sensor_id' en 'alarm_id' hebben")
        uit.append((v["sensor_id"], v["alarm_id"]))
    return uit


def lees_hand(payload: bytes) -> tuple[str, str | None, str | None, float | None]:
    """Leest een opdracht van het web: geeft (sensor_id, keuze, grootheid, waarde).

    Twee vormen (§3.3, §5.6):
    - de keuze van een relais: `keuze` is "auto", "aan" of "uit"; grootheid en waarde zijn
      dan None;
    - een ingevoerde waarde: `grootheid` en `waarde` (een getal); keuze is dan None.
    ValueError bij een ongeldig bericht."""
    d = _json(payload)
    sensor_id = d.get("sensor_id")
    if not isinstance(sensor_id, str) or not sensor_id:
        raise ValueError("sensor_id ontbreekt")
    if "waarde" in d:
        waarde, grootheid = d["waarde"], d.get("grootheid")
        if isinstance(waarde, bool) or not isinstance(waarde, (int, float)):
            raise ValueError("'waarde' moet een getal zijn")
        if not isinstance(grootheid, str) or not grootheid:
            raise ValueError("bij een 'waarde' hoort een 'grootheid'")
        return sensor_id, None, grootheid, float(waarde)
    keuze = d.get("keuze")
    if keuze not in (AUTO, *config.STANDEN):
        raise ValueError("'keuze' moet \"auto\", \"aan\" of \"uit\" zijn")
    return sensor_id, keuze, None, None


# =============================================================================
# De schakellaag: de beslissing
# =============================================================================

class Schakellaag:
    """De stand van alle relais. Geen netwerk: elke methode geeft terug wat er moet
    gebeuren, als een lijst van RelaisOpdracht (voor de Shelly-reader) en Stand (voor het
    web). Zo is de werking los te testen (tests/test_schakel.py)."""

    def __init__(self, cfg: Config, klok=time.monotonic, tijd=time.time):
        """klok: monotone klok voor tijdsduren; tijd: systeemklok voor de tijdstempels op
        de bus. Beide te vervangen in tests."""
        self._klok = klok
        self._tijd = tijd
        # Alle relais uit de configuratie, ook een relais zonder 'controls': dat neemt
        # geen keuze of verzoek aan en blijft dus in zijn veilige stand, maar krijgt bij
        # de start wel de opdracht daarheen (punt 7).
        self._relais = {s.id: s for s in cfg.sensoren.values() if s.type == config.RELAIS_TYPE}
        # De relais die bediend mogen worden (§5.4.2 punt 8).
        self._bedienbaar = {r for r, s in self._relais.items() if RELAIS in s.controls}
        # De namen van de alarmregels, voor de reden ("alarmregel Vorst").
        self._regelnaam = {r.id: r.naam for r in cfg.alarmen}

        # --- de vragers (punt 3) ---
        # De relais die met de hand in hun actieve stand staan. Na een herstart geen:
        # alles staat op AUTO (punt 7).
        self._hand: set[str] = set()
        # De verzoeken van de alarmlaag: per relais de alarmregels die om de actieve stand
        # vragen, en wanneer het laatste bericht van de alarmlaag binnenkwam.
        self._verzoeken: dict[str, list[str]] = {}
        self._verzoeken_op: float | None = None
        # Verzoeken voor een relais dat niet bediend mag worden; alleen om het één keer te
        # loggen en niet elke minuut opnieuw.
        self._geweigerd: set[str] = set()

        # --- de voorwaarden (punt 4.1, §5.2.6 punt 5) ---
        # Per relais wat moet gelden om het in de actieve stand te zetten, als (sensor-ID,
        # grootheid, waarde, tekst voor het log), uit het veld 'voorwaarde' van het relais.
        self._voorwaarden: dict[str, list[tuple[str, str, float, str]]] = {
            r: [(sid, g, w, f"{cfg.sensoren[sid].naam}.{g}") for sid, g, w in s.voorwaarde]
            for r, s in self._relais.items()}
        # Van elke (sensor, grootheid) uit een voorwaarde de laatste waarde en het
        # tijdstip (monotone klok). Andere metingen worden niet bijgehouden.
        self._volgen = {(sid, g) for lijst in self._voorwaarden.values() for sid, g, _, _ in lijst}
        self._gevolgd: dict[tuple[str, str], tuple[float, float]] = {}

        # --- wat er het laatst is opgedragen en gemeld ---
        # Per relais of het in de actieve stand hoort te staan; None = nog geen opdracht
        # gegeven (bij de start). De eerste ronde geeft dus elk relais zijn opdracht.
        self._actief: dict[str, bool | None] = {r: None for r in self._relais}
        # Wanneer de actieve stand opnieuw gegeven moet worden (de herhaling, punt 5).
        self._volgende: dict[str, float] = {}
        # De relais waarvan de opdracht opnieuw gegeven moet worden zonder dat er iets
        # veranderd is: de Shelly-reader vroeg erom, of de broker was niet bereikbaar.
        self._opnieuw: set[str] = set()
        # Wat het laatst voor het web gemeld is (keuze, stand, reden), en wanneer.
        self._gemeld: dict[str, tuple[str, str, str]] = {}
        self._gemeld_op: dict[str, float] = {}

    @property
    def relais(self) -> list[str]:
        """De ID's van de relais waarover de schakellaag beslist."""
        return list(self._relais)

    @property
    def volgt(self) -> set[tuple[str, str]]:
        """De (sensor-ID, grootheid) waar een voorwaarde naar kijkt."""
        return set(self._volgen)

    # --- de beslissing (punt 4) -----------------------------------------------------------
    def _voorwaarde_belet(self, relais_id: str, nu: float) -> str | None:
        """Waarom de voorwaarde van dit relais nu niet geldt, als tekst; None = hij geldt.
        Elke voorwaarde moet gelden met een waarde die vers is (jonger dan
        VOORWAARDE_GELDIG); onbekend is niet voldaan (§5.2.6 punt 5)."""
        for sensor_id, grootheid, gevraagd, tekst in self._voorwaarden[relais_id]:
            waarde, op = self._gevolgd.get((sensor_id, grootheid), (None, 0.0))
            if waarde is None or nu - op >= VOORWAARDE_GELDIG:
                return f"{tekst} is onbekend"
            if waarde != gevraagd:
                return f"{tekst} is {waarde:g}, niet {gevraagd:g}"
        return None

    def _beslis(self, relais_id: str, nu: float) -> tuple[bool, str]:
        """De stand die nu geldt voor dit relais: (in de actieve stand?, de reden).

        De volgorde van punt 4. De reden noemt de voorwaarde alleen als er iets om de
        actieve stand vraagt: zonder vraag staat het relais hoe dan ook in zijn veilige
        stand, en is "veilige stand" de eerlijke reden."""
        if relais_id in self._hand:
            vraag = "met de hand"
        elif self._verzoeken.get(relais_id):
            vraag = "alarmregel " + ", ".join(self._regelnaam.get(a, a)
                                              for a in self._verzoeken[relais_id])
        else:
            return False, "veilige stand"
        belet = self._voorwaarde_belet(relais_id, nu)
        if belet is not None:
            # Punt 4.1: de voorwaarde gaat voor alles, ook voor de hand. De keuze of het
            # verzoek blijft staan: geldt de voorwaarde weer, dan gaat het relais alsnog.
            return False, f"voorwaarde geldt niet: {belet} (gevraagd: {vraag})"
        return True, vraag

    def _stand(self, relais_id: str, actief: bool) -> str:
        """"aan" of "uit": de actieve of de veilige stand van dit relais."""
        sensor = self._relais[relais_id]
        return config.actieve_stand(sensor) if actief else sensor.veilige_stand

    def _opdracht(self, relais_id: str, actief: bool) -> RelaisOpdracht:
        """De opdracht voor de Shelly-reader: de actieve stand met de terugschakeltijd, de
        veilige stand zonder (punt 5)."""
        aan = self._stand(relais_id, actief) == "aan"
        return RelaisOpdracht(relais_id, aan, TERUGSCHAKELTIJD if actief else None)

    def _bijwerken(self, nu: float, uit: list) -> None:
        """Zet elk relais in de stand die nu hoort en voegt de opdrachten en de meldingen
        voor het web toe aan `uit`. Wordt na elke meting, elke opdracht en elke tik
        aangeroepen: zo volgt de veilige stand direct op een voorwaarde die niet meer
        geldt, en gaat het relais weer naar de actieve stand zodra hij weer geldt."""
        # De verzoeken van de alarmlaag vervallen als hij te lang zwijgt (punt 3).
        if (self._verzoeken and self._verzoeken_op is not None
                and nu - self._verzoeken_op >= VERZOEK_GELDIG):
            log.warning("geen bericht van de alarmlaag in %g s; zijn verzoeken vervallen (%s)",
                        VERZOEK_GELDIG, ", ".join(self._verzoeken))
            self._verzoeken = {}

        for relais_id, sensor in self._relais.items():
            actief, reden = self._beslis(relais_id, nu)
            stand = self._stand(relais_id, actief)
            keuze = config.actieve_stand(sensor) if relais_id in self._hand else AUTO
            was = self._actief[relais_id]

            if was is None or was != actief:
                # Een andere stand (of de eerste opdracht na de start): opdracht en log.
                log.info("relais %s (%s) %s: %s", sensor.naam, relais_id, stand,
                         "start, " + reden if was is None else reden)
                self._actief[relais_id] = actief
                self._opnieuw.discard(relais_id)
                if actief:
                    self._volgende[relais_id] = nu + HERHAAL
                uit.append(self._opdracht(relais_id, actief))
            elif relais_id in self._opnieuw or (actief and nu >= self._volgende[relais_id]):
                # Dezelfde stand opnieuw: de herhaling van de actieve stand (de Shelly
                # begint zijn terugschakeltijd opnieuw), of op verzoek. Niet in het log.
                self._opnieuw.discard(relais_id)
                if actief:
                    self._volgende[relais_id] = nu + HERHAAL
                uit.append(self._opdracht(relais_id, actief))

            nu_gemeld = (keuze, stand, reden)
            if nu_gemeld != self._gemeld.get(relais_id):
                # De keuze of de reden is veranderd zonder dat de stand veranderde (dat
                # staat hierboven al in het log): bijvoorbeeld de hand op een relais waarvan
                # de voorwaarde niet geldt. Eén regel, zodat te zien is waarom het relais
                # niet volgt (punt 10).
                if was is not None and was == actief:
                    log.info("relais %s (%s) blijft %s: %s", sensor.naam, relais_id, stand, reden)
                self._gemeld[relais_id] = nu_gemeld
                self._gemeld_op[relais_id] = nu
                uit.append(Stand(int(self._tijd()), relais_id, *nu_gemeld))
            elif nu - self._gemeld_op[relais_id] >= MELD_INTERVAL:
                # Elke minuut opnieuw, ook zonder verandering (punt 9).
                self._gemeld_op[relais_id] = nu
                uit.append(Stand(int(self._tijd()), relais_id, *nu_gemeld))

    # --- wat er binnenkomt ------------------------------------------------------------------
    def bij_meting(self, m: Meting) -> list:
        """Een meting van de bus. Alleen een waarde waar een voorwaarde naar kijkt, doet
        ertoe; al het andere geeft niets."""
        if (m.sensor_id, m.grootheid) not in self._volgen:
            return []
        nu = self._klok()
        self._gevolgd[(m.sensor_id, m.grootheid)] = (m.waarde, nu)
        uit: list = []
        self._bijwerken(nu, uit)
        return uit

    def bij_verzoeken(self, verzoeken: list[tuple[str, str]]) -> list:
        """De lijst van verzoeken van de alarmlaag, als (sensor_id, alarm_id). Vervangt
        alles wat de schakellaag van de alarmlaag had (punt 3)."""
        nu = self._klok()
        nieuw: dict[str, list[str]] = {}
        geweigerd: set[str] = set()
        for relais_id, alarm_id in verzoeken:
            if relais_id not in self._bedienbaar:
                # Geen relais, of een relais zonder "relais" in zijn 'controls' (§5.4.2
                # punt 8). check-config houdt zo'n alarmregel al tegen; dit is het vangnet.
                geweigerd.add(relais_id)
            elif alarm_id not in nieuw.setdefault(relais_id, []):
                nieuw[relais_id].append(alarm_id)
        for relais_id in sorted(geweigerd - self._geweigerd):
            log.warning("verzoek van de alarmlaag voor %r genegeerd: geen relais met "
                        "controls = [\"relais\"] in de configuratie", relais_id)
        self._geweigerd = geweigerd
        self._verzoeken = nieuw
        self._verzoeken_op = nu
        uit: list = []
        self._bijwerken(nu, uit)
        return uit

    def bij_hand(self, sensor_id: str, keuze: str) -> list:
        """De keuze van de gebruiker voor één relais (punt 3): "auto", of de actieve stand
        van dat relais. Een onbekend relais, een relais dat niet bediend mag worden of de
        veilige stand als keuze wordt geweigerd, met een regel in het log."""
        sensor = self._relais.get(sensor_id)
        if sensor is None or sensor_id not in self._bedienbaar:
            log.warning("keuze voor %r genegeerd: geen relais met controls = [\"relais\"] in "
                        "de configuratie", sensor_id)
            return []
        if keuze == AUTO:
            self._hand.discard(sensor_id)
        elif keuze == config.actieve_stand(sensor):
            self._hand.add(sensor_id)
        else:
            # Er zijn maar twee keuzes (besluit v0.97): de veilige stand met de hand
            # bestaat niet. Wie een relais tegen een alarm in veilig wil houden, zet de
            # alarmregel uit (punt 6).
            log.warning("keuze %r voor relais %s (%s) geweigerd: de keuzes zijn \"auto\" en "
                        "\"%s\"", keuze, sensor.naam, sensor_id, config.actieve_stand(sensor))
            return []
        uit: list = []
        self._bijwerken(self._klok(), uit)
        return uit

    def opnieuw(self, relais_id: str | None = None) -> None:
        """De opdracht van één relais (of van alle) wordt bij de volgende tik opnieuw
        gegeven, zonder dat er iets veranderd is:
        - de Shelly-reader vraagt bij zijn eigen start om de stand (punt 7): hij heeft de
          opdrachten van vóór zijn start gemist;
        - een opdracht kon niet op de bus (broker niet bereikbaar)."""
        for r in ([relais_id] if relais_id is not None else self._relais):
            if r in self._relais and self._actief[r] is not None:
                self._opnieuw.add(r)

    def tik(self) -> list:
        """Wat door het verstrijken van de tijd verandert; ongeveer elke seconde aanroepen:
        de eerste opdracht na de start, de herhaling van de actieve stand, een voorwaarde
        die te oud wordt, verzoeken die vervallen en de melding voor het web."""
        uit: list = []
        self._bijwerken(self._klok(), uit)
        return uit


# =============================================================================
# De service
# =============================================================================

async def draai(cfg: Config, bus, stop: asyncio.Event) -> None:
    """Hoofdlus van de schakellaag; loopt tot `stop` gezet wordt. Schrijft niets naar
    schijf (§5.8 punt 1)."""
    laag = Schakellaag(cfg)
    if not laag.relais:
        # Geen fout: de service draait dan zonder iets te doen, zodat `brms start` en
        # `brms update` voor alle services gelijk werken.
        log.warning("geen relais (type %s) in de configuratie; er valt niets te schakelen",
                    config.RELAIS_TYPE)

    loop = asyncio.get_running_loop()
    # Alle metingen; de schakellaag gebruikt alleen die waar een voorwaarde naar kijkt.
    # QoS 1: een voorwaarde is een toestand (walstroom), en die komt met QoS 1.
    q = bus.wachtrij(f"{TOPIC_PREFIX}/#", qos=1)

    def voer_uit(uit: list) -> None:
        """Zet opdrachten en meldingen op de bus."""
        for item in uit:
            if isinstance(item, RelaisOpdracht):
                # De Shelly-reader schakelt (§5.2.6 punt 3). Kan de opdracht niet op de
                # bus, dan volgt hij bij de volgende tik opnieuw; en zolang dat niet lukt,
                # zet de Shelly zichzelf na de terugschakeltijd in de veilige stand.
                if not bus.publiceer(TOPIC_RELAIS_ZET, item.naar_json(), qos=1):
                    log.warning("broker niet bereikbaar: relais %s niet %s gezet",
                                item.sensor_id, "aan" if item.aan else "uit")
                    laag.opnieuw(item.sensor_id)
            else:
                # De stand voor het web. Gaat hij verloren, dan komt hij binnen een minuut
                # opnieuw (punt 9).
                bus.publiceer(item.topic(), item.naar_json(), qos=1)

    def bij_verzoek(payload: bytes) -> None:
        """De lijst van verzoeken van de alarmlaag (draait in de asyncio-loop)."""
        try:
            verzoeken = lees_verzoek(payload)
        except ValueError as e:
            log.warning("ongeldig bericht op %s genegeerd: %s", TOPIC_VERZOEK, e)
            return
        voer_uit(laag.bij_verzoeken(verzoeken))

    def bij_hand(payload: bytes) -> None:
        """Een opdracht van het web (draait in de asyncio-loop)."""
        try:
            sensor_id, keuze, grootheid, waarde = lees_hand(payload)
        except ValueError as e:
            log.warning("ongeldige opdracht op %s genegeerd: %s", TOPIC_HAND, e)
            return
        if keuze is None:
            # Een ingevoerde waarde (§5.6): het web is erop voorbereid, maar wat de
            # schakellaag en de reader ermee doen is nog niet ontworpen (OPEN-44).
            log.warning("waarde %g voor %s.%s niet uitgevoerd: er is nog geen sensortype met "
                        "een bedienbare waarde (OPEN-44)", waarde, sensor_id, grootheid)
            return
        voer_uit(laag.bij_hand(sensor_id, keuze))

    # De berichten komen binnen in paho's netwerkthread; verwerken in de asyncio-loop.
    bus.abonneer(TOPIC_VERZOEK, 1,
                 lambda topic, payload: loop.call_soon_threadsafe(bij_verzoek, payload))
    bus.abonneer(TOPIC_HAND, 1,
                 lambda topic, payload: loop.call_soon_threadsafe(bij_hand, payload))
    # De Shelly-reader vraagt bij zijn eigen start om de stand van de relais (punt 7):
    # bij de volgende tik krijgt elk relais zijn opdracht opnieuw.
    bus.abonneer(TOPIC_RELAIS_STAND_VRAAG, 1,
                 lambda topic, payload: loop.call_soon_threadsafe(laag.opnieuw))

    log.info("schakellaag gestart; %d relais: %s", len(laag.relais),
             ", ".join(f"{s.naam} ({s.id}), veilige stand {s.veilige_stand}"
                       + ("" if RELAIS in s.controls else ", niet te bedienen (geen controls)")
                       for s in cfg.sensoren.values() if s.id in laag.relais) or "geen")

    # Na de start staat elk relais op AUTO en krijgt het de opdracht naar zijn veilige
    # stand (punt 7); dat doet de eerste tik. Daarna de alarmlaag om zijn verzoeken vragen:
    # een relais waarvoor een alarm actief is, gaat zo binnen een paar seconden weer naar
    # de actieve stand. Beide pas als de broker bereikbaar is (anders gaat het verloren).
    alarm_gevraagd = False
    laatste_tik = 0.0
    try:
        while not stop.is_set():
            nu = time.monotonic()
            if bus.verbonden:
                # Ongeveer elke seconde: de eerste opdracht, de herhaling, wat veroudert.
                if nu - laatste_tik >= 1.0:
                    laatste_tik = nu
                    voer_uit(laag.tik())
                if not alarm_gevraagd and laag.relais:
                    alarm_gevraagd = bus.publiceer(
                        TOPIC_ALARM_STAND_VRAAG, json.dumps({"ts": int(time.time())}), qos=1)

            # Wachten op de volgende meting, hooguit 1 s (voor de tik en een stopverzoek).
            try:
                topic, payload = await asyncio.wait_for(q.get(), timeout=1.0)
            except TimeoutError:
                continue
            try:
                m = Meting.van_json(payload)
            except ValueError as e:
                # De opslag en de alarmlaag melden een ongeldig bericht al.
                log.debug("ongeldig bericht op %s genegeerd: %s", topic, e)
                continue
            voer_uit(laag.bij_meting(m))
    finally:
        log.info("schakellaag gestopt")
