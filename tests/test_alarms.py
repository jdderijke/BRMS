"""Tests van de alarmlaag (ARCHITECTURE.md §5.5.2, §5.5.3) – zonder broker en zonder
netwerk, met een nep-klok."""

import json
import unittest

from brms import alarms, config
from brms.alarms import BEGIN, HERSTEL, Alarmlaag, Bewaker, Gebeurtenis
from brms.config import Alarmregel, Config, MqttConfig, OpslagConfig, Sensor
from brms.melding import Melding
from brms.meting import maak

MAC = "A4:C1:38:00:00:01"
MAC2 = "A4:C1:38:00:00:02"
KAJUIT = Sensor(MAC, "bthome", "kajuit")
VOORONDER = Sensor(MAC2, "bthome", "vooronder")


def _regel(soort=config.GRENS, **velden) -> Alarmregel:
    basis = {"id": "vorst", "naam": "Vorst", "sensoren": (MAC,), "soort": soort}
    if soort != config.STIL:
        basis["grootheid"] = "temperatuur"
    return Alarmregel(**{**basis, **velden})


class NepKlok:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class TestGrens(unittest.TestCase):
    def test_duur_en_hysterese(self):
        # Vorst: onder 2 °C gedurende 15 min; herstel pas vanaf 3 °C.
        b = Bewaker(_regel(onder=2.0, herstel=3.0, duur=900), KAJUIT, 0)
        self.assertIsNone(b.meting("temperatuur", 1.5, 0))        # de duur begint
        self.assertIsNone(b.meting("temperatuur", 1.4, 600))
        self.assertEqual(b.meting("temperatuur", 1.3, 900), BEGIN)   # 15 min onafgebroken
        self.assertTrue(b.actief)
        # Boven de grens maar onder de herstelgrens: nog geen herstel (hysterese).
        self.assertIsNone(b.meting("temperatuur", 2.5, 1000))
        self.assertTrue(b.actief)
        self.assertEqual(b.meting("temperatuur", 3.0, 1100), HERSTEL)
        self.assertFalse(b.actief)

    def test_een_waarde_die_niet_voldoet_zet_de_duur_terug(self):
        b = Bewaker(_regel(onder=2.0, duur=900), KAJUIT, 0)
        b.meting("temperatuur", 1.5, 0)
        self.assertIsNone(b.meting("temperatuur", 2.1, 800))     # onderbroken
        self.assertIsNone(b.meting("temperatuur", 1.5, 900))     # begint opnieuw
        self.assertIsNone(b.meting("temperatuur", 1.5, 1700))
        self.assertEqual(b.meting("temperatuur", 1.5, 1800), BEGIN)

    def test_direct_en_zonder_herstelgrens(self):
        # Bilge: = 1 (boven 0,5), direct; zonder herstelgrens herstelt hij op de grens zelf.
        b = Bewaker(_regel(grootheid="bilge", boven=0.5), KAJUIT, 0)
        self.assertEqual(b.meting("bilge", 1.0, 10), BEGIN)
        self.assertIsNone(b.meting("bilge", 1.0, 12))            # blijft actief, geen tweede begin
        self.assertEqual(b.meting("bilge", 0.0, 14), HERSTEL)

    def test_herstelduur(self):
        # Walstroom weg: = 0 gedurende 2 min; herstel pas na 2 min onafgebroken = 1.
        b = Bewaker(_regel(grootheid="walstroom", onder=0.5, duur=120, herstelduur=120), KAJUIT, 0)
        b.meting("walstroom", 0.0, 0)
        self.assertEqual(b.meting("walstroom", 0.0, 120), BEGIN)
        self.assertIsNone(b.meting("walstroom", 1.0, 200))
        self.assertIsNone(b.meting("walstroom", 0.0, 250))       # weer weg: herstel onderbroken
        self.assertIsNone(b.meting("walstroom", 1.0, 300))
        self.assertEqual(b.meting("walstroom", 1.0, 420), HERSTEL)

    def test_boven(self):
        b = Bewaker(_regel(grootheid="vocht", boven=80.0, herstel=75.0), KAJUIT, 0)
        self.assertIsNone(b.meting("vocht", 80.0, 0))            # precies op de grens: geen alarm
        self.assertEqual(b.meting("vocht", 80.1, 1), BEGIN)
        self.assertIsNone(b.meting("vocht", 76.0, 2))
        self.assertEqual(b.meting("vocht", 75.0, 3), HERSTEL)

    def test_andere_grootheid_en_geen_data(self):
        b = Bewaker(_regel(onder=2.0), KAJUIT, 0)
        self.assertIsNone(b.meting("vocht", 1.0, 0))             # telt niet mee
        self.assertEqual(b.meting("temperatuur", 1.0, 1), BEGIN)
        # Geen data is geen herstel: de tijd alleen verandert niets (§5.5.2 punt 5).
        self.assertIsNone(b.tik(999999))
        self.assertTrue(b.actief)


