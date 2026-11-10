"""Tests van de camera-reader (ARCHITECTURE.md §5.7) – zonder camera: het ophalen van de
foto is vervangen. Er gaat in deze tests niets het netwerk op."""

import asyncio
import io
import json
import os
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
            with self.assertLogs("brms.readers.camera", "WARNING") as log:
                self.opnemer.opdrachten.put_nowait((camera.VIDEO, "alarm:beweging"))
                await asyncio.sleep(0.1)
            self.assertIn("nog niet gebouwd", log.output[0])
            stop.set()
            self.opnemer.opdrachten.put_nowait((None, ""))
            await asyncio.wait_for(taak, 2)

        asyncio.run(proef())
        self.assertEqual([(b[1]["ts"], b[1]["aanleiding"]) for b in self.bus.berichten],
                         [(TS, "interval"), (TS + 5, "handmatig")])


class TestOpdracht(unittest.TestCase):
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
