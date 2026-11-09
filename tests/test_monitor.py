"""Tests van de opmaak en het opzoeken van sensoren in de monitor (§10.2)."""

import unittest

from brms import monitor
from brms.config import Config, MqttConfig, OpslagConfig, Sensor

MAC = "A4:C1:38:00:00:01"
CFG = Config(MqttConfig(), OpslagConfig(), {MAC: Sensor(MAC, "bthome", "Kajuit", {})})
NAMEN = {MAC: "Kajuit"}


class TestZoekSensoren(unittest.TestCase):
    def test_op_naam_en_id(self):
        self.assertEqual(monitor.zoek_sensoren(["kajuit"], CFG)[0].id, MAC)
        self.assertEqual(monitor.zoek_sensoren([MAC.lower()], CFG)[0].id, MAC)
        self.assertEqual(monitor.zoek_sensoren([], CFG), [])

    def test_onbekend(self):
        with self.assertRaises(ValueError) as ctx:
            monitor.zoek_sensoren(["salon"], CFG)
        self.assertIn("Kajuit", str(ctx.exception))


class TestOpmaak(unittest.TestCase):
    def test_meting(self):
        regel = monitor.formatteer_meting({"ts": 0, "sensor_id": MAC, "grootheid": "temperatuur",
                                           "waarde": 8.01}, NAMEN)[0]
        self.assertIn("Kajuit", regel)
        self.assertIn("8,01 °C", regel)

    def test_onbekende_sensor_toont_id(self):
        regel = monitor.formatteer_meting({"ts": 0, "sensor_id": "AA:BB", "grootheid": "vocht",
                                           "waarde": 50}, NAMEN)[0]
        self.assertIn("AA:BB", regel)

    def test_trace_blok(self):
        regels = monitor.formatteer_trace({"ts": 0, "sensor_id": MAC, "stap": "ruw",
                                           "tekst": "regel 1\nregel 2"}, NAMEN)
        self.assertTrue(regels[0].startswith("─"))     # nieuwe keten: scheidingslijn
        self.assertIn("RUW", regels[1])
        self.assertTrue(regels[2].endswith("regel 2"))
        self.assertEqual(regels[2].index("regel 2"), regels[1].index("regel 1"))

    def test_onleesbaar(self):
        self.assertIn("ONLEESBAAR", monitor._maak_regels("brms/meting/x", b"kapot", NAMEN)[0])


if __name__ == "__main__":
    unittest.main()
