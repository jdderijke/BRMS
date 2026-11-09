import json
import unittest

from brms import grootheden
from brms.meting import Meting, maak


class TestMeting(unittest.TestCase):
    def test_soort_volgt_uit_tabel(self):
        self.assertEqual(maak(1, "A", "temperatuur", 1.5).soort, grootheden.DOORLOPEND)
        self.assertEqual(maak(1, "A", "walstroom", 1).soort, grootheden.TOESTAND)
        self.assertEqual(maak(1, "A", "beweging", 3).soort, grootheden.TOENAME)

    def test_topic(self):
        m = maak(1, "A4:C1:38:00:00:01", "vocht", 55)
        self.assertEqual(m.topic(), "brms/meting/A4:C1:38:00:00:01/vocht")

    def test_json_heen_en_terug(self):
        m = maak(1762700000, "A4:C1:38:00:00:01", "temperatuur", -2.5)
        tekst = m.naar_json()
        self.assertEqual(json.loads(tekst), {"ts": 1762700000, "sensor_id": "A4:C1:38:00:00:01",
                                             "grootheid": "temperatuur", "waarde": -2.5,
                                             "soort": "doorlopend"})
        self.assertEqual(Meting.van_json(tekst.encode()), m)

    def test_ongeldig(self):
        for args in [(1, "A", "onbekend", 1), (1.5, "A", "vocht", 1), (1, "", "vocht", 1),
                     (1, "A", "vocht", float("nan")), (1, "A", "vocht", True),
                     (1, "A", "vocht", 1, "toestand")]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                maak(*args)

    def test_van_json_ongeldig(self):
        for data in [b"\xff", b"geen json", b"[1]", b'{"ts": 1}']:
            with self.subTest(data=data), self.assertRaises(ValueError):
                Meting.van_json(data)


if __name__ == "__main__":
    unittest.main()
