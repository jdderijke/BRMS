"""Camera-reader: maakt foto's en video's van de IP-camera's (ARCHITECTURE.md §5.7).

Werking per camera uit de configuratie (blok [[camera]]):

1. Elke `foto_interval` (standaard 3 minuten) haalt de reader één foto op van het
   snapshot-adres van de camera: één HTTP-aanvraag die een JPEG teruggeeft.
2. De foto wordt als bestand in de opnamemap gezet:
       <map>/<camera_id>/JJJJ/MM/DD/UUMMSS.jpg      (tijd in UTC)
   eerst onder een tijdelijke naam en dan hernoemd, zodat er nooit een half bestand staat.
3. Daarna publiceert de reader de gegevens van de opname (geen beeld) op de bus:
       brms/opname/<camera_id>   {"ts", "camera_id", "type", "duur", "bestand", "bytes",
                                  "aanleiding"}
   De opslag-service legt die vast in de tabel `opname` en ruimt oude foto's op.

Poll-nu (§5.7 punt 12): de opdracht

    brms/opdracht/camera/poll-nu   {"camera_id": "dek", "type": "foto", "aanleiding": "handmatig"}

laat de reader direct een foto maken, los van het interval. Zonder camera_id: alle
camera's.

Video (§5.7 punt 11): alleen op verzoek, met poll-nu en type "video". De reader start dan
het programma ffmpeg, dat de RTSP-stroom van de camera `video_duur` seconden lang
wegschrijft naar een MP4-bestand, zonder het beeld om te rekenen (dat belast de Pi
nauwelijks) en zonder geluid. Het bestand komt naast de foto's, met dezelfde naamgeving en
`.mp4`. Tijdens een video gaan de intervalfoto's gewoon door; een tweede opdracht voor een
video tijdens een lopende video wordt genegeerd (§5.7 punt 3).

Een mislukte opname (camera onbereikbaar, antwoord is geen JPEG, ffmpeg geeft een fout)
geeft een waarschuwing in het log en geen bericht op de bus; er blijft geen bestand
achter. De volgende intervalfoto komt vanzelf. Zolang de klok
niet gesynchroniseerd is, worden er geen opnames gemaakt (§4.2): de tijd staat in de
bestandsnaam en in de database.

Het wachtwoord van de camera is een geheim (§5.7 punt 8): het staat in het adres waarmee
de foto wordt opgehaald, en komt daarom nooit in het log of in een foutmelding. Een adres
wordt alleen getoond als schema, host en poort. Bij een video staat het adres met
wachtwoord in de opdrachtregel van ffmpeg; dat is zolang de opname loopt zichtbaar voor
wie op de Pi is ingelogd.

Het ophalen gebeurt met urllib uit de standaardbibliotheek (geen extra afhankelijkheid,
P3), in een thread: urllib blokkeert, en de asyncio-loop moet door kunnen lopen.
"""

import asyncio
import http.client
import json
import logging
import os
import time
import urllib.parse
import urllib.request

from brms.config import PLAATS_GEBRUIKER, PLAATS_WACHTWOORD, Camera, Config

log = logging.getLogger(__name__)

TOPIC_OPNAME = "brms/opname"
TOPIC_POLL_NU = "brms/opdracht/camera/poll-nu"

FOTO, VIDEO = "foto", "video"
# Aanleidingen van een opname (kolom `aanleiding` in de tabel opname, §5.3.1).
INTERVAL, HANDMATIG = "interval", "handmatig"

# Langste wachttijd op een foto, in seconden (§5.7 punt 10).
TIMEOUT = 10.0

# Een foto is enkele honderden kB; meer dan dit lezen we niet (vangnet tegen een adres
# waarop iets heel anders antwoordt, bijvoorbeeld een videostroom).
MAX_FOTO = 20 * 1024 * 1024

# Het programma dat de video opneemt (§5.7 punt 9); via apt geïnstalleerd.
FFMPEG = ("ffmpeg",)

# Zoveel seconden na de duur van de video mag ffmpeg nog bezig zijn (verbinden met de
# camera, het bestand afsluiten); daarna wordt het gestopt (§5.7 punt 11).
VIDEO_MARGE = 30.0

# Elk JPEG-bestand begint met deze drie bytes.
_JPEG = b"\xff\xd8\xff"

# Een aanleiding komt van de bus en gaat de database in: korte tekst, beperkte tekens.
_MAX_AANLEIDING = 60


class CameraFout(Exception):
    """Een opname is mislukt. De tekst is bedoeld voor het log en bevat geen geheim."""


