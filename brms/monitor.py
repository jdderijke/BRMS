"""Monitor met trace – beheerhulpmiddel (ARCHITECTURE.md §10.2).

    brms monitor                 busverkeer (metingen en opdrachten) onderin het scherm
    brms monitor kajuit [...]    trace: per meting de hele keten voor deze sensor(en)
    brms monitor stop            het monitorvak sluiten (trace uit); de shell blijft

De monitor opent een tmux-sessie 'brms': bovenin de gewone shell, onderin een vak van
ongeveer 20 regels. Elke waarde heeft daarin een vaste plaats: een nieuwe meting of keten
overschrijft de vorige, er schuift niets. Past niet alles in het vak, sleep dan de rand
tussen shell en vak met de muis omhoog. Wegvallen van de ssh-sessie is geen probleem:
opnieuw `brms monitor` brengt je terug in dezelfde sessie. Opnieuw `brms monitor` met
andere sensoren vervangt het vak.

tmux in het kort:
    brms monitor stop  (in de shell) monitor stoppen; het vak sluit
    muisklik           een vak kiezen; de rand slepen maakt het vak groter of kleiner
                       (tekst selecteren: met Shift)
    Ctrl-b  ↑ / ↓      wisselen tussen shell en vak (zonder muis)
    Ctrl-c             (in het vak zelf) monitor stoppen – let op: na het starten staat
                       het toetsenbord in de shell bovenin, niet in het vak
    Ctrl-b  d          sessie loskoppelen (blijft draaien; terug met `brms monitor`)
    exit               (in de shell) sessie beëindigen

De monitor is geen onderdeel van het draaiende systeem: BRMS werkt zonder monitor precies
hetzelfde. Hij publiceert alleen trace-opdrachten (aan/uit) en luistert verder alleen.
"""

import json
import os
import queue
import shlex
import shutil
import signal
import subprocess
import sys
import time

from brms import grootheden, trace
from brms.config import Config, Sensor

SESSIE = "brms"
VAK_REGELS = 20             # hoogte van het vak onderin (§10.2 punt 1)
VERNIEUW_TRACE = 60         # trace-opdracht elke minuut opnieuw sturen (§10.2 punt 3)
TRACE_DUUR = 600            # looptijd per opdracht; verloopt vanzelf als de monitor stopt


# =============================================================================
# Sensoren zoeken en berichten opmaken (zuivere functies, getest in tests/test_monitor.py)
# =============================================================================

def zoek_sensoren(namen: list[str], config: Config) -> list[Sensor]:
    """Zoekt sensoren op naam (hoofdletterongevoelig) of op sensor-ID.

    ValueError met een duidelijke melding als een naam niet bestaat.
    """
    gevonden = []
    for naam in namen:
        sensor = config.sensoren.get(naam.upper())  # op ID (MAC-adres)
        if sensor is None:
            sensor = next((s for s in config.sensoren.values()
                           if s.naam.lower() == naam.lower()), None)
        if sensor is None:
            bekend = ", ".join(s.naam for s in config.sensoren.values()) or "(geen)"
            raise ValueError(f"onbekende sensor '{naam}'; bekend: {bekend}")
        gevonden.append(sensor)
    return gevonden


def _naam(sensor_id: str, namen: dict[str, str]) -> str:
    # Naam uit de configuratie; een sensor die niet (meer) in de configuratie staat, wordt
    # met zijn ID getoond.
    return namen.get(sensor_id, sensor_id)


def _getal(waarde) -> str:
    # Zoals een mens het leest: geen overbodige nullen, decimale komma.
    return f"{waarde:g}".replace(".", ",") if isinstance(waarde, (int, float)) else str(waarde)


def formatteer_meting(d: dict, namen: dict[str, str]) -> list[str]:
    """Eén regel voor een meting van de bus: tijd, naam, grootheid, waarde met eenheid."""
    tijd = time.strftime("%H:%M:%S", time.localtime(d.get("ts", 0)))
    g = d.get("grootheid", "?")
    eenheid = grootheden.GROOTHEDEN[g].eenheid if g in grootheden.GROOTHEDEN else ""
    return [f"{tijd}  {_naam(d.get('sensor_id', '?'), namen):<12} {g:<17} "
            f"{_getal(d.get('waarde')):>9} {eenheid}"]


