import os
import tempfile
import tomllib
import unittest

from brms import config

VOORBEELD = os.path.join(os.path.dirname(__file__), "..", "config", "brms.example.toml")


def _toml(tekst):
    return config.controleer(tomllib.loads(tekst))


class TestConfig(unittest.TestCase):
    def test_voorbeeldbestand_is_geldig(self):
        cfg = config.lees(VOORBEELD)
        self.assertEqual(len(cfg.sensoren), 3)
        self.assertEqual(cfg.opslag.database, os.path.join(config.PROJECTMAP, "data", "brms.db"))
        self.assertEqual(cfg.mqtt, config.MqttConfig("localhost", 1883))

    def test_standaardwaarden(self):
        cfg = _toml("")
        self.assertEqual(cfg.mqtt.host, "localhost")
        self.assertEqual(cfg.opslag.database, os.path.join(config.PROJECTMAP, "data", "brms.db"))
        self.assertEqual(cfg.sensoren, {})

    def test_relatief_databasepad_vanaf_projectmap(self):
        # Een relatief pad geldt vanaf de projectmap, niet vanaf de huidige map.
        cfg = _toml('[opslag]\ndatabase = "data/test.db"\n')
        self.assertEqual(cfg.opslag.database, os.path.join(config.PROJECTMAP, "data", "test.db"))

    def test_mac_wordt_hoofdletters(self):
        cfg = _toml('[[sensor]]\nid = "a4:c1:38:0a:0b:0c"\ntype = "bthome"\nnaam = "kajuit"\n'
                    'correctie = { temperatuur = -0.3 }\n')
        s = cfg.sensoren["A4:C1:38:0A:0B:0C"]
        self.assertEqual(s.naam, "kajuit")
        self.assertEqual(s.correctie, {"temperatuur": config.Correctie(offset=-0.3)})
        self.assertIsNone(s.grootheden)  # geen lijst = alles

    def test_grootheden_en_factor(self):
        cfg = _toml('[[sensor]]\nid = "A4:C1:38:0A:0B:0C"\ntype = "bthome"\nnaam = "k"\n'
                    'grootheden = ["temperatuur", "vocht", "rssi"]\n'
                    'correctie = { vocht = { factor = 1.02, offset = -1.5 } }\n')
        s = cfg.sensoren["A4:C1:38:0A:0B:0C"]
        self.assertEqual(s.grootheden, ("temperatuur", "vocht", "rssi"))
        self.assertEqual(s.correctie["vocht"], config.Correctie(1.02, -1.5))
        self.assertEqual(s.correctie["vocht"].toepassen(50.0), 49.5)

    def test_fouten_in_grootheden_en_correctie(self):
        tekst = """
            [[sensor]]
            id = "A4:C1:38:00:00:01"
            type = "bthome"
            naam = "a"
            grootheden = ["temperatuur", "luchtdruk", "hoeksnelheid"]
            correctie = { vocht = 1, temperatuur = { factor = 0 }, rssi = { schaal = 2 } }
        """
        with self.assertRaises(config.ConfigFout) as ctx:
            _toml(tekst)
        fouten = "\n".join(ctx.exception.fouten)
        for verwacht in ["'luchtdruk' wordt door type bthome niet geleverd",
                         "onbekende grootheid 'hoeksnelheid'",
                         "'vocht' staat niet in 'grootheden'",
                         "factor 0", "onbekende sleutel 'schaal'",
                         "brms grootheden bthome"]:
            self.assertIn(verwacht, fouten)

    def test_victron_met_sleutel(self):
        cfg = _toml('[[sensor]]\nid = "d3:11:22:33:44:55"\ntype = "victron-bmv"\nnaam = "accu"\n'
                    'sleutel = "aff4d0995b7d1e176c0c33ecb9e70dcd"\n'
                    'grootheden = ["spanning", "stroom", "soc", "rssi"]\n')
        s = cfg.sensoren["D3:11:22:33:44:55"]
        self.assertEqual((s.type, s.sleutel), ("victron-bmv", "aff4d0995b7d1e176c0c33ecb9e70dcd"))

    def test_fouten_bij_de_sleutel(self):
        tekst = """
            [[sensor]]
            id = "D3:11:22:33:44:01"
            type = "victron-mppt"
            naam = "zon zonder sleutel"
            [[sensor]]
            id = "D3:11:22:33:44:02"
            type = "victron-multiplus"
            naam = "te korte sleutel"
            sleutel = "geheim123"
            grootheden = ["walstroom", "spanning"]
            [[sensor]]
            id = "A4:C1:38:00:00:01"
            type = "bthome"
            naam = "kajuit"
            sleutel = "aff4d0995b7d1e176c0c33ecb9e70dcd"
        """
        with self.assertRaises(config.ConfigFout) as ctx:
            _toml(tekst)
        fouten = "\n".join(ctx.exception.fouten)
        self.assertEqual(fouten.count("'sleutel' ontbreekt of is geen 32 hexadecimale tekens"), 2)
        self.assertIn("kajuit): 'sleutel' hoort alleen bij een Victron-apparaat", fouten)
        self.assertIn("'spanning' wordt door type victron-multiplus niet geleverd", fouten)
        self.assertNotIn("geheim123", fouten)   # een sleutel komt nooit in een melding

    def test_alle_fouten_tegelijk(self):
        tekst = """
            onzin = 1
            [mqtt]
            port = 0
            [[sensor]]
            id = "geen-mac"
            type = "bthome"
            naam = "a"
            correctie = { walstroom = 1, foo = 2, vocht = "x" }
            [[sensor]]
            id = "A4:C1:38:00:00:01"
            type = "zigbee"
            naam = "b"
            kleur = "rood"
            [[sensor]]
            id = "A4:C1:38:00:00:01"
            type = "bthome"
            naam = "c"
        """
        with self.assertRaises(config.ConfigFout) as ctx:
            _toml(tekst)
        tekst_fouten = "\n".join(ctx.exception.fouten)
        for verwacht in ["onbekende sleutel 'onzin'", "port", "MAC-adres", "'walstroom'",
                         "onbekende grootheid 'foo'", "'vocht': moet een getal", "'type'",
                         "onbekende sleutel 'kleur'", "meer dan eens"]:
            self.assertIn(verwacht, tekst_fouten)

    def test_ontbrekend_bestand(self):
        with self.assertRaises(config.ConfigFout):
            config.lees(os.path.join(tempfile.gettempdir(), "bestaat-niet-brms.toml"))

    def test_ongeldige_toml(self):
        with tempfile.TemporaryDirectory() as d:
            pad = os.path.join(d, "brms.toml")
            with open(pad, "w") as f:
                f.write("[mqtt\n")
            with self.assertRaises(config.ConfigFout):
                config.lees(pad)


if __name__ == "__main__":
    unittest.main()
