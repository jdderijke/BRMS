"""Tests van de opmaak en het opzoeken van sensoren in de monitor (§10.2)."""

import unittest

from brms import monitor
from brms.config import Config, MqttConfig, OpslagConfig, Sensor

MAC = "A4:C1:38:00:00:01"
MAC2 = "A4:C1:38:00:00:02"
KAJUIT = Sensor(MAC, "bthome", "Kajuit", {})
VOORONDER = Sensor(MAC2, "bthome", "Vooronder", {})
CFG = Config(MqttConfig(), OpslagConfig(), {MAC: KAJUIT, MAC2: VOORONDER})
NAMEN = {MAC: "Kajuit"}


def _meting(sensor_id, grootheid, waarde, ts=0):
    return {"ts": ts, "sensor_id": sensor_id, "grootheid": grootheid, "waarde": waarde}


def _trace(sensor_id, stap, tekst, deel=None, ts=0):
    d = {"ts": ts, "sensor_id": sensor_id, "stap": stap, "tekst": tekst}
    if deel:
        d["deel"] = deel
    return d


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
        regel = monitor.formatteer_meting(_meting(MAC, "temperatuur", 8.01), NAMEN)[0]
        self.assertIn("Kajuit", regel)
        self.assertIn("8,01 °C", regel)

    def test_onbekende_sensor_toont_id(self):
        regel = monitor.formatteer_meting(_meting("AA:BB", "vocht", 50), NAMEN)[0]
        self.assertIn("AA:BB", regel)

    def test_onleesbaar(self):
        self.assertIn("ONLEESBAAR", monitor._verwerk(monitor.Tabel(CFG), "brms/meting/x", b"kapot"))
        self.assertIn("ONLEESBAAR", monitor._verwerk(monitor.Tabel(CFG), "brms/meting/x", b"{}"))


class TestTabel(unittest.TestCase):
    """Busmodus: een nieuwe meting overschrijft de vorige op dezelfde regel."""

    def test_nieuwe_waarde_overschrijft_op_dezelfde_plaats(self):
        t = monitor.Tabel(CFG)
        t.meting(_meting(MAC, "temperatuur", 8.0))
        t.meting(_meting(MAC, "vocht", 50))
        voor = t.regels()
        t.meting(_meting(MAC, "temperatuur", 7.5))
        na = t.regels()
        self.assertEqual(len(na), len(voor))          # er komt geen regel bij
        self.assertIn("7,5 °C", na[1])                # zelfde regel, nieuwe waarde
        self.assertEqual(na[2], voor[2])              # de regel eronder staat stil

    def test_vaste_volgorde(self):
        # Volgorde: sensoren zoals in de configuratie, grootheden zoals in de tabel –
        # ongeacht de volgorde van binnenkomst.
        t = monitor.Tabel(CFG)
        t.meting(_meting(MAC2, "vocht", 60))
        t.meting(_meting(MAC, "vocht", 50))
        t.meting(_meting(MAC, "temperatuur", 8.0))
        regels = t.regels()[1:]
        self.assertEqual([("Kajuit" in r, "Vooronder" in r) for r in regels],
                         [(True, False), (True, False), (False, True)])
        self.assertIn("temperatuur", regels[0])

    def test_opdracht_op_vaste_regel(self):
        t = monitor.Tabel(CFG)
        self.assertIn("(nog geen)", t.regels()[0])
        self.assertIn("wachten", t.regels()[1])
        monitor._verwerk(t, "brms/opdracht/simulatie/aan", b'{"duur": 60}')
        self.assertIn("brms/opdracht/simulatie/aan", t.regels()[0])


class TestKetens(unittest.TestCase):
    """Tracemodus: elke stap van de keten heeft een vaste plaats."""

    def test_alle_stappen_staan_er_vanaf_het_begin(self):
        regels = monitor.Ketens([KAJUIT]).regels()
        self.assertEqual(len(regels), len(monitor.STAPPEN))
        self.assertTrue(regels[0].startswith("Kajuit"))
        self.assertIn("RUW", regels[0])
        self.assertIn("OPGESLAGEN", regels[-1])

    def test_nieuwe_keten_overschrijft_de_vorige(self):
        k = monitor.Ketens([KAJUIT])
        k.trace(_trace(MAC, "ruw", "BTHome 40 00"))
        k.trace(_trace(MAC, "gedecodeerd", "temperatuur 25.06\nvocht 50.55"))
        voor = k.regels()
        k.trace(_trace(MAC, "ruw", "BTHome 40 01"))
        k.trace(_trace(MAC, "gedecodeerd", "temperatuur 24.9\nvocht 51"))
        na = k.regels()
        self.assertEqual(len(na), len(voor))
        self.assertIn("BTHome 40 01", na[0])
        self.assertIn("temperatuur 24.9", na[1])
        self.assertTrue(na[2].endswith("vocht 51"))
        # De tweede regel van een stap staat recht onder de eerste.
        self.assertEqual(na[2].index("vocht"), na[1].index("temperatuur"))

    def test_kortere_inhoud_laat_de_rest_niet_verspringen(self):
        # Het aantal regels per stap kan verschillen (bijv. minder correcties): de stappen
        # eronder moeten op hun plaats blijven.
        k = monitor.Ketens([KAJUIT, VOORONDER])
        k.trace(_trace(MAC, "gedecodeerd", "a\nb\nc"))
        voor = k.regels()
        k.trace(_trace(MAC, "gedecodeerd", "a"))
        na = k.regels()
        self.assertEqual(len(na), len(voor))
        plaats = lambda regels, tekst: next(i for i, r in enumerate(regels) if tekst in r)
        self.assertEqual(plaats(na, "GECORRIGEERD"), plaats(voor, "GECORRIGEERD"))
        self.assertEqual(plaats(na, "Vooronder"), plaats(voor, "Vooronder"))

    def test_delen_binnen_een_stap(self):
        # De opslag stuurt 'opgenomen' per grootheid; elk deel houdt zijn eigen regel.
        k = monitor.Ketens([KAJUIT])
        k.trace(_trace(MAC, "opgenomen", "temperatuur 8 → nu 1 waarden", "temperatuur"))
        k.trace(_trace(MAC, "opgenomen", "vocht 50 → nu 1 waarden", "vocht"))
        voor = k.regels()
        k.trace(_trace(MAC, "opgenomen", "temperatuur 7 → nu 2 waarden", "temperatuur"))
        na = k.regels()
        self.assertEqual(len(na), len(voor))
        opgenomen = [r for r in na if "waarden" in r]
        self.assertIn("temperatuur 7 → nu 2", opgenomen[0])
        self.assertIn("vocht 50 → nu 1", opgenomen[1])

    def test_trace_van_andere_sensor_genegeerd(self):
        k = monitor.Ketens([KAJUIT])
        voor = k.regels()
        k.trace(_trace(MAC2, "ruw", "BTHome"))
        self.assertEqual(k.regels(), voor)


class TestPasIn(unittest.TestCase):
    def test_past(self):
        self.assertEqual(monitor.pas_in(["a", "b"], 5), ["a", "b"])

    def test_te_veel_regels(self):
        regels = monitor.pas_in([str(i) for i in range(10)], 4)
        self.assertEqual(regels[:3], ["0", "1", "2"])
        self.assertIn("nog 7 regels", regels[3])

    def test_geen_ruimte(self):
        self.assertEqual(monitor.pas_in(["a"], 0), [])


if __name__ == "__main__":
    unittest.main()
