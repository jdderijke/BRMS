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
        self.assertEqual(cfg.mqtt, config.MqttConfig("localhost", 1883))

    def test_standaardwaarden(self):
        cfg = _toml("")
        self.assertEqual(cfg.mqtt.host, "localhost")
        self.assertEqual(cfg.opslag.database, "/var/lib/brms/brms.db")
        self.assertEqual(cfg.sensoren, {})

    def test_mac_wordt_hoofdletters(self):
        cfg = _toml('[[sensor]]\nid = "a4:c1:38:0a:0b:0c"\ntype = "bthome"\nnaam = "kajuit"\n'
                    'correctie = { temperatuur = -0.3 }\n')
        s = cfg.sensoren["A4:C1:38:0A:0B:0C"]
        self.assertEqual(s.naam, "kajuit")
        self.assertEqual(s.correctie, {"temperatuur": -0.3})

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
            type = "ruuvi"
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
                         "onbekende grootheid 'foo'", "'vocht' moet een getal", "'type'",
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