class TestAantal(unittest.TestCase):
    def test_bewegingen_binnen_het_venster(self):
        # Beweging: 5 of meer binnen 60 s; daarna 10 min rust voordat het alarm herstelt.
        b = Bewaker(_regel(config.AANTAL, grootheid="beweging", aantal=5, venster=60,
                           herstelduur=600), KAJUIT, 0)
        self.assertIsNone(b.meting("beweging", 2, 0))
        self.assertIsNone(b.meting("beweging", 0, 10))
        self.assertIsNone(b.meting("beweging", 2, 20))
        self.assertEqual(b.meting("beweging", 1, 30), BEGIN)     # 5 binnen 60 s
        self.assertEqual(b.laatste, 5)
        self.assertIsNone(b.tik(100))        # venster leeg: de rust begint
        self.assertIsNone(b.tik(600))
        self.assertEqual(b.tik(700), HERSTEL)

    def test_oude_tellingen_vervallen(self):
        b = Bewaker(_regel(config.AANTAL, grootheid="beweging", aantal=5, venster=60), KAJUIT, 0)
        b.meting("beweging", 4, 0)
        self.assertIsNone(b.meting("beweging", 1, 61))           # de 4 vallen buiten het venster
        self.assertEqual(b.laatste, 1)

    def test_pompbeurten_zijn_overgangen_van_0_naar_1(self):
        # Bilgepomp vaak: 3 beurten binnen een uur.
        b = Bewaker(_regel(config.AANTAL, grootheid="bilgepomp", aantal=3, venster=3600), KAJUIT, 0)
        uit = [b.meting("bilgepomp", w, t) for t, w in
               enumerate([1, 1, 0, 1, 1, 0, 0, 1, 0, 1])]
        # De eerste 1 is geen overgang (vorige stand onbekend); de derde beurt geeft het alarm.
        self.assertEqual(uit, [None, None, None, None, None, None, None, None, None, BEGIN])
        self.assertEqual(b.tik(3604), HERSTEL)    # de eerste beurt (t=3) valt het venster uit


class TestStil(unittest.TestCase):
    def test_stil_en_weer_terug(self):
        b = Bewaker(_regel(config.STIL, id="stil", duur=1800), KAJUIT, 0)
        self.assertIsNone(b.meting("temperatuur", 20.0, 100))
        self.assertIsNone(b.tik(1899))
        self.assertEqual(b.tik(1900), BEGIN)                     # 30 min na de laatste waarde
        self.assertIsNone(b.tik(5000))
        self.assertEqual(b.meting("rssi", -70, 6000), HERSTEL)   # elke grootheid telt

    def test_sensor_die_nooit_iets_levert(self):
        b = Bewaker(_regel(config.STIL, id="stil", duur=1800), KAJUIT, 500)
        self.assertIsNone(b.tik(2299))
        self.assertEqual(b.tik(2300), BEGIN)


class TestTekst(unittest.TestCase):
    def test_getallen_en_tijden(self):
        self.assertEqual(alarms.waardetekst("temperatuur", 1.8), "1,8 °C")
        self.assertEqual(alarms.waardetekst("walstroom", 0.0), "0")
        self.assertEqual([alarms.duurtekst(s) for s in (45, 720, 3600, 11520, 187200)],
                         ["45 s", "12 min", "1 u", "3 u 12 min", "2 d 4 u"])

    def test_voorwaarde(self):
        self.assertEqual(alarms.voorwaardetekst(_regel(onder=2.0)), "onder 2 °C")
        self.assertEqual(alarms.voorwaardetekst(_regel(grootheid="vocht", boven=80.0)), "boven 80 %")
        self.assertEqual(alarms.voorwaardetekst(
            _regel(config.AANTAL, grootheid="bilgepomp", aantal=4, venster=21600)),
            "4 of meer binnen 6 u")


