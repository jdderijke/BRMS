"""Tests van de Shelly-reader en de decoder voor de Shelly Plus Uni (ARCHITECTURE.md
§5.2.4) – zonder Shelly en zonder netwerk.

Er is hier geen Shelly. De antwoorden in deze tests zijn opgeschreven naar de documentatie
van de Shelly-API (Shelly.GetStatus van een Gen2-apparaat); het ophalen zelf is vervangen
door een functie die zo'n antwoord teruggeeft.
"""

import asyncio
import json
import unittest

from brms import simulatie
from brms.config import Correctie, Sensor
from brms.readers import shelly, shelly_uni

ID = "shelly-bilge"
BRON = {"bilge": "input:0", "bilgepomp": "voltmeter:100"}


def _sensor(**extra) -> Sensor:
    velden = {"adres": "192.168.1.50", "interval": 2.0, "bron": BRON, "drempel": 6.0, **extra}
    return Sensor(ID, "shelly-uni", "bilge", **velden)


def _status(vlotter=False, spanning=0.02, rssi=-58) -> dict:
    """Een antwoord van Shelly.GetStatus, met ook onderdelen die BRMS niet gebruikt."""
    return {"ble": {}, "cloud": {"connected": False},
            "input:0": {"id": 0, "state": vlotter}, "input:1": {"id": 1, "state": False},
            "input:2": {"id": 2, "counts": {"total": 0}},
            "switch:0": {"id": 0, "output": False},
            "sys": {"mac": "A8032ABE54DC", "uptime": 11081},
            "voltmeter:100": {"id": 100, "voltage": spanning},
            "wifi": {"sta_ip": "192.168.1.50", "status": "got ip", "ssid": "boot", "rssi": rssi}}


class NepBus:
    verbonden = True

    def __init__(self):
        self.metingen, self.berichten, self.abonnementen = [], [], []

    def publiceer_meting(self, m):
        self.metingen.append(m)
        return True

    def publiceer(self, topic, payload, qos=0):
        self.berichten.append((topic, json.loads(payload)))
        return True

    def abonneer(self, topic, qos, callback):
        self.abonnementen.append(topic)


class NepTracer:
    """Trace die altijd aanstaat en de stappen onthoudt."""

    def __init__(self):
        self.stappen = []

    def voor(self, sensor_id):
        return lambda stap, tekst, deel="": self.stappen.append((stap, tekst))


class NepKlok:
    t = 1000.0

    def __call__(self):
        return self.t


class TestDecoder(unittest.TestCase):
    def test_vlotter_en_pompspanning(self):
        self.assertEqual(shelly_uni.decodeer(_status(False, 0.02), BRON, 6.0),
                         {"bilge": 0.0, "bilgepomp": 0.0})
        self.assertEqual(shelly_uni.decodeer(_status(True, 12.61), BRON, 6.0),
                         {"bilge": 1.0, "bilgepomp": 1.0})
        # Precies op de drempel telt als "pomp krijgt spanning".
        self.assertEqual(shelly_uni.decodeer(_status(False, 6.0), BRON, 6.0)["bilgepomp"], 1.0)

    def test_omgekeerd(self):
        # Een vlotter waarvan het contact opengaat als het water stijgt.
        self.assertEqual(shelly_uni.decodeer(_status(False), {"bilge": "input:0"},
                                             omgekeerd=("bilge",)), {"bilge": 1.0})

    def test_pomp_op_een_digitale_ingang(self):
        bron = {"bilgepomp": "input:1"}
        self.assertEqual(shelly_uni.decodeer(_status(), bron), {"bilgepomp": 0.0})

    def test_wat_niet_te_bepalen_is_ontbreekt(self):
        status = _status(True, None)             # de Shelly kon de spanning niet meten
        self.assertEqual(shelly_uni.decodeer(status, BRON, 6.0), {"bilge": 1.0})
        del status["voltmeter:100"]              # voltmeter niet toegevoegd in de Shelly
        v = {v.grootheid: v for v in shelly_uni.decodeer_velden(status, BRON, 6.0)}
        self.assertEqual((v["bilgepomp"].waarde, v["bilgepomp"].uitleg),
                         (None, "ontbreekt in het antwoord"))
        # Een tellende ingang heeft geen toestand.
        self.assertEqual(shelly_uni.decodeer(_status(), {"bilge": "input:2"}), {})

    def test_velden_voor_trace(self):
        v = {v.grootheid: v for v in shelly_uni.decodeer_velden(_status(True, 12.61), BRON, 6.0)}
        self.assertEqual((v["bilgepomp"].bron, v["bilgepomp"].ruw, v["bilgepomp"].uitleg),
                         ("voltmeter:100", 12.61, "12.61 V >= drempel 6 V"))
        self.assertEqual((v["bilge"].ruw, v["bilge"].waarde), (True, 1.0))

    def test_geen_json_object(self):
        with self.assertRaises(shelly_uni.ShellyFout):
            shelly_uni.decodeer_velden(["kapot"], BRON, 6.0)

    def test_rssi(self):
        self.assertEqual(shelly_uni.rssi(_status(rssi=-58)), -58)
        self.assertIsNone(shelly_uni.rssi(_status(rssi=0)))    # geen wifi-verbinding
        self.assertIsNone(shelly_uni.rssi({"wifi": None}))
        self.assertIsNone(shelly_uni.rssi({}))

    def test_bron_in_de_configuratie(self):
        for goed in ("input:0", "input:1", "voltmeter:100"):
            self.assertTrue(shelly_uni.BRON.match(goed))
        for fout in ("switch:0", "input", "input:", "voltmeter:x", "Input:0", " input:0"):
            self.assertFalse(shelly_uni.BRON.match(fout))