def vul_in(adres: str, camera: Camera) -> str:
    """Zet het account van de camera op de invulplaatsen in een adres (§5.7 punt 7).

    Naam en wachtwoord worden gecodeerd voor gebruik in een adres: een wachtwoord met
    tekens als '&', '@' of '/' zou het adres anders breken.
    """
    if camera.gebruiker is None:
        return adres
    return (adres.replace(PLAATS_GEBRUIKER, urllib.parse.quote(camera.gebruiker, safe=""))
                 .replace(PLAATS_WACHTWOORD, urllib.parse.quote(camera.wachtwoord, safe="")))


def toon(adres: str) -> str:
    """Een adres voor het log: alleen schema, host en poort. Pad en account blijven weg,
    want daarin kan het wachtwoord staan (ook als het letterlijk in de configuratie staat)."""
    try:
        d = urllib.parse.urlsplit(adres)
        return f"{d.scheme}://{d.hostname}" + (f":{d.port}" if d.port else "")
    except ValueError:
        return "(ongeldig adres)"


def schoon(tekst: str, camera: Camera) -> str:
    """Haalt het wachtwoord uit een tekst (bijvoorbeeld een foutmelding van urllib waarin
    het adres staat), zowel letterlijk als gecodeerd."""
    if camera.wachtwoord:
        for geheim in {camera.wachtwoord, urllib.parse.quote(camera.wachtwoord, safe="")}:
            tekst = tekst.replace(geheim, "***")
    return tekst


def haal_foto(adres: str, timeout: float = TIMEOUT) -> bytes:
    """Haalt één foto op van een snapshot-adres en geeft de bytes van de JPEG.

    Blokkeert tot het antwoord er is (of de timeout); de reader voert dit in een thread
    uit. OSError of http.client.HTTPException als de camera niet (goed) antwoordt;
    ValueError als het antwoord geen JPEG is.
    """
    with urllib.request.urlopen(adres, timeout=timeout) as r:
        data = r.read(MAX_FOTO + 1)
    if len(data) > MAX_FOTO:
        raise ValueError(f"antwoord is groter dan {MAX_FOTO // (1024 * 1024)} MB; geen foto")
    if not data.startswith(_JPEG):
        # Bijvoorbeeld een foutmelding van de camera (verkeerd wachtwoord) of een webpagina.
        # Het begin van het antwoord helpt bij het zoeken naar de oorzaak.
        begin = data[:80].decode("ascii", "replace").replace("\n", " ").replace("\r", "")
        raise ValueError(f"antwoord is geen JPEG; het begint met: {begin!r}")
    return data


def bestandsnaam(camera_id: str, ts: int, extensie: str) -> str:
    """Het pad van een opname binnen de opnamemap (§5.7 punt 10), met '/' als scheiding:
    zo staat het ook in de database. De tijd is UTC."""
    return f"{camera_id}/" + time.strftime("%Y/%m/%d/%H%M%S", time.gmtime(ts)) + f".{extensie}"


def bewaar(map_: str, bestand: str, data: bytes) -> None:
    """Schrijft een opname naar <map_>/<bestand>.

    Eerst onder een tijdelijke naam en dan hernoemen: valt de stroom weg tijdens het
    schrijven, dan staat er geen half bestand onder de echte naam. OSError als het niet
    lukt (schijf vol, geen schrijfrecht).
    """
    pad = os.path.join(map_, *bestand.split("/"))
    os.makedirs(os.path.dirname(pad), exist_ok=True)
    tijdelijk = pad + ".tmp"
    try:
        with open(tijdelijk, "wb") as f:
            f.write(data)
        os.replace(tijdelijk, pad)
    except OSError:
        # Geen half bestand achterlaten.
        try:
            os.remove(tijdelijk)
        except OSError:
            pass
        raise


def ffmpeg_argumenten(adres: str, duur: int, uit: str) -> list[str]:
    """De argumenten waarmee ffmpeg één video opneemt (§5.7 punt 11).

    -nostdin, -loglevel error  geen vragen, alleen fouten melden
    -rtsp_transport tcp        de stroom over TCP: geen verloren pakketten (blokken in beeld)
    -t <duur>                  na zoveel seconden beeld vanzelf stoppen
    -an                        geen geluid: het geluid van een camera past meestal niet in
                               een MP4 zonder omrekenen
    -c:v copy                  het beeld kopiëren zoals de camera het levert (H.264)
    -f mp4                     MP4, ook al eindigt de tijdelijke naam niet op .mp4
    -y                         een bestaand bestand overschrijven (een rest van eerder)
    """
    return ["-nostdin", "-hide_banner", "-loglevel", "error", "-rtsp_transport", "tcp",
            "-i", adres, "-t", str(duur), "-an", "-c:v", "copy", "-f", "mp4", "-y", uit]


