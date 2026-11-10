"""Tests van de Victron-decoder (ARCHITECTURE.md §5.2.2) – zonder Bluetooth.

Er zijn hier geen echte Victron-apparaten. De tests gebruiken:
- het voorbeeld uit de documentatie van de bibliotheek victron-ble (een echte advertentie
  van een accumonitor, met de bijbehorende sleutel);
- zelf gemaakte advertenties: bekende waarden, op dezelfde manier versleuteld als een
  Victron-apparaat dat doet (AES in CTR-modus).
"""

import struct
import unittest

from Crypto.Cipher import AES
from Crypto.Util import Counter

from brms.config import Correctie, Sensor
from brms.readers import victron
from brms.readers.ble import verwerk

SLEUTEL = "aff4d0995b7d1e176c0c33ecb9e70dcd"
ANDERE_SLEUTEL = "00f4d0995b7d1e176c0c33ecb9e70dcd"

# Voorbeeld uit victron-ble: accumonitor, 12,53 V, 0 A, 50 %, 50 Ah verbruikt, resterende
# tijd "oneindig".
BMV_VOORBEELD = bytes.fromhex("100289a302b040af925d09a4d89aa0128bdef48c6298a9")

MAC = "D3:11:22:33:44:55"


def _bits(*velden) -> bytes:
    """Pakt (waarde, aantal bits)-paren achter elkaar in, minst significante bit eerst –
    zoals Victron het doet. Negatieve waarden in two's complement."""
    getal, plaats = 0, 0
    for waarde, bits in velden:
        getal |= (waarde & ((1 << bits) - 1)) << plaats
        plaats += bits
    return getal.to_bytes((plaats + 7) // 8, "little")


def _advertentie(soort: int, inhoud: bytes, sleutel: str = SLEUTEL, iv: int = 0x1234) -> bytes:
    """Maakt Instant Readout manufacturer data: kop, soort apparaat, teller, eerste byte van
    de sleutel, en de versleutelde inhoud."""
    sleutel_b = bytes.fromhex(sleutel)
    teller = Counter.new(128, initial_value=iv, little_endian=True)
    versleuteld = AES.new(sleutel_b, AES.MODE_CTR, counter=teller).encrypt(inhoud)
    return (b"\x10\x02" + struct.pack("<H", 0xA389) + bytes([soort]) + struct.pack("<H", iv)
            + sleutel_b[:1] + versleuteld)


def _bmv(resterend=240, spanning=1325, stroom=-2500, verbruikt=123, soc=876) -> bytes:
    # resterende tijd (min), spanning (0,01 V), alarm, aux, aux-modus (3 = uit),
    # stroom (mA), verbruikt (0,1 Ah), laadtoestand (0,1 %)
    return _advertentie(0x02, _bits((resterend, 16), (spanning, 16), (0, 16), (0, 16), (3, 2),
                                    (stroom, 22), (verbruikt, 20), (soc, 10)))


def _mppt(vermogen=40, laadstroom=25) -> bytes:
    # laadfase (3 = bulk), fout, accuspanning (0,01 V), laadstroom (0,1 A),
    # opbrengst vandaag (10 Wh), vermogen (W), belasting (onbekend)
    return _advertentie(0x01, _bits((3, 8), (0, 8), (1325, 16), (laadstroom, 16), (12, 16),
                                    (vermogen, 16), (0x1FF, 9)))


def _multiplus(ac_in=0) -> bytes:
    # toestand, fout, accustroom (0,1 A), accuspanning (0,01 V, 14 bits), AC-ingang (2 bits:
    # 0 = AC in 1, 1 = AC in 2, 2 = niet aangesloten, 3 = onbekend), vermogen in en uit
    # (19 bits), alarm, temperatuur, laadtoestand
    return _advertentie(0x0C, _bits((3, 8), (0, 8), (50, 16), (1325, 14), (ac_in, 2),
                                    (300, 19), (100, 19), (0, 2), (60, 7), (80, 7)))


class TestVictron(unittest.TestCase):
    def test_voorbeeld_van_de_bibliotheek(self):
        # Resterende tijd is "oneindig" en ontbreekt dus; verbruikt is positief.
        self.assertEqual(victron.decodeer(BMV_VOORBEELD, SLEUTEL, "victron-bmv"),
                         {"spanning": 12.53, "stroom": 0.0, "soc": 50.0, "verbruikt": 50.0})

    def test_accumonitor(self):
        self.assertEqual(victron.decodeer(_bmv(), SLEUTEL, "victron-bmv"),
                         {"spanning": 13.25, "stroom": -2.5, "soc": 87.6, "verbruikt": 12.3,
                          "resterende_tijd": 240.0})

    def test_zonnelader(self):
        self.assertEqual(victron.decodeer(_mppt(), SLEUTEL, "victron-mppt"),
                         {"zonnevermogen": 40.0, "laadstroom_zon": 2.5})
        # 's Nachts: 0 W is een echte meting, geen ontbrekende waarde.
        self.assertEqual(victron.decodeer(_mppt(0, 0), SLEUTEL, "victron-mppt"),
                         {"zonnevermogen": 0.0, "laadstroom_zon": 0.0})

    def test_walstroom_van_de_multiplus(self):
        def walstroom(ac_in):
            return victron.decodeer(_multiplus(ac_in), SLEUTEL, "victron-multiplus")

        self.assertEqual(walstroom(0), {"walstroom": 1.0})   # AC in 1
        self.assertEqual(walstroom(1), {"walstroom": 1.0})   # AC in 2
        self.assertEqual(walstroom(2), {"walstroom": 0.0})   # niet aangesloten: walstroom weg
        self.assertEqual(walstroom(3), {})                   # onbekend: niets publiceren

    def test_velden_voor_trace(self):
        ontsleuteld, velden = victron.decodeer_velden(BMV_VOORBEELD, SLEUTEL, "victron-bmv")
        self.assertEqual(ontsleuteld[:4].hex(), "ffffe504")   # 0xffff = oneindig, 0x04e5 = 1253
        v = {v.grootheid: v for v in velden}
        self.assertIsNone(v["resterende_tijd"].waarde)
        self.assertEqual((v["verbruikt"].bron, v["verbruikt"].ruw, v["verbruikt"].waarde),
                         ("consumed_ah", -50.0, 50.0))

    def test_levert(self):
        self.assertEqual(set(victron.LEVERT), {"victron-bmv", "victron-mppt", "victron-multiplus"})
        self.assertEqual(set(victron.decodeer(_bmv(), SLEUTEL, "victron-bmv")),
                         set(victron.LEVERT["victron-bmv"]))

    def test_verkeerde_sleutel(self):
        with self.assertRaises(victron.VictronFout) as ctx:
            victron.decodeer(BMV_VOORBEELD, ANDERE_SLEUTEL, "victron-bmv")
        self.assertIn("sleutel", str(ctx.exception))
        self.assertNotIn(ANDERE_SLEUTEL, str(ctx.exception))   # de sleutel is een geheim

    def test_ander_apparaat_dan_het_type(self):
        with self.assertRaises(victron.VictronFout) as ctx:
            victron.decodeer(_mppt(), SLEUTEL, "victron-bmv")
        self.assertIn("SolarCharger", str(ctx.exception))

    def test_afgebroken_bericht(self):
        with self.assertRaises(victron.VictronFout):
            victron.decodeer(BMV_VOORBEELD[:10], SLEUTEL, "victron-bmv")

    def test_alleen_instant_readout(self):
        self.assertTrue(victron.is_instant_readout(BMV_VOORBEELD))
        self.assertFalse(victron.is_instant_readout(b"\x02\x57\x0a"))   # andere advertentie
        self.assertFalse(victron.is_instant_readout(b""))


class TestVerwerkVictron(unittest.TestCase):
    """De Victron-decoder in de BLE-reader."""

    def _waarden(self, sensor, data, rssi=-75):
        ms = verwerk(MAC, {}, {victron.FABRIKANT_ID: data}, rssi, 1000, {MAC: sensor})
        return {m.grootheid: (m.waarde, m.soort) for m in ms}

    def test_accumonitor_met_correctie_en_rssi(self):
        sensor = Sensor(MAC, "victron-bmv", "accu", {"spanning": Correctie(offset=0.05)},
                        sleutel=SLEUTEL)
        self.assertEqual(self._waarden(sensor, _bmv()), {
            "spanning": (13.3, "doorlopend"), "stroom": (-2.5, "doorlopend"),
            "soc": (87.6, "doorlopend"), "verbruikt": (12.3, "doorlopend"),
            "resterende_tijd": (240.0, "doorlopend"), "rssi": (-75.0, "doorlopend")})

    def test_walstroom_is_een_toestand(self):
        sensor = Sensor(MAC, "victron-multiplus", "multiplus", sleutel=SLEUTEL)
        self.assertEqual(self._waarden(sensor, _multiplus(2))["walstroom"], (0.0, "toestand"))

    def test_verkeerde_sleutel_levert_niets(self):
        sensor = Sensor(MAC, "victron-bmv", "accu", sleutel=ANDERE_SLEUTEL)
        with self.assertLogs("brms.readers.ble", "WARNING") as log:
            self.assertEqual(self._waarden(sensor, _bmv()), {})
        self.assertNotIn(ANDERE_SLEUTEL, "\n".join(log.output))

    def test_andere_advertentie_overgeslagen(self):
        sensor = Sensor(MAC, "victron-mppt", "zon", sleutel=SLEUTEL)
        self.assertEqual(self._waarden(sensor, b"\x02\x57\x0a\x00\x00\x00\x00\x00\x00\x00"), {})
        self.assertEqual(verwerk(MAC, {}, {}, -70, 1, {MAC: sensor}), [])


if __name__ == "__main__":
    unittest.main()
