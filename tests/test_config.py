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
        # De alarmregels van §5.5.2 die bij de drie voorbeeldsensoren passen.
        self.assertEqual([r.id for r in cfg.alarmen],
                         ["vorst", "vocht", "sensor-stil", "sensorbatterij"])
        self.assertFalse(cfg.melding.ingesteld)   # de kanalen staan uitgecommentarieerd
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

    def test_shelly(self):
        cfg = _toml('[[sensor]]\nid = "shelly-bilge"\ntype = "shelly-uni"\nnaam = "bilge"\n'
                    'adres = "192.168.1.50"\ninterval = 2\ndrempel = 6\nomgekeerd = ["bilge"]\n'
                    'bron = { bilge = "input:0", bilgepomp = "voltmeter:100" }\n')
        s = cfg.sensoren["shelly-bilge"]   # het ID blijft zoals het getypt is
        self.assertEqual((s.adres, s.interval, s.drempel, s.omgekeerd),
                         ("192.168.1.50", 2.0, 6.0, ("bilge",)))
        self.assertEqual(s.bron, {"bilge": "input:0", "bilgepomp": "voltmeter:100"})
        self.assertEqual(config.levert(s), ("bilge", "bilgepomp", "rssi"))

        # Alleen de vlotter: standaardinterval, geen drempel nodig, en de pagina biedt
        # alleen aan wat een bron heeft.
        cfg = _toml('[[sensor]]\nid = "shelly-bilge"\ntype = "shelly-uni"\nnaam = "bilge"\n'
                    'adres = "shelly-bilge.lan:80"\nbron = { bilge = "input:1" }\n')
        s = cfg.sensoren["shelly-bilge"]
        self.assertEqual((s.interval, s.drempel), (30.0, None))
        self.assertEqual(config.levert(s), ("bilge", "rssi"))

    def test_fouten_bij_een_shelly(self):
        tekst = """
            [[sensor]]
            id = "shelly bilge"
            type = "shelly-uni"
            naam = "zonder adres en bron"
            interval = 0
            [[sensor]]
            id = "shelly-2"
            type = "shelly-uni"
            naam = "verkeerde bron"
            adres = "http://192.168.1.50/"
            bron = { bilge = "switch:0", bilgepomp = "voltmeter:100", temperatuur = "input:0" }
            omgekeerd = ["walstroom"]
            [[sensor]]
            id = "shelly-3"
            type = "shelly-uni"
            naam = "drempel zonder voltmeter"
            adres = "192.168.1.51"
            bron = { bilge = "input:0" }
            drempel = 6.0
            grootheden = ["bilge", "bilgepomp"]
            [[sensor]]
            id = "A4:C1:38:00:00:01"
            type = "bthome"
            naam = "kajuit"
            adres = "192.168.1.52"
        """
        with self.assertRaises(config.ConfigFout) as ctx:
            _toml(tekst)
        fouten = "\n".join(ctx.exception.fouten)
        for verwacht in ["'id' mag alleen letters, cijfers",
                         "zonder adres en bron): 'adres' ontbreekt",
                         "'interval' moet een getal tussen 1 en 3600",
                         "zonder adres en bron): 'bron' ontbreekt",
                         "verkeerde bron): 'adres' ontbreekt of is geen IP-adres",
                         "bron voor 'bilge' moet \"input:<nummer>\"",
                         "bron voor 'temperatuur', maar een Shelly levert alleen",
                         "'drempel' ontbreekt",
                         "'walstroom' staat in 'omgekeerd' maar heeft geen 'bron'",
                         "'drempel' heeft alleen betekenis bij een voltmeter-bron",
                         "'bilgepomp' staat in 'grootheden' maar heeft geen 'bron'",
                         "kajuit): 'adres' hoort alleen bij een Shelly"]:
            self.assertIn(verwacht, fouten)

    SENSOREN = ('[[sensor]]\nid = "A4:C1:38:00:00:01"\ntype = "bthome"\nnaam = "kajuit"\n'
                '[[sensor]]\nid = "A4:C1:38:00:00:02"\ntype = "bthome"\nnaam = "vooronder"\n'
                'grootheden = ["temperatuur"]\n'
                '[[sensor]]\nid = "CB:B8:33:4C:88:4F"\ntype = "ruuvi"\nnaam = "motorruim"\n')

    def test_tijdsduur(self):
        self.assertEqual([config.duur(t) for t in ("90s", "15m", "6u", "7d", " 2 u ", "0,5m")],
                         [90, 900, 21600, 604800, 7200, 30])
        for fout in ("15", "15 min", "m", "-5m", 900, None, ""):
            with self.assertRaises(ValueError):
                config.duur(fout)

    def test_alarmregels(self):
        cfg = _toml(self.SENSOREN + """
            [[alarm]]
            id = "vorst"
            naam = "Vorst"
            sensoren = ["kajuit", "a4:c1:38:00:00:02"]
            grootheid = "temperatuur"
            soort = "grens"
            onder = 2
            duur = "15m"
            herstel = 3.0
            herhaling = "12u"
            prioriteit = "hoog"
            [[alarm]]
            id = "beweging"
            sensoren = ["motorruim"]
            grootheid = "beweging"
            soort = "aantal"
            aantal = 5
            venster = "60s"
            herstelduur = "10m"
            melding = "uit"
            [[alarm]]
            id = "stil"
            sensoren = ["kajuit"]
            soort = "stil"
            duur = "30m"
            herstelmelding = false
        """)
        vorst, beweging, stil = cfg.alarmen
        # Sensoren op naam of ID; de regel onthoudt de ID's.
        self.assertEqual(vorst.sensoren, ("A4:C1:38:00:00:01", "A4:C1:38:00:00:02"))
        self.assertEqual((vorst.onder, vorst.boven, vorst.herstel, vorst.duur), (2.0, None, 3.0, 900))
        # 'herhaling' zonder 'melding' betekent herhaald.
        self.assertEqual((vorst.melding, vorst.herhaling, vorst.prioriteit), ("herhaald", 43200, "hoog"))
        self.assertEqual((beweging.naam, beweging.aantal, beweging.venster, beweging.herstelduur,
                          beweging.melding), ("beweging", 5, 60, 600, "uit"))
        self.assertEqual((stil.grootheid, stil.duur, stil.melding, stil.herstelmelding),
                         (None, 1800, "eenmalig", False))

    def test_fouten_in_alarmregels(self):
        with self.assertRaises(config.ConfigFout) as ctx:
            _toml(self.SENSOREN + """
                [[alarm]]
                id = "a"
                sensoren = ["salon", "kajuit", "kajuit"]
                grootheid = "temperatuur"
                soort = "grens"
                onder = 2
                boven = 30
                duur = "15 minuten"
                aantal = 3
                [[alarm]]
                id = "a"
                sensoren = ["kajuit"]
                grootheid = "luchtdruk"
                soort = "grens"
                onder = 2
                herstel = 1
                melding = "herhaald"
                kleur = "rood"
                [[alarm]]
                id = "b"
                sensoren = ["vooronder"]
                grootheid = "vocht"
                soort = "aantal"
                aantal = 0
                herhaling = "10s"
                melding = "eenmalig"
                [[alarm]]
                id = "c"
                sensoren = ["kajuit"]
                soort = "stil"
                grootheid = "temperatuur"
                prioriteit = "paniek"
                herstelmelding = "ja"
                actie = [{ type = "video" }]
                [[alarm]]
                id = "met spatie"
                sensoren = []
                soort = "drempel"
            """)
        fouten = "\n".join(ctx.exception.fouten)
        for verwacht in ["(a): onbekende sensor 'salon'",
                         "sensor 'kajuit' staat meer dan eens",
                         "geef precies één van 'boven' of 'onder'",
                         "'duur' moet een tijdsduur zijn",
                         "'aantal' hoort niet bij soort 'grens'",
                         "id a komt meer dan eens voor",
                         "sensor 'kajuit' levert geen 'luchtdruk'",
                         "'herstel' moet gelijk aan of hoger dan 'onder' zijn",
                         "bij melding 'herhaald' hoort een 'herhaling'",
                         "onbekende sleutel 'kleur'",
                         # vooronder heeft alleen 'temperatuur' in zijn lijst 'grootheden'
                         "(b): sensor 'vooronder' levert geen 'vocht'",
                         "'aantal' moet een geheel getal van 1 of meer zijn",
                         "(b): 'venster' ontbreekt",
                         "soort 'aantal' kan alleen bij een toename of een toestand",
                         "'herhaling' heeft alleen betekenis bij melding 'herhaald'",
                         "bij soort 'stil' hoort geen 'grootheid'",
                         "(c): 'duur' ontbreekt",
                         "'prioriteit' moet een van urgent, hoog, normaal zijn",
                         "'herstelmelding' moet true of false zijn",
                         "'foto' of 'actie' gevraagd, maar er is geen [[camera]] ingesteld",
                         "nr. 5: 'id' ontbreekt of bevat andere tekens",
                         "'soort' moet een van grens, aantal, stil zijn"]:
            self.assertIn(verwacht, fouten)

    def test_meldingskanalen(self):
        cfg = _toml("""
            [melding.telegram]
            token = "123:abc"
            chat = -100200300
            [melding.email]
            server = "smtp.voorbeeld.nl"
            gebruiker = "boot@voorbeeld.nl"
            wachtwoord = "geheim"
            aan = "ik@voorbeeld.nl"
        """)
        self.assertEqual(cfg.melding.telegram, config.TelegramConfig("123:abc", "-100200300"))
        # Standaard poort 587, afzender = gebruiker, één ontvanger mag als tekst.
        self.assertEqual(cfg.melding.email, config.EmailConfig(
            "smtp.voorbeeld.nl", 587, "boot@voorbeeld.nl", "geheim", "boot@voorbeeld.nl",
            ("ik@voorbeeld.nl",)))
        self.assertTrue(cfg.melding.ingesteld)
        self.assertFalse(_toml("").melding.ingesteld)

    def test_fouten_in_meldingskanalen(self):
        with self.assertRaises(config.ConfigFout) as ctx:
            _toml("""
                [melding]
                sms = 1
                [melding.telegram]
                token = "GEHEIME-token"
                [melding.email]
                server = "smtp.voorbeeld.nl"
                gebruiker = "boot"
                wachtwoord = "GEHEIM-wachtwoord"
                poort = 0
                aan = ["geen adres"]
            """)
        fouten = "\n".join(ctx.exception.fouten)
        for verwacht in ["[melding]: onbekende sleutel 'sms'", "[melding.telegram]: 'chat' ontbreekt",
                         "'poort' moet een geheel getal", "'aan' moet een e-mailadres zijn",
                         "'van' moet een e-mailadres zijn"]:
            self.assertIn(verwacht, fouten)
        # Geheimen komen nooit in een melding.
        self.assertNotIn("GEHEIM", fouten)

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

    def test_camera_en_opnames(self):
        cfg = _toml("""
            [opnames]
            map = "data/fotos"
            foto_bewaren = "7d"
            video_ruimte = 5

            [[camera]]
            id = "dek"
            naam = "Dek"
            gebruiker = "brms"
            wachtwoord = "geheim"
            foto_url = "http://192.168.1.20:89/snap?usr={gebruiker}&pwd={wachtwoord}"
            video_url = "rtsp://{gebruiker}:{wachtwoord}@192.168.1.20:89/videoMain"
            foto_interval = "1m"

            [[camera]]
            id = "kajuit-cam"
            foto_url = "https://camera.local/snap.jpg"
        """)
        dek, kajuit = cfg.cameras["dek"], cfg.cameras["kajuit-cam"]
        self.assertEqual((dek.naam, dek.gebruiker, dek.wachtwoord, dek.foto_interval, dek.video_duur),
                         ("Dek", "brms", "geheim", 60.0, 120.0))
        self.assertTrue(dek.video_url.startswith("rtsp://"))
        # Zonder naam geldt het ID; zonder account en zonder video mag ook.
        self.assertEqual((kajuit.naam, kajuit.gebruiker, kajuit.video_url, kajuit.foto_interval),
                         ("kajuit-cam", None, None, 180.0))
        self.assertEqual(cfg.opnames, config.OpnamenConfig(
            os.path.join(config.PROJECTMAP, "data", "fotos"), 7 * 86400.0, 5.0))

    _MET_CAMERA = """
        [[sensor]]
        id = "A4:C1:38:00:00:01"
        type = "bthome"
        naam = "kajuit"

        [[camera]]
        id = "dek"
        foto_url = "http://camera/snap.jpg"
        video_url = "rtsp://camera/video"

        [[camera]]
        id = "kajuit-cam"
        foto_url = "http://camera2/snap.jpg"

        [[alarm]]
        sensoren = ["kajuit"]
        grootheid = "temperatuur"
        soort = "grens"
        onder = 2.0
    """

    def test_foto_en_acties_bij_een_alarm(self):
        cfg = _toml(self._MET_CAMERA + """
            id = "vorst"
            foto = "elke"
            herhaling = "1u"
            actie = [{ type = "video" }, { type = "foto" }]

            [[alarm]]
            id = "vocht"
            sensoren = ["kajuit"]
            grootheid = "vocht"
            soort = "grens"
            boven = 85
            foto = "begin"
            camera = "kajuit-cam"

            [[alarm]]
            id = "stil"
            sensoren = ["kajuit"]
            soort = "stil"
            duur = "30m"
        """)
        vorst, vocht, stil = cfg.alarmen
        # Zonder 'camera' geldt de eerste camera uit de configuratie.
        self.assertEqual((vorst.foto, vorst.acties, vorst.camera), ("elke", ("video", "foto"), "dek"))
        self.assertEqual((vocht.foto, vocht.acties, vocht.camera), ("begin", (), "kajuit-cam"))
        self.assertEqual((stil.foto, stil.acties, stil.camera), (None, (), None))

    def test_fouten_in_foto_en_acties(self):
        for regel, verwacht in [
                ('foto = "altijd"', "'foto' moet \"geen\", \"begin\" of \"elke\" zijn"),
                ('foto = "geen"\ncamera = "dek"', "'camera' zonder 'foto' of 'actie' doet niets"),
                ('foto = "begin"\nmelding = "uit"', "'foto' gaat mee met de melding"),
                ('actie = { type = "video" }', "'actie' moet een lijst van tabellen zijn"),
                ('actie = [{ type = "relais", relais = "kachel" }]', "actie 'relais' is nog niet gebouwd"),
                ('actie = [{ type = "sirene" }]', "actie 'type' moet een van video, foto zijn"),
                ('actie = [{ type = "foto" }, { type = "foto" }]', "staat er meer dan eens"),
                ('actie = [{ type = "foto", kleur = "rood" }]', "onbekende sleutel 'kleur'"),
                ('camera = "dek"', "'camera' zonder 'foto' of 'actie' doet niets"),
                ('foto = "begin"\ncamera = "mast"', "onbekende camera 'mast'; ingesteld: dek, kajuit-cam"),
                # De tweede camera heeft geen videostroom.
                ('actie = [{ type = "video" }]\ncamera = "kajuit-cam"', "heeft geen 'video_url'")]:
            with self.assertRaises(config.ConfigFout, msg=regel) as ctx:
                _toml(self._MET_CAMERA + 'id = "vorst"\n' + regel)
            self.assertIn(verwacht, "\n".join(ctx.exception.fouten))

    def test_live_beeld(self):
        cfg = _toml("""
            [[camera]]
            id = "dek"
            gebruiker = "brms"
            wachtwoord = "geheim"
            foto_url = "http://camera/snap?usr={gebruiker}&pwd={wachtwoord}"
            live_url = "http://camera/stream?usr={gebruiker}&pwd={wachtwoord}"
            live_beelden = 2

            [[camera]]
            id = "mast"
            foto_url = "http://camera2/snap.jpg"
            live_url = "http://camera2/stream"

            [[camera]]
            id = "kajuit-cam"
            foto_url = "http://camera3/snap.jpg"
        """)
        dek, mast, kajuit = cfg.cameras.values()
        self.assertEqual((dek.live_url.startswith("http://camera/stream"), dek.live_beelden), (True, 2.0))
        self.assertEqual((mast.live_url, mast.live_beelden), ("http://camera2/stream", 5.0))
        self.assertIsNone(kajuit.live_url)
        self.assertNotIn("stream", repr(dek))          # ook dit adres niet in de repr

    def test_fouten_in_live_beeld(self):
        for regels, verwacht in [
                ('live_url = "rtsp://camera/video"', "'live_url' moet een adres zijn dat begint met http://"),
                ('live_url = "http://camera/stream"\nlive_beelden = 0', "'live_beelden' moet een getal tussen"),
                ('live_url = "http://camera/stream"\nlive_beelden = "5"', "'live_beelden' moet een getal tussen"),
                ('live_beelden = 5', "'live_beelden' zonder 'live_url' doet niets"),
                ('live_url = "http://camera/stream?pwd={wachtwoord}"', "maar 'gebruiker' en 'wachtwoord' ontbreken")]:
            with self.assertRaises(config.ConfigFout, msg=regels) as ctx:
                _toml('[[camera]]\nid = "dek"\nfoto_url = "http://camera/snap.jpg"\n' + regels)
            self.assertIn(verwacht, "\n".join(ctx.exception.fouten))

    def test_zonder_camera(self):
        cfg = _toml("")
        self.assertEqual(cfg.cameras, {})
        self.assertEqual(cfg.opnames.map, os.path.join(config.PROJECTMAP, "data", "opnames"))
        self.assertEqual((cfg.opnames.foto_bewaren, cfg.opnames.video_ruimte), (14 * 86400.0, 20.0))

    def test_fouten_in_camera_en_opnames(self):
        tekst = """
            [opnames]
            foto_bewaren = "14"
            video_ruimte = 0
            kleur = "rood"

            [[camera]]
            id = "dek"
            foto_url = "http://camera/snap?pwd=GEHEIM-in-het-adres {wachtwoord}"

            [[camera]]
            id = "dek"
            foto_url = "ftp://camera/snap.jpg"
            video_url = "http://camera/video"
            wachtwoord = "w"
            foto_interval = "1s"

            [[camera]]
            id = "voor dek"
            foto_url = "http://camera/snap?pwd={wachtwoord}"
        """
        with self.assertRaises(config.ConfigFout) as ctx:
            _toml(tekst)
        fouten = "\n".join(ctx.exception.fouten)
        for verwacht in ["'foto_bewaren' moet een tijdsduur", "'video_ruimte'",
                         "onbekende sleutel 'kleur'", "zonder spaties", "'foto_url' moet een adres",
                         "'video_url' moet een adres", "horen bij elkaar",
                         "'foto_interval' moet tussen", "'id' ontbreekt of bevat",
                         "maar 'gebruiker' en 'wachtwoord' ontbreken"]:
            self.assertIn(verwacht, fouten)
        # Een adres komt nooit in een foutmelding: er kan een wachtwoord in staan.
        self.assertNotIn("GEHEIM", fouten)

    def test_camera_id_botst_niet_met_sensor_of_camera(self):
        with self.assertRaises(config.ConfigFout) as ctx:
            _toml("""
                [[sensor]]
                id = "shelly-bilge"
                type = "shelly-uni"
                naam = "bilge"
                adres = "192.168.1.50"
                bron = { bilge = "input:0" }

                [[camera]]
                id = "shelly-bilge"
                foto_url = "http://camera/a.jpg"

                [[camera]]
                id = "dek"
                foto_url = "http://camera/a.jpg"

                [[camera]]
                id = "dek"
                foto_url = "http://camera/b.jpg"
            """)
        self.assertEqual(sum("komt meer dan eens voor" in f for f in ctx.exception.fouten), 2)

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
