"""Tests van de trace (ARCHITECTURE.md §10.2) – zonder broker, met een nep-bus en nep-klok."""

import json
import unittest

from brms import trace
from brms.config import Sensor
from brms.meting import maak
from brms.readers.ble import verwerk
from brms.readers.bthome import FCD2_UUID
from brms.storage import Aggregator

MAC = "A4:C1:38:00:00:01"
PVVX = bytes.fromhex("40" "00a4" "0164" "02ca09" "03bf13" "0c020c")


class NepBus:
    """Onthoudt wat er gepubliceerd wordt."""

    def __init__(self):
        self.berichten = []

    def abonneer(self, topic, qos, callback):
        pass

    def publiceer(self, topic, payload, qos=0):
        self.berichten.append((topic, json.loads(payload)))
        return True


class NepKlok:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _aan(tracer, ids, duur=600):
    tracer.bij_opdracht(trace.TOPIC_AAN, json.dumps({"sensor_ids": ids, "duur": duur}).encode())


class TestTracer(unittest.TestCase):
    def setUp(self):
        self.bus, self.klok = NepBus(), NepKlok()
        self.tracer = trace.Tracer(self.bus, "test", klok=self.klok)

    def test_standaard_uit(self):
        self.assertFalse(self.tracer.actief(MAC))
        self.assertIsNone(self.tracer.voor(MAC))

    def test_aan_en_vanzelf_verlopen(self):
        _aan(self.tracer, [MAC], duur=60)
        self.assertTrue(self.tracer.actief(MAC))
        self.klok.t += 59
        self.assertTrue(self.tracer.actief(MAC))
        self.klok.t += 2
        self.assertFalse(self.tracer.actief(MAC))

    def test_uit(self):
        _aan(self.tracer, [MAC])
        self.tracer.bij_opdracht(trace.TOPIC_UIT, json.dumps({"sensor_ids": [MAC]}).encode())
        self.assertFalse(self.tracer.actief(MAC))

    def test_maximale_duur(self):
        _aan(self.tracer, [MAC], duur=10 ** 9)
        self.klok.t += trace.MAX_DUUR + 1
        self.assertFalse(self.tracer.actief(MAC))

    def test_ongeldige_opdracht_genegeerd(self):
        for payload in [b"kapot", b'{"sensor_ids": "geen lijst"}', b"[1, 2]"]:
            self.tracer.bij_opdracht(trace.TOPIC_AAN, payload)
        self.assertFalse(self.tracer.actief(MAC))

    def test_stuur(self):
        _aan(self.tracer, [MAC])
        self.tracer.voor(MAC)("ruw", "40 00")
        topic, d = self.bus.berichten[0]
        self.assertEqual(topic, f"brms/trace/{MAC}")
        self.assertEqual((d["sensor_id"], d["dienst"], d["stap"], d["tekst"]), (MAC, "test", "ruw", "40 00"))


class TestTraceInDeKeten(unittest.TestCase):
    def test_ble_reader_stappen(self):
        bus = NepBus()
        tracer = trace.Tracer(bus, "ble-reader", klok=NepKlok())
        _aan(tracer, [MAC])
        sensoren = {MAC: Sensor(MAC, "bthome", "kajuit", {"temperatuur": -0.3})}
        verwerk(MAC, {FCD2_UUID: PVVX}, -70, 1, sensoren, tracer)
        stappen = {d["stap"]: d["tekst"] for _, d in bus.berichten}
        self.assertEqual(list(stappen), ["ruw", "gedecodeerd", "gecorrigeerd"])
        self.assertIn("40 00 a4 01 64 02 ca 09", stappen["ruw"])
        self.assertIn("ca 09 = 2506 × 0.01 = 25.06", stappen["gedecodeerd"])
        self.assertIn("25.06 − 0.3 = 24.76", stappen["gecorrigeerd"])

    def test_zonder_trace_geen_berichten(self):
        bus = NepBus()
        tracer = trace.Tracer(bus, "ble-reader", klok=NepKlok())
        sensoren = {MAC: Sensor(MAC, "bthome", "kajuit", {})}
        self.assertEqual(len(verwerk(MAC, {FCD2_UUID: PVVX}, -70, 1, sensoren, tracer)), 4)
        self.assertEqual(bus.berichten, [])

    def test_opslag_beschrijving(self):
        a = Aggregator()
        m1, m2 = maak(1, MAC, "temperatuur", 2.0), maak(2, MAC, "temperatuur", 4.0)
        a.voeg_toe(m1)
        a.voeg_toe(m2)
        self.assertIn("nu 2 waarden, gemiddelde 3", a.beschrijf(m2, False))
        w = maak(3, "M", "walstroom", 1)
        self.assertIn("VERANDERD", a.beschrijf(w, a.voeg_toe(w)))
        self.assertIn("ongewijzigd", a.beschrijf(w, a.voeg_toe(w)))
        self.assertEqual(a.aantallen(), {(MAC, "temperatuur"): 2})


if __name__ == "__main__":
    unittest.main()
