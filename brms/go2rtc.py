"""go2rtc: het live-beeld van de camera's (ARCHITECTURE.md §5.7 punt 18).

go2rtc is een los programma (niet van BRMS) dat de videostroom van een camera doorgeeft.
Een browser kan de RTSP-stroom van een camera niet openen; go2rtc pakt dezelfde
H.264-video opnieuw in als WebRTC, wat elke browser afspeelt. Het rekent het beeld niet
om: het volle beeld op volle snelheid, met vrijwel geen belasting van de Pi. go2rtc houdt
één verbinding met de camera voor alle afnemers samen, en alleen zolang er een afnemer is.

Deze module is alles wat BRMS van go2rtc weet:

1. **Het instellingenbestand.** go2rtc leest zijn instellingen uit een eigen bestand
   (YAML). BRMS schrijft dat bestand bij elke start van de service uit `brms.toml`, zodat
   het account van de camera op één plek blijft staan. Elke camera met een `video_url`
   wordt een stroom met het `id` van de camera als naam.
2. **De service `brms-go2rtc`** (`brms go2rtc`): schrijft het instellingenbestand en start
   dan go2rtc in plaats van zichzelf.
3. **Het live-beeld aanvragen** (voor de webservice): het WebRTC-aanbod van de browser
   doorgeven aan go2rtc en het antwoord teruggeven. Daarna loopt de video rechtstreeks
   tussen de browser en go2rtc (poort 8555).
4. **Het adres voor een video-opname** (voor de camera-reader): de stroom die go2rtc op de
   Pi doorgeeft. Kijken en opnemen delen zo één verbinding met de camera.

Bereikbaarheid: het beheer van go2rtc (poort 1984) en zijn RTSP-uitgang (poort 8554)
luisteren alleen op de Pi zelf (127.0.0.1). De beheerpagina van go2rtc heeft geen
aanmelding en toont het wachtwoord van de camera; die mag dus niet vanaf het netwerk te
openen zijn. Alleen de poort voor de video zelf (8555) staat open.

Alles hier gebruikt de standaardbibliotheek (geen extra afhankelijkheid, P3).
"""

import json
import logging
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

from brms.config import PROJECTMAP, Config

log = logging.getLogger(__name__)

# Het programma zelf; deploy/go2rtc.sh zet het hier neer (vaste versie, §10.1 punt 6).
PROGRAMMA = os.path.join(PROJECTMAP, "bin", "go2rtc")
# Het instellingenbestand dat BRMS schrijft. In data/: alleen daar mogen de services
# schrijven (§10.1 punt 5).
INSTELLINGEN = os.path.join(PROJECTMAP, "data", "go2rtc.yaml")

# Alleen op de Pi zelf: het beheer (API) en de RTSP-uitgang van go2rtc.
HOST = "127.0.0.1"
POORT_API = 1984
POORT_RTSP = 8554
# Voor het netwerk en Tailscale: de video naar de browser (WebRTC), over TCP en UDP.
POORT_WEBRTC = 8555

# Langste wachttijd op een antwoord van go2rtc, in seconden. Bij een aanvraag van het
# live-beeld verbindt go2rtc eerst met de camera; dat duurde in de proef ruim een seconde.
TIMEOUT = 10.0
# Zo lang wacht de camera-reader op de RTSP-poort van go2rtc voordat hij besluit om
# rechtstreeks van de camera op te nemen.
TIMEOUT_POORT = 1.0

# Zo lang wacht de service bij het starten op een adres van Tailscale (zie
# _wacht_op_tailscale).
WACHT_TAILSCALE = 30.0

# Een WebRTC-aanbod of -antwoord is enkele kB tekst; meer dan dit is iets anders.
MAX_SDP = 64 * 1024


class Go2rtcFout(Exception):
    """go2rtc is niet bereikbaar of weigert. De tekst is bedoeld voor de gebruiker en het
    log en bevat geen geheim."""


# De melding als go2rtc helemaal niet antwoordt (de service draait niet, of start nog).
NIET_BEREIKBAAR = "go2rtc is niet bereikbaar op de Pi (draait de service brms-go2rtc?)"