def formatteer_opdracht(topic: str, payload: bytes) -> list[str]:
    tijd = time.strftime("%H:%M:%S")
    return [f"{tijd}  OPDRACHT     {topic}  {payload.decode(errors='replace')}"]


# =============================================================================
# De inhoud van het vak: vaste plaatsen die overschreven worden (§10.2 punt 2)
# =============================================================================
#
# Het vak is geen doorlopende lijst: elke waarde heeft een vaste plaats op het scherm en
# een nieuwe waarde overschrijft de vorige. Zo verspringt er niets. Regels komen er alleen
# bij (een nieuwe sensor of grootheid); er verdwijnt nooit een regel.

class Tabel:
    """Busmodus (`brms monitor`): één regel per sensor en grootheid, met de laatst
    ontvangen waarde en het tijdstip daarvan. Bovenaan de laatste opdracht op de bus."""

    def __init__(self, config: Config):
        self._namen = {s.id: s.naam for s in config.sensoren.values()}
        # Volgorde van de sensoren: zoals in de configuratie; een sensor die daar niet
        # (meer) in staat, komt achteraan zodra hij voor het eerst iets stuurt.
        self._sensoren = list(config.sensoren)
        self._metingen: dict[tuple[str, str], dict] = {}   # (sensor-ID, grootheid) -> bericht
        self._opdracht = "(nog geen)"

    def meting(self, d: dict) -> None:
        sensor_id, g = d.get("sensor_id"), d.get("grootheid")
        if not isinstance(sensor_id, str) or not isinstance(g, str):
            raise ValueError("meting zonder sensor_id of grootheid")
        if sensor_id not in self._sensoren:
            self._sensoren.append(sensor_id)
        self._metingen[(sensor_id, g)] = d

    def opdracht(self, topic: str, payload: bytes) -> None:
        self._opdracht = formatteer_opdracht(topic, payload)[0]

    def regels(self) -> list[str]:
        # Grootheden in de volgorde van de tabel van grootheden (§5.3.3).
        volgorde = list(grootheden.GROOTHEDEN)

        def plaats(sleutel):
            sensor_id, g = sleutel
            return (self._sensoren.index(sensor_id),
                    volgorde.index(g) if g in volgorde else len(volgorde), g)

        uit = [f"Laatste opdracht: {self._opdracht}"]
        for sleutel in sorted(self._metingen, key=plaats):
            uit += formatteer_meting(self._metingen[sleutel], self._namen)
        if not self._metingen:
            uit.append("wachten op metingen …")
        return uit


# De stappen van een keten, in de volgorde waarin ze op het scherm staan (§10.2 punt 4).
STAPPEN = ("ruw", "gedecodeerd", "gecorrigeerd", "gepubliceerd", "opgenomen", "opgeslagen")


