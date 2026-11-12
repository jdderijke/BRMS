"""Tests van de schakelaar per alarmregel (ARCHITECTURE.md §5.5.3 punt 7), van een camera
in een regel 'stil' (§5.5.3 punt 2) en van de melding bij een fout in het logboek (§5.5.5
punt 5) – in de configuratie, de alarmlaag en de opslag, zonder broker."""

import asyncio
import json
import os
import tempfile
import tomllib
import unittest
from unittest import mock

from brms import alarms, config, storage, systeem
from brms.alarms import Alarmlaag, Gebeurtenis
from brms.config import Alarmregel, Camera, Config, MqttConfig, OpnamenConfig, OpslagConfig, Sensor
from brms.melding import Melding
from brms.meting import maak

MAC = "A4:C1:38:00:00:01"
MAC2 = "A4:C1:38:00:00:02"
KAJUIT = Sensor(MAC, "bthome", "kajuit")
VOORONDER = Sensor(MAC2, "bthome", "vooronder")
DEK = Camera("dek", "Dek", "http://camera/snap.jpg", foto_interval=180.0)

VORST = Alarmregel("vorst", "Vorst", (MAC, MAC2), "grens", "temperatuur", onder=2.0, herstel=3.0,
                   melding="herhaald", herhaling=3600.0)
VOCHT = Alarmregel("vocht", "Vocht", (MAC,), "grens", "vocht", boven=80.0)
CAMERA_STIL = Alarmregel("camera-stil", "Camera stil", ("dek",), "stil", duur=900.0,
                         melding="herhaald", herhaling=86400.0)


class NepKlok:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _laag(*regels, klok=None):
    cfg = Config(MqttConfig(), OpslagConfig("x"), {MAC: KAJUIT, MAC2: VOORONDER},
                 alarmen=tuple(regels), cameras={"dek": DEK})
    return Alarmlaag(cfg, klok=klok or NepKlok(), tijd=lambda: 1_800_000_000.4)


def _soorten(uit):
    """Wat een aanroep van de alarmlaag opleverde, kort: gebeurtenissen en meldingen."""
    return [(x.gebeurtenis, x.sensor_id) if isinstance(x, Gebeurtenis) else "melding"
            for x in uit if isinstance(x, (Gebeurtenis, Melding))]


# =============================================================================
# De schakelaar in de alarmlaag
# =============================================================================

