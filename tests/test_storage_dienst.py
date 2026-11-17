"""Opslag-service als geheel, met een nep-bus (geen broker nodig)."""

import asyncio
import json
import os
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

from brms import storage
from brms.config import Config, MqttConfig, OpnamenConfig, OpslagConfig
from brms.meting import maak

S = "A4:C1:38:00:00:01"


class NepBus:
    def __init__(self):
        self.q = None          # de wachtrij met metingen
        self.alarmen = None    # de wachtrij met berichten van de alarmlaag
        self.opnames = None    # de wachtrij met berichten van de camera-reader
        self.gepubliceerd = [] # de topics waarop de opslag zelf iets publiceert

    def wachtrij(self, topic, qos):
        q = asyncio.Queue()
        if topic.startswith("brms/alarm"):
            self.alarmen = q
        elif topic.startswith("brms/opname"):
            self.opnames = q
        elif topic.startswith("brms/opdracht"):
            self.opdrachten = q      # verwijderopdrachten van het web
        else:
            self.q = q
        return q

    def abonneer(self, topic, qos, callback):
        pass  # trace-opdrachten: niet gebruikt in deze test

    def publiceer(self, topic, payload, qos=0):
        self.gepubliceerd.append(topic)
        return True


class TestStandVragen(unittest.TestCase):
    """Bij zijn start vraagt de opslag de alarmlaag om de stand (§5.5.3 punt 8, v0.86)."""

    def _proef(self, bus):
        with tempfile.TemporaryDirectory() as map_:
            cfg = Config(MqttConfig(), OpslagConfig(os.path.join(map_, "brms.db")), {},
                         opnames=OpnamenConfig(os.path.join(map_, "opnames")))

            async def proef():
                stop = asyncio.Event()
                taak = asyncio.create_task(storage.draai(cfg, bus, stop, klok_ok=lambda: True))
                await asyncio.sleep(2.5)
                stop.set()
                await taak
            asyncio.run(proef())
        return [t for t in bus.gepubliceerd if t == storage.TOPIC_STAND_VRAAG]

    def test_een_keer_bij_de_start(self):
        # Eén vraag, ook al draait de lus daarna nog een paar ronden.
        self.assertEqual(len(self._proef(NepBus())), 1)

    def test_opnieuw_tot_de_broker_bereikbaar_is(self):
        # De eerste twee keer is de broker er nog niet (publiceer geeft False): de opslag
        # blijft het proberen en stopt zodra de vraag verstuurd is.
        class TrageBus(NepBus):
            def publiceer(self, topic, payload, qos=0):
                self.gepubliceerd.append(topic)
                return len(self.gepubliceerd) > 2
        self.assertEqual(len(self._proef(TrageBus())), 3)


async def _draai(pad, berichten, klok_ok, alarmen=(), opnames=()):
    cfg = Config(MqttConfig(), OpslagConfig(pad), {},
                 opnames=OpnamenConfig(os.path.join(os.path.dirname(pad), "opnames")))
    bus, stop = NepBus(), asyncio.Event()
    with mock.patch.object(storage, "INTERVAL", 1):
        taak = asyncio.create_task(storage.draai(cfg, bus, stop, klok_ok=klok_ok))
        await asyncio.sleep(0)
        ts = int(time.time())
        for b in berichten:
            bus.q.put_nowait(("brms/meting/x", b))
        for topic, inhoud in alarmen:
            bus.alarmen.put_nowait((topic, inhoud))
        for inhoud in opnames:
            bus.opnames.put_nowait(("brms/opname/dek", inhoud))
        await asyncio.sleep(2.5)
        stop.set()
        await taak
    return ts


def _alarm(ts, gebeurtenis, alarm_id="vorst", sensor_id=S, waarde=1.6):
    return (f"brms/alarm/{alarm_id}", json.dumps({
        "ts": ts, "alarm_id": alarm_id, "sensor_id": sensor_id, "grootheid": "temperatuur",
        "gebeurtenis": gebeurtenis, "waarde": waarde}).encode())