class Ketens:
    """Tracemodus (`brms monitor kajuit`): per sensor één blok met de laatste keten.

        kajuit       RUW          22:03:41  BTHome 40 00 a4 01 64 ...
                     GEDECODEERD  22:03:41  temperatuur  0x02: ca 09 = 2506 × 0.01 = 25.06
                                            vocht        ...
                     GECORRIGEERD 22:03:41  ...

    Elke stap heeft een vaste plaats; een nieuw tracebericht overschrijft de vorige inhoud
    van die stap. Het tijdstip staat per stap, zodat te zien is hoe oud de inhoud is (de
    stap 'opgeslagen' komt maar eens per 5 minuten).

    Het aantal regels van een stap verschilt per sensor en hangt af van de weggelaten
    grootheden. Daarom onthoudt het blok per stap het grootste aantal regels dat het gezien
    heeft en vult het korter uitvallende inhoud aan met lege regels: de stappen eronder
    blijven dan op hun plaats.

    Een stap kan uit delen bestaan (veld 'deel' in het tracebericht): de opslag stuurt
    'opgenomen' per grootheid. Elk deel heeft dan zijn eigen regel(s) binnen de stap.
    """

    def __init__(self, sensoren: list[Sensor]):
        self._sensoren = sensoren
        # sensor-ID -> stap -> deel -> (tijdstip, regels tekst)
        self._inhoud: dict[str, dict[str, dict[str, tuple[int, list[str]]]]] = {
            s.id: {stap: {} for stap in STAPPEN} for s in sensoren}
        # (sensor-ID, stap) -> grootste aantal regels tot nu toe
        self._hoogte: dict[tuple[str, str], int] = {}

    def trace(self, d: dict) -> None:
        stappen = self._inhoud.get(d.get("sensor_id"))
        if stappen is None:
            return  # trace van een sensor die niet in dit vak staat
        stap, deel = str(d.get("stap", "?")), str(d.get("deel", ""))
        tekst = str(d.get("tekst", "")).split("\n")
        # Een stap die niet in STAPPEN staat (nieuwe dienst), komt onderaan het blok.
        stappen.setdefault(stap, {})[deel] = (d.get("ts", 0), tekst)

    def regels(self) -> list[str]:
        uit = []
        for sensor in self._sensoren:
            naam = sensor.naam   # alleen op de eerste regel van het blok
            for stap, delen in self._inhoud[sensor.id].items():
                # De inhoud van deze stap: per deel het tijdstip en de tekst.
                blok = []
                for ts, tekst in delen.values():
                    tijd = time.strftime("%H:%M:%S", time.localtime(ts))
                    blok.append(f"{tijd}  {tekst[0]}")
                    blok += [" " * 10 + t for t in tekst[1:]]
                if not blok:
                    blok = ["–"]   # nog niets ontvangen voor deze stap
                # Aanvullen tot het grootste aantal regels dat deze stap ooit had.
                hoogte = max(len(blok), self._hoogte.get((sensor.id, stap), 0))
                self._hoogte[(sensor.id, stap)] = hoogte
                blok += [""] * (hoogte - len(blok))
                for i, regel in enumerate(blok):
                    label = stap.upper() if i == 0 else ""
                    uit.append(f"{naam:<12} {label:<12} {regel}".rstrip())
                    naam = ""
        return uit


def pas_in(regels: list[str], ruimte: int) -> list[str]:
    """De regels die in het vak passen. Past niet alles, dan zegt de laatste regel hoeveel
    er niet te zien is (terugbladeren kan niet in een scherm dat overschreven wordt)."""
    if len(regels) <= ruimte:
        return list(regels)
    if ruimte <= 0:
        return []
    verborgen = len(regels) - (ruimte - 1)
    return list(regels[:ruimte - 1]) + [f"… nog {verborgen} regels: maak het vak groter "
                                        f"(sleep de rand met de muis)"]


# =============================================================================
# Het vak onderin (draait in het onderste tmux-vak: `brms monitor-vak ...`)
# =============================================================================

def _teken(regels: list[str], kop: str) -> None:
    """Tekent de kop en de regels over het bestaande scherm heen.

    Het scherm wordt niet eerst gewist (dat flikkert): elke regel wordt overschreven en de
    rest van die regel gewist (\\033[K); onder de laatste regel wordt alles gewist (\\033[J).
    Regels worden afgekapt op de breedte: een afgebroken regel zou het vak laten verspringen.
    """
    breedte, hoogte = shutil.get_terminal_size((100, VAK_REGELS))
    uit = ["\033[7m" + kop[:breedte].ljust(breedte) + "\033[0m"]
    # Eén teken minder dan de breedte: op de laatste kolom zou \033[K het laatste teken wissen.
    uit += [r[:breedte - 1] + "\033[K" for r in pas_in(regels, hoogte - 1)]
    sys.stdout.write("\033[H" + "\n".join(uit) + "\033[J")
    sys.stdout.flush()


