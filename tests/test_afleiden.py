"""Tests voor de afgeleide grootheden (ARCHITECTURE.md §5.2.5), in het bijzonder de
slagzij (punt 10): de berekening zelf, de plaats in de BLE-reader, de controles in de
configuratie en de tekst met sb/bb in meldingen."""

import math
import unittest

from brms import afleiden, alarms, config, grootheden
from brms.config import Alarmregel, Correctie, Sensor
from brms.readers.ble import verwerk
from brms.readers.ruuvi import FABRIKANT_ID

# Ruuvi formaat 5: de officiële testvector "valid data" (zie tests/test_ruuvi.py):
# versnelling_x = 4, versnelling_y = -4, versnelling_z = 1036 mG.
RMAC = "CB:B8:33:4C:88:4F"
RUUVI = bytes.fromhex("0512FC5394C37C0004FFFC040CAC364200CDCBB8334C884F")


def _sensor(stuurboord=None, correctie=None, lijst=None) -> Sensor:
    """Een Ruuvi met (of zonder) het veld 'stuurboord'."""
    return Sensor(RMAC, "ruuvi", "hoofdkajuit", correctie or {}, lijst, stuurboord=stuurboord)


def _slagzij(stuurboord, x=0.0, y=0.0, z=1000.0):
    """De slagzij voor één stel versnellingen, of None als er geen waarde is."""
    nieuw, _ = afleiden.leid_af(
        {"versnelling_x": x, "versnelling_y": y, "versnelling_z": z}, _sensor(stuurboord))
    return nieuw.get("slagzij")


class TestSlagzij(unittest.TestCase):
    def test_recht_is_nul(self):
        self.assertEqual(_slagzij("x"), 0.0)

    def test_hoek_volgt_uit_dwars_en_z(self):
        # 10 graden helling: de zwaartekracht (1000 mG) verdeeld over de dwarsas en z.
        dwars = 1000 * math.sin(math.radians(10))
        z = 1000 * math.cos(math.radians(10))
        self.assertAlmostEqual(_slagzij("x", x=dwars, z=z), 10.0, places=2)

    def test_teken_volgt_het_veld_stuurboord(self):
        # Dezelfde stand van de sensor; het veld bepaalt welke kant stuurboord (+) is.
        self.assertGreater(_slagzij("x", x=100), 0)    # +x wijst naar stuurboord
        self.assertLess(_slagzij("-x", x=100), 0)      # +x wijst naar bakboord
        self.assertGreater(_slagzij("-y", y=-100), 0)
        self.assertLess(_slagzij("y", y=-100), 0)

    def test_alleen_de_dwarsas_telt(self):
        # Met stuurboord = "x" doet de versnelling op y (de trim) niet mee.
        self.assertEqual(_slagzij("x", x=0, y=300), 0.0)

    def test_onderste_boven_gemonteerd(self):
        # Van z telt de grootte: een sensor tegen het plafond geeft dezelfde hoek.
        self.assertEqual(_slagzij("x", x=100, z=-1000), _slagzij("x", x=100, z=1000))

    def test_zonder_veld_geen_slagzij(self):
        nieuw, regels = afleiden.leid_af(
            {"versnelling_x": 1, "versnelling_y": 2, "versnelling_z": 1000}, _sensor(None))
        self.assertEqual((nieuw, regels), ({}, []))

    def test_ontbrekende_invoer_geeft_geen_waarde(self):
        # §5.2.5 punt 5: zonder z (ongeldig uitgezonden) geen slagzij voor deze ontvangst.
        nieuw, regels = afleiden.leid_af({"versnelling_x": 1}, _sensor("x"))
        self.assertEqual(nieuw, {})
        self.assertIn("invoer ontbreekt", regels[0])

    def test_vrije_val_geeft_geen_waarde(self):
        self.assertIsNone(_slagzij("x", x=0, z=0))


