"""Monitor met trace – beheerhulpmiddel (ARCHITECTURE.md §10.2).

    brms monitor                 busverkeer (metingen en opdrachten) onderin het scherm
    brms monitor kajuit [...]    trace: per meting de hele keten voor deze sensor(en)
    brms monitor stop            het monitorvak sluiten (trace uit); de shell blijft

De monitor opent een tmux-sessie 'brms': bovenin de gewone shell, onderin een vak van
ongeveer 20 regels met de laatste berichten. Wegvallen van de ssh-sessie is geen probleem:
opnieuw `brms monitor` brengt je terug in dezelfde sessie. Opnieuw `brms monitor` met
andere sensoren vervangt het vak.

tmux in het kort:
    brms monitor stop  (in de shell) monitor stoppen; het vak sluit
    muisklik           een vak kiezen; scrollwiel bladert terug (selecteren: met Shift)
    Ctrl-b  ↑ / ↓      wisselen tussen shell en vak (zonder muis)
    Ctrl-c             (in het vak zelf) monitor stoppen – let op: na het starten staat
                       het toetsenbord in de shell bovenin, niet in het vak
    Ctrl-b  d          sessie loskoppelen (blijft draaien; terug met `brms monitor`)
    exit               (in de shell) sessie beëindigen

De monitor is geen onderdeel van het draaiende systeem: BRMS werkt zonder monitor precies
hetzelfde. Hij publiceert alleen trace-opdrachten (aan/uit) en luistert verder alleen.
"""

import collections
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


def formatteer_trace(d: dict, namen: dict[str, str]) -> list[str]:
    """Een tracebericht als blok regels:

        22:03:41  kajuit       RUW          BTHome 40 00 a4 01 64 ...
                               GEDECODEERD  temperatuur  0x02: ca 09 = 2506 × 0.01 = 25.06
                                            vocht        ...
    De stap 'ruw' begint een nieuwe keten en krijgt een scheidingslijn erboven.
    """
    tijd = time.strftime("%H:%M:%S", time.localtime(d.get("ts", 0)))
    stap = str(d.get("stap", "?")).upper()
    tekst = str(d.get("tekst", "")).split("\n")
    kop = f"{tijd}  {_naam(d.get('sensor_id', '?'), namen):<12} {stap:<12} "
    regels = [kop + tekst[0]] + [" " * len(kop) + t for t in tekst[1:]]
    if d.get("stap") == "ruw":
        regels.insert(0, "─" * 40)
    return regels


# =============================================================================
# Het vak onderin (draait in het onderste tmux-vak: `brms monitor-vak ...`)
# =============================================================================

def _teken(regels, kop: str) -> None:
    """Scherm wissen en de kop plus de laatste regels tekenen, afgekapt op de breedte
    (een afgebroken regel zou het vak laten verspringen)."""
    breedte, hoogte = shutil.get_terminal_size((100, VAK_REGELS))
    zichtbaar = list(regels)[-(hoogte - 1):] if hoogte > 1 else []
    uit = ["\033[H\033[J", "\033[7m" + kop[:breedte].ljust(breedte) + "\033[0m"]
    uit += [r[:breedte] for r in zichtbaar]
    sys.stdout.write(uit[0] + "\n".join(uit[1:]))
    sys.stdout.flush()


def draai_vak(config: Config, sensoren: list[Sensor]) -> int:
    """Luistert op de bus en tekent de laatste berichten. Loopt tot Ctrl-c of tot tmux
    het vak sluit."""
    from brms.bus import Bus

    namen = {s.id: s.naam for s in config.sensoren.values()}
    ids = [s.id for s in sensoren]
    regels = collections.deque(maxlen=500)
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
    regels.append("wachten op berichten …")
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
                    regels.extend(_maak_regels(topic, payload, namen))
                    gewijzigd = True
                    topic, payload = binnen.get_nowait()
            except queue.Empty:
                pass

            # Hooguit 5 keer per seconde tekenen, en ook bij een gewijzigde venstergrootte
            # (eens per seconde opnieuw).
            nu = time.monotonic()
            if (gewijzigd and nu - getekend >= 0.2) or nu - getekend >= 1.0:
                _teken(regels, kop)
                getekend, gewijzigd = nu, False
    except KeyboardInterrupt:
        pass
    finally:
        if ids:
            trace_opdracht(trace.TOPIC_UIT)
            time.sleep(0.3)  # paho de tijd geven om de opdracht te versturen
        bus.stop()
    return 0


def _maak_regels(topic: str, payload: bytes, namen: dict[str, str]) -> list[str]:
    """Kiest de opmaak bij de soort bericht; een onleesbaar bericht wordt ruw getoond."""
    try:
        if topic.startswith(trace.TOPIC_PREFIX + "/"):
            return formatteer_trace(json.loads(payload), namen)
        if topic.startswith("brms/meting/"):
            return formatteer_meting(json.loads(payload), namen)
        return formatteer_opdracht(topic, payload)
    except (ValueError, AttributeError):
        return [f"{time.strftime('%H:%M:%S')}  ONLEESBAAR  {topic}  {payload[:200]!r}"]


# =============================================================================
# tmux-sessie openen (`brms monitor ...`)
# =============================================================================

def _tmux(*args, uitvoer=False):
    r = subprocess.run(["tmux", *args], capture_output=True, text=True)
    return r.stdout.strip() if uitvoer else r.returncode == 0


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
    vak_id = _tmux("show-options", "-v", "-t", SESSIE, "@brms_vak", uitvoer=True)
    if vak_id and _tmux("display-message", "-p", "-t", vak_id, ""):
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
    vak_id = _tmux("show-options", "-v", "-t", SESSIE, "@brms_vak", uitvoer=True)
    if not vak_id or not _tmux("display-message", "-p", "-t", vak_id, ""):
        print("Er draait geen monitor.")
        return 0
    _tmux("kill-pane", "-t", vak_id)
    _tmux("set-option", "-u", "-t", SESSIE, "@brms_vak")
    print("Monitor gestopt; trace staat uit.")
    return 0