def instellingen(cfg: Config) -> str:
    """De inhoud van het instellingenbestand van go2rtc, uit de configuratie van BRMS.

    Het bestand is YAML. Elke waarde wordt geschreven als JSON-tekst (tussen dubbele
    aanhalingstekens, met \\ voor bijzondere tekens): dat is ook geldige YAML, en zo kan
    een wachtwoord met een ':' of '#' de indeling niet breken.
    """
    # Hier binnen de functie: brms.readers.camera gebruikt deze module ook.
    from brms.readers.camera import vul_in

    regels = [
        "# Door BRMS geschreven uit config/brms.toml bij elke start van brms-go2rtc.",
        "# Niet met de hand wijzigen: het wordt overschreven. Bevat het camerawachtwoord.",
        "api:",
        # Het beheer alleen op de Pi zelf: geen aanmelding, en het toont dit bestand.
        f"  listen: {json.dumps(f'{HOST}:{POORT_API}')}",
        "rtsp:",
        # De RTSP-uitgang is alleen voor de camera-reader (video-opnames), op de Pi zelf.
        f"  listen: {json.dumps(f'{HOST}:{POORT_RTSP}')}",
        "webrtc:",
        # De video naar de browser. go2rtc biedt de browser zelf de adressen van de Pi aan
        # (het netwerk aan boord en Tailscale).
        f"  listen: {json.dumps(f':{POORT_WEBRTC}')}",
        # Geen hulp van een server op internet (STUN) bij het zoeken naar een adres: de
        # browser bereikt de Pi rechtstreeks via het netwerk of Tailscale. Met zo'n server
        # duurde het aanvragen in de proef 9 s in plaats van een halve seconde.
        "  ice_servers: []",
        "log:",
        "  level: \"info\"",
        "streams:",
    ]
    stromen = [c for c in cfg.cameras.values() if c.video_url]
    for c in stromen:
        # De naam van de stroom is het id van de camera (alleen letters, cijfers, '-', '_').
        regels.append(f"  {json.dumps(c.id)}: {json.dumps(vul_in(c.video_url, c))}")
    if not stromen:
        # Zonder camera met video_url draait go2rtc zonder iets te doen.
        regels[-1] = "streams: {}"
    return "\n".join(regels) + "\n"


def schrijf_instellingen(cfg: Config, pad: str | None = None) -> None:
    """Schrijft het instellingenbestand (zonder `pad`: INSTELLINGEN), alleen leesbaar voor
    de eigenaar: het bevat het wachtwoord van de camera (§5.7 punt 8). OSError als het
    niet lukt."""
    pad = pad or INSTELLINGEN
    os.makedirs(os.path.dirname(pad), exist_ok=True)
    tijdelijk = pad + ".tmp"
    # 0o600 al bij het aanmaken, zodat het bestand geen moment voor anderen leesbaar is.
    with os.fdopen(os.open(tijdelijk, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w",
                   encoding="utf-8") as f:
        f.write(instellingen(cfg))
    os.replace(tijdelijk, pad)


def _wacht_op_tailscale(hooguit: float = WACHT_TAILSCALE) -> None:
    """Wacht tot Tailscale een adres heeft, hooguit `hooguit` seconden.

    go2rtc leest de adressen van de Pi één keer, bij het starten, en biedt alleen die aan
    de browser aan. Na het opstarten van de Pi kan Tailscale iets later klaar zijn dan
    deze service; zonder te wachten zou het live-beeld dan via Tailscale (de enige weg naar
    de boot) niet werken tot go2rtc opnieuw gestart is. Staat Tailscale niet op deze
    machine, dan wordt er niet gewacht.
    """
    if shutil.which("tailscale") is None:
        return
    einde = time.monotonic() + hooguit
    while True:
        try:
            # 'tailscale ip -4' geeft het adres en exitcode 0 zodra Tailscale verbonden is.
            r = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=5)
            if r.returncode == 0 and r.stdout.strip():
                return
        except (OSError, subprocess.SubprocessError):
            pass
        if time.monotonic() >= einde:
            log.warning("Tailscale heeft na %g s nog geen adres; go2rtc start zonder. Het "
                        "live-beeld werkt dan alleen op het lokale netwerk, tot "
                        "brms-go2rtc opnieuw gestart is.", hooguit)
            return
        time.sleep(2)


