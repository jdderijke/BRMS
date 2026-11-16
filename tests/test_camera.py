"""Tests van de camera-reader (ARCHITECTURE.md §5.7) – zonder camera: het ophalen van de
foto is vervangen. Er gaat in deze tests niets het netwerk op."""

import asyncio
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

from brms.config import Camera
from brms.readers import camera
from brms.readers.camera import Opnemer

JPEG = b"\xff\xd8\xff\xe0" + b"beeld" * 10
WACHTWOORD = "GEH&IM/w@chtwoord"
ADRES = "http://192.168.1.20:89/cgi-bin/CGIProxy.fcgi?cmd=snapPicture2&usr={gebruiker}&pwd={wachtwoord}"
DEK = Camera("dek", "Dek", ADRES, None, "brms", WACHTWOORD, 180.0)
# 10 november 2026 20:30:00 UTC.
TS = 1794342600

# Een nagebootste ffmpeg: een Python-script dat doet wat het pad van het camera-adres zegt.
# Het uitvoerbestand is het laatste argument, het adres staat na -i (zoals bij ffmpeg).
NEP_FFMPEG = """
import sys, time
adres, uit = sys.argv[sys.argv.index("-i") + 1], sys.argv[-1]
if adres == "rtsp://127.0.0.1:8554/stuk":
    # De stroom van go2rtc voor de camera "stuk": go2rtc krijgt geen beeld.
    sys.stderr.write("rtsp://127.0.0.1:8554/stuk: Server returned 404 Not Found\\n")
    sys.exit(1)
elif adres.startswith("rtsp://127.0.0.1:8554/"):
    # De stroom van go2rtc: een video van een andere grootte dan rechtstreeks.
    open(uit, "wb").write(b"g2r" * 500)
elif adres.endswith("/goed"):
    open(uit, "wb").write(b"mp4" * 1000)
elif adres.endswith("/leeg"):
    open(uit, "wb").close()
elif adres.endswith("/fout"):
    open(uit, "wb").write(b"half")
    sys.stderr.write("eerste regel\\n" + adres + ": Connection refused\\n")
    sys.exit(1)
elif adres.endswith("/hangt"):
    open(uit, "wb").write(b"half")
    time.sleep(30)
"""


def _camera(pad="goed", duur=5.0):
    """Een camera met een videostroom; het pad stuurt de nagebootste ffmpeg."""
    return Camera("dek", "Dek", ADRES, "rtsp://{gebruiker}:{wachtwoord}@192.168.1.20:89/" + pad,
                  "brms", WACHTWOORD, 180.0, duur)


class NepBus:
    verbonden = True

    def __init__(self):
        self.berichten = []
        self.abonnementen = {}

    def publiceer(self, topic, payload, qos=0):
        if not self.verbonden:
            return False
        self.berichten.append((topic, json.loads(payload), qos))
        return True

    def abonneer(self, topic, qos, callback):
        self.abonnementen[topic] = callback