class TestInDeReader(unittest.TestCase):
    """De plaats in de keten (§5.2.5 punt 4): corrigeren -> afleiden -> lijst -> publiceren."""

    def _verwerk(self, sensor, tracer=None):
        ms = verwerk(RMAC, {}, {FABRIKANT_ID: RUUVI}, -80, 1000, {RMAC: sensor}, tracer)
        return {m.grootheid: m.waarde for m in ms}

    def test_slagzij_wordt_gepubliceerd(self):
        # x = 4, z = 1036 -> atan2(4, 1036) = 0,22 graden over stuurboord.
        w = self._verwerk(_sensor("x"))
        self.assertEqual(w["slagzij"], 0.22)
        self.assertEqual(w["versnelling_x"], 4)   # de invoer gaat gewoon mee

    def test_zonder_veld_niets_extra(self):
        self.assertNotIn("slagzij", self._verwerk(_sensor(None)))

    def test_kalibratie_is_de_correctie_op_slagzij(self):
        # De boot ligt recht en de sensor geeft 0,22° sb: correctie −0,22 zet hem op nul.
        w = self._verwerk(_sensor("x", {"slagzij": Correctie(offset=-0.22)}))
        self.assertAlmostEqual(w["slagzij"], 0.0)

    def test_afleiden_uit_de_gecorrigeerde_meting(self):
        # Een correctie op de invoer werkt door in de slagzij: x = 4 + 96 = 100 mG.
        w = self._verwerk(_sensor("x", {"versnelling_x": Correctie(offset=96)}))
        self.assertEqual(w["slagzij"], round(math.degrees(math.atan2(100, 1036)), 2))

    def test_slagzij_in_de_lijst_zonder_de_versnellingen(self):
        # De lijst 'grootheden' komt ná het afleiden: slagzij kan zonder haar invoer.
        w = self._verwerk(_sensor("x", lijst=("temperatuur", "slagzij")))
        self.assertEqual(w, {"temperatuur": 24.3, "slagzij": 0.22})

    def test_trace_heeft_een_stap_afgeleid(self):
        stappen = []

        class Tracer:
            def voor(self, sensor_id):
                return lambda stap, tekst: stappen.append((stap, tekst))

        self._verwerk(_sensor("x", {"slagzij": Correctie(offset=-0.22)}), Tracer())
        self.assertEqual([s for s, _ in stappen], ["ruw", "gedecodeerd", "gecorrigeerd", "afgeleid"])
        tekst = stappen[-1][1]
        self.assertIn("atan2(versnelling_x = 4, |versnelling_z| = 1036) = 0.22 graden", tekst)
        self.assertIn("correctie: 0.22 − 0.22", tekst)


def _toml(sensor_extra: str = "", rest: str = "") -> str:
    """Een kleinst mogelijke configuratie met één Ruuvi."""
    return (f'[[sensor]]\nid = "{RMAC}"\ntype = "ruuvi"\nnaam = "hoofdkajuit"\n'
            f'{sensor_extra}\n{rest}')


def _lees(tekst: str):
    import tomllib
    return config.controleer(tomllib.loads(tekst))


def _fouten(tekst: str) -> str:
    """De foutmeldingen van check-config als één tekst; faalt als er geen fout is."""
    try:
        _lees(tekst)
    except config.ConfigFout as e:
        return "\n".join(e.fouten)
    raise AssertionError("configuratie had geweigerd moeten worden")


ALARM = ('[[alarm]]\nid = "slagzij-bb"\nnaam = "Slagzij bakboord"\nsensoren = ["hoofdkajuit"]\n'
         'grootheid = "slagzij"\nsoort = "grens"\nonder = -5.0\nduur = "15m"\nherstel = -4.0\n')