def draai_vak(config: Config, sensoren: list[Sensor]) -> int:
    """Luistert op de bus en tekent de laatste berichten. Loopt tot Ctrl-c of tot tmux
    het vak sluit."""
    from brms.bus import Bus

    ids = [s.id for s in sensoren]
    # De inhoud van het vak: ketens per sensor (trace) of een tabel van laatste waarden.
    scherm = Ketens(sensoren) if ids else Tabel(config)
    fout = ""                             # laatste onleesbare bericht, onderaan getoond
    binnen: queue.Queue = queue.Queue()   # paho's thread zet berichten hierin

    # Een eigen client-ID per monitor (pid), zodat twee monitors elkaar niet afmelden.
    bus = Bus(f"monitor-{os.getpid()}", config.mqtt.host, config.mqtt.port)
    if ids:
        # Trace-modus: alleen de traceberichten van de gekozen sensoren.
        for i in ids:
            bus.abonneer(f"{trace.TOPIC_PREFIX}/{i}", 0, lambda t, p: binnen.put((t, p)))
        kop = f" BRMS trace: {', '.join(s.naam for s in sensoren)}   (Ctrl-c stopt)"
    else:
        # Busmodus: alle metingen en opdrachten.
        bus.abonneer("brms/meting/#", 1, lambda t, p: binnen.put((t, p)))
        bus.abonneer("brms/opdracht/#", 1, lambda t, p: binnen.put((t, p)))
        kop = " BRMS bus: metingen en opdrachten   (Ctrl-c stopt)"
    bus.start()

    # tmux sluit een vak met SIGHUP; systemd/kill met SIGTERM. Netjes afsluiten, zodat
    # trace direct uitgaat (anders pas na TRACE_DUUR).
    def stop(signum, frame):
        raise KeyboardInterrupt
    for sig in ("SIGHUP", "SIGTERM"):
        if hasattr(signal, sig):
            signal.signal(getattr(signal, sig), stop)

    def trace_opdracht(topic, **extra):
        bus.publiceer(topic, json.dumps({"sensor_ids": ids, **extra}), qos=1)

    vernieuwd = 0.0     # monotone tijd van de laatste trace-opdracht
    getekend = 0.0
    gewijzigd = True
    try:
        while True:
            # Trace aan houden zolang de monitor draait (pas zodra de bus verbonden is).
            if ids and bus.verbonden and time.monotonic() - vernieuwd >= VERNIEUW_TRACE:
                trace_opdracht(trace.TOPIC_AAN, duur=TRACE_DUUR)
                vernieuwd = time.monotonic()

            # Alle binnengekomen berichten verwerken.
            try:
                topic, payload = binnen.get(timeout=0.2)
                while True:
                    fout = _verwerk(scherm, topic, payload) or fout
                    gewijzigd = True
                    topic, payload = binnen.get_nowait()
            except queue.Empty:
                pass

            # Hooguit 5 keer per seconde tekenen, en ook bij een gewijzigde venstergrootte
            # (eens per seconde opnieuw).
            nu = time.monotonic()
            if (gewijzigd and nu - getekend >= 0.2) or nu - getekend >= 1.0:
                _teken(scherm.regels() + ([fout] if fout else []), kop)
                getekend, gewijzigd = nu, False
    except KeyboardInterrupt:
        pass
    finally:
        if ids:
            trace_opdracht(trace.TOPIC_UIT)
            time.sleep(0.3)  # paho de tijd geven om de opdracht te versturen
        bus.stop()
    return 0


def _verwerk(scherm, topic: str, payload: bytes) -> str:
    """Zet een bericht van de bus op zijn plaats in het scherm (Ketens of Tabel).

    Geeft een lege tekst terug, of bij een onleesbaar bericht een regel die dat meldt.
    """
    try:
        if topic.startswith(trace.TOPIC_PREFIX + "/"):
            scherm.trace(json.loads(payload))
        elif topic.startswith("brms/meting/"):
            scherm.meting(json.loads(payload))
        else:
            scherm.opdracht(topic, payload)
        return ""
    except (ValueError, AttributeError):
        return f"{time.strftime('%H:%M:%S')}  ONLEESBAAR  {topic}  {payload[:200]!r}"


