import unittest

from brms.config import Correctie, Sensor
from brms.readers.ble import verwerk
from brms.readers.bthome import FCD2_UUID
from brms.readers.ruuvi import FABRIKANT_ID

MAC = "A4:C1:38:00:00:01"
PVVX = bytes.fromhex("40" "00a4" "0164" "02ca09" "03bf13" "0c020c")
SENSOREN = {MAC: Sensor(MAC, "bthome", "kajuit", {"temperatuur": Correctie(offset=-0.3)})}

# Ruuvi formaat 5: de officiële testvector "valid data" (zie tests/test_ruuvi.py);
# bewegingsteller = 66 (byte 15).
RMAC = "CB:B8:33:4C:88:4F"
RUUVI = bytes.fromhex("0512FC5394C37C0004FFFC040CAC364200CDCBB8334C884F")
RSENSOREN = {RMAC: Sensor(RMAC, "ruuvi", "motorruim")}


def _waarden(metingen):
    return {m.grootheid: m.waarde for m in metingen}


def _met_teller(stand: int) -> bytes:
    """Het Ruuvi-bericht met een andere stand van de bewegingsteller."""
    data = bytearray(RUUVI)
    data[15] = stand
    return bytes(data)


class TestVerwerk(unittest.TestCase):
    def test_geconfigureerde_sensor(self):
        ms = verwerk(MAC.lower(), {FCD2_UUID: PVVX}, {}, -71, 1000, SENSOREN)
        self.assertEqual(_waarden(ms), {"temperatuur": 24.76, "vocht": 50.55,
                                        "batterijspanning": 3.074, "rssi": -71})
        self.assertTrue(all(m.ts == 1000 and m.sensor_id == MAC for m in ms))

    def test_onbekend_apparaat_genegeerd(self):
        self.assertEqual(verwerk("11:22:33:44:55:66", {FCD2_UUID: PVVX}, {}, -60, 1, SENSOREN), [])

    def test_geen_bthome_data(self):
        self.assertEqual(verwerk(MAC, {}, {}, -60, 1, SENSOREN), [])

    def test_ongeldig_bericht_levert_niets(self):
        self.assertEqual(verwerk(MAC, {FCD2_UUID: bytes.fromhex("41ca")}, {}, -60, 1, SENSOREN), [])

    def test_factor_en_offset(self):
        s = {MAC: Sensor(MAC, "bthome", "kajuit", {"vocht": Correctie(1.02, -1.5)})}
        # 50,55 × 1,02 − 1,5 = 50,061
        self.assertEqual(_waarden(verwerk(MAC, {FCD2_UUID: PVVX}, {}, -70, 1, s))["vocht"], 50.061)

    def test_alleen_grootheden_uit_de_lijst(self):
        s = {MAC: Sensor(MAC, "bthome", "kajuit", {}, ("temperatuur", "rssi"))}
        self.assertEqual(_waarden(verwerk(MAC, {FCD2_UUID: PVVX}, {}, -70, 1, s)),
                         {"temperatuur": 25.06, "rssi": -70})

    def test_zonder_rssi(self):
        self.assertNotIn("rssi", _waarden(verwerk(MAC, {FCD2_UUID: PVVX}, {}, None, 1, SENSOREN)))


class TestVerwerkRuuvi(unittest.TestCase):
    def test_ruuvi_eerste_ontvangst(self):
        # Eerste ontvangst: alle waarden, maar nog geen beweging (geen vorige tellerstand).
        tellers = {}
        ms = verwerk(RMAC, {}, {FABRIKANT_ID: RUUVI}, -80, 1000, RSENSOREN, None, tellers)
        self.assertEqual(_waarden(ms), {
            "temperatuur": 24.3, "vocht": 53.49, "luchtdruk": 1000.44,
            "versnelling_x": 4, "versnelling_y": -4, "versnelling_z": 1036,
            "batterijspanning": 2.977, "rssi": -80})
        self.assertEqual(tellers, {RMAC: 66})

    def test_beweging_is_de_toename(self):
        tellers = {}

        def beweging(stand):
            ms = verwerk(RMAC, {}, {FABRIKANT_ID: _met_teller(stand)}, -80, 1, RSENSOREN,
                         None, tellers)
            return _waarden(ms).get("beweging")

        self.assertIsNone(beweging(250))   # eerste stand: toename onbekend
        self.assertEqual(beweging(250), 0)  # geen beweging: een echte 0
        self.assertEqual(beweging(253), 3)
        self.assertEqual(beweging(1), 3)    # 253 -> 254 -> 0 -> 1
        m = verwerk(RMAC, {}, {FABRIKANT_ID: _met_teller(2)}, -80, 1, RSENSOREN, None, tellers)
        self.assertEqual({x.grootheid: x.soort for x in m}["beweging"], "toename")

    def test_ongeldige_teller_bewaart_vorige_stand(self):
        tellers = {RMAC: 10}
        ms = verwerk(RMAC, {}, {FABRIKANT_ID: _met_teller(255)}, -80, 1, RSENSOREN, None, tellers)
        self.assertNotIn("beweging", _waarden(ms))
        self.assertEqual(tellers, {RMAC: 10})

    def test_ongeldige_luchtdruk_niet_gepubliceerd(self):
        # RuuviTag Pro 3-in-1: geen luchtdruksensor (§5.2.3 punt 4).
        data = bytearray(RUUVI)
        data[5:7] = b"\xff\xff"
        ms = verwerk(RMAC, {}, {FABRIKANT_ID: bytes(data)}, -80, 1, RSENSOREN)
        self.assertNotIn("luchtdruk", _waarden(ms))
        self.assertIn("temperatuur", _waarden(ms))

    def test_ander_dataformaat_levert_niets(self):
        self.assertEqual(verwerk(RMAC, {}, {FABRIKANT_ID: b"\x03" + RUUVI[1:]}, -80, 1,
                                 RSENSOREN), [])

    def test_geen_ruuvi_data(self):
        self.assertEqual(verwerk(RMAC, {FCD2_UUID: PVVX}, {}, -80, 1, RSENSOREN), [])

    def test_grootheden_en_correctie(self):
        s = {RMAC: Sensor(RMAC, "ruuvi", "buiten", {"temperatuur": Correctie(offset=0.5)},
                          ("temperatuur", "vocht"))}
        ms = verwerk(RMAC, {}, {FABRIKANT_ID: RUUVI}, -80, 1, s, None, {RMAC: 60})
        self.assertEqual(_waarden(ms), {"temperatuur": 24.8, "vocht": 53.49})


if __name__ == "__main__":
    unittest.main()