def opnamebericht(ts: int, camera_id: str, type_: str, duur: int | None, bestand: str,
                  bytes_: int, aanleiding: str) -> str:
    """Het bericht voor brms/opname/<camera_id> (§3.3), als JSON."""
    return json.dumps({"ts": ts, "camera_id": camera_id, "type": type_, "duur": duur,
                       "bestand": bestand, "bytes": bytes_, "aanleiding": aanleiding},
                      separators=(",", ":"))


def _klok_ok() -> bool:
    """Of de systeemklok gesynchroniseerd is (§4.2); dezelfde controle als de opslag."""
    from brms.storage import klok_gesynchroniseerd
    return klok_gesynchroniseerd()


class Opnemer:
    """De opnames van één camera: ophalen, bewaren en melden op de bus.

    Wordt alleen in de asyncio-loop gebruikt; een lock is niet nodig.
    """

    def __init__(self, camera: Camera, map_: str, bus, haal=haal_foto, klok_ok=_klok_ok,
                 tijd=time.time, ffmpeg=FFMPEG):
        """haal, klok_ok, tijd en ffmpeg (het programma, als lijst) zijn te vervangen in
        tests."""
        self._ffmpeg = tuple(ffmpeg)
        # De taak van de lopende video, of None.
        self._videotaak: asyncio.Task | None = None
        self.camera = camera
        self._map = map_
        self._bus = bus
        self._haal = haal
        self._klok_ok = klok_ok
        self._tijd = tijd
        # Aantal mislukte intervalfoto's op rij. Een onbereikbare camera faalt elk interval
        # opnieuw; alleen de eerste keer komt er een waarschuwing, en één regel bij herstel.
        self._mislukt = 0
        self._klok_gemeld = False
        # Opdrachten van poll-nu die nog uitgevoerd moeten worden: (type, aanleiding).
        self.opdrachten: asyncio.Queue = asyncio.Queue()

    async def foto(self, aanleiding: str = INTERVAL) -> str | None:
        """Maakt één foto. Geeft het pad binnen de opnamemap terug, of None als het niet
        gelukt is (dan staat de reden in het log)."""
        camera = self.camera

        if not await self._klok_goed():
            return None

        try:
            # urllib blokkeert; in een thread, zodat de andere camera's doorlopen.
            data = await asyncio.to_thread(self._haal, vul_in(camera.foto_url, camera), TIMEOUT)
            # De tijd van de opname is "nu", na het ophalen.
            ts = int(self._tijd())
            bestand = bestandsnaam(camera.id, ts, "jpg")
            await asyncio.to_thread(bewaar, self._map, bestand, data)
        except (OSError, ValueError, http.client.HTTPException) as e:
            # Een foto op verzoek (poll-nu, alarm) mislukt zelden stil: altijd melden.
            if self._mislukt == 0 or aanleiding != INTERVAL:
                log.warning("foto van %s (%s) op %s mislukt (%s): %s", camera.naam, camera.id,
                            toon(camera.foto_url), aanleiding, schoon(str(e), camera))
            self._mislukt += 1
            return None
        if self._mislukt:
            log.info("%s (%s) levert weer foto's, na %d mislukte", camera.naam, camera.id,
                     self._mislukt)
            self._mislukt = 0

        # Is de broker onbereikbaar, dan staat het bestand er wel maar komt het niet in de
        # database; de opslag ruimt het na de bewaartermijn toch op (§5.7 punt 14).
        bericht = opnamebericht(ts, camera.id, FOTO, None, bestand, len(data), aanleiding)
        if not self._bus.publiceer(f"{TOPIC_OPNAME}/{camera.id}", bericht, qos=1):
            log.warning("broker niet bereikbaar: foto %s niet gemeld aan de opslag", bestand)
        log.debug("foto %s (%d bytes, %s)", bestand, len(data), aanleiding)
        return bestand

    async def _klok_goed(self) -> bool:
        """Zonder gesynchroniseerde klok geen opname (§4.2): de tijd staat in de naam.
        Meldt het één keer in het log."""
        if await asyncio.to_thread(self._klok_ok):
            self._klok_gemeld = False
            return True
        if not self._klok_gemeld:
            log.warning("klok niet gesynchroniseerd; %s (%s) maakt nog geen opnames",
                        self.camera.naam, self.camera.id)
            self._klok_gemeld = True
        return False

    async def video(self, aanleiding: str = HANDMATIG) -> str | None:
        """Neemt één video op van `video_duur` seconden (§5.7 punt 11). Geeft het pad
        binnen de opnamemap terug, of None als het niet gelukt is (dan staat de reden in
        het log). Duurt zo lang als de video."""
        camera = self.camera
        if camera.video_url is None:
            log.warning("video van %s (%s) gevraagd (%s), maar de camera heeft geen 'video_url'",
                        camera.naam, camera.id, aanleiding)
            return None
        if not await self._klok_goed():
            return None

        # De tijd van een video is het begin van de opname (§5.3.1).
        ts = int(self._tijd())
        duur = int(round(camera.video_duur))
        bestand = bestandsnaam(camera.id, ts, "mp4")
        pad = os.path.join(self._map, *bestand.split("/"))
        # ffmpeg schrijft onder een tijdelijke naam; pas een geslaagde video krijgt de
        # echte naam.
        tijdelijk = pad + ".tmp"
        adres = vul_in(camera.video_url, camera)
        log.info("video van %s (%s) gestart: %d s (%s)", camera.naam, camera.id, duur, aanleiding)
        proces = None
        try:
            os.makedirs(os.path.dirname(pad), exist_ok=True)
            try:
                proces = await asyncio.create_subprocess_exec(
                    *self._ffmpeg, *ffmpeg_argumenten(adres, duur, tijdelijk),
                    stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE)
            except FileNotFoundError:
                raise CameraFout("ffmpeg is niet geïnstalleerd (sudo apt install ffmpeg)") from None
            try:
                _, fout = await asyncio.wait_for(proces.communicate(), duur + VIDEO_MARGE)
            except asyncio.TimeoutError:
                raise CameraFout(f"ffmpeg was {VIDEO_MARGE:g} s na de duur nog niet klaar "
                                 f"en is gestopt") from None
            if proces.returncode != 0:
                # De laatste regel van ffmpeg zegt meestal wat er mis is. Het adres (met
                # het wachtwoord) kan erin staan; dat gaat eruit.
                regels = fout.decode("utf-8", "replace").strip().splitlines()
                tekst = (regels[-1] if regels else "geen melding").replace(adres, toon(adres))
                raise CameraFout(f"ffmpeg gaf fout {proces.returncode}: {schoon(tekst, camera)}")
            grootte = os.path.getsize(tijdelijk) if os.path.exists(tijdelijk) else 0
            if grootte == 0:
                raise CameraFout("ffmpeg heeft geen beeld weggeschreven")
            os.replace(tijdelijk, pad)
        except (CameraFout, OSError) as e:
            log.warning("video van %s (%s) op %s mislukt (%s): %s", camera.naam, camera.id,
                        toon(camera.video_url), aanleiding, schoon(str(e), camera))
            return None
        finally:
            # Ook als de service stopt terwijl de video loopt (de taak wordt dan
            # afgebroken): ffmpeg stoppen en geen half bestand achterlaten (§5.7 punt 11).
            if proces is not None and proces.returncode is None:
                proces.kill()
                await proces.wait()
            try:
                os.remove(tijdelijk)
            except OSError:
                pass

        bericht = opnamebericht(ts, camera.id, VIDEO, duur, bestand, grootte, aanleiding)
        if not self._bus.publiceer(f"{TOPIC_OPNAME}/{camera.id}", bericht, qos=1):
            log.warning("broker niet bereikbaar: video %s niet gemeld aan de opslag", bestand)
        log.info("video %s klaar (%.1f MB)", bestand, grootte / 1e6)
        return bestand

    def _start_video(self, aanleiding: str) -> None:
        """Start een video als eigen taak, zodat de intervalfoto's doorgaan. Loopt er al
        een, dan wordt de opdracht genegeerd (§5.7 punt 3)."""
        if self._videotaak is not None and not self._videotaak.done():
            log.info("video van %s (%s) gevraagd (%s), maar er loopt al een video; genegeerd",
                     self.camera.naam, self.camera.id, aanleiding)
            return

        async def veilig():
            try:
                await self.video(aanleiding)
            except Exception:
                log.exception("fout bij video van %s (%s)", self.camera.naam, self.camera.id)

        self._videotaak = asyncio.create_task(veilig())

    async def draai(self, stop: asyncio.Event) -> None:
        """Maakt opnames tot `stop` gezet wordt: een foto elke `foto_interval` seconden, en
        direct een foto of video bij een opdracht van poll-nu."""
        try:
            await self._lus(stop)
        finally:
            # Een lopende video afbreken; video() ruimt dan zelf op.
            if self._videotaak is not None and not self._videotaak.done():
                self._videotaak.cancel()
                try:
                    await self._videotaak
                except asyncio.CancelledError:
                    pass
                log.info("lopende video van %s (%s) afgebroken", self.camera.naam, self.camera.id)

    async def _lus(self, stop: asyncio.Event) -> None:
        interval = self.camera.foto_interval
        # Vaste tijdstippen op de monotone klok (§4.2), zodat het interval niet oploopt met
        # de duur van een opname.
        volgende = time.monotonic()
        while not stop.is_set():
            nu = time.monotonic()
            if nu >= volgende:
                await self._veilig(INTERVAL)
                volgende += interval
                if volgende < time.monotonic():
                    # Een opname duurde langer dan het interval: niet inhalen.
                    volgende = time.monotonic()
                continue
            # Wachten tot het volgende interval, of tot er een opdracht komt.
            try:
                type_, aanleiding = await asyncio.wait_for(self.opdrachten.get(), volgende - nu)
            except asyncio.TimeoutError:
                continue
            if type_ is None:
                continue        # gewekt om te stoppen
            if type_ == VIDEO:
                self._start_video(aanleiding)
                continue
            await self._veilig(aanleiding)

    async def _veilig(self, aanleiding: str) -> None:
        """Eén foto; een onverwachte fout mag de lus (en de andere camera's) niet stoppen."""
        try:
            await self.foto(aanleiding)
        except Exception:
            log.exception("fout bij foto van %s (%s)", self.camera.naam, self.camera.id)


