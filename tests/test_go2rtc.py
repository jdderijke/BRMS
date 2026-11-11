"""Tests van brms/go2rtc.py (ARCHITECTURE.md §5.7 punt 18) – zonder go2rtc: de aanvragen
aan go2rtc zijn vervangen. Er gaat in deze tests niets het netwerk op."""

import io
import json
import os
import socket
import tempfile
import unittest
import urllib.error
from unittest import mock

from brms import config, go2rtc

WACHTWOORD = "GEH&IM: w@cht#woord"


def _config(*cameras) -> config.Config:
    return config.Config(config.MqttConfig(), config.OpslagConfig("x"), {},
                         cameras={c.id: c for c in cameras})


DEK = config.Camera("dek", "Dek", "http://c/snap", "rtsp://{gebruiker}:{wachtwoord}@192.168.1.20:88/videoMain",
                    "brms", WACHTWOORD)
MAST = config.Camera("mast", "Mast", "http://c2/snap")          # zonder videostroom


class _Antwoord(io.BytesIO):
    """Het antwoord van urlopen: te gebruiken met 'with'."""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


class TestInstellingen(unittest.TestCase):
    def test_stroom_per_camera_met_video(self):
        tekst = go2rtc.instellingen(_config(DEK, MAST))
        # Het account is ingevuld en gecodeerd voor gebruik in een adres; de waarde staat
        # tussen aanhalingstekens, zodat ':' en '#' de YAML niet breken.
        self.assertIn('  "dek": "rtsp://brms:GEH%26IM%3A%20w%40cht%23woord@192.168.1.20:88/videoMain"\n', tekst)
        # Een camera zonder videostroom heeft geen live-beeld.
        self.assertNotIn("mast", tekst)

    def test_beheer_en_rtsp_alleen_op_de_pi_zelf(self):
        tekst = go2rtc.instellingen(_config(DEK))
        self.assertIn('api:\n  listen: "127.0.0.1:1984"\n', tekst)
        self.assertIn('rtsp:\n  listen: "127.0.0.1:8554"\n', tekst)
        # Alleen de video naar de browser staat open, zonder hulp van een server op internet.
        self.assertIn('webrtc:\n  listen: ":8555"\n  ice_servers: []\n', tekst)

    def test_zonder_camera_met_video(self):
        self.assertTrue(go2rtc.instellingen(_config(MAST)).endswith("streams: {}\n"))
        self.assertTrue(go2rtc.instellingen(_config()).endswith("streams: {}\n"))

    def test_bestand_alleen_voor_de_eigenaar(self):
        with tempfile.TemporaryDirectory() as map_:
            pad = os.path.join(map_, "data", "go2rtc.yaml")
            go2rtc.schrijf_instellingen(_config(DEK), pad)
            go2rtc.schrijf_instellingen(_config(DEK), pad)     # overschrijven mag
            with open(pad, encoding="utf-8") as f:
                self.assertEqual(f.read(), go2rtc.instellingen(_config(DEK)))
            self.assertEqual(os.listdir(os.path.dirname(pad)), ["go2rtc.yaml"])   # geen .tmp
            if os.name == "posix":                             # Windows kent deze rechten niet
                self.assertEqual(os.stat(pad).st_mode & 0o777, 0o600)


class TestStart(unittest.TestCase):
    def test_programma_ontbreekt(self):
        with mock.patch.object(go2rtc, "PROGRAMMA", os.path.join("bestaat", "niet", "go2rtc")), \
                self.assertLogs("brms.go2rtc", "ERROR") as log:
            self.assertEqual(go2rtc.start(_config(DEK)), 1)
        self.assertIn("brms update", log.output[0])

    def test_schrijft_instellingen_en_start_het_programma(self):
        with tempfile.TemporaryDirectory() as map_:
            programma = os.path.join(map_, "go2rtc")
            open(programma, "w").close()
            pad = os.path.join(map_, "go2rtc.yaml")
            with mock.patch.object(go2rtc, "PROGRAMMA", programma), \
                    mock.patch.object(go2rtc, "INSTELLINGEN", pad), \
                    mock.patch.object(go2rtc, "_wacht_op_tailscale") as wacht, \
                    mock.patch("os.execv") as execv, \
                    self.assertLogs("brms.go2rtc", "INFO") as log:
                go2rtc.start(_config(DEK, MAST))
            execv.assert_called_once_with(programma, [programma, "-config", pad])
            wacht.assert_called_once()
            self.assertTrue(os.path.isfile(pad))
            self.assertIn("1 camera's: dek", log.output[0])
            self.assertNotIn("GEH", "\n".join(log.output))

    def test_wachten_op_tailscale(self):
        # Geen Tailscale op deze machine: niet wachten.
        with mock.patch("shutil.which", return_value=None), mock.patch("subprocess.run") as run:
            go2rtc._wacht_op_tailscale()
        run.assert_not_called()
        # Tailscale heeft een adres: meteen door.
        klaar = mock.Mock(returncode=0, stdout="100.64.0.1\n")
        with mock.patch("shutil.which", return_value="/usr/bin/tailscale"), \
                mock.patch("subprocess.run", return_value=klaar) as run:
            go2rtc._wacht_op_tailscale()
        run.assert_called_once()
        # Tailscale komt niet: na de wachttijd toch starten, met een waarschuwing.
        nog_niet = mock.Mock(returncode=1, stdout="")
        with mock.patch("shutil.which", return_value="/usr/bin/tailscale"), \
                mock.patch("subprocess.run", return_value=nog_niet), \
                mock.patch("time.sleep"), self.assertLogs("brms.go2rtc", "WARNING") as log:
            go2rtc._wacht_op_tailscale(hooguit=0)
        self.assertIn("alleen op het lokale netwerk", log.output[0])


