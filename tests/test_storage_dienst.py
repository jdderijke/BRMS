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
from brms.config import Config, MqttConfig, OpslagConfig
from brms.meting import maak

S = "A4:C1:38:00:00:01"


class NepBus:
    def __init__(self):
        self.q = None          # de wachtrij met metingen
        self.alarmen = None    # de wachtrij met berichten van de alarmlaag

    def wachtrij(self, topic, qos):
        q = asyncio.Queue()
        if topic.startswith("brms/alarm"):
            self.alarmen = q
        else:
            self.q = q
        return q

    def abonneer(self, topic, qos, callback):
        pass  # trace-opdrachten: niet gebruikt in deze test

    def publiceer(self, topic, payload, qos=0):
        return True


async def _draai(pad, berichten, klok_ok, alarmen=()):
    cfg = Config(MqttConfig(), OpslagConfig(pad), {})
    bus, stop = NepBus(), asyncio.Event()
    with mock.patch.object(storage, "INTERVAL", 1):
        taak = asyncio.create_task(storage.draai(cfg, bus, stop, klok_ok=klok_ok))
        await asyncio.sleep(0)
        ts = int(time.time())
        for b in berichten:
            bus.q.put_nowait(("brms/meting/x", b))
        for topic, inhoud in alarmen:
            bus.alarmen.put_nowait((topic, inhoud))
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


if __name__ == "__main__":
    unittest.main()

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