class _Antwoord(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


class TestAdres(unittest.TestCase):
    def test_account_wordt_ingevuld_en_gecodeerd(self):
        self.assertEqual(camera.vul_in(ADRES, DEK),
                         "http://192.168.1.20:89/cgi-bin/CGIProxy.fcgi?cmd=snapPicture2"
                         "&usr=brms&pwd=GEH%26IM%2Fw%40chtwoord")
        zonder = Camera("dek", "Dek", "http://camera/snap.jpg")
        self.assertEqual(camera.vul_in(zonder.foto_url, zonder), "http://camera/snap.jpg")

    def test_tonen_zonder_pad_en_account(self):
        self.assertEqual(camera.toon(ADRES), "http://192.168.1.20:89")
        self.assertEqual(camera.toon("rtsp://brms:geheim@192.168.1.20:89/videoMain"),
                         "rtsp://192.168.1.20:89")
        self.assertEqual(camera.toon("http://camera/snap.jpg?pwd=geheim"), "http://camera")

    def test_wachtwoord_uit_een_tekst(self):
        tekst = f"fout bij {camera.vul_in(ADRES, DEK)} en {WACHTWOORD}"
        schoon = camera.schoon(tekst, DEK)
        self.assertNotIn(WACHTWOORD, schoon)
        self.assertNotIn("GEH%26IM", schoon)

    def test_geheimen_niet_in_repr(self):
        self.assertNotIn(WACHTWOORD, repr(DEK))
        self.assertNotIn("CGIProxy", repr(DEK))     # ook het adres niet


class TestHaalFoto(unittest.TestCase):
    def test_jpeg(self):
        with mock.patch("urllib.request.urlopen", return_value=_Antwoord(JPEG)) as u:
            self.assertEqual(camera.haal_foto("http://camera/snap.jpg", 3.0), JPEG)
        self.assertEqual(u.call_args.kwargs["timeout"], 3.0)

    def test_geen_jpeg(self):
        # Zo antwoordt een Foscam op een verkeerd wachtwoord: geen fout, maar XML.
        xml = b"<CGI_Result>\n<result>-2</result>\n</CGI_Result>"
        with mock.patch("urllib.request.urlopen", return_value=_Antwoord(xml)):
            with self.assertRaises(ValueError) as ctx:
                camera.haal_foto("http://camera/snap.jpg")
        self.assertIn("geen JPEG", str(ctx.exception))
        self.assertIn("<CGI_Result>", str(ctx.exception))

    def test_te_groot(self):
        with mock.patch.object(camera, "MAX_FOTO", 10), \
                mock.patch("urllib.request.urlopen", return_value=_Antwoord(JPEG)):
            with self.assertRaises(ValueError):
                camera.haal_foto("http://camera/snap.jpg")


class TestBestand(unittest.TestCase):
    def test_naam_in_utc(self):
        self.assertEqual(camera.bestandsnaam("dek", TS, "jpg"), "dek/2026/11/10/203000.jpg")

    def test_bewaren_zonder_tijdelijk_bestand(self):
        with tempfile.TemporaryDirectory() as d:
            camera.bewaar(d, "dek/2026/11/10/203000.jpg", JPEG)
            map_ = os.path.join(d, "dek", "2026", "11", "10")
            self.assertEqual(os.listdir(map_), ["203000.jpg"])
            with open(os.path.join(map_, "203000.jpg"), "rb") as f:
                self.assertEqual(f.read(), JPEG)


class TestOpnemer(unittest.TestCase):
    def setUp(self):
        self._map = tempfile.TemporaryDirectory()
        self.map = self._map.name
        self.bus = NepBus()
        self.gevraagd = []        # de adressen waarmee een foto is opgehaald
        self.antwoord = JPEG      # of een uitzondering
        self.klok_ok = True
        self.t = float(TS)

        def haal(adres, timeout):
            self.gevraagd.append(adres)
            if isinstance(self.antwoord, Exception):
                raise self.antwoord
            return self.antwoord

        self.opnemer = Opnemer(DEK, self.map, self.bus, haal=haal,
                               klok_ok=lambda: self.klok_ok, tijd=lambda: self.t)

    def tearDown(self):
        self._map.cleanup()

    def _foto(self, aanleiding=camera.INTERVAL):
        return asyncio.run(self.opnemer.foto(aanleiding))

    def test_foto_op_schijf_en_op_de_bus(self):
        self.assertEqual(self._foto(), "dek/2026/11/10/203000.jpg")
        self.assertEqual(self.gevraagd, [camera.vul_in(ADRES, DEK)])
        with open(os.path.join(self.map, "dek", "2026", "11", "10", "203000.jpg"), "rb") as f:
            self.assertEqual(f.read(), JPEG)
        self.assertEqual(self.bus.berichten, [("brms/opname/dek", {
            "ts": TS, "camera_id": "dek", "type": "foto", "duur": None,
            "bestand": "dek/2026/11/10/203000.jpg", "bytes": len(JPEG),
            "aanleiding": "interval"}, 1)])

    def test_bericht_past_bij_de_opslag(self):
        # Wat de reader publiceert, moet de opslag aannemen.
        from brms import storage
        self._foto("alarm:vorst")
        db = storage.Database(os.path.join(self.map, "brms.db"))
        try:
            storage.verwerk_opname(db, json.dumps(self.bus.berichten[0][1]).encode())
            self.assertEqual(db._con.execute("SELECT bestand, aanleiding FROM opname").fetchall(),
                             [("dek/2026/11/10/203000.jpg", "alarm:vorst")])
        finally:
            db.sluit()

    def test_mislukt_zonder_bestand_bericht_of_wachtwoord_in_het_log(self):
        self.antwoord = urllib.error.URLError(f"geen verbinding met {camera.vul_in(ADRES, DEK)}")
        with self.assertLogs("brms.readers.camera", "WARNING") as log:
            self.assertIsNone(self._foto())
            self.assertIsNone(self._foto())       # tweede keer op rij: geen nieuwe regel
        self.assertEqual(len(log.output), 1)
        self.assertIn("http://192.168.1.20:89", log.output[0])
        self.assertNotIn("GEH", log.output[0])
        self.assertEqual(self.bus.berichten, [])
        self.assertEqual(os.listdir(self.map), [])
        # Een foto op verzoek die mislukt, wordt wel altijd gemeld.
        with self.assertLogs("brms.readers.camera", "WARNING"):
            self._foto(camera.HANDMATIG)
        # Weer bereikbaar: één regel in het log.
        self.antwoord = JPEG
        with self.assertLogs("brms.readers.camera", "INFO") as log:
            self.assertIsNotNone(self._foto())
        self.assertIn("na 3 mislukte", log.output[0])

    def test_geen_jpeg_is_mislukt(self):
        self.antwoord = ValueError("antwoord is geen JPEG; het begint met: '<html>'")
        with self.assertLogs("brms.readers.camera", "WARNING"):
            self.assertIsNone(self._foto())
        self.assertEqual(self.bus.berichten, [])

    def test_geen_opname_zonder_gesynchroniseerde_klok(self):
        self.klok_ok = False
        with self.assertLogs("brms.readers.camera", "WARNING") as log:
            self.assertIsNone(self._foto())
            self.assertIsNone(self._foto())
        self.assertEqual(len(log.output), 1)
        self.assertEqual(self.gevraagd, [])

    def test_broker_weg_foto_staat_er_wel(self):
        self.bus.verbonden = False
        with self.assertLogs("brms.readers.camera", "WARNING"):
            self.assertEqual(self._foto(), "dek/2026/11/10/203000.jpg")
        self.assertTrue(os.path.exists(os.path.join(self.map, "dek", "2026", "11", "10", "203000.jpg")))

    def test_lus_interval_poll_nu_en_stoppen(self):
        async def proef():
            stop = asyncio.Event()
            taak = asyncio.create_task(self.opnemer.draai(stop))
            await asyncio.sleep(0.2)              # de eerste intervalfoto komt meteen
            self.t += 5
            self.opnemer.opdrachten.put_nowait((camera.FOTO, "handmatig"))
            await asyncio.sleep(0.2)
            # Deze camera heeft geen videostroom: een video kan niet.
            with self.assertLogs("brms.readers.camera", "WARNING") as log:
                self.opnemer.opdrachten.put_nowait((camera.VIDEO, "alarm:beweging"))
                await asyncio.sleep(0.3)
            self.assertIn("geen 'video_url'", log.output[0])
            stop.set()
            self.opnemer.opdrachten.put_nowait((None, ""))
            await asyncio.wait_for(taak, 2)

        asyncio.run(proef())
        self.assertEqual([(b[1]["ts"], b[1]["aanleiding"]) for b in self.bus.berichten],
                         [(TS, "interval"), (TS + 5, "handmatig")])


class TestVideo(unittest.TestCase):
    """Video's, met een nagebootste ffmpeg (§5.7 punt 11)."""

    def setUp(self):
        self._map = tempfile.TemporaryDirectory()
        self.map = os.path.join(self._map.name, "opnames")
        self.script = os.path.join(self._map.name, "nep_ffmpeg.py")
        with open(self.script, "w") as f:
            f.write(NEP_FFMPEG)
        self.bus = NepBus()
        self.t = float(TS)

    def tearDown(self):
        self._map.cleanup()

    def _opnemer(self, cam, ffmpeg=None, go2rtc_draait=False):
        return Opnemer(cam, self.map, self.bus, haal=lambda adres, timeout: JPEG,
                       klok_ok=lambda: True, tijd=lambda: self.t,
                       ffmpeg=ffmpeg or (sys.executable, self.script),
                       via_go2rtc=lambda: go2rtc_draait)

    def _bestanden(self):
        return sorted(b for _, _, bestanden in os.walk(self.map) for b in bestanden)

    def test_argumenten(self):
        a = camera.ffmpeg_argumenten("rtsp://camera/videoMain", 120, "/data/x.mp4.tmp")
        self.assertEqual(a[a.index("-i") + 1], "rtsp://camera/videoMain")
        self.assertEqual(a[a.index("-t") + 1], "120")
        self.assertEqual(a[a.index("-c:v") + 1], "copy")       # niet omrekenen
        self.assertIn("-an", a)                                 # geen geluid
        self.assertEqual(a[a.index("-rtsp_transport") + 1], "tcp")
        self.assertEqual(a[-1], "/data/x.mp4.tmp")

    def test_video_op_schijf_en_op_de_bus(self):
        with self.assertLogs("brms.readers.camera", "INFO"):
            bestand = asyncio.run(self._opnemer(_camera("goed")).video("alarm:beweging"))
        self.assertEqual(bestand, "dek/2026/11/10/203000.mp4")
        self.assertEqual(self._bestanden(), ["203000.mp4"])     # geen tijdelijk bestand
        self.assertEqual(self.bus.berichten, [("brms/opname/dek", {
            "ts": TS, "camera_id": "dek", "type": "video", "duur": 5,
            "bestand": "dek/2026/11/10/203000.mp4", "bytes": 3000,
            "aanleiding": "alarm:beweging"}, 1)])

    def test_via_go2rtc_als_dat_draait(self):
        # Draait go2rtc, dan komt de stroom daarvandaan (§5.7 punt 18): één verbinding met
        # de camera voor kijken en opnemen samen. In het adres staat dan geen wachtwoord.
        with self.assertLogs("brms.readers.camera", "INFO") as log:
            bestand = asyncio.run(self._opnemer(_camera("goed"), go2rtc_draait=True).video())
        self.assertEqual(bestand, "dek/2026/11/10/203000.mp4")
        self.assertEqual(self.bus.berichten[0][1]["bytes"], 1500)        # de stroom van go2rtc
        self.assertIn("via go2rtc", log.output[0])

    def test_zonder_go2rtc_rechtstreeks(self):
        with self.assertLogs("brms.readers.camera", "INFO") as log:
            asyncio.run(self._opnemer(_camera("goed")).video())
        self.assertEqual(self.bus.berichten[0][1]["bytes"], 3000)        # de camera zelf
        self.assertIn("rechtstreeks van de camera", log.output[0])

    def test_mislukt_via_go2rtc_dan_rechtstreeks(self):
        # go2rtc draait, maar geeft geen beeld: de opname begint opnieuw, rechtstreeks van
        # de camera. Een video bij een alarm mag niet afhangen van go2rtc.
        stuk = Camera("stuk", "Dek", ADRES, "rtsp://{gebruiker}:{wachtwoord}@192.168.1.20:89/goed",
                      "brms", WACHTWOORD, 180.0, 5.0)
        with self.assertLogs("brms.readers.camera", "INFO") as log:
            bestand = asyncio.run(self._opnemer(stuk, go2rtc_draait=True).video("alarm:beweging"))
        tekst = "\n".join(log.output)
        self.assertIn("via go2rtc mislukt (alarm:beweging): ffmpeg gaf fout 1", tekst)
        self.assertIn("de opname begint opnieuw, rechtstreeks van de camera", tekst)
        self.assertEqual(bestand, "stuk/2026/11/10/203000.mp4")
        self.assertEqual(self._bestanden(), ["203000.mp4"])               # geen rest van de eerste poging
        self.assertEqual([b[1]["bytes"] for b in self.bus.berichten], [3000])
        self.assertNotIn("GEH", tekst)

    def test_mislukt_via_go2rtc_en_rechtstreeks(self):
        stuk = Camera("stuk", "Dek", ADRES, "rtsp://{gebruiker}:{wachtwoord}@192.168.1.20:89/fout",
                      "brms", WACHTWOORD, 180.0, 5.0)
        with self.assertLogs("brms.readers.camera", "WARNING") as log:
            self.assertIsNone(asyncio.run(self._opnemer(stuk, go2rtc_draait=True).video()))
        self.assertEqual(len(log.output), 2)                              # beide pogingen gemeld
        self.assertIn("op rtsp://192.168.1.20:89 mislukt", log.output[1])
        self.assertEqual((self._bestanden(), self.bus.berichten), ([], []))

    def test_bericht_past_bij_de_opslag(self):
        from brms import storage
        with self.assertLogs("brms.readers.camera", "INFO"):
            asyncio.run(self._opnemer(_camera("goed")).video())
        db = storage.Database(os.path.join(self._map.name, "brms.db"))
        try:
            storage.verwerk_opname(db, json.dumps(self.bus.berichten[0][1]).encode())
            self.assertEqual(db.videos(), [("dek", TS, "dek/2026/11/10/203000.mp4", 3000)])
        finally:
            db.sluit()

    def test_fout_van_ffmpeg_zonder_bestand_of_wachtwoord(self):
        with self.assertLogs("brms.readers.camera", "WARNING") as log:
            self.assertIsNone(asyncio.run(self._opnemer(_camera("fout")).video()))
        regel = log.output[-1]
        self.assertIn("ffmpeg gaf fout 1", regel)
        self.assertIn("Connection refused", regel)
        self.assertIn("rtsp://192.168.1.20:89", regel)
        self.assertNotIn("GEH", regel)
        self.assertEqual((self._bestanden(), self.bus.berichten), ([], []))

    def test_leeg_bestand_is_mislukt(self):
        with self.assertLogs("brms.readers.camera", "WARNING") as log:
            self.assertIsNone(asyncio.run(self._opnemer(_camera("leeg")).video()))
        self.assertIn("geen beeld", log.output[-1])
        self.assertEqual(self._bestanden(), [])

    def test_ffmpeg_die_blijft_hangen_wordt_gestopt(self):
        with mock.patch.object(camera, "VIDEO_MARGE", 0.5), \
                self.assertLogs("brms.readers.camera", "WARNING") as log:
            self.assertIsNone(asyncio.run(self._opnemer(_camera("hangt", duur=1.0)).video()))
        self.assertIn("nog niet klaar", log.output[-1])
        self.assertEqual((self._bestanden(), self.bus.berichten), ([], []))

    def test_ffmpeg_ontbreekt(self):
        opnemer = self._opnemer(_camera("goed"), ffmpeg=("bestaat-niet-brms-ffmpeg",))
        with self.assertLogs("brms.readers.camera", "WARNING") as log:
            self.assertIsNone(asyncio.run(opnemer.video()))
        self.assertIn("niet geïnstalleerd", log.output[-1])

    def test_tweede_video_genegeerd_fotos_gaan_door_en_stoppen_breekt_af(self):
        opnemer = self._opnemer(_camera("hangt", duur=20.0))

        async def proef():
            stop = asyncio.Event()
            taak = asyncio.create_task(opnemer.draai(stop))
            await asyncio.sleep(0.2)                      # de eerste intervalfoto
            opnemer.opdrachten.put_nowait((camera.VIDEO, "alarm:beweging"))
            await asyncio.sleep(1.0)                      # de video loopt (ffmpeg hangt)
            opnemer.opdrachten.put_nowait((camera.VIDEO, "handmatig"))
            self.t += 7
            opnemer.opdrachten.put_nowait((camera.FOTO, "handmatig"))
            await asyncio.sleep(0.5)
            stop.set()
            opnemer.opdrachten.put_nowait((None, ""))
            await asyncio.wait_for(taak, 5)

        with self.assertLogs("brms.readers.camera", "INFO") as log:
            asyncio.run(proef())
        tekst = "\n".join(log.output)
        self.assertIn("er loopt al een video; genegeerd", tekst)
        self.assertIn("lopende video van Dek (dek) afgebroken", tekst)
        # Twee foto's, geen video; het halve bestand van de afgebroken video is weg.
        self.assertEqual([b[1]["type"] for b in self.bus.berichten], ["foto", "foto"])
        self.assertEqual(self._bestanden(), ["203000.jpg", "203007.jpg"])


class TestRichten(unittest.TestCase):
    """Richten van een camera die kan bewegen (§5.7 punt 19): per klik één stap. De camera
    is vervangen: `stuur` onthoudt de adressen en geeft het antwoord van een Foscam."""

    GOED = b"<CGI_Result>\n    <result>0</result>\n</CGI_Result>"

    def setUp(self):
        # Een tijdelijke opnamemap: de lus van de opnemer maakt bij zijn start een foto.
        self._map = tempfile.TemporaryDirectory()
        self.addCleanup(self._map.cleanup)
        self.bus = NepBus()
        self.gestuurd = []          # de adressen van de commando's, in volgorde
        # Het antwoord per commando (de naam na "cmd="); standaard "gelukt". Een
        # uitzondering wordt opgegooid; een lijst geeft per aanroep het volgende antwoord.
        self.antwoorden = {}

    def _stuur(self, adres, timeout):
        self.gestuurd.append(adres)
        commando = adres.split("cmd=")[1].split("&")[0]
        antwoord = self.antwoorden.get(commando, self.GOED)
        if isinstance(antwoord, list):
            antwoord = antwoord.pop(0)
        if isinstance(antwoord, Exception):
            raise antwoord
        return antwoord

    def _camera(self, stap=0.1, richten="foscam"):
        return Camera("dek", "Dek", ADRES, "rtsp://{gebruiker}:{wachtwoord}@192.168.1.20:89/videoMain",
                      "brms", WACHTWOORD, 180.0, 60.0, richten, stap)

    def _opnemer(self, cam=None):
        return Opnemer(cam or self._camera(), self._map.name, self.bus,
                       haal=lambda adres, timeout: JPEG, klok_ok=lambda: True,
                       stuur=self._stuur)

    def _commandos(self):
        """De namen van de verstuurde commando's, zonder de rest van het adres."""
        return [a.split("cmd=")[1].split("&")[0] for a in self.gestuurd]

    def test_elk_merk_uit_de_configuratie_heeft_commandos(self):
        # Wat check-config goedkeurt als 'richten', moet de reader ook kunnen uitvoeren.
        from brms import config
        self.assertEqual(set(camera._MERKEN), set(config.RICHT_MERKEN))

    def test_adres_van_een_foscam(self):
        # Schema, host en poort van het snapshot-adres; het account gecodeerd erachter.
        cam = self._camera()
        self.assertEqual(camera.foscam_adres(cam, "op"),
                         "http://192.168.1.20:89/cgi-bin/CGIProxy.fcgi?cmd=ptzMoveUp"
                         "&usr=brms&pwd=GEH%26IM%2Fw%40chtwoord")
        self.assertEqual([camera.foscam_adres(cam, r).split("cmd=")[1].split("&")[0]
                          for r in ("neer", "links", "rechts", None)],
                         ["ptzMoveDown", "ptzMoveLeft", "ptzMoveRight", "ptzStopRun"])
        # Een camera zonder account: geen usr en pwd in het adres.
        zonder = Camera("dek", "Dek", "https://camera.local/snap.jpg", "rtsp://camera/v",
                        richten="foscam")
        self.assertEqual(camera.foscam_adres(zonder, None),
                         "https://camera.local/cgi-bin/CGIProxy.fcgi?cmd=ptzStopRun")

    def test_antwoord_van_een_foscam(self):
        camera.foscam_controleer(self.GOED)          # result 0: geen fout
        for antwoord, verwacht in [
                (b"<CGI_Result><result>-2</result></CGI_Result>", "naam of wachtwoord"),
                (b"<CGI_Result><result>-3</result></CGI_Result>", "operator"),
                (b"<CGI_Result><result>-1</result></CGI_Result>", "result -1"),
                (b"<html>404 Not Found</html>", "onverwacht antwoord")]:
            with self.assertRaises(ValueError) as ctx:
                camera.foscam_controleer(antwoord)
            self.assertIn(verwacht, str(ctx.exception))

    def test_een_stap_is_bewegen_wachten_en_stoppen(self):
        import time
        opnemer = self._opnemer(self._camera(stap=0.2))
        begin = time.monotonic()
        self.assertTrue(asyncio.run(opnemer.richt("links")))
        self.assertEqual(self._commandos(), ["ptzMoveLeft", "ptzStopRun"])
        # Tussen bewegen en stoppen zit de stap.
        self.assertGreaterEqual(time.monotonic() - begin, 0.2)

    def test_bewegen_mislukt_dan_geen_stop_en_geen_wachtwoord_in_het_log(self):
        self.antwoorden["ptzMoveUp"] = urllib.error.URLError(
            f"geen verbinding met {camera.foscam_adres(self._camera(), 'op')}")
        with self.assertLogs("brms.readers.camera", "WARNING") as log:
            self.assertFalse(asyncio.run(self._opnemer().richt("op")))
        self.assertEqual(self._commandos(), ["ptzMoveUp"])
        self.assertIn("richten van Dek (dek) naar op mislukt op http://192.168.1.20:89", log.output[0])
        self.assertNotIn("GEH", log.output[0])

    def test_afgewezen_door_de_camera(self):
        # Een account met alleen kijkrechten: de camera antwoordt wel, maar beweegt niet.
        self.antwoorden["ptzMoveUp"] = b"<CGI_Result><result>-3</result></CGI_Result>"
        with self.assertLogs("brms.readers.camera", "WARNING") as log:
            self.assertFalse(asyncio.run(self._opnemer().richt("op")))
        self.assertIn("afgewezen (result -3)", log.output[0])
        self.assertEqual(self._commandos(), ["ptzMoveUp"])

    def test_stoppen_wordt_een_keer_opnieuw_geprobeerd(self):
        # De eerste stop mislukt, de tweede lukt: geen waarschuwing, de camera staat stil.
        self.antwoorden["ptzStopRun"] = [OSError("timed out"), self.GOED]
        self.assertTrue(asyncio.run(self._opnemer().richt("rechts")))
        self.assertEqual(self._commandos(), ["ptzMoveRight", "ptzStopRun", "ptzStopRun"])
        # Beide mislukt: dat is het melden waard, want de camera loopt dan door.
        self.gestuurd.clear()
        self.antwoorden["ptzStopRun"] = [OSError("timed out"), OSError("timed out")]
        with self.assertLogs("brms.readers.camera", "WARNING") as log:
            self.assertFalse(asyncio.run(self._opnemer().richt("rechts")))
        self.assertEqual(self._commandos(), ["ptzMoveRight", "ptzStopRun", "ptzStopRun"])
        self.assertIn("loopt mogelijk door", log.output[0])

    def test_opdracht_tijdens_een_stap_genegeerd_en_stoppen_maakt_de_stap_af(self):
        opnemer = self._opnemer(self._camera(stap=0.5))

        async def proef():
            stop = asyncio.Event()
            taak = asyncio.create_task(opnemer.draai(stop))
            await asyncio.sleep(0.1)
            opnemer.start_richten("op")
            await asyncio.sleep(0.1)                 # de stap loopt
            opnemer.start_richten("neer")            # genegeerd
            # De service stopt midden in de stap: de camera krijgt toch zijn stopcommando.
            stop.set()
            opnemer.opdrachten.put_nowait((None, ""))
            await asyncio.wait_for(taak, 5)

        asyncio.run(proef())
        self.assertEqual(self._commandos(), ["ptzMoveUp", "ptzStopRun"])

    def test_vaste_camera_wordt_niet_gericht(self):
        opnemer = self._opnemer(self._camera(richten=None))

        async def proef():
            with self.assertLogs("brms.readers.camera", "WARNING") as log:
                opnemer.start_richten("op")
                await asyncio.sleep(0.1)
            self.assertIn("geen 'richten'", log.output[0])

        asyncio.run(proef())
        self.assertEqual(self.gestuurd, [])

    def test_opdracht_van_de_bus(self):
        # De hele weg in de reader: de opdracht van de bus wordt een stap van de camera.
        from brms.config import Config, MqttConfig, OpnamenConfig, OpslagConfig
        cam = self._camera(stap=0.1)
        cfg = Config(MqttConfig(), OpslagConfig("x"), {}, cameras={"dek": cam},
                     opnames=OpnamenConfig(tempfile.gettempdir()))

        def urlopen(adres, timeout):
            # De nagebootste camera: het antwoord op een richtcommando.
            return _Antwoord(self._stuur(adres, timeout))

        async def proef():
            stop = asyncio.Event()
            # De klok "niet gesynchroniseerd": dan maakt de reader geen foto's (§4.2) en
            # gaat er in deze test alleen het richtcommando naar de camera. Richten zelf
            # heeft de klok niet nodig.
            with mock.patch("urllib.request.urlopen", urlopen), \
                    mock.patch("brms.storage.klok_gesynchroniseerd", return_value=False), \
                    self.assertLogs("brms.readers.camera", "WARNING") as log:
                taak = asyncio.create_task(camera.draai(cfg, self.bus, stop))
                await asyncio.sleep(0.1)
                bij_richt = self.bus.abonnementen["brms/opdracht/camera/richt"]
                bij_richt("brms/opdracht/camera/richt", b'{"camera_id": "dek", "richting": "neer"}')
                # Ongeldig of voor een onbekende camera: een waarschuwing, geen commando.
                bij_richt("brms/opdracht/camera/richt", b'{"camera_id": "dek", "richting": "schuin"}')
                bij_richt("brms/opdracht/camera/richt", b'{"camera_id": "mast", "richting": "op"}')
                await asyncio.sleep(0.6)
                stop.set()
                await asyncio.wait_for(taak, 5)
            return "\n".join(log.output)

        tekst = asyncio.run(proef())
        self.assertEqual(self._commandos(), ["ptzMoveDown", "ptzStopRun"])
        self.assertIn("richting moet een van op, neer, links, rechts zijn", tekst)
        self.assertIn("onbekende camera 'mast'", tekst)


class TestOpdracht(unittest.TestCase):
    def test_richtopdracht_lezen(self):
        self.assertEqual(camera.lees_richtopdracht(b'{"camera_id": "dek", "richting": "links"}'),
                         ("dek", "links"))
        # De camera is verplicht (anders dan bij poll-nu), en de richting een van de vier.
        for inhoud in [b"", b"kapot", b"[1]", b'{"richting": "op"}', b'{"camera_id": 3, "richting": "op"}',
                       b'{"camera_id": "dek"}', b'{"camera_id": "dek", "richting": "schuin"}']:
            with self.assertRaises(ValueError):
                camera.lees_richtopdracht(inhoud)

    def test_lezen(self):
        self.assertEqual(camera.lees_opdracht(b""), (None, "foto", "handmatig"))
        self.assertEqual(camera.lees_opdracht(
            b'{"camera_id": "dek", "type": "video", "aanleiding": "alarm:beweging"}'),
            ("dek", "video", "alarm:beweging"))

    def test_ongeldig(self):
        for inhoud in [b"kapot", b"[1]", b'{"type": "film"}', b'{"camera_id": 3}',
                       b'{"aanleiding": ""}', json.dumps({"aanleiding": "x" * 61}).encode()]:
            with self.assertRaises(ValueError):
                camera.lees_opdracht(inhoud)


if __name__ == "__main__":
    unittest.main()
