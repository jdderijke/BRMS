import unittest

from brms.readers.bthome import BTHomeFout, decodeer

# Waarden uit de voorbeelden op https://bthome.io/format/
# 0x40 = apparaatinformatie: BTHome v2, onversleuteld, regelmatig verzonden
PVVX = bytes.fromhex("40" "00a4" "0164" "02ca09" "03bf13" "0c020c")


class TestBTHome(unittest.TestCase):
    def test_pvvx_bericht(self):
        self.assertEqual(decodeer(PVVX),
                         {"temperatuur": 25.06, "vocht": 50.55, "batterijspanning": 3.074})

    def test_negatieve_temperatuur(self):
        # -2,00 °C = -200 = 0xFF38
        self.assertEqual(decodeer(bytes.fromhex("40" "0238ff")), {"temperatuur": -2.0})

    def test_temperatuur_en_vocht_lage_resolutie(self):
        # 0x45: sint16 x 0,1 (-1,5 °C = -15 = 0xFFF1); 0x2E: uint8 %
        self.assertEqual(decodeer(bytes.fromhex("40" "2e37" "45f1ff")),
                         {"vocht": 55, "temperatuur": -1.5})

    def test_onbekend_object_stopt_maar_houdt_eerdere_waarden(self):
        self.assertEqual(decodeer(bytes.fromhex("40" "02ca09" "ee0102")), {"temperatuur": 25.06})

    def test_niet_gebruikte_objecten_worden_overgeslagen(self):
        # 0x04 luchtdruk (3 bytes), 0x53 tekst (variabel), daarna temperatuur
        data = bytes.fromhex("40" "04138a01" "5303414243" "02ca09")
        self.assertEqual(decodeer(data), {"temperatuur": 25.06})

    def test_versleuteld(self):
        with self.assertRaises(BTHomeFout):
            decodeer(bytes.fromhex("41" "02ca09"))

    def test_verkeerde_versie(self):
        with self.assertRaises(BTHomeFout):
            decodeer(bytes.fromhex("20" "02ca09"))

    def test_afgebroken(self):
        with self.assertRaises(BTHomeFout):
            decodeer(bytes.fromhex("40" "02ca"))

    def test_leeg(self):
        with self.assertRaises(BTHomeFout):
            decodeer(b"")

    def test_alleen_apparaatinformatie(self):
        self.assertEqual(decodeer(bytes.fromhex("40")), {})


if __name__ == "__main__":
    unittest.main()
