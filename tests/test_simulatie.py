"""Tests van de simulatie (ARCHITECTURE.md §10.3) – zonder broker en zonder Bluetooth,
met een nep-bus en nep-klok."""

import json
import unittest

from brms import simulatie, trace
from brms.config import Sensor
from brms.readers import bthome, ruuvi
from brms.readers.ble import Ontvanger

MAC = "A4:C1:38:00:00:01"
RMAC = "CB:B8:33:4C:88:4F"
SENSOREN = {MAC: Sensor(MAC, "bthome", "kajuit"), RMAC: Sensor(RMAC, "ruuvi", "motorruim")}

# Een echte advertentie van de Xiaomi: 25,06 °C (zie tests/test_bthome.py).
PVVX = bytes.fromhex("40" "00a4" "0164" "02ca09" "03bf13" "0c020c")


class NepBus:
    """Onthoudt wat er gepubliceerd wordt."""

    def __init__(self):
        self.metingen = []
        self.berichten = []

    def abonneer(self, topic, qos, callback):
        pass

    def publiceer(self, topic, payload, qos=0):
        self.berichten.append((topic, json.loads(payload)))
        return True

    def publiceer_meting(self, m):
        self.metingen.append(m)
        return True


class NepKlok:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _opdracht(sim, topic, sensor_id, **extra):
    sim.bij_opdracht(topic, json.dumps({"sensor_id": sensor_id, **extra}).encode())


class TestTestreeks(unittest.TestCase):
    def test_begin_en_einde(self):
        self.assertEqual(simulatie.waarden(0.0)["temperatuur"], 25.0)
        self.assertEqual(simulatie.waarden(1.0)["temperatuur"], -5.0)
        self.assertEqual(simulatie.waarden(0.5)["vocht"], 67.5)

    def test_bthome_bytes_door_de_echte_decoder(self):
        # Wat de simulator maakt, moet de echte decoder precies teruggeven.
        for fractie, verwacht in [
                (0.0, {"temperatuur": 25.0, "vocht": 40.0, "batterijspanning": 3.0}),
                (1.0, {"temperatuur": -5.0, "vocht": 95.0, "batterijspanning": 2.2})]:
            data = simulatie.bthome_bytes(simulatie.waarden(fractie), 7)
            self.assertEqual(bthome.decodeer(data), verwacht)

    def test_ruuvi_bytes_door_de_echte_decoder(self):
        data = simulatie.ruuvi_bytes(simulatie.waarden(1.0), 300, RMAC)
        self.assertEqual(len(data), 24)
        self.assertEqual(ruuvi.decodeer(data), {
            "temperatuur": -5.0, "vocht": 95.0, "luchtdruk": 980.0,
            "versnelling_x": 200, "versnelling_y": -100, "versnelling_z": 975,
            "batterijspanning": 2.2, ruuvi.TELLER: 300 % 255})
        self.assertEqual(data[18:].hex(":").upper(), RMAC)

    def test_advertentie_per_sensortype(self):
        sd, fd = simulatie.advertentie(SENSOREN[MAC], 0.0, 0)
        self.assertEqual((list(sd), fd), ([bthome.FCD2_UUID], {}))
        sd, fd = simulatie.advertentie(SENSOREN[RMAC], 0.0, 0)
        self.assertEqual((sd, list(fd)), ({}, [ruuvi.FABRIKANT_ID]))