def start(cfg: Config) -> int:
    """De service brms-go2rtc: schrijft het instellingenbestand en vervangt dit proces dan
    door go2rtc (exec). Komt alleen terug als dat niet lukt; dan is de waarde de exitcode.
    """
    if not os.path.isfile(PROGRAMMA):
        log.error("go2rtc staat niet in %s; er is geen live-beeld. Installeer het met: "
                  "brms update (of: bash deploy/go2rtc.sh)", PROGRAMMA)
        return 1
    try:
        schrijf_instellingen(cfg)
    except OSError as e:
        log.error("instellingen van go2rtc niet te schrijven in %s: %s", INSTELLINGEN, e)
        return 1
    _wacht_op_tailscale()
    stromen = [c.id for c in cfg.cameras.values() if c.video_url]
    log.info("go2rtc starten; live-beeld voor %d camera's: %s", len(stromen),
             ", ".join(stromen) or "geen")
    try:
        os.execv(PROGRAMMA, [PROGRAMMA, "-config", INSTELLINGEN])
    except OSError as e:
        log.error("go2rtc kon niet gestart worden: %s", e)
        return 1


def rtsp_adres(camera_id: str) -> str:
    """Het adres waarop go2rtc de stroom van een camera op de Pi doorgeeft; hiervan neemt
    de camera-reader een video op. Er staat geen wachtwoord in."""
    return f"rtsp://{HOST}:{POORT_RTSP}/{urllib.parse.quote(camera_id, safe='')}"


def bereikbaar(timeout: float = TIMEOUT_POORT) -> bool:
    """Of go2rtc draait: is zijn RTSP-poort op de Pi te openen? Blokkeert hooguit
    `timeout` seconden."""
    try:
        with socket.create_connection((HOST, POORT_RTSP), timeout=timeout):
            return True
    except OSError:
        return False


def _api(pad: str) -> str:
    return f"http://{HOST}:{POORT_API}{pad}"


def kijkers(camera_id: str, timeout: float = TIMEOUT) -> int:
    """Het aantal browsers dat nu live naar deze camera kijkt. Een video-opname is ook een
    afnemer van go2rtc, maar geen kijker: alleen afnemers via WebRTC tellen.
    Go2rtcFout als go2rtc niet antwoordt."""
    try:
        with urllib.request.urlopen(_api("/api/streams"), timeout=timeout) as r:
            stromen = json.loads(r.read(1024 * 1024))
    except (OSError, ValueError) as e:
        # De technische reden is voor het log; de gebruiker ziet een gewone zin.
        log.debug("go2rtc antwoordt niet op /api/streams: %s", e)
        raise Go2rtcFout(NIET_BEREIKBAAR) from None
    stroom = stromen.get(camera_id) if isinstance(stromen, dict) else None
    afnemers = (stroom or {}).get("consumers") or []
    return sum(1 for a in afnemers
               if isinstance(a, dict) and str(a.get("format_name", "")).startswith("webrtc"))


def webrtc(camera_id: str, aanbod: str, timeout: float = TIMEOUT) -> str:
    """Vraagt het live-beeld van een camera aan: geeft het WebRTC-aanbod van de browser
    (SDP, tekst) aan go2rtc en geeft het antwoord (SDP) terug.

    go2rtc verbindt hierbij zo nodig eerst met de camera. Go2rtcFout als go2rtc niet
    bereikbaar is, de camera niet kent of geen beeld van de camera krijgt.
    """
    vraag = urllib.request.Request(
        _api("/api/webrtc?" + urllib.parse.urlencode({"src": camera_id})),
        data=aanbod.encode("utf-8"), method="POST", headers={"Content-Type": "application/sdp"})
    try:
        with urllib.request.urlopen(vraag, timeout=timeout) as r:
            antwoord = r.read(MAX_SDP + 1)
    except urllib.error.HTTPError as e:
        # go2rtc antwoordt wel, maar met een fout: meestal krijgt het geen beeld van de
        # camera (uit, onbereikbaar, verkeerd wachtwoord), of het kent de camera niet
        # (brms-go2rtc is niet herstart na een wijziging van brms.toml).
        raise Go2rtcFout(f"go2rtc krijgt geen beeld van de camera (fout {e.code})") from None
    except OSError as e:
        log.debug("go2rtc antwoordt niet op /api/webrtc: %s", e)
        raise Go2rtcFout(NIET_BEREIKBAAR) from None
    if len(antwoord) > MAX_SDP or not antwoord.startswith(b"v=0"):
        raise Go2rtcFout("go2rtc gaf een onverwacht antwoord")
    return antwoord.decode("utf-8", "replace")