class TestAdresEnPoort(unittest.TestCase):
    def test_rtsp_adres(self):
        self.assertEqual(go2rtc.rtsp_adres("dek"), "rtsp://127.0.0.1:8554/dek")

    def test_bereikbaar(self):
        # Een poort waar iets luistert, en daarna dezelfde poort zonder luisteraar.
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            s.listen(1)
            poort = s.getsockname()[1]
            with mock.patch.object(go2rtc, "POORT_RTSP", poort):
                self.assertTrue(go2rtc.bereikbaar())
        with mock.patch.object(go2rtc, "POORT_RTSP", poort):
            self.assertFalse(go2rtc.bereikbaar(timeout=0.5))


class TestApi(unittest.TestCase):
    def test_kijkers_zijn_alleen_de_afnemers_via_webrtc(self):
        stromen = {"dek": {"producers": [{"url": "rtsp://..."}],
                           "consumers": [{"format_name": "webrtc/whep"}, {"format_name": "rtsp"},
                                         {"format_name": "webrtc/json"}]},
                   "mast": {"producers": [], "consumers": None}}
        with mock.patch("urllib.request.urlopen",
                        side_effect=lambda *a, **k: _Antwoord(json.dumps(stromen).encode())) as u:
            self.assertEqual(go2rtc.kijkers("dek"), 2)     # de opname (rtsp) telt niet mee
            self.assertEqual(go2rtc.kijkers("mast"), 0)
            self.assertEqual(go2rtc.kijkers("boeg"), 0)    # go2rtc kent deze camera niet
        self.assertEqual(u.call_args.args[0], "http://127.0.0.1:1984/api/streams")

    def test_kijkers_zonder_go2rtc(self):
        with mock.patch("urllib.request.urlopen", side_effect=ConnectionRefusedError("geweigerd")):
            with self.assertRaises(go2rtc.Go2rtcFout):
                go2rtc.kijkers("dek")

    def test_webrtc_geeft_aanbod_door_en_antwoord_terug(self):
        with mock.patch("urllib.request.urlopen", return_value=_Antwoord(b"v=0\r\no=antwoord\r\n")) as u:
            self.assertEqual(go2rtc.webrtc("dek", "v=0\r\no=aanbod\r\n"), "v=0\r\no=antwoord\r\n")
        vraag = u.call_args.args[0]
        self.assertEqual((vraag.full_url, vraag.get_method(), vraag.data),
                         ("http://127.0.0.1:1984/api/webrtc?src=dek", "POST", b"v=0\r\no=aanbod\r\n"))

    def test_webrtc_fouten(self):
        geen_beeld = urllib.error.HTTPError("http://x", 500, "fout", {}, io.BytesIO(b""))
        for fout, verwacht in [(geen_beeld, "geen beeld van de camera (fout 500)"),
                               (urllib.error.URLError(ConnectionRefusedError("geweigerd")), "niet bereikbaar"),
                               (TimeoutError("timed out"), "niet bereikbaar")]:
            with mock.patch("urllib.request.urlopen", side_effect=fout):
                with self.assertRaises(go2rtc.Go2rtcFout) as ctx:
                    go2rtc.webrtc("dek", "v=0\r\n")
            self.assertIn(verwacht, str(ctx.exception))
        # Iets anders dan een WebRTC-antwoord.
        with mock.patch("urllib.request.urlopen", return_value=_Antwoord(b"<html>")):
            with self.assertRaises(go2rtc.Go2rtcFout):
                go2rtc.webrtc("dek", "v=0\r\n")


if __name__ == "__main__":
    unittest.main()