class TestConfig(unittest.TestCase):
    def test_type_levert_slagzij_alleen_met_versnellingen(self):
        # §5.2.5 punt 3: een type levert de afleiding als het alle invoer levert.
        self.assertIn("slagzij", config.LEVERT["ruuvi"])
        self.assertNotIn("slagzij", config.LEVERT["bthome"])

    def test_veld_stuurboord(self):
        s = _lees(_toml('stuurboord = "-y"\ncorrectie = { slagzij = -1.8 }')).sensoren[RMAC]
        self.assertEqual(s.stuurboord, "-y")
        self.assertIn("slagzij", config.levert(s))

    def test_zonder_veld_levert_de_sensor_geen_slagzij(self):
        s = _lees(_toml()).sensoren[RMAC]
        self.assertIsNone(s.stuurboord)
        self.assertNotIn("slagzij", config.levert(s))

    def test_verkeerde_waarde(self):
        self.assertIn("'stuurboord' moet een van", _fouten(_toml('stuurboord = "z"')))

    def test_veld_bij_een_sensor_zonder_versnellingen(self):
        tekst = ('[[sensor]]\nid = "A4:C1:38:00:00:01"\ntype = "bthome"\nnaam = "kajuit"\n'
                 'stuurboord = "x"\n')
        self.assertIn("hoort alleen bij een sensor die versnellingen levert", _fouten(tekst))

    def test_slagzij_zonder_veld_wordt_geweigerd(self):
        # In 'grootheden', in 'correctie' en in een alarmregel (§5.2.5 punt 10).
        self.assertIn("het veld 'stuurboord' ontbreekt",
                      _fouten(_toml('grootheden = ["temperatuur", "slagzij"]')))
        self.assertIn("het veld 'stuurboord' ontbreekt",
                      _fouten(_toml('correctie = { slagzij = -1.8 }')))
        self.assertIn("zolang het veld 'stuurboord' bij die sensor ontbreekt",
                      _fouten(_toml(rest=ALARM)))

    def test_alarmregel_voor_bakboord(self):
        cfg = _lees(_toml('stuurboord = "x"', ALARM))
        self.assertEqual((cfg.alarmen[0].onder, cfg.alarmen[0].herstel), (-5.0, -4.0))

    def test_voorbeeldbestand_blijft_geldig(self):
        # De uitgecommentarieerde blokken voor de slagzij in config/brms.example.toml:
        # zonder # moeten ze samen een geldige configuratie zijn.
        import os
        pad = os.path.join(config.PROJECTMAP, "config", "brms.example.toml")
        with open(pad, encoding="utf-8") as f:
            regels = f.read().split("\n")
        blokken, blok = [], []
        for regel in regels:
            if regel.startswith("# [["):
                blok = [regel[2:]]
                blokken.append(blok)
            elif blok and regel.startswith("# ") and "=" in regel and not regel.startswith("#  "):
                blok.append(regel[2:])
            else:
                blok = []
        tekst = "\n".join("\n".join(b) for b in blokken
                          if any("hoofdkajuit" in r for r in b))
        cfg = _lees(tekst)
        self.assertEqual(cfg.sensoren["C1:00:00:00:00:01"].stuurboord, "-y")
        self.assertEqual([a.id for a in cfg.alarmen], ["slagzij-sb", "slagzij-bb"])


class TestTekst(unittest.TestCase):
    """Tonen (§5.2.5 punt 10): zonder teken, met sb of bb erachter."""

    def test_zijde(self):
        self.assertEqual(grootheden.zijde("slagzij", 3.2), "sb")
        self.assertEqual(grootheden.zijde("slagzij", -1.0), "bb")
        self.assertEqual(grootheden.zijde("slagzij", 0.0), "")
        self.assertIsNone(grootheden.zijde("temperatuur", -1.0))

    def test_waardetekst(self):
        # Een gemeten slagzij staat in hele graden (v0.89), ook in een melding.
        self.assertEqual(alarms.waardetekst("slagzij", 3.2), "3° sb")
        self.assertEqual(alarms.waardetekst("slagzij", -1.0), "1° bb")
        self.assertEqual(alarms.waardetekst("slagzij", -6.7), "7° bb")
        # Een half rondt naar boven af, zoals op de webpagina (niet naar het even getal).
        self.assertEqual(alarms.waardetekst("slagzij", 2.5), "3° sb")
        # Afgerond nul heeft geen kant.
        self.assertEqual(alarms.waardetekst("slagzij", 0.0), "0°")
        self.assertEqual(alarms.waardetekst("slagzij", 0.4), "0°")
        self.assertEqual(alarms.waardetekst("slagzij", -0.4), "0°")
        # Een alarmgrens staat er zoals hij in brms.toml staat, niet afgerond.
        self.assertEqual(alarms.waardetekst("slagzij", 4.5, grens=True), "4,5° sb")
        self.assertEqual(alarms.waardetekst("temperatuur", -1.8), "-1,8 °C")   # ongewijzigd

    def test_voorwaardetekst(self):
        def regel(**grens):
            return Alarmregel(id="r", naam="r", sensoren=(RMAC,), grootheid="slagzij",
                              soort=config.GRENS, **grens)
        self.assertEqual(alarms.voorwaardetekst(regel(boven=5.0)), "boven 5° sb")
        # 'onder −5' is in woorden "meer dan 5 graden over bakboord".
        self.assertEqual(alarms.voorwaardetekst(regel(onder=-5.0)), "boven 5° bb")
        # Een grens met een decimaal blijft zo staan (v0.89): het alarm gaat af op 4,5.
        self.assertEqual(alarms.voorwaardetekst(regel(boven=4.5)), "boven 4,5° sb")


if __name__ == "__main__":
    unittest.main()