# =============================================================================
# tmux-sessie openen (`brms monitor ...`)
# =============================================================================

def _tmux(*args, uitvoer=False):
    r = subprocess.run(["tmux", *args], capture_output=True, text=True)
    return r.stdout.strip() if uitvoer else r.returncode == 0


def _monitorvak() -> str | None:
    """Het pane-ID van het monitorvak, of None als er geen (levend) monitorvak is.

    Het ID staat in de sessie-optie @brms_vak. Of dat vak nog bestaat, wordt gecontroleerd
    in de lijst van vakken die echt in de sessie zitten. (Niet met 'display-message -t':
    tmux 3.5 meldt daarbij ook "gelukt" voor een vak dat al gesloten is.)
    """
    vak_id = _tmux("show-options", "-qv", "-t", SESSIE, "@brms_vak", uitvoer=True)
    if not vak_id:
        return None
    vakken = _tmux("list-panes", "-s", "-t", SESSIE, "-F", "#{pane_id}", uitvoer=True).split()
    return vak_id if vak_id in vakken else None


def open_sessie(argumenten: list[str], config_pad: str) -> int:
    """Opent (of hervat) de tmux-sessie 'brms' met de shell boven en het vak onder.

    Het pane-ID van het vak wordt in de sessie bewaard (optie @brms_vak), zodat een
    volgende `brms monitor` precies dat vak vervangt, ook als de gebruiker zelf extra
    vensters of vakken heeft gemaakt.
    """
    if shutil.which("tmux") is None:
        print("tmux is niet geïnstalleerd (sudo apt install tmux, of draai deploy/install.sh).",
              file=sys.stderr)
        return 1

    # De opdracht die in het vak draait.
    vak = " ".join(shlex.quote(a) for a in
                   ["brms", "--config", config_pad, "monitor-vak", *argumenten])

    if not _tmux("has-session", "-t", SESSIE):
        # Nieuwe sessie, losgekoppeld aangemaakt; daarna koppelen we eraan.
        _tmux("new-session", "-d", "-s", SESSIE, "-c", os.path.expanduser("~"))
    # Muisbediening: klikken kiest een vak, het scrollwiel bladert terug. Tekst selecteren
    # om te kopiëren gaat dan met Shift ingedrukt (in de meeste terminals).
    _tmux("set-option", "-t", SESSIE, "mouse", "on")
    vak_id = _monitorvak()
    if vak_id:
        # Het vak bestaat nog: de monitor erin vervangen door een nieuwe.
        _tmux("respawn-pane", "-k", "-t", vak_id, vak)
    else:
        # Nieuw vak onderin, VAK_REGELS hoog; de shell bovenin blijft het actieve vak.
        vak_id = _tmux("split-window", "-d", "-v", "-l", str(VAK_REGELS), "-P",
                       "-F", "#{pane_id}", "-t", SESSIE, vak, uitvoer=True)
        _tmux("set-option", "-t", SESSIE, "@brms_vak", vak_id)

    if os.environ.get("TMUX"):
        # Al binnen tmux: overschakelen naar de sessie (doet niets als we er al in zitten).
        _tmux("switch-client", "-t", SESSIE)
        return 0
    # Buiten tmux: dit proces vervangen door tmux, gekoppeld aan de sessie.
    os.execvp("tmux", ["tmux", "attach-session", "-t", SESSIE])


def sluit_vak() -> int:
    """`brms monitor stop`: sluit het monitorvak; de shell (en de tmux-sessie) blijft.

    tmux stuurt de monitor bij het sluiten SIGHUP; draai_vak vangt dat op en zet de trace
    netjes uit.
    """
    if shutil.which("tmux") is None or not _tmux("has-session", "-t", SESSIE):
        print("Er draait geen monitor.")
        return 0
    vak_id = _monitorvak()
    if not vak_id:
        print("Er draait geen monitor.")
        return 0
    _tmux("kill-pane", "-t", vak_id)
    _tmux("set-option", "-u", "-t", SESSIE, "@brms_vak")
    print("Monitor gestopt; trace staat uit.")
    return 0
