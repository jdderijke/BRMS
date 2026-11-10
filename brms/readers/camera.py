"""Camera-reader: haalt foto's op van de IP-camera's (ARCHITECTURE.md §5.7).

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
camera's. Video (type "video") volgt in een latere bouwstap.

Een mislukte foto (camera onbereikbaar, antwoord is geen JPEG) geeft een waarschuwing in
het log en geen bericht op de bus; de volgende intervalfoto komt vanzelf. Zolang de klok
niet gesynchroniseerd is, worden er geen opnames gemaakt (§4.2): de tijd staat in de
bestandsnaam en in de database.

Het wachtwoord van de camera is een geheim (§5.7 punt 8): het staat in het adres waarmee
de foto wordt opgehaald, en komt daarom nooit in het log of in een foutmelding. Een adres
wordt alleen getoond als schema, host en poort.

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
                 tijd=time.time):
        """haal, klok_ok en tijd zijn te vervangen in tests."""
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

        # Zonder gesynchroniseerde klok geen opname (§4.2): de tijd staat in de naam.
        if not await asyncio.to_thread(self._klok_ok):
            if not self._klok_gemeld:
                log.warning("klok niet gesynchroniseerd; %s (%s) maakt nog geen opnames",
                            camera.naam, camera.id)
                self._klok_gemeld = True
            return None
        self._klok_gemeld = False

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

    async def draai(self, stop: asyncio.Event) -> None:
        """Maakt foto's tot `stop` gezet wordt: elke `foto_interval` seconden, en direct
        bij een opdracht van poll-nu."""
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
                log.warning("video van %s (%s) gevraagd (%s), maar video is nog niet gebouwd",
                            self.camera.naam, self.camera.id, aanleiding)
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
                       for c in cameras.values()) or "geen", config.opnames.map)
    taken = [asyncio.create_task(o.draai(stop)) for o in opnemers.values()]
    await stop.wait()
    # De opnemers wachten op hun volgende foto; wekken, zodat ze het stopsein zien.
    for o in opnemers.values():
        o.opdrachten.put_nowait((None, ""))
    await asyncio.gather(*taken)
    log.info("camera-reader gestopt")