class TestVerwerk(unittest.TestCase):
    def _waarden(self, sensor, status):
        return {m.grootheid: (m.waarde, m.soort) for m in shelly.verwerk(sensor, status, 1000)}

    def test_metingen(self):
        ms = shelly.verwerk(_sensor(), _status(True, 12.61), 1000)
        self.assertEqual({m.grootheid: (m.waarde, m.soort) for m in ms}, {
            "bilge": (1.0, "toestand"), "bilgepomp": (1.0, "toestand"),
            "rssi": (-58.0, "doorlopend")})
        self.assertEqual({(m.ts, m.sensor_id) for m in ms}, {(1000, ID)})

    def test_lijst_grootheden_en_correctie(self):
        sensor = _sensor(grootheden=("bilge", "rssi"), correctie={"rssi": Correctie(offset=3)})
        self.assertEqual(self._waarden(sensor, _status()),
                         {"bilge": (0.0, "toestand"), "rssi": (-55.0, "doorlopend")})

    def test_onbruikbaar_antwoord(self):
        self.assertEqual(shelly.verwerk(_sensor(), ["kapot"], 1000), [])
        self.assertEqual(shelly.verwerk(_sensor(), {}, 1000), [])   # niets bruikbaars

    def test_trace(self):
        tracer = NepTracer()
        shelly.verwerk(_sensor(), _status(False, 12.61), 1000, tracer)
        self.assertEqual([s for s, _ in tracer.stappen], ["ruw", "gedecodeerd", "gecorrigeerd"])
        ruw, gedecodeerd = tracer.stappen[0][1], tracer.stappen[1][1]
        # Alleen de gebruikte onderdelen van het antwoord, niet het hele antwoord.
        self.assertIn('"voltmeter:100": {"id": 100, "voltage": 12.61}', ruw)
        self.assertNotIn("cloud", ruw)
        self.assertIn("12.61 V >= drempel 6 V", gedecodeerd)
        self.assertIn("wifi.rssi = -58 dBm", gedecodeerd)
        tracer = NepTracer()
        shelly.verwerk(_sensor(), _status(), 1000, tracer, gesimuleerd=True)
        self.assertTrue(tracer.stappen[0][1].startswith("SIMULATIE Shelly"))


class TestPoller(unittest.TestCase):
    """Eén Shelly pollen, met een nagemaakte ophaalfunctie."""

    def setUp(self):
        self.bus, self.tracer, self.klok = NepBus(), NepTracer(), NepKlok()
        self.sensor = _sensor()
        self.simulator = simulatie.Simulator(self.bus, {ID: self.sensor}, klok=self.klok,
                                             types=simulatie.SHELLY_TYPES)
        self.antwoord = _status(False, 12.61)
        self.gevraagd = []

        def haal(adres, timeout):
            self.gevraagd.append((adres, timeout))
            if isinstance(self.antwoord, Exception):
                raise self.antwoord
            return self.antwoord

        self.poller = shelly.Poller(self.sensor, self.bus, self.tracer, self.simulator, haal)

    def _poll(self):
        return {m.grootheid: m.waarde for m in asyncio.run(self.poller.poll())}

    def test_poll_publiceert(self):
        self.assertEqual(self._poll(), {"bilge": 0.0, "bilgepomp": 1.0, "rssi": -58.0})
        self.assertEqual(len(self.bus.metingen), 3)
        # De timeout is hooguit het interval (2 s), zodat de volgende poll niet wacht.
        self.assertEqual(self.gevraagd, [("192.168.1.50", 2.0)])
        self.assertEqual(self.tracer.stappen[-1][0], "gepubliceerd")

    def test_niet_bereikbaar_publiceert_niets(self):
        self.antwoord = OSError("timed out")
        # Eén waarschuwing; daarna stil tot hij terug is.
        with self.assertLogs("brms.readers.shelly", "WARNING") as log:
            self.assertEqual(self._poll(), {})
            self.assertEqual(self._poll(), {})
        self.assertEqual(len(log.output), 1)
        self.assertIn("2 keer op rij", self.tracer.stappen[-1][1])
        self.antwoord = ValueError("geen JSON")
        self.assertEqual(self._poll(), {})
        self.assertEqual(self.bus.metingen, [])
        # Weer bereikbaar: één regel in het log en er wordt weer gepubliceerd.
        self.antwoord = _status()
        with self.assertLogs("brms.readers.shelly", "INFO") as log:
            self.assertEqual(self._poll()["bilgepomp"], 0.0)
        self.assertIn("na 3 mislukte polls", log.output[0])

    def test_simulatie_slaat_de_echte_shelly_over(self):
        opdracht = json.dumps({"sensor_id": ID, "duur": 100}).encode()
        self.simulator.bij_opdracht(simulatie.TOPIC_AAN, opdracht)
        self.klok.t += 84     # fractie 0,84: de pomp loopt en de vlotter staat aan
        self.assertEqual(self._poll(), {"bilge": 1.0, "bilgepomp": 1.0,
                                        "rssi": self.bus.metingen[-1].waarde})
        self.assertEqual(self.gevraagd, [])                       # niet gepolld (bypass)
        self.assertTrue(self.tracer.stappen[0][1].startswith("SIMULATIE"))
        # Na afloop nog één laatste stap, daarna telt de echte Shelly weer mee.
        self.klok.t += 20
        self._poll()
        self.assertEqual(self.gevraagd, [])
        self._poll()
        self.assertEqual(len(self.gevraagd), 1)

    def test_lus_pollt_en_stopt(self):
        async def proef():
            stop = asyncio.Event()
            taak = asyncio.create_task(self.poller.draai(stop))
            await asyncio.sleep(0.05)          # de eerste poll komt meteen
            eerste = len(self.gevraagd)
            self.poller.wek.set()              # poll-nu: niet wachten op het interval
            await asyncio.sleep(0.05)
            tweede = len(self.gevraagd)
            stop.set()
            self.poller.wek.set()
            await asyncio.wait_for(taak, 1)
            return eerste, tweede

        self.assertEqual(asyncio.run(proef()), (1, 2))