class TestAlarmlaag(unittest.TestCase):
    def _laag(self, *regels):
        self.klok = NepKlok()
        cfg = Config(MqttConfig(), OpslagConfig("x"), {MAC: KAJUIT, MAC2: VOORONDER},
                     alarmen=tuple(regels))
        return Alarmlaag(cfg, klok=self.klok, tijd=lambda: 1_800_000_000.4)

    def _meet(self, laag, waarde, sensor_id=MAC, grootheid="temperatuur"):
        return laag.bij_meting(maak(1, sensor_id, grootheid, waarde))

    def test_begin_herinnering_en_herstel(self):
        laag = self._laag(_regel(onder=2.0, herstel=3.0, duur=900, melding="herhaald",
                                 herhaling=3600, prioriteit="hoog"))
        self.assertEqual(self._meet(laag, 1.8), [])
        self.klok.t += 900
        gebeurtenis, melding = self._meet(laag, 1.6)
        self.assertEqual(gebeurtenis, Gebeurtenis(1_800_000_000, "vorst", MAC, "temperatuur",
                                                  BEGIN, 1.6))
        self.assertEqual(melding, Melding(
            "⚠️ Vorst – kajuit: temperatuur 1,6 °C (onder 2 °C, al 15 min)", prioriteit="hoog"))
        self.assertEqual(json.loads(gebeurtenis.naar_json())["gebeurtenis"], "begin")
        self.assertEqual(gebeurtenis.topic(), "brms/alarm/vorst")

        # Nog geen uur voorbij: geen herinnering. Daarna één per uur.
        self.klok.t += 3599
        self.assertEqual(laag.tik(), [])
        self.klok.t += 1
        self._meet(laag, 1.2)
        self.assertEqual(laag.tik(), [Melding(
            "🔁 Herinnering: Vorst – kajuit: temperatuur 1,2 °C; alarm actief sinds 1 u",
            prioriteit="hoog")])
        self.assertEqual(laag.tik(), [])

        # Herstel: gebeurtenis en een herstelmelding (nooit dringend); daarna geen herinnering.
        self.klok.t += 720
        gebeurtenis, melding = self._meet(laag, 3.4)
        self.assertEqual((gebeurtenis.gebeurtenis, gebeurtenis.waarde), (HERSTEL, 3.4))
        self.assertEqual(melding, Melding(
            "✅ Hersteld: Vorst – kajuit: temperatuur 3,4 °C (alarm was 1 u 12 min actief)"))
        self.klok.t += 99999
        self.assertEqual(laag.tik(), [])

    def test_eenmalig_geeft_geen_herinnering(self):
        laag = self._laag(_regel(onder=2.0))
        self.assertEqual(len(self._meet(laag, 1.0)), 2)
        self.klok.t += 99999
        self.assertEqual(laag.tik(), [])

    def test_melding_uit_legt_wel_vast(self):
        # Een alarm hoeft niet tot een notificatie te leiden (§5.5.3).
        laag = self._laag(_regel(onder=2.0, melding="uit"))
        self.assertEqual([type(x) for x in self._meet(laag, 1.0)], [Gebeurtenis])
        self.assertEqual([type(x) for x in self._meet(laag, 5.0)], [Gebeurtenis])

    def test_zonder_herstelmelding(self):
        laag = self._laag(_regel(onder=2.0, herstelmelding=False))
        self._meet(laag, 1.0)
        self.assertEqual([type(x) for x in self._meet(laag, 5.0)], [Gebeurtenis])

    def test_elke_sensor_apart(self):
        laag = self._laag(_regel(onder=2.0, sensoren=(MAC, MAC2)))
        self.assertEqual(len(laag.bewakers), 2)
        _, melding = self._meet(laag, 1.0, MAC2)
        self.assertIn("Vorst – vooronder", melding.titel)
        # De kajuit is nog niet in alarm en krijgt zijn eigen melding.
        _, melding = self._meet(laag, 0.5, MAC)
        self.assertIn("Vorst – kajuit", melding.titel)
        # Een sensor zonder regel doet niets.
        self.assertEqual(laag.bij_meting(maak(1, "A4:C1:38:00:00:09", "temperatuur", -9)), [])

    def test_stil_en_aantal_in_een_melding(self):
        laag = self._laag(
            _regel(config.STIL, id="stil", naam="Sensor stil", duur=1800),
            _regel(config.AANTAL, id="beweging", naam="Beweging", grootheid="beweging",
                   aantal=3, venster=60))
        self._meet(laag, 20.0)
        gebeurtenis, melding = self._meet(laag, 4, grootheid="beweging")
        self.assertEqual((gebeurtenis.alarm_id, gebeurtenis.waarde), ("beweging", 4.0))
        self.assertEqual(melding.titel, "⚠️ Beweging – kajuit: beweging 4 keer binnen 1 min "
                                        "(3 of meer binnen 1 min)")
        self.klok.t += 1800
        uit = laag.tik()     # de bewegingen vallen het venster uit, en de sensor is stil
        self.assertEqual([(g.alarm_id, g.gebeurtenis, g.grootheid) for g in uit
                          if isinstance(g, Gebeurtenis)],
                         [("stil", BEGIN, None), ("beweging", HERSTEL, "beweging")])
        self.assertIn("⚠️ Sensor stil – kajuit: geen waarden ontvangen gedurende 30 min",
                      [m.titel for m in uit if isinstance(m, Melding)])
        self.klok.t += 600
        melding = [m for m in self._meet(laag, 20.0) if isinstance(m, Melding)][0]
        self.assertEqual(melding.titel,
                         "✅ Hersteld: Sensor stil – kajuit: levert weer waarden (was 40 min stil)")


if __name__ == "__main__":
    unittest.main()