def lees_opdracht(payload: bytes) -> tuple[str | None, str, str]:
    """Leest een poll-nu-opdracht: (camera_id of None voor alle, type, aanleiding).
    ValueError bij een ongeldige opdracht. Een lege opdracht is een foto van alle camera's."""
    try:
        d = json.loads(payload) if payload.strip() else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError(f"geen geldige JSON: {e}") from None
    if not isinstance(d, dict):
        raise ValueError("opdracht is geen JSON-object")
    camera_id, type_, aanleiding = d.get("camera_id"), d.get("type", FOTO), d.get("aanleiding", HANDMATIG)
    if camera_id is not None and not isinstance(camera_id, str):
        raise ValueError("camera_id moet een tekst zijn")
    if type_ not in (FOTO, VIDEO):
        raise ValueError(f"type moet '{FOTO}' of '{VIDEO}' zijn, niet {type_!r}")
    if not isinstance(aanleiding, str) or not aanleiding or len(aanleiding) > _MAX_AANLEIDING:
        raise ValueError(f"aanleiding moet een tekst van hooguit {_MAX_AANLEIDING} tekens zijn")
    return camera_id, type_, aanleiding


async def draai(config: Config, bus, stop: asyncio.Event) -> None:
    """Maakt opnames van alle camera's tot `stop` gezet wordt (bij SIGTERM van systemd)."""
    cameras = config.cameras
    if not cameras:
        # Geen fout: de service draait dan zonder iets te doen, zodat `brms start` en
        # `brms update` voor alle services gelijk werken.
        log.warning("geen camera's geconfigureerd")

    opnemers = {c.id: Opnemer(c, config.opnames.map, bus) for c in cameras.values()}

    # Poll-nu (§5.7 punt 12). De opdracht komt binnen in paho's netwerkthread; in de
    # wachtrij zetten moet in de asyncio-loop gebeuren.
    loop = asyncio.get_running_loop()

    def bij_poll_nu(topic: str, payload: bytes) -> None:
        try:
            camera_id, type_, aanleiding = lees_opdracht(payload)
        except ValueError as e:
            log.warning("ongeldige opdracht op %s: %s", topic, e)
            return
        if camera_id is not None and camera_id not in opnemers:
            log.warning("opdracht op %s voor onbekende camera %r genegeerd", topic, camera_id)
            return
        for o in opnemers.values():
            if camera_id is None or camera_id == o.camera.id:
                loop.call_soon_threadsafe(o.opdrachten.put_nowait, (type_, aanleiding))

    bus.abonneer(TOPIC_POLL_NU, 1, bij_poll_nu)

    log.info("camera-reader gestart; %d camera's: %s; opnamemap %s", len(cameras),
             ", ".join(f"{c.naam} ({c.id}) op {toon(c.foto_url)} elke {c.foto_interval:g} s"
                       + (f", video {c.video_duur:g} s" if c.video_url else ", geen video")
                       for c in cameras.values()) or "geen", config.opnames.map)
    taken = [asyncio.create_task(o.draai(stop)) for o in opnemers.values()]
    await stop.wait()
    # De opnemers wachten op hun volgende foto; wekken, zodat ze het stopsein zien.
    for o in opnemers.values():
        o.opdrachten.put_nowait((None, ""))
    await asyncio.gather(*taken)
    log.info("camera-reader gestopt")