class TestAlarmen(unittest.TestCase):
    """De tabel `alarm` (§5.5.3 punt 8)."""

    def setUp(self):
        self._map = tempfile.TemporaryDirectory()
        self.db = storage.Database(os.path.join(self._map.name, "brms.db"))

    def tearDown(self):
        self.db.sluit()
        self._map.cleanup()

    def _rijen(self):
        return self.db._con.execute(
            "SELECT alarm_id, sensor_id, begin, grootheid, waarde, einde, reden FROM alarm "
            "ORDER BY begin").fetchall()

    def test_begin_en_herstel(self):
        storage.verwerk_alarm(self.db, *_alarm(1000, "begin"))
        self.assertEqual(self._rijen(), [("vorst", S, 1000, "temperatuur", 1.6, None, None)])
        storage.verwerk_alarm(self.db, *_alarm(1900, "herstel", waarde=3.2))
        # De waarde blijft die van het begin; einde en reden worden ingevuld.
        self.assertEqual(self._rijen(), [("vorst", S, 1000, "temperatuur", 1.6, 1900, "herstel")])
        # Een herstel zonder openstaand alarm doet niets.
        storage.verwerk_alarm(self.db, *_alarm(2000, "herstel"))
        self.assertEqual(len(self._rijen()), 1)

    def test_herstart_sluit_alles_wat_open_staat(self):
        storage.verwerk_alarm(self.db, *_alarm(1000, "begin"))
        storage.verwerk_alarm(self.db, *_alarm(1100, "begin", alarm_id="vocht"))
        storage.verwerk_alarm(self.db, *_alarm(1200, "herstel", alarm_id="vocht"))
        tekst = storage.verwerk_alarm(self.db, "brms/alarm/herstart", b'{"ts": 1500}')
        self.assertIn("1 openstaande alarmen afgesloten", tekst)
        self.assertEqual([(r[0], r[5], r[6]) for r in self._rijen()],
                         [("vorst", 1500, "herstart"), ("vocht", 1200, "herstel")])
        # Niets meer open: niets te melden (het bericht komt elke minuut).
        self.assertIsNone(storage.verwerk_alarm(self.db, "brms/alarm/herstart", b'{"ts": 1600}'))

    def test_stand_laat_actieve_alarmen_open(self):
        # De alarmlaag meldt elke minuut wat actief is; alleen de rest wordt afgesloten.
        storage.verwerk_alarm(self.db, *_alarm(1000, "begin"))
        storage.verwerk_alarm(self.db, *_alarm(1100, "begin", alarm_id="vocht"))
        stand = json.dumps({"ts": 1500, "actief": [["vocht", S]]}).encode()
        tekst = storage.verwerk_alarm(self.db, "brms/alarm/herstart", stand)
        self.assertIn("1 openstaande alarmen afgesloten", tekst)
        self.assertEqual([(r[0], r[5], r[6]) for r in self._rijen()],
                         [("vorst", 1500, "herstart"), ("vocht", None, None)])
        self.assertIsNone(storage.verwerk_alarm(self.db, "brms/alarm/herstart", stand))

    def test_nieuw_begin_sluit_een_vergeten_alarm(self):
        # Het einde is nooit gemeld (bijv. de broker was weg): hooguit één alarm open.
        storage.verwerk_alarm(self.db, *_alarm(1000, "begin"))
        storage.verwerk_alarm(self.db, *_alarm(5000, "begin", waarde=0.4))
        self.assertEqual([(r[2], r[5], r[6]) for r in self._rijen()],
                         [(1000, 5000, "herstart"), (5000, None, None)])

    def test_stil_heeft_geen_grootheid(self):
        storage.verwerk_alarm(self.db, "brms/alarm/stil", json.dumps({
            "ts": 1000, "alarm_id": "stil", "sensor_id": S, "grootheid": None,
            "gebeurtenis": "begin", "waarde": None}).encode())
        self.assertEqual(self._rijen(), [("stil", S, 1000, None, None, None, None)])

    def test_ongeldige_berichten(self):
        for topic, inhoud in [("brms/alarm/x", b"kapot"), ("brms/alarm/x", b"[1]"),
                              ("brms/alarm/herstart", b'{"ts": "nu"}'),
                              ("brms/alarm/herstart", b'{"ts": 1, "actief": [["vorst"]]}'),
                              _alarm(1000, "ontploft"), _alarm(1000, "begin", sensor_id=""),
                              _alarm(1000, "begin", waarde="koud"),
                              ("brms/alarm/x", b'{"ts": 1, "alarm_id": "x", "sensor_id": "s", '
                                               b'"gebeurtenis": "begin", "grootheid": "onzin"}')]:
            with self.assertRaises(ValueError):
                storage.verwerk_alarm(self.db, topic, inhoud)
        self.assertEqual(self._rijen(), [])


