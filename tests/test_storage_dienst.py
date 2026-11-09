"""Opslag-service als geheel, met een nep-bus (geen broker nodig)."""

import asyncio
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
        self.q = None

    def wachtrij(self, topic, qos):
        self.q = asyncio.Queue()
        return self.q

    def abonneer(self, topic, qos, callback):
        pass  # trace-opdrachten: niet gebruikt in deze test

    def publiceer(self, topic, payload, qos=0):
        return True


async def _draai(pad, berichten, klok_ok):
    cfg = Config(MqttConfig(), OpslagConfig(pad), {})
    bus, stop = NepBus(), asyncio.Event()
    with mock.patch.object(storage, "INTERVAL", 1):
        taak = asyncio.create_task(storage.draai(cfg, bus, stop, klok_ok=klok_ok))
        await asyncio.sleep(0)
        ts = int(time.time())
        for b in berichten:
            bus.q.put_nowait(("brms/meting/x", b))
        await asyncio.sleep(2.5)
        stop.set()
        await taak
    return ts


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