class TestSchakelaar(unittest.TestCase):
    def test_uit_sluit_actieve_alarmen_af_zonder_herstelmelding(self):
        klok = NepKlok()
        laag = _laag(VORST, VOCHT, klok=klok)
        with self.assertLogs("brms.alarms", "INFO"):
            self.assertEqual(_soorten(laag.bij_meting(maak(1, MAC, "temperatuur", 1.0))),
                             [("begin", MAC), "melding"])
            # Uit: het alarm van de kajuit wordt afgesloten met reden 'uitgezet'. Er komt
            # geen melding: er is niets hersteld. De vooronder had geen alarm.
            uit = laag.schakel("vorst", False)
        self.assertEqual(_soorten(uit), [("uitgezet", MAC)])
        self.assertEqual(json.loads(uit[0].naar_json())["gebeurtenis"], "uitgezet")
        self.assertEqual(laag.uit, {"vorst"})
        self.assertEqual(laag.actief(), [])

    def test_uit_is_de_hele_regel(self):
        klok = NepKlok()
        laag = _laag(VORST, VOCHT, klok=klok)
        with self.assertLogs("brms.alarms", "INFO"):
            laag.schakel("vorst", False)
            # Geen beoordeling, geen melding, geen herinnering – ook niet na lange tijd.
            self.assertEqual(laag.bij_meting(maak(1, MAC, "temperatuur", -5.0)), [])
            klok.t += 10 * 3600
            self.assertEqual(laag.tik(), [])
            # Een andere regel werkt gewoon door.
            self.assertEqual(_soorten(laag.bij_meting(maak(1, MAC, "vocht", 90.0))),
                             [("begin", MAC), "melding"])

    def test_weer_aan_begint_opnieuw(self):
        klok = NepKlok()
        regel = Alarmregel("vorst", "Vorst", (MAC,), "grens", "temperatuur", onder=2.0, duur=600.0)
        laag = _laag(regel, klok=klok)
        with self.assertLogs("brms.alarms", "INFO"):
            laag.bij_meting(maak(1, MAC, "temperatuur", 1.0))       # de duur loopt
            klok.t += 500
            laag.schakel("vorst", False)
            laag.schakel("vorst", True)
            # De duur van vóór het uitzetten telt niet meer mee: opnieuw 10 minuten.
            klok.t += 200
            self.assertEqual(laag.bij_meting(maak(1, MAC, "temperatuur", 1.0)), [])
            klok.t += 599
            self.assertEqual(laag.bij_meting(maak(1, MAC, "temperatuur", 1.0)), [])
            klok.t += 1
            self.assertEqual(_soorten(laag.bij_meting(maak(1, MAC, "temperatuur", 1.0))),
                             [("begin", MAC), "melding"])

    def test_onbekende_regel_of_dezelfde_stand_doet_niets(self):
        laag = _laag(VORST)
        self.assertEqual(laag.schakel("bestaat-niet", False), [])
        self.assertEqual(laag.schakel("vorst", True), [])            # stond al aan
        self.assertEqual(laag.uit, set())

    def test_stand_uit_de_database(self):
        laag = _laag(VORST, VOCHT)
        with self.assertLogs("brms.alarms", "INFO"):
            laag.zet_uit({"vocht", "bestaat-niet-meer"})
            self.assertEqual(laag.uit, {"vocht"})
            # Een volgende stand: vocht weer aan, vorst uit.
            laag.zet_uit({"vorst"})
        self.assertEqual(laag.uit, {"vorst"})

    def test_lees_schakel(self):
        self.assertEqual(alarms.lees_schakel(b'{"alarm_id": "vorst", "aan": false}'), ("vorst", False))
        for ongeldig in (b"geen json", b"[]", b'{"aan": true}', b'{"alarm_id": "", "aan": true}',
                         b'{"alarm_id": "vorst"}', b'{"alarm_id": "vorst", "aan": 1}'):
            with self.assertRaises(ValueError, msg=ongeldig):
                alarms.lees_schakel(ongeldig)


# =============================================================================
# De schakelaar in de opslag (tabel alarm_uit)
# =============================================================================