class TestOpslagDienst(unittest.TestCase):
    def test_schrijft_gemiddelde_en_negeert_ongeldig(self):
        with tempfile.TemporaryDirectory() as d:
            pad = os.path.join(d, "brms.db")
            berichten = [maak(1, S, "temperatuur", 2.0).naar_json(),
                         maak(1, S, "temperatuur", 4.0).naar_json(),
                         b"kapot"]
            asyncio.run(_draai(pad, berichten, klok_ok=lambda: True))
            con = sqlite3.connect(pad)
            rijen = con.execute("SELECT sensor_id, grootheid, waarde FROM meting").fetchall()
            con.close()
            self.assertEqual(rijen, [(S, "temperatuur", 3.0)])

    def test_niets_schrijven_zonder_tijdsynchronisatie(self):
        with tempfile.TemporaryDirectory() as d:
            pad = os.path.join(d, "brms.db")
            asyncio.run(_draai(pad, [maak(1, S, "vocht", 50).naar_json()], klok_ok=lambda: False))
            con = sqlite3.connect(pad)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM meting").fetchone()[0], 0)
            con.close()

    def test_alarmen_van_de_bus(self):
        with tempfile.TemporaryDirectory() as d:
            pad = os.path.join(d, "brms.db")
            asyncio.run(_draai(pad, [], klok_ok=lambda: True, alarmen=[
                _alarm(1000, "begin"), ("brms/alarm/vorst", b"kapot"), _alarm(1900, "herstel")]))
            con = sqlite3.connect(pad)
            rijen = con.execute("SELECT alarm_id, begin, einde, reden FROM alarm").fetchall()
            con.close()
            self.assertEqual(rijen, [("vorst", 1000, 1900, "herstel")])

    def test_alarmen_niet_vastgelegd_zonder_gesynchroniseerde_klok(self):
        with tempfile.TemporaryDirectory() as d:
            pad = os.path.join(d, "brms.db")
            asyncio.run(_draai(pad, [], klok_ok=lambda: False, alarmen=[_alarm(1000, "begin")]))
            con = sqlite3.connect(pad)
            aantal = con.execute("SELECT COUNT(*) FROM alarm").fetchone()[0]
            con.close()
            self.assertEqual(aantal, 0)

    def test_opnames_van_de_bus(self):
        with tempfile.TemporaryDirectory() as d:
            pad = os.path.join(d, "brms.db")
            asyncio.run(_draai(pad, [], klok_ok=lambda: True, opnames=[
                _opname(1000), b"kapot", _opname(1180, aanleiding="handmatig")]))
            con = sqlite3.connect(pad)
            rijen = con.execute("SELECT ts, camera_id, type, aanleiding FROM opname").fetchall()
            con.close()
            self.assertEqual(rijen, [(1000, "dek", "foto", "interval"),
                                     (1180, "dek", "foto", "handmatig")])


def _opname(ts, camera_id="dek", bestand=None, type_="foto", aanleiding="interval", **extra):
    """Een bericht van de camera-reader; de bestandsnaam volgt uit de tijd (UTC)."""
    if bestand is None:
        bestand = f"{camera_id}/" + time.strftime("%Y/%m/%d/%H%M%S", time.gmtime(ts)) + ".jpg"
    return json.dumps({"ts": ts, "camera_id": camera_id, "type": type_, "duur": None,
                       "bestand": bestand, "bytes": 3, "aanleiding": aanleiding, **extra}).encode()


