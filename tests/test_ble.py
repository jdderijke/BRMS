import unittest

from brms.config import Sensor
from brms.readers.ble import verwerk
from brms.readers.bthome import FCD2_UUID

MAC = "A4:C1:38:00:00:01"
PVVX = bytes.fromhex("40" "00a4" "0164" "02ca09" "03bf13" "0c020c")
SENSOREN = {MAC: Sensor(MAC, "bthome", "kajuit", {"temperatuur": -0.3})}


def _waarden(metingen):
    return {m.grootheid: m.waarde for m in metingen}


class TestVerwerk(unittest.TestCase):
    def test_geconfigureerde_sensor(self):
        ms = verwerk(MAC.lower(), {FCD2_UUID: PVVX}, -71, 1000, SENSOREN)
        self.assertEqual(_waarden(ms), {"temperatuur": 24.76, "vocht": 50.55,
                                        "batterijspanning": 3.074, "rssi": -71})
        self.assertTrue(all(m.ts == 1000 and m.sensor_id == MAC for m in ms))

    def test_onbekend_apparaat_genegeerd(self):
        self.assertEqual(verwerk("11:22:33:44:55:66", {FCD2_UUID: PVVX}, -60, 1, SENSOREN), [])

    def test_geen_bthome_data(self):
        self.assertEqual(verwerk(MAC, {}, -60, 1, SENSOREN), [])

    def test_ongeldig_bericht_levert_niets(self):
        self.assertEqual(verwerk(MAC, {FCD2_UUID: bytes.fromhex("41ca")}, -60, 1, SENSOREN), [])

    def test_zonder_rssi(self):
        self.assertNotIn("rssi", _waarden(verwerk(MAC, {FCD2_UUID: PVVX}, None, 1, SENSOREN)))


if __name__ == "__main__":
    unittest.main()