class TestSimulator(unittest.TestCase):
    def setUp(self):
        self.klok = NepKlok()
        self.sim = simulatie.Simulator(NepBus(), SENSOREN, klok=self.klok)

    def test_standaard_uit(self):
        self.assertFalse(self.sim.actief(MAC))
        self.assertEqual(self.sim.stappen(), [])

    def test_verloop_en_vanzelf_stoppen(self):
        _opdracht(self.sim, simulatie.TOPIC_AAN, MAC.lower(), duur=60)
        self.assertTrue(self.sim.actief(MAC))
        self.assertFalse(self.sim.actief(RMAC))

        self.assertEqual(self.sim.stappen(), [(SENSOREN[MAC], 0.0, 0)])
        self.klok.t += 30
        self.assertEqual(self.sim.stappen(), [(SENSOREN[MAC], 0.5, 1)])
        # Duur om: nog één advertentie met de eindwaarden, daarna is de simulatie voorbij.
        self.klok.t += 31
        self.assertEqual(self.sim.stappen(), [(SENSOREN[MAC], 1.0, 2)])
        self.assertFalse(self.sim.actief(MAC))
        self.assertEqual(self.sim.stappen(), [])

    def test_uit(self):
        _opdracht(self.sim, simulatie.TOPIC_AAN, MAC, duur=60)
        _opdracht(self.sim, simulatie.TOPIC_UIT, MAC)
        self.assertFalse(self.sim.actief(MAC))

    def test_standaard_en_maximale_duur(self):
        _opdracht(self.sim, simulatie.TOPIC_AAN, MAC)                 # geen duur: 1 minuut
        _opdracht(self.sim, simulatie.TOPIC_AAN, RMAC, duur=10 ** 9)  # begrensd op het maximum
        self.klok.t += simulatie.STANDAARD_DUUR
        self.assertEqual([s.id for s, _, _ in self.sim.stappen()], [MAC, RMAC])
        self.assertFalse(self.sim.actief(MAC))
        self.assertTrue(self.sim.actief(RMAC))
        self.klok.t += simulatie.MAX_DUUR
        self.sim.stappen()
        self.assertFalse(self.sim.actief(RMAC))

    def test_sensor_blijft_nooit_genegeerd(self):
        # Ook als stappen() niet meer aangeroepen wordt, eindigt de bypass.
        _opdracht(self.sim, simulatie.TOPIC_AAN, MAC, duur=60)
        self.klok.t += 60 + 3 * simulatie.STAP + 1
        self.assertFalse(self.sim.actief(MAC))

    def test_onbekende_sensor_en_ongeldige_opdracht_genegeerd(self):
        _opdracht(self.sim, simulatie.TOPIC_AAN, "11:22:33:44:55:66", duur=60)
        for payload in [b"kapot", b"[1, 2]", b'{"sensor_id": 5}', b'{"duur": 60}']:
            self.sim.bij_opdracht(simulatie.TOPIC_AAN, payload)
        self.assertEqual(self.sim.stappen(), [])