class TestOpslag(unittest.TestCase):
    def setUp(self):
        self._map = tempfile.TemporaryDirectory()
        self.pad = os.path.join(self._map.name, "brms.db")

    def tearDown(self):
        self._map.cleanup()

    def test_tabel_alarm_uit(self):
        # Zonder database is er geen stand te lezen (None: de alarmlaag laat alles zoals het is).
        self.assertIsNone(alarms.lees_alarm_uit(self.pad))
        db = storage.Database(self.pad)
        self.assertEqual(alarms.lees_alarm_uit(self.pad), set())
        db.alarm_schakel(100, "vorst", False)
        db.alarm_schakel(200, "vorst", False)        # nog eens uit: het eerste tijdstip blijft
        db.alarm_schakel(300, "vocht", False)
        self.assertEqual(alarms.lees_alarm_uit(self.pad), {"vorst", "vocht"})
        self.assertEqual(db._con.execute("SELECT sinds FROM alarm_uit WHERE alarm_id = 'vorst'").fetchone(),
                         (100,))
        db.alarm_schakel(400, "vorst", True)
        db.alarm_schakel(500, "vorst", True)         # nog eens aan: geen fout
        self.assertEqual(alarms.lees_alarm_uit(self.pad), {"vocht"})
        db.sluit()

    def test_opdracht_van_het_web(self):
        """De opslag-service legt de stand vast, ook als de klok nog niet gelijk staat."""
        class NepBus:
            def __init__(self):
                self.wachtrijen = {}

            def wachtrij(self, topic, qos):
                self.wachtrijen[topic] = asyncio.Queue()
                return self.wachtrijen[topic]

            def abonneer(self, topic, qos, callback):
                pass

            def publiceer(self, topic, payload, qos=0):
                return True

        cfg = Config(MqttConfig(), OpslagConfig(self.pad), {MAC: KAJUIT}, alarmen=(VORST, VOCHT),
                     opnames=OpnamenConfig(os.path.join(self._map.name, "opnames")))

        async def proef():
            bus, stop = NepBus(), asyncio.Event()
            taak = asyncio.create_task(storage.draai(cfg, bus, stop, klok_ok=lambda: False))
            await asyncio.sleep(0.1)
            q = bus.wachtrijen[storage.TOPIC_SCHAKEL]
            for inhoud in (b'{"alarm_id": "vorst", "aan": false}', b'{"alarm_id": "vocht", "aan": false}',
                           b'{"alarm_id": "vocht", "aan": true}',
                           b'{"alarm_id": "onbekend", "aan": false}', b"kapot"):
                q.put_nowait((storage.TOPIC_SCHAKEL, inhoud))
            await asyncio.sleep(0.5)
            stop.set()
            await asyncio.wait_for(taak, 5)

        with self.assertLogs("brms.storage", "INFO") as log:
            asyncio.run(proef())
        self.assertEqual(alarms.lees_alarm_uit(self.pad), {"vorst"})
        tekst = "\n".join(log.output)
        self.assertIn("alarmregel vorst is uit gezet", tekst)
        self.assertIn("onbekende alarmregel: 'onbekend'", tekst)
        self.assertIn("geen geldige JSON", tekst)


# =============================================================================
# Camera in een regel 'stil'
# =============================================================================

def _toml(tekst):
    return config.controleer(tomllib.loads(tekst))


class TestCameraStil(unittest.TestCase):
    BASIS = """
        [[sensor]]
        id = "A4:C1:38:00:00:01"
        type = "bthome"
        naam = "kajuit"

        [[camera]]
        id = "dek"
        naam = "Dekcamera"
        foto_url = "http://camera/snap.jpg"
        foto_interval = "3m"
    """

    def test_configuratie(self):
        cfg = _toml(self.BASIS + """
            [[alarm]]
            id = "stil"
            sensoren = ["kajuit", "dek"]
            soort = "stil"
            duur = "30m"

            [[alarm]]
            id = "camera-stil"
            sensoren = ["dekcamera"]
            soort = "stil"
            duur = "15m"
        """)
        # Een camera mag op ID of op naam, ook samen met sensoren in één regel.
        self.assertEqual([r.sensoren for r in cfg.alarmen], [(MAC, "dek"), ("dek",)])

    def test_fouten(self):
        for regel, verwacht in [
                # Een camera levert geen metingen: alleen 'stil' kan.
                ('sensoren = ["dek"]\ngrootheid = "temperatuur"\nsoort = "grens"\nonder = 2',
                 "'dek' is een camera; een camera kan alleen in een regel van soort 'stil' staan"),
                # De duur moet langer zijn dan het foto-interval (3 minuten).
                ('sensoren = ["dek"]\nsoort = "stil"\nduur = "3m"',
                 "'duur' moet langer zijn dan het foto-interval van camera dek (180 s)"),
                ('sensoren = ["dek", "dek"]\nsoort = "stil"\nduur = "15m"', "staat meer dan eens"),
                ('sensoren = ["mast"]\nsoort = "stil"\nduur = "15m"', "onbekende sensor 'mast'")]:
            with self.assertRaises(config.ConfigFout, msg=regel) as ctx:
                _toml(self.BASIS + '[[alarm]]\nid = "x"\n' + regel)
            self.assertIn(verwacht, "\n".join(ctx.exception.fouten))

    def test_opname_is_het_levensteken(self):
        klok = NepKlok()
        laag = _laag(CAMERA_STIL, klok=klok)
        with self.assertLogs("brms.alarms", "INFO"):
            klok.t += 600
            self.assertEqual(laag.bij_levensteken("dek"), [])        # een intervalfoto
            klok.t += 899
            self.assertEqual(laag.tik(), [])
            klok.t += 1
            # 15 minuten geen opname: alarm, met een tekst die bij een camera past.
            uit = laag.tik()
            self.assertEqual(_soorten(uit), [("begin", "dek"), "melding"])
            self.assertIn("Camera stil – Dek: geen opnames ontvangen gedurende 15 min", uit[1].titel)
            # De volgende geslaagde opname is het herstel.
            klok.t += 3600
            uit = laag.bij_levensteken("dek")
        self.assertEqual(_soorten(uit), [("herstel", "dek"), "melding"])
        self.assertIn("levert weer opnames", uit[1].titel)

    def test_levensteken_van_iets_anders_telt_niet(self):
        laag = _laag(VORST, CAMERA_STIL)
        # Een camera zonder regel, of een sensor-ID: er gebeurt niets (en een grensregel
        # wordt er niet door beoordeeld).
        self.assertEqual(laag.bij_levensteken("mast"), [])
        self.assertEqual(laag.bij_levensteken(MAC), [])


