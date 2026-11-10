import unittest

from brms.readers.ruuvi import LEVERT, TELLER, RuuviFout, decodeer, decodeer_velden, toename

# De officiële testvectoren van Ruuvi voor dataformaat 5 (ARCHITECTURE.md §5.2.3 punt 2):
# https://docs.ruuvi.com/communication/bluetooth-advertisements/data-format-5-rawv2
# (zonder het fabrikant-ID 0x0499; bleak levert de bytes ook zo aan)
GELDIG = bytes.fromhex("0512FC5394C37C0004FFFC040CAC364200CDCBB8334C884F")
MAXIMUM = bytes.fromhex("057FFFFFFEFFFE7FFF7FFF7FFFFFDEFEFFFECBB8334C884F")
MINIMUM = bytes.fromhex("058001000000008001800180010000000000CBB8334C884F")
ONGELDIG = bytes.fromhex("058000FFFFFFFF800080008000FFFFFFFFFFFFFFFFFFFFFF")


class TestRuuvi(unittest.TestCase):
    def test_geldige_waarden(self):
        # Ruuvi: 24,3 °C; 53,49 %; 100044 Pa; 0,004 / -0,004 / 1,036 G; 2,977 V; teller 66
        self.assertEqual(decodeer(GELDIG), {
            "temperatuur": 24.3, "vocht": 53.49, "luchtdruk": 1000.44,
            "versnelling_x": 4, "versnelling_y": -4, "versnelling_z": 1036,
            "batterijspanning": 2.977, TELLER: 66})

    def test_maximale_waarden(self):
        # Ruuvi: 163,835 °C; 163,835 %; 115534 Pa; 32,767 G; 3,646 V; teller 254
        self.assertEqual(decodeer(MAXIMUM), {
            "temperatuur": 163.835, "vocht": 163.835, "luchtdruk": 1155.34,
            "versnelling_x": 32767, "versnelling_y": 32767, "versnelling_z": 32767,
            "batterijspanning": 3.646, TELLER: 254})

    def test_minimale_waarden(self):
        # Ruuvi: -163,835 °C; 0 %; 50000 Pa; -32,767 G; 1,6 V; teller 0
        self.assertEqual(decodeer(MINIMUM), {
            "temperatuur": -163.835, "vocht": 0.0, "luchtdruk": 500.0,
            "versnelling_x": -32767, "versnelling_y": -32767, "versnelling_z": -32767,
            "batterijspanning": 1.6, TELLER: 0})

    def test_ongeldige_waarden_ontbreken(self):
        # Alle velden "ongeldig": er blijft niets over om te publiceren (§5.2.3 punt 4).
        self.assertEqual(decodeer(ONGELDIG), {})

    def test_alleen_luchtdruk_ongeldig(self):
        # Zoals een RuuviTag Pro 3-in-1 (zonder luchtdruksensor) uitzendt.
        data = bytearray(GELDIG)
        data[5:7] = b"\xff\xff"
        waarden = decodeer(bytes(data))
        self.assertNotIn("luchtdruk", waarden)
        self.assertEqual(waarden["temperatuur"], 24.3)

    def test_velden_voor_trace(self):
        velden = {v.grootheid: v for v in decodeer_velden(ONGELDIG)}
        self.assertIsNone(velden["luchtdruk"].waarde)
        self.assertEqual(velden["luchtdruk"].ruw, b"\xff\xff")
        v = {v.grootheid: v for v in decodeer_velden(GELDIG)}["batterijspanning"]
        self.assertEqual((v.ruw, v.getal, v.waarde), (b"\xac\x36", 1377, 2.977))

    def test_levert(self):
        # Alles wat de decoder teruggeeft is een grootheid uit LEVERT, behalve de ruwe teller.
        self.assertEqual(set(decodeer(GELDIG)) - {TELLER}, set(LEVERT) - {"beweging"})

    def test_ander_dataformaat(self):
        with self.assertRaises(RuuviFout):
            decodeer(b"\x03" + GELDIG[1:])

    def test_afgebroken(self):
        with self.assertRaises(RuuviFout):
            decodeer(GELDIG[:20])

    def test_leeg(self):
        with self.assertRaises(RuuviFout):
            decodeer(b"")


class TestToename(unittest.TestCase):
    def test_gewone_toename(self):
        self.assertEqual(toename(66, 69), 3)

    def test_geen_beweging(self):
        self.assertEqual(toename(66, 66), 0)

    def test_terugspringen_naar_nul(self):
        # 253 -> 254 -> 0 -> 1 -> 2: vier keer beweging.
        self.assertEqual(toename(253, 2), 4)
        self.assertEqual(toename(254, 0), 1)


if __name__ == "__main__":
    unittest.main()