class TestSimulatie(unittest.TestCase):
    def test_testreeks_van_de_bilge(self):
        # Tien perioden; de pompbeurt wordt langer; de vlotter gaat aan in het laatste deel.
        t = simulatie.bilge_toestand
        self.assertEqual(t(0.0), {"bilgepomp": True, "bilge": False})
        self.assertEqual(t(0.05), {"bilgepomp": False, "bilge": False})
        self.assertEqual(t(0.52), {"bilgepomp": True, "bilge": False})    # 20 % van de periode
        self.assertEqual(t(0.56), {"bilgepomp": False, "bilge": False})
        self.assertEqual(t(0.84), {"bilgepomp": True, "bilge": True})
        self.assertEqual(t(1.0), {"bilgepomp": True, "bilge": True})
        beurten = sum(1 for i in range(1000)
                      if t(i / 1000)["bilgepomp"] and not t((i - 1) / 1000)["bilgepomp"])
        self.assertEqual(beurten, 10)

    def test_antwoord_in_het_formaat_van_de_shelly(self):
        status = simulatie.shelly_status(_sensor(), 0.84)
        self.assertEqual(status, {"wifi": {"rssi": -55},
                                  "input:0": {"id": 0, "state": True},
                                  "voltmeter:100": {"id": 100, "voltage": 12.6}})
        # De decoder maakt er de testreeks weer van, ook met 'omgekeerd' en een andere bron.
        for sensor in (_sensor(), _sensor(omgekeerd=("bilge", "bilgepomp")),
                       _sensor(bron={"bilgepomp": "input:1"}, drempel=None),
                       _sensor(drempel=20.0)):
            for fractie in (0.0, 0.05, 0.84):
                status = simulatie.shelly_status(sensor, fractie)
                verwacht = {g: float(aan) for g, aan in simulatie.bilge_toestand(fractie).items()
                            if g in sensor.bron}
                self.assertEqual(shelly_uni.decodeer(status, sensor.bron, sensor.drempel,
                                                     sensor.omgekeerd), verwacht)

    def test_opdracht_met_vrij_id(self):
        # Het ID van een Shelly wordt niet naar hoofdletters omgezet; een BLE-simulator
        # voert een opdracht voor een Shelly niet uit.
        klok = NepKlok()
        sensor = _sensor()
        opdracht = json.dumps({"sensor_id": ID, "duur": 60}).encode()
        sim = simulatie.Simulator(NepBus(), {ID: sensor}, klok=klok, types=simulatie.SHELLY_TYPES)
        sim.bij_opdracht(simulatie.TOPIC_AAN, opdracht)
        self.assertEqual(sim.stap(ID), (0.0, 0))
        klok.t += 30
        self.assertEqual(sim.stap(ID), (0.5, 1))
        sim.bij_opdracht(simulatie.TOPIC_UIT, opdracht)
        self.assertIsNone(sim.stap(ID))
        ble = simulatie.Simulator(NepBus(), {ID: sensor}, klok=klok)
        ble.bij_opdracht(simulatie.TOPIC_AAN, opdracht)
        self.assertIsNone(ble.stap(ID))


if __name__ == "__main__":
    unittest.main()