# =============================================================================
# De service: schakelen via de bus, opnames als levensteken, fouten uit het logboek
# =============================================================================

class NepBus:
    verbonden = True

    def __init__(self):
        self.berichten = []          # (topic, inhoud als dict)
        self.callbacks = {}
        self.q = None

    def wachtrij(self, topic, qos):
        self.q = asyncio.Queue()
        return self.q

    def abonneer(self, topic, qos, callback):
        self.callbacks[topic] = callback

    def publiceer(self, topic, payload, qos=0):
        self.berichten.append((topic, json.loads(payload)))
        return True


class TestDienst(unittest.TestCase):
    def _draai(self, cfg, stappen, logregels=()):
        """Draait de alarmlaag kort; `stappen(bus)` is een coroutine met wat er gebeurt.
        `logregels` komen binnen alsof journald ze meldde. Geeft (bus, verstuurde meldingen)."""
        verstuurd = []
        bus = NepBus()

        async def nep_volg(bij_regel, stop, opnieuw=10.0):
            for regel in logregels:
                bij_regel(regel)
            await stop.wait()

        async def proef():
            stop = asyncio.Event()
            taak = asyncio.create_task(alarms.draai(cfg, bus, stop))
            await asyncio.sleep(0.1)
            await stappen(bus)
            await asyncio.sleep(1.3)
            stop.set()
            await asyncio.wait_for(taak, 5)

        from brms import melding
        with mock.patch.object(melding, "kanalen", lambda c: {"Nep": verstuurd.append}), \
                mock.patch.object(systeem, "volg", nep_volg), \
                self.assertLogs("brms", "INFO"):
            asyncio.run(proef())
        return bus, verstuurd

    def test_schakelen_via_de_bus(self):
        cfg = Config(MqttConfig(), OpslagConfig("x"), {MAC: KAJUIT}, alarmen=(VOCHT,))

        async def stappen(bus):
            bus.q.put_nowait(("t", maak(1, MAC, "vocht", 90.0).naar_json()))
            await asyncio.sleep(0.3)
            # Het web zet de regel uit (in het echt vanuit de thread van paho).
            bus.callbacks[alarms.TOPIC_SCHAKEL](alarms.TOPIC_SCHAKEL, b'{"alarm_id": "vocht", "aan": false}')
            bus.callbacks[alarms.TOPIC_SCHAKEL](alarms.TOPIC_SCHAKEL, b"kapot")

        bus, verstuurd = self._draai(cfg, stappen)
        gebeurtenissen = [b[1]["gebeurtenis"] for b in bus.berichten if b[0] == "brms/alarm/vocht"]
        self.assertEqual(gebeurtenissen, ["begin", "uitgezet"])
        # Eén melding (het begin); het uitzetten geeft geen herstelmelding.
        self.assertEqual(len(verstuurd), 1)

    def test_begint_met_de_stand_uit_de_database(self):
        with tempfile.TemporaryDirectory() as map_:
            pad = os.path.join(map_, "brms.db")
            db = storage.Database(pad)
            db.alarm_schakel(1, "vocht", False)
            db.sluit()
            cfg = Config(MqttConfig(), OpslagConfig(pad), {MAC: KAJUIT}, alarmen=(VOCHT,))

            async def stappen(bus):
                bus.q.put_nowait(("t", maak(1, MAC, "vocht", 90.0).naar_json()))

            bus, verstuurd = self._draai(cfg, stappen)
        # De regel staat uit: geen alarm en geen melding.
        self.assertEqual([b for b in bus.berichten if b[0] == "brms/alarm/vocht"], [])
        self.assertEqual(verstuurd, [])

    def test_opname_op_de_bus_is_het_levensteken(self):
        regel = Alarmregel("camera-stil", "Camera stil", ("dek",), "stil", duur=0.6)
        cfg = Config(MqttConfig(), OpslagConfig("x"), {}, alarmen=(regel,), cameras={"dek": DEK})

        async def stappen(bus):
            await asyncio.sleep(1.2)         # langer dan de duur: het alarm gaat af
            bus.callbacks["brms/opname/#"]("brms/opname/dek", json.dumps({
                "ts": 1, "camera_id": "dek", "type": "foto", "bestand": "dek/2026/11/12/120000.jpg",
                "bytes": 9, "aanleiding": "interval"}).encode())

        bus, verstuurd = self._draai(cfg, stappen)
        gebeurtenissen = [b[1]["gebeurtenis"] for b in bus.berichten if b[0] == "brms/alarm/camera-stil"]
        # (Daarna blijft de camera in deze test weer stil; wat dan volgt, telt hier niet.)
        self.assertEqual(gebeurtenissen[:2], ["begin", "herstel"])
        self.assertIn("geen opnames", verstuurd[0].titel)
        self.assertIn("levert weer opnames", verstuurd[1].titel)

    def test_fout_in_het_logboek_geeft_een_melding(self):
        cfg = Config(MqttConfig(), OpslagConfig("x"), {MAC: KAJUIT})

        async def stappen(bus):
            pass

        logregels = [
            {"service": "brms-opslag", "niveau": "waarschuwing", "tekst": "klok niet gesynchroniseerd"},
            {"service": "brms-opslag", "niveau": "fout", "tekst": "schrijven naar database mislukt"},
            {"service": "brms-opslag", "niveau": "fout", "tekst": "nog een keer"},
            {"service": "kernel", "niveau": "fout", "tekst": "I/O error, dev sda"}]
        bus, verstuurd = self._draai(cfg, stappen, logregels)
        # Een waarschuwing geeft geen melding; van de opslag komt er één per uur.
        self.assertEqual([m.titel for m in verstuurd],
                         ["🛑 Fout in brms-opslag: schrijven naar database mislukt",
                          "🛑 Fout in kernel: I/O error, dev sda"])
        self.assertEqual({m.prioriteit for m in verstuurd}, {"hoog"})

    def test_systeemfouten_uit(self):
        self.assertFalse(_toml("[melding]\nsysteemfouten = false\n").melding.systeemfouten)
        self.assertTrue(_toml("").melding.systeemfouten)             # standaard aan
        with self.assertRaises(config.ConfigFout) as ctx:
            _toml('[melding]\nsysteemfouten = "ja"\n')
        self.assertIn("'systeemfouten' moet true of false zijn", ctx.exception.fouten[0])

        cfg = Config(MqttConfig(), OpslagConfig("x"), {MAC: KAJUIT},
                     melding=config.MeldingConfig(systeemfouten=False))

        async def stappen(bus):
            pass

        _, verstuurd = self._draai(cfg, stappen, [
            {"service": "kernel", "niveau": "fout", "tekst": "I/O error"}])
        self.assertEqual(verstuurd, [])


if __name__ == "__main__":
    unittest.main()