class TestOntvanger(unittest.TestCase):
    """De ingang van de BLE-reader, met echte en gesimuleerde advertenties."""

    def setUp(self):
        self.bus, self.klok = NepBus(), NepKlok()
        self.sim = simulatie.Simulator(self.bus, SENSOREN, klok=self.klok)
        self.tracer = trace.Tracer(self.bus, "ble-reader", klok=self.klok)
        self.ontvang = Ontvanger(SENSOREN, self.bus, self.tracer, self.sim)

    def _temperaturen(self):
        return [m.waarde for m in self.bus.metingen if m.grootheid == "temperatuur"]

    def _simuleer_stap(self):
        for sensor, fractie, n in self.sim.stappen():
            sd, fd = simulatie.advertentie(sensor, fractie, n)
            self.ontvang(sensor.id, sd, fd, simulatie.RSSI, True)

    def test_bypass_tijdens_simulatie(self):
        # Zonder simulatie: de echte advertentie wordt verwerkt.
        self.ontvang(MAC, {bthome.FCD2_UUID: PVVX}, {}, -70)
        self.assertEqual(self._temperaturen(), [25.06])

        # Simulatie aan: de echte advertentie wordt genegeerd, de gesimuleerde verwerkt.
        _opdracht(self.sim, simulatie.TOPIC_AAN, MAC, duur=60)
        self.ontvang(MAC, {bthome.FCD2_UUID: PVVX}, {}, -70)
        self.assertEqual(self._temperaturen(), [25.06])
        self.klok.t += 30
        self._simuleer_stap()
        self.assertEqual(self._temperaturen(), [25.06, 10.0])
        self.assertEqual([m.waarde for m in self.bus.metingen if m.grootheid == "rssi"][-1],
                         simulatie.RSSI)

        # Simulatie voorbij: de echte sensor telt weer mee.
        self.klok.t += 31
        self._simuleer_stap()
        self.ontvang(MAC, {bthome.FCD2_UUID: PVVX}, {}, -70)
        self.assertEqual(self._temperaturen(), [25.06, 10.0, -5.0, 25.06])

    def test_andere_sensor_loopt_door(self):
        _opdracht(self.sim, simulatie.TOPIC_AAN, RMAC, duur=60)
        self.ontvang(MAC, {bthome.FCD2_UUID: PVVX}, {}, -70)
        self.assertEqual(self._temperaturen(), [25.06])

    def test_onbekend_apparaat_genegeerd(self):
        self.ontvang("11:22:33:44:55:66", {bthome.FCD2_UUID: PVVX}, {}, -70)
        self.assertEqual(self.bus.metingen, [])

    def test_ruuvi_beweging_zonder_sprong_bij_de_overgang(self):
        def beweging():
            return [m.waarde for m in self.bus.metingen if m.grootheid == "beweging"]

        # Echte Ruuvi met tellerstand 200 en 201: één beweging.
        echt = bytearray(simulatie.ruuvi_bytes(simulatie.waarden(0.0), 0, RMAC))
        for stand in (200, 201):
            echt[15] = stand
            self.ontvang(RMAC, {}, {ruuvi.FABRIKANT_ID: bytes(echt)}, -80)
        self.assertEqual(beweging(), [1])

        # Simulatie: de teller begint bij 0. De sprong 201 -> 0 is geen beweging; daarna
        # telt elke gesimuleerde advertentie één beweging.
        _opdracht(self.sim, simulatie.TOPIC_AAN, RMAC, duur=60)
        for _ in range(3):
            self._simuleer_stap()
        self.assertEqual(beweging(), [1, 1, 1])

        # Terug naar de echte sensor: ook dan geen sprong.
        _opdracht(self.sim, simulatie.TOPIC_UIT, RMAC)
        for stand in (201, 203):
            echt[15] = stand
            self.ontvang(RMAC, {}, {ruuvi.FABRIKANT_ID: bytes(echt)}, -80)
        self.assertEqual(beweging(), [1, 1, 1, 2])

    def test_trace_vermeldt_simulatie(self):
        self.tracer.bij_opdracht(trace.TOPIC_AAN, json.dumps({"sensor_ids": [MAC]}).encode())
        _opdracht(self.sim, simulatie.TOPIC_AAN, MAC, duur=60)
        self._simuleer_stap()
        stappen = {d["stap"]: d["tekst"] for _, d in self.bus.berichten}
        self.assertEqual(list(stappen), ["ruw", "gedecodeerd", "gecorrigeerd", "gepubliceerd"])
        self.assertTrue(stappen["ruw"].startswith("SIMULATIE BTHome 40 00 00"))


class TestVraagDuur(unittest.TestCase):
    def _vraag(self, *antwoorden):
        reeks = iter(antwoorden)
        return simulatie.vraag_duur(lambda tekst: next(reeks))

    def test_standaard_is_een_minuut(self):
        self.assertEqual(self._vraag(""), 60)

    def test_minuten(self):
        self.assertEqual(self._vraag("5"), 300)
        self.assertEqual(self._vraag("0,5"), 30)

    def test_ongeldige_invoer_opnieuw_vragen(self):
        self.assertEqual(self._vraag("lang", "0", "-1", "999", "2"), 120)


if __name__ == "__main__":
    unittest.main()