class TestOpnames(unittest.TestCase):
    """De tabel `opname` en het opruimen van oude foto's (§5.7 punt 14)."""

    def setUp(self):
        self._map = tempfile.TemporaryDirectory()
        self.map = os.path.join(self._map.name, "opnames")
        self.db = storage.Database(os.path.join(self._map.name, "brms.db"))

    def tearDown(self):
        self.db.sluit()
        self._map.cleanup()

    def _rijen(self):
        return self.db._con.execute("SELECT ts, bestand FROM opname ORDER BY ts").fetchall()

    def _bestand(self, bestand, leeftijd=0.0, nu=10 * 86400):
        """Zet een bestand in de opnamemap, `leeftijd` seconden oud ten opzichte van `nu`."""
        pad = os.path.join(self.map, *bestand.split("/"))
        os.makedirs(os.path.dirname(pad), exist_ok=True)
        with open(pad, "wb") as f:
            f.write(b"jpg")
        os.utime(pad, (nu - leeftijd, nu - leeftijd))
        return pad

    def test_bericht_wordt_een_rij(self):
        tekst = storage.verwerk_opname(self.db, _opname(86400 + 3661))
        self.assertIn("foto van dek: dek/1970/01/02/010101.jpg", tekst)
        self.assertEqual(self.db._con.execute("SELECT * FROM opname").fetchall(), [
            (90061, "dek", "foto", None, "dek/1970/01/02/010101.jpg", 3, "interval")])

    def test_ongeldige_berichten(self):
        for inhoud in [b"kapot", b"[1]", _opname(1).replace(b'"ts": 1', b'"ts": "nu"'), _opname(1, type_="film"),
                       _opname(1, bestand="../../etc/passwd"),
                       _opname(1, bestand="/dek/1970/01/01/000001.jpg"),
                       _opname(1, bestand="dek/1970/01/01/000001.exe"),
                       _opname(1, bestand="ander/1970/01/01/000001.jpg"),   # map van een andere camera
                       _opname(1, aanleiding=""), _opname(1, bytes=-1)]:
            with self.assertRaises(ValueError):
                storage.verwerk_opname(self.db, inhoud)
        self.assertEqual(self._rijen(), [])

    def test_oude_fotos_worden_opgeruimd(self):
        dag, nu = 86400, 10 * 86400
        oud, nieuw = "dek/1970/01/02/000000.jpg", "dek/1970/01/09/000000.jpg"
        storage.verwerk_opname(self.db, _opname(1 * dag, bestand=oud))
        storage.verwerk_opname(self.db, _opname(8 * dag, bestand=nieuw))
        pad_oud = self._bestand(oud, 9 * dag)
        pad_nieuw = self._bestand(nieuw, 2 * dag)
        # Zonder rij in de tabel: een oude foto, een oud tijdelijk bestand, een oude video
        # (blijft staan) en een verse foto (blijft staan).
        wees = self._bestand("dek/1970/01/03/000000.jpg", 8 * dag)
        tijdelijk = self._bestand("dek/1970/01/03/000001.jpg.tmp", 8 * dag)
        video = self._bestand("dek/1970/01/03/000002.mp4", 8 * dag)
        vers = self._bestand("dek/1970/01/10/000000.jpg", 60)

        self.assertEqual(storage.ruim_fotos(self.db, self.map, 7 * dag, nu), 3)
        self.assertEqual(self._rijen(), [(8 * dag, nieuw)])
        self.assertEqual([os.path.exists(p) for p in (pad_oud, wees, tijdelijk)], [False] * 3)
        self.assertEqual([os.path.exists(p) for p in (pad_nieuw, video, vers)], [True] * 3)
        # De map van 2 januari is leeg geraakt en weg; die van 3 januari heeft nog de video.
        self.assertFalse(os.path.exists(os.path.dirname(pad_oud)))
        self.assertTrue(os.path.exists(os.path.dirname(video)))

    def test_oudste_videos_weg_als_de_ruimte_vol_is(self):
        dag = 86400
        namen = [f"dek/1970/01/0{n}/000000.mp4" for n in (2, 3, 4)]
        paden = []
        for n, naam in enumerate(namen, 1):
            storage.verwerk_opname(self.db, _opname(n * dag, bestand=naam, type_="video",
                                                    bytes=400, duur=120))
            paden.append(self._bestand(naam))
        foto = "dek/1970/01/02/000001.jpg"
        storage.verwerk_opname(self.db, _opname(dag + 1, bestand=foto, bytes=5000))
        self._bestand(foto)

        # 1200 bytes aan video's (de foto telt niet mee): past in 1200, niet in 1000.
        self.assertEqual(storage.ruim_videos(self.db, self.map, 1200), 0)
        with self.assertLogs("brms.storage", "INFO"):
            self.assertEqual(storage.ruim_videos(self.db, self.map, 1000), 1)
        self.assertEqual([os.path.exists(p) for p in paden], [False, True, True])
        self.assertEqual([r[1] for r in self._rijen()], [foto, namen[1], namen[2]])
        # Een video die al van schijf is: de rij gaat ook weg.
        os.remove(paden[1])
        with self.assertLogs("brms.storage", "INFO"):
            self.assertEqual(storage.ruim_videos(self.db, self.map, 400), 1)
        self.assertEqual([r[1] for r in self._rijen()], [foto, namen[2]])

    def test_verwijderen_op_verzoek_van_het_web(self):
        namen = ["dek/1970/01/02/000000.jpg", "dek/1970/01/02/000100.mp4", "dek/1970/01/02/000200.jpg"]
        tijden = [86400, 86460, 86520]
        paden = []
        for ts, naam in zip(tijden, namen):
            storage.verwerk_opname(self.db, _opname(ts, bestand=naam,
                                                    type_="video" if naam.endswith("mp4") else "foto"))
            paden.append(self._bestand(naam))
        # De eerste twee, een opname die niet bestaat en een van een andere camera.
        opdracht = json.dumps({"opnames": [["dek", 86400], ["dek", 86460], ["dek", 1], ["mast", 86520]]})
        self.assertEqual(storage.verwerk_verwijder(self.db, self.map, opdracht.encode()), 2)
        self.assertEqual([os.path.exists(p) for p in paden], [False, False, True])
        self.assertEqual(self._rijen(), [(86520, namen[2])])
        # Het bestand is al weg: de rij verdwijnt toch.
        os.remove(paden[2])
        self.assertEqual(storage.verwerk_verwijder(self.db, self.map, b'{"opnames": [["dek", 86520]]}'), 1)
        self.assertEqual(self._rijen(), [])

    def test_ongeldige_verwijderopdracht_verwijdert_niets(self):
        storage.verwerk_opname(self.db, _opname(86400))
        for inhoud in [b"kapot", b"[1]", b'{"opnames": "alles"}', b'{"opnames": [["dek"]]}',
                       b'{"opnames": [["dek", "86400"]]}', b'{"opnames": [[1, 86400]]}',
                       b'{"opnames": [["dek", 86400], ["dek", true]]}',
                       json.dumps({"opnames": [["dek", 1]] * (storage.MAX_VERWIJDER + 1)}).encode()]:
            with self.assertRaises(ValueError):
                storage.verwerk_verwijder(self.db, self.map, inhoud)
        self.assertEqual(len(self._rijen()), 1)

    def test_rij_zonder_bestand_verdwijnt_ook(self):
        storage.verwerk_opname(self.db, _opname(86400))
        os.makedirs(self.map)
        self.assertEqual(storage.ruim_fotos(self.db, self.map, 86400, 10 * 86400), 0)
        self.assertEqual(self._rijen(), [])


if __name__ == "__main__":
    unittest.main()
