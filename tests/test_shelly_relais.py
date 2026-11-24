"""Tests van het relais (ARCHITECTURE.md §5.2.6) – zonder plug, zonder netwerk en zonder
broker.

Vier delen, zoals de bouw:
- de decoder en de schakelopdracht (brms/readers/shelly_relais.py);
- het pollen en schakelen door de Shelly-reader (brms/readers/shelly.py);
- de actie `relais` en de velden `voorwaarde`, `veilige_stand` en `controls` in de
  configuratie (brms/config.py);
- de verzoeken van de alarmlaag aan de schakellaag (brms/alarms.py, §5.8 punt 3 en 8):
  een regel vraagt om zijn relais zolang het alarm actief is en de sensor waarden levert.

Het beslissen zelf (de voorwaarde, de hand, de herhaling, de veilige stand) zit sinds
v0.29.0 in de schakellaag en wordt getest in tests/test_schakel.py.

Het antwoord van de plug in deze tests is overgenomen van de echte Shelly Plug M Gen3
(§5.2.6, "Bevindingen met de plug"); het ophalen en het schakelen zelf zijn vervangen door
functies die zo'n antwoord teruggeven.
"""

import asyncio
import json
import os
import tempfile
import unittest

from brms import config, grootheden
from brms import alarms, schakel
from brms.alarms import RELAIS_DUUR, Alarmlaag
from brms.config import Alarmregel, Config, Correctie, MqttConfig, OpslagConfig, Sensor
from brms.meting import maak
from brms.readers import shelly, shelly_relais

MAC = "A4:C1:38:00:00:01"
MAC2 = "A4:C1:38:00:00:02"
MULTI = "D3:00:00:00:00:03"
# De plug van de kachel; hij mag alleen aan als de MultiPlus walstroom meldt (§5.2.6 punt 5).
KACHEL = Sensor("kachel", "shelly-relais", "kachel", adres="192.168.1.121", interval=30.0,
                voorwaarde=((MULTI, "walstroom", 1.0),), controls=("relais",))
# Een relais met de veilige stand aan (§5.8 punt 2), zoals later dat van de walstroom.
WAL = Sensor("wal", "shelly-relais", "walrelais", adres="192.168.1.123", interval=30.0,
             veilige_stand="aan", controls=("relais",))


def _status(aan=False, vermogen=0.0, spanning=221.3, stroom=0.0, rssi=-61) -> dict:
    """Een antwoord van Shelly.GetStatus van de plug, met ook wat BRMS niet gebruikt."""
    return {"ble": {}, "cloud": {"connected": False},
            "switch:0": {"id": 0, "source": "HTTP_in", "output": aan, "apower": vermogen,
                         "voltage": spanning, "freq": 50.0, "current": stroom,
                         "aenergy": {"total": 12.5, "by_minute": [0.0, 0.0, 0.0]},
                         "temperature": {"tC": 37.3, "tF": 99.1}},
            "sys": {"mac": "083AF2CF9D7C", "uptime": 11081},
            "wifi": {"sta_ip": "192.168.1.121", "status": "got ip", "ssid": "boot", "rssi": rssi}}


class NepBus:
    verbonden = True

    def __init__(self):
        self.metingen, self.berichten = [], []

    def publiceer_meting(self, m):
        self.metingen.append(m)
        return True

    def publiceer(self, topic, payload, qos=0):
        self.berichten.append((topic, json.loads(payload)))
        return True


class NepKlok:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


# =============================================================================
# De decoder en de schakelopdracht
# =============================================================================

class TestDecoder(unittest.TestCase):
    def _waarden(self, status):
        return {v.grootheid: v.waarde for v in shelly_relais.decodeer_velden(status)}

    def test_uit_en_aan(self):
        self.assertEqual(self._waarden(_status()),
                         {"relais": 0.0, "vermogen": 0.0, "spanning": 221.3, "stroom": 0.0})
        # De kachel stookt: een relais dat aan staat én vermogen opneemt.
        self.assertEqual(self._waarden(_status(True, 1480.2, 228.0, 6.49)),
                         {"relais": 1.0, "vermogen": 1480.2, "spanning": 228.0, "stroom": 6.49})

    def test_een_veld_dat_ontbreekt_wordt_niet_verzonnen(self):
        status = _status(True)
        status["switch:0"]["apower"] = None
        del status["switch:0"]["current"]
        w = self._waarden(status)
        self.assertEqual((w["relais"], w["vermogen"], w["stroom"]), (1.0, None, None))

    def test_geen_relais_op_dit_adres(self):
        # Een ander apparaat (of een verkeerd adres): geen uitgang in het antwoord.
        for kapot in ({"wifi": {"rssi": -50}}, ["kapot"]):
            with self.assertRaises(shelly_relais.ShellyFout):
                shelly_relais.decodeer_velden(kapot)

    def test_adres_van_de_schakelopdracht(self):
        self.assertEqual(shelly_relais.zet_url("192.168.1.121", True, 600.0),
                         "http://192.168.1.121/rpc/Switch.Set?id=0&on=true&toggle_after=600")
        self.assertEqual(shelly_relais.zet_url("192.168.1.121", False),
                         "http://192.168.1.121/rpc/Switch.Set?id=0&on=false")

    def test_aan_kan_niet_zonder_uitschakeltijd(self):
        # De beveiliging van §5.2.6 punt 4: de kachel gaat nooit aan zonder tijd.
        for duur in (None, 0, -5):
            with self.assertRaises(ValueError):
                shelly_relais.zet_url("192.168.1.121", True, duur)

    def test_veilige_stand_aan_keert_de_beveiliging_om(self):
        # §5.8 punt 5: de actieve stand gaat met een terugschakeltijd, de veilige zonder.
        # Bij een relais met veilige stand aan is "uit" de actieve stand.
        self.assertEqual(shelly_relais.zet_url("x", False, 120.0, veilig_aan=True),
                         "http://x/rpc/Switch.Set?id=0&on=false&toggle_after=120")
        self.assertEqual(shelly_relais.zet_url("x", True, None, veilig_aan=True),
                         "http://x/rpc/Switch.Set?id=0&on=true")
        # De veilige stand blijft staan: een meegegeven tijd gaat niet mee.
        self.assertEqual(shelly_relais.zet_url("x", True, 120.0, veilig_aan=True),
                         "http://x/rpc/Switch.Set?id=0&on=true")
        self.assertEqual(shelly_relais.zet_url("x", False, 120.0),
                         "http://x/rpc/Switch.Set?id=0&on=false")
        with self.assertRaises(ValueError):
            shelly_relais.zet_url("x", False, None, veilig_aan=True)

    def test_alleen_het_relais_is_te_bedienen(self):
        # §5.4.2 punt 8: de decoder legt vast wat bedienbaar is, en in welke vorm.
        self.assertEqual(shelly_relais.BEDIENBAAR, {"relais": grootheden.Bediening(grootheden.AANUIT)})
        self.assertEqual(config.BEDIENBAAR, {"shelly-relais": shelly_relais.BEDIENBAAR})


# =============================================================================
# De Shelly-reader: pollen en schakelen
# =============================================================================

class TestReader(unittest.TestCase):
    def test_poll_geeft_de_zes_grootheden(self):
        metingen = shelly.verwerk(KACHEL, _status(True, 1480.2, 228.0, 6.49), 1_800_000_000)
        self.assertEqual({m.grootheid: m.waarde for m in metingen},
                         {"relais": 1.0, "vermogen": 1480.2, "spanning": 228.0, "stroom": 6.49,
                          "walstroom": 1.0, "rssi": -61})
        self.assertEqual({m.sensor_id for m in metingen}, {"kachel"})

    def test_lijst_grootheden_beperkt_wat_meegaat(self):
        sensor = Sensor("kachel", "shelly-relais", "kachel", adres="x",
                        grootheden=("relais", "vermogen"))
        metingen = shelly.verwerk(sensor, _status(), 1_800_000_000)
        self.assertEqual([m.grootheid for m in metingen], ["relais", "vermogen"])

    def test_walstroom_volgt_uit_de_spanning(self):
        # §5.2.6 punt 5: boven 180 V is er walstroom; precies 180 V telt niet.
        def wal(sensor=KACHEL, **status):
            metingen = shelly.verwerk(sensor, _status(**status), 1_800_000_000)
            return {m.grootheid: m.waarde for m in metingen}.get("walstroom")

        self.assertEqual([wal(spanning=v) for v in (221.3, 180.1, 180.0, 0.0)], [1.0, 1.0, 0.0, 0.0])
        # Zonder gemeten spanning is de walstroom niet te bepalen: geen verzonnen 0.
        self.assertIsNone(wal(spanning=None))
        # De afleiding rekent met de gecorrigeerde spanning, ook als 'spanning' zelf niet
        # wordt meegenomen.
        sensor = Sensor("kachel", "shelly-relais", "kachel", adres="x",
                        grootheden=("walstroom",), correctie={"spanning": Correctie(offset=-50.0)})
        metingen = shelly.verwerk(sensor, _status(spanning=221.3), 1_800_000_000)
        self.assertEqual([(m.grootheid, m.waarde) for m in metingen], [("walstroom", 0.0)])

    def test_opdracht_lezen(self):
        self.assertEqual(shelly.lees_zet(b'{"sensor_id":"kachel","aan":true,"duur":600}'),
                         ("kachel", True, 600.0))
        self.assertEqual(shelly.lees_zet(b'{"sensor_id":"kachel","aan":false}'),
                         ("kachel", False, None))
        # Een opdracht zonder duur is hier geldig: of hij mag, hangt af van de veilige
        # stand van het relais, en dat beoordeelt de reader bij het schakelen (§5.8 punt 5).
        self.assertEqual(shelly.lees_zet(b'{"sensor_id":"wal","aan":true}'), ("wal", True, None))
        self.assertEqual(shelly.lees_zet(b'{"sensor_id":"wal","aan":false,"duur":120}'),
                         ("wal", False, 120.0))
        for kapot in (b"niks", b"[]", b'{"aan":true,"duur":600}', b'{"sensor_id":"kachel","aan":1}',
                      b'{"sensor_id":"kachel","aan":true,"duur":0}',
                      b'{"sensor_id":"kachel","aan":true,"duur":true}'):
            with self.assertRaises(ValueError):
                shelly.lees_zet(kapot)

    def test_schakelen_en_direct_pollen(self):
        opdrachten = []

        def nep_schakel(adres, aan, duur, timeout, veilig_aan):
            opdrachten.append((adres, aan, duur, veilig_aan))
            return {"was_on": not aan}

        async def proef():
            poller = shelly.Poller(KACHEL, NepBus(), None, None, schakel=nep_schakel)
            gelukt = await poller.zet(True, 600.0)
            # Het wekken breekt de wachttijd tot de volgende poll af (zoals poll-nu).
            return gelukt, poller.wek.is_set()

        self.assertEqual(asyncio.run(proef()), (True, True))
        self.assertEqual(opdrachten, [("192.168.1.121", True, 600.0, False)])

    def test_de_veilige_stand_gaat_zonder_tijd_en_de_actieve_nooit(self):
        """§5.8 punt 5, voor beide soorten relais: de veilige stand gaat zonder
        terugschakeltijd (ook als er een bij de opdracht staat), en een opdracht naar de
        actieve stand zonder tijd weigert de reader, van wie hij ook komt."""
        from unittest import mock
        urls = []

        class Antwoord:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, n):
                return b'{"was_on": false}'

        def nep_open(url, timeout):
            urls.append(url.split("Switch.Set?")[1])
            return Antwoord()

        async def proef(sensor, aan, duur):
            poller = shelly.Poller(sensor, NepBus(), None, None)
            return await poller.zet(aan, duur)

        with mock.patch.object(shelly.urllib.request, "urlopen", nep_open):
            self.assertTrue(asyncio.run(proef(KACHEL, True, 120.0)))     # actief, met tijd
            self.assertTrue(asyncio.run(proef(KACHEL, False, 120.0)))    # veilig: tijd vervalt
            self.assertTrue(asyncio.run(proef(WAL, False, 120.0)))       # actief, met tijd
            self.assertTrue(asyncio.run(proef(WAL, True, None)))         # veilig
            with self.assertLogs("brms.readers.shelly", "WARNING") as log:
                self.assertFalse(asyncio.run(proef(KACHEL, True, None)))   # actief zonder tijd
                self.assertFalse(asyncio.run(proef(WAL, False, None)))
        self.assertEqual(urls, ["id=0&on=true&toggle_after=120", "id=0&on=false",
                                "id=0&on=false&toggle_after=120", "id=0&on=true"])
        self.assertEqual(sum("niet geschakeld" in r for r in log.output), 2)

    def test_de_reader_vraagt_bij_de_start_om_de_stand(self):
        """Bij zijn start vraagt de reader om de stand van de relais (§5.2.6 punt 6; de
        schakellaag antwoordt, §5.8 punt 7): één keer, en pas als de broker bereikbaar is."""
        from unittest import mock

        class Bus(NepBus):
            def __init__(self, weigeren):
                super().__init__()
                self.weigeren = weigeren     # zo vaak is de broker nog niet bereikbaar

            def abonneer(self, topic, qos, callback):
                pass

            def publiceer(self, topic, payload, qos=0):
                if topic == shelly.TOPIC_RELAIS_STAND_VRAAG and self.weigeren:
                    self.weigeren -= 1
                    return False
                return super().publiceer(topic, payload, qos)

        async def niets(self, stop):
            await stop.wait()                # geen echte Shelly in deze test

        def proef(sensoren, weigeren=0):
            cfg = Config(MqttConfig(), OpslagConfig("x"), sensoren)
            bus = Bus(weigeren)

            async def draai():
                stop = asyncio.Event()
                taak = asyncio.create_task(shelly.draai(cfg, bus, stop))
                await asyncio.sleep(1.5)
                stop.set()
                await asyncio.wait_for(taak, 5)

            with mock.patch.object(shelly.Poller, "draai", niets), \
                    self.assertLogs("brms.readers.shelly", "INFO"):
                asyncio.run(draai())
            return [b for b in bus.berichten if b[0] == shelly.TOPIC_RELAIS_STAND_VRAAG]

        self.assertEqual(len(proef({"kachel": KACHEL})), 1)
        # De eerste keer is de broker er nog niet: een seconde later opnieuw.
        self.assertEqual(len(proef({"kachel": KACHEL}, weigeren=1)), 1)
        # Zonder relais valt er niets te vragen.
        bilge = Sensor("bilge", "shelly-uni", "bilge", adres="192.168.1.120", interval=2.0)
        self.assertEqual(proef({"bilge": bilge}), [])

    def test_mislukt_schakelen_is_geen_fout(self):
        def onbereikbaar(adres, aan, duur, timeout, veilig_aan):
            raise OSError("timed out")

        async def proef():
            poller = shelly.Poller(KACHEL, NepBus(), None, None, schakel=onbereikbaar)
            with self.assertLogs("brms.readers.shelly", "WARNING") as log:
                gelukt = await poller.zet(False, None)
            return gelukt, poller.wek.is_set(), log.output[0]

        gelukt, gewekt, regel = asyncio.run(proef())
        self.assertEqual((gelukt, gewekt), (False, False))
        self.assertIn("niet geschakeld (uit)", regel)


# =============================================================================
# De configuratie
# =============================================================================

BASIS = """
[[sensor]]
id = "A4:C1:38:00:00:01"
type = "bthome"
naam = "kajuit"
[[sensor]]
id = "kachel"
type = "shelly-relais"
naam = "verwarming"
adres = "192.168.1.121"
voorwaarde = { multiplus.walstroom = 1 }
controls = ["relais"]
"""
MULTIPLUS = """
[[sensor]]
id = "D3:00:00:00:00:03"
type = "victron-multiplus"
naam = "multiplus"
sleutel = "00000000000000000000000000000000"
"""
VORST = """
[[alarm]]
id = "vorst"
sensoren = ["kajuit"]
grootheid = "temperatuur"
soort = "grens"
onder = 2.0
herstel = 3.0
"""


def _toml(tekst):
    """Leest een configuratie uit een tijdelijk bestand, zoals `brms` het doet."""
    with tempfile.TemporaryDirectory() as map_:
        pad = os.path.join(map_, "brms.toml")
        with open(pad, "w", encoding="utf-8") as f:
            f.write(tekst)
        return config.lees(pad)


class TestConfig(unittest.TestCase):
    def _fouten(self, tekst):
        with self.assertRaises(config.ConfigFout) as ctx:
            _toml(tekst)
        return "\n".join(ctx.exception.fouten)

    def test_sensor_en_actie(self):
        cfg = _toml(BASIS + MULTIPLUS + VORST
                    + 'melding = "uit"\nactie = [{ type = "relais", relais = "verwarming" }]\n')
        s = cfg.sensoren["kachel"]
        self.assertEqual((s.type, s.adres, s.interval), ("shelly-relais", "192.168.1.121", 30.0))
        self.assertEqual(config.levert(s),
                         ("relais", "vermogen", "spanning", "stroom", "walstroom", "rssi"))
        # De voorwaarde verwijst op naam; in de sensor staat het ID.
        self.assertEqual(s.voorwaarde, (("D3:00:00:00:00:03", "walstroom", 1.0),))
        # De actie verwijst op naam; in de regel staat het ID. Een camera is niet nodig.
        regel = cfg.alarmen[0]
        self.assertEqual((regel.relais, regel.acties, regel.camera), ("kachel", (), None))
        # Zonder het veld is de veilige stand "uit" (de kachel); "aan" is dan de actieve.
        self.assertEqual((s.veilige_stand, config.actieve_stand(s), s.controls),
                         ("uit", "aan", ("relais",)))
        self.assertEqual(list(config.bedienbaar(s)), ["relais"])

    def test_veilige_stand(self):
        # §5.8 punt 2 en punt 11 (check-config).
        aan = _toml(BASIS.replace('controls', 'veilige_stand = "aan"\ncontrols') + MULTIPLUS)
        s = aan.sensoren["kachel"]
        self.assertEqual((s.veilige_stand, config.actieve_stand(s)), ("aan", "uit"))
        for waarde in ('"half"', 'true', '1'):
            self.assertIn("'veilige_stand' moet \"uit\" of \"aan\" zijn", self._fouten(
                BASIS.replace('controls', f'veilige_stand = {waarde}\ncontrols') + MULTIPLUS))
        self.assertIn("'veilige_stand' hoort alleen bij een relais", self._fouten(
            BASIS.replace('naam = "kajuit"', 'naam = "kajuit"\nveilige_stand = "uit"') + MULTIPLUS))

    def test_controls(self):
        # §5.4.2 punt 8: alleen wat het type levert én wat bij dat type te bedienen is.
        zonder = BASIS.replace('controls = ["relais"]\n', "")
        s = _toml(zonder + MULTIPLUS).sensoren["kachel"]
        self.assertEqual((s.controls, config.bedienbaar(s)), ((), {}))
        for controls, verwacht in [
                ('["vermogen"]', "'vermogen' in 'controls' is bij type shelly-relais niet te bedienen; wel: relais"),
                ('["soc"]', "'soc' in 'controls' wordt door type shelly-relais niet geleverd"),
                ('["sauna"]', "onbekende grootheid 'sauna' in 'controls'"),
                ('["relais", "relais"]', "'controls' bevat een naam meer dan eens"),
                ('"relais"', "'controls' moet een lijst van grootheden zijn")]:
            with self.subTest(controls=controls):
                self.assertIn(verwacht, self._fouten(
                    BASIS.replace('controls = ["relais"]', f"controls = {controls}") + MULTIPLUS))
        # Een type zonder bedienbare grootheden.
        self.assertIn("dit type heeft geen bedienbare grootheden", self._fouten(
            BASIS.replace('naam = "kajuit"', 'naam = "kajuit"\ncontrols = ["temperatuur"]') + MULTIPLUS))
        # Wat bediend wordt, moet ook gemeten worden.
        self.assertIn("'relais' staat in 'controls' maar niet in 'grootheden'", self._fouten(
            BASIS.replace('controls', 'grootheden = ["vermogen"]\ncontrols') + MULTIPLUS))

    def test_een_alarmregel_houdt_zich_aan_controls(self):
        # Besluit v0.95: een regel mag alleen een relais schakelen dat bediend mag worden.
        zonder = BASIS.replace('controls = ["relais"]\n', "")
        fouten = self._fouten(zonder + MULTIPLUS + VORST
                              + 'actie = [{ type = "relais", relais = "verwarming" }]\n')
        self.assertIn("actie 'relais': sensor 'verwarming' mag niet bediend worden", fouten)
        self.assertIn('controls = ["relais"]', fouten)

    def test_de_plug_als_eigen_voorwaarde(self):
        # Zonder MultiPlus: de plug ziet zelf of er netspanning is. Op naam of op ID, en
        # meer dan één voorwaarde mag.
        zonder = BASIS.replace("multiplus.walstroom", "verwarming.walstroom")
        self.assertEqual(_toml(zonder).sensoren["kachel"].voorwaarde, (("kachel", "walstroom", 1.0),))
        beide = BASIS.replace("{ multiplus.walstroom = 1 }",
                              '{ kachel.walstroom = 1, "d3:00:00:00:00:03".walstroom = 1 }')
        self.assertEqual(_toml(beide + MULTIPLUS).sensoren["kachel"].voorwaarde,
                         (("kachel", "walstroom", 1.0), ("D3:00:00:00:00:03", "walstroom", 1.0)))

    def test_voorwaarde_is_verplicht_en_wordt_gecontroleerd(self):
        # Besluit OPEN-34: geen relais zonder voorwaarde.
        self.assertIn("'voorwaarde' ontbreekt",
                      self._fouten(BASIS.replace("voorwaarde = { multiplus.walstroom = 1 }\n", "")))
        for voorwaarde, verwacht in [
                ('{ multiplus.walstroom = 1 }', "onbekende sensor 'multiplus'"),   # geen MultiPlus
                ('{ kajuit.walstroom = 1 }', "sensor 'kajuit' levert geen 'walstroom'"),
                ('{ verwarming.vermogen = 1 }', "'vermogen' is geen toestand"),
                ('{ verwarming.walstroom = 2 }', "moet 0 of 1 zijn"),
                ('{ verwarming.walstroom = true }', "moet 0 of 1 zijn"),
                ('{ verwarming = 1 }', "moet een tabel <sensor>.<grootheid> = <waarde> zijn"),
                ('"verwarming.walstroom = 1"', "moet een tabel")]:
            with self.subTest(voorwaarde=voorwaarde):
                self.assertIn(verwacht, self._fouten(
                    BASIS.replace("{ multiplus.walstroom = 1 }", voorwaarde)))
        # Bij een andere sensor heeft het veld geen betekenis.
        self.assertIn("'voorwaarde' hoort alleen bij een relais", self._fouten(
            BASIS.replace('naam = "kajuit"', 'naam = "kajuit"\nvoorwaarde = { verwarming.walstroom = 1 }')
            + MULTIPLUS))

    def test_fouten_in_de_actie(self):
        for actie, verwacht in [
                ('[{ type = "relais", relais = "kajuit" }]', "is van type bthome, niet shelly-relais"),
                ('[{ type = "relais", relais = "sauna" }]', "onbekende sensor 'sauna'"),
                ('[{ type = "relais" }]', "mist het veld 'relais'"),
                ('[{ type = "relais", relais = "kachel", duur = "5m" }]', "onbekende sleutel 'duur'"),
                ('[{ type = "relais", relais = "kachel" }, { type = "relais", relais = "kachel" }]',
                 "actie 'relais' staat er meer dan eens")]:
            with self.subTest(actie=actie):
                self.assertIn(verwacht, self._fouten(BASIS + MULTIPLUS + VORST + f"actie = {actie}\n"))

    def test_fouten_bij_de_sensor(self):
        fouten = self._fouten("""
            [[sensor]]
            id = "kachel"
            type = "shelly-relais"
            naam = "zonder adres"
            voorwaarde = { kachel.walstroom = 1 }
            bron = { bilge = "input:0" }
            drempel = 6.0
        """)
        self.assertIn("'adres' ontbreekt", fouten)
        self.assertIn("'bron' hoort bij type shelly-uni, niet bij een relais", fouten)
        self.assertIn("'drempel' hoort bij type shelly-uni, niet bij een relais", fouten)


# =============================================================================
# De alarmlaag: de verzoeken aan de schakellaag
# =============================================================================

def _regel(**velden) -> Alarmregel:
    """De vorstregel als thermostaat: aan onder 2 °C, uit vanaf 3 °C, zonder melding."""
    basis = {"id": "vorst", "naam": "Vorst", "sensoren": (MAC,), "soort": config.GRENS,
             "grootheid": "temperatuur", "onder": 2.0, "herstel": 3.0, "melding": "uit",
             "relais": "kachel"}
    return Alarmregel(**{**basis, **velden})


VERZOEK = [("kachel", "vorst")]


class TestAlarmlaag(unittest.TestCase):
    """De alarmlaag beslist niet meer over het relais (§5.8 punt 8): hij houdt alleen bij
    welke regels om hun relais vragen. Geen opdrachten aan de Shelly-reader meer."""

    def _laag(self, *regels):
        """Een alarmlaag met een kajuitsensor, een tweede sensor en de kachel."""
        self.klok = NepKlok()
        sensoren = {MAC: Sensor(MAC, "bthome", "kajuit"),
                    MAC2: Sensor(MAC2, "bthome", "vooronder"),
                    MULTI: Sensor(MULTI, "victron-multiplus", "multiplus"),
                    "kachel": KACHEL}
        cfg = Config(MqttConfig(), OpslagConfig("x"), sensoren, alarmen=tuple(regels or (_regel(),)))
        return Alarmlaag(cfg, klok=self.klok, tijd=lambda: 1_800_000_000.4)

    def _meet(self, laag, waarde, sensor_id=MAC, grootheid="temperatuur"):
        """Eén meting; geeft de verzoeken die daarna gelden."""
        laag.bij_meting(maak(1, sensor_id, grootheid, waarde))
        return laag.verzoeken()

    def test_bericht_als_json(self):
        # Het bericht bevat de hele lijst, en de schakellaag kan het lezen (§3.3).
        tekst = alarms.verzoek_json(1_800_000_000, VERZOEK)
        self.assertEqual(json.loads(tekst), {"ts": 1_800_000_000, "bron": "alarm",
                                             "verzoeken": [{"sensor_id": "kachel", "alarm_id": "vorst"}]})
        self.assertEqual(schakel.lees_verzoek(tekst.encode()), VERZOEK)
        self.assertEqual(schakel.lees_verzoek(alarms.verzoek_json(1, []).encode()), [])
        self.assertEqual(alarms.TOPIC_VERZOEK, "brms/opdracht/schakel/verzoek")
        # De grens voor "de sensor levert nog waarden" is de terugschakeltijd (2 minuten).
        self.assertEqual(RELAIS_DUUR, 120.0)

    def test_geen_verzoek_bij_de_start(self):
        # Na een herstart is er geen actief alarm: de lijst is leeg (§5.8 punt 3).
        laag = self._laag()
        self.assertTrue(laag.schakelt)
        self.assertEqual(laag.verzoeken(), [])

    def test_verzoek_bij_alarm_en_ingetrokken_bij_herstel(self):
        laag = self._laag()
        self.assertEqual(self._meet(laag, 4.0), [])
        self.assertEqual(self._meet(laag, 1.5), VERZOEK)
        # Tussen de grens en de herstelgrens blijft het verzoek staan (de hysterese).
        self.assertEqual(self._meet(laag, 2.5), VERZOEK)
        self.assertEqual(self._meet(laag, 3.0), [])
        # De alarmlaag geeft zelf geen opdrachten meer: alleen gebeurtenissen voor de bus.
        uit = laag.bij_meting(maak(1, MAC, "temperatuur", 1.5))
        self.assertEqual([type(x).__name__ for x in uit], ["Gebeurtenis"])

    def test_de_walstroom_doet_er_hier_niet_toe(self):
        # De voorwaarde van het relais beoordeelt de schakellaag (§5.8 punt 4): de
        # alarmlaag vraagt, ook zonder walstroom.
        laag = self._laag()
        self._meet(laag, 0.0, MULTI, "walstroom")
        self.assertEqual(self._meet(laag, 1.5), VERZOEK)

    def test_stille_sensor_trekt_het_verzoek_in(self):
        laag = self._laag()
        self.assertEqual(self._meet(laag, 1.5), VERZOEK)
        # De kajuitsensor zwijgt. Het alarm blijft actief (geen data is geen herstel), maar
        # het verzoek vervalt zodra de laatste waarde zo oud is als de terugschakeltijd.
        self.klok.t += RELAIS_DUUR - 1
        self.assertEqual(laag.verzoeken(), VERZOEK)
        self.klok.t += 1
        self.assertEqual(laag.verzoeken(), [])
        self.assertEqual(laag.actief(), [["vorst", MAC]])
        # Levert de sensor weer een waarde onder de herstelgrens, dan vraagt de regel weer.
        self.assertEqual(self._meet(laag, 1.2), VERZOEK)

    def test_regel_uitzetten_trekt_het_verzoek_in(self):
        laag = self._laag()
        self.assertEqual(self._meet(laag, 1.5), VERZOEK)
        laag.schakel("vorst", False)
        self.assertEqual(laag.verzoeken(), [])
        # Weer aan: de beoordeling begint opnieuw, en bij de volgende lage waarde vraagt de
        # regel weer om zijn relais.
        laag.schakel("vorst", True)
        self.assertEqual(laag.verzoeken(), [])
        self.assertEqual(self._meet(laag, 1.5), VERZOEK)

    def test_een_van_meer_sensoren_is_genoeg(self):
        laag = self._laag(_regel(sensoren=(MAC, MAC2)))
        self.assertEqual(self._meet(laag, 1.5, MAC2), VERZOEK)
        self.assertEqual(self._meet(laag, 1.0, MAC), VERZOEK)     # één verzoek, niet twee
        self.assertEqual(self._meet(laag, 5.0, MAC2), VERZOEK)    # de kajuit is nog te koud
        self.assertEqual(self._meet(laag, 5.0, MAC), [])

    def test_twee_regels_op_hetzelfde_relais(self):
        laag = self._laag(_regel(), _regel(id="nacht", naam="Nacht", onder=5.0, herstel=6.0))
        self.assertEqual(self._meet(laag, 4.0), [("kachel", "nacht")])
        self.assertEqual(self._meet(laag, 1.5), [("kachel", "nacht"), ("kachel", "vorst")])

    def test_duur_van_de_regel_geldt_ook_voor_het_verzoek(self):
        # Het verzoek volgt het alarm: pas als de voorwaarde de duur heeft gegolden.
        laag = self._laag(_regel(duur=900))
        self.assertEqual(self._meet(laag, 1.5), [])
        self.klok.t += 900
        self.assertEqual(self._meet(laag, 1.5), VERZOEK)

    def test_zonder_relais_verandert_er_niets(self):
        laag = self._laag(_regel(relais=None))
        self.assertFalse(laag.schakelt)
        self.assertEqual(self._meet(laag, 1.5), [])

    def test_de_service_stuurt_de_lijst_bij_de_start_bij_verandering_en_op_verzoek(self):
        """De service-lus stuurt de hele lijst: bij de start (leeg), bij een verandering,
        en opnieuw als de schakellaag om de stand vraagt (bij zijn eigen start, §5.8
        punt 7)."""
        from unittest import mock
        from brms import melding

        class Bus(NepBus):
            def __init__(self):
                super().__init__()
                self.callbacks = {}
                self.q = None

            def wachtrij(self, topic, qos):
                self.q = asyncio.Queue()
                return self.q

            def abonneer(self, topic, qos, callback):
                self.callbacks[topic] = callback

        sensoren = {MAC: Sensor(MAC, "bthome", "kajuit"), "kachel": KACHEL}
        cfg = Config(MqttConfig(), OpslagConfig("x"), sensoren, alarmen=(_regel(),))
        bus = Bus()

        def gestuurd():
            return [b[1]["verzoeken"] for b in bus.berichten if b[0] == alarms.TOPIC_VERZOEK]

        async def proef():
            stop = asyncio.Event()
            taak = asyncio.create_task(alarms.draai(cfg, bus, stop))
            await asyncio.sleep(0.3)
            eerst = gestuurd()
            # Het wordt koud: het alarm gaat af.
            koud = maak(1, MAC, "temperatuur", 1.0)
            bus.q.put_nowait((koud.topic(), koud.naar_json().encode()))
            await asyncio.sleep(0.3)
            dan = gestuurd()
            # De schakellaag start en vraagt om de stand (in het echt vanuit de thread van paho).
            vraag = alarms.TOPIC_STAND_VRAAG
            bus.callbacks[vraag](vraag, b'{"ts": 1}')
            await asyncio.sleep(1.5)
            stop.set()
            await asyncio.wait_for(taak, 5)
            return eerst, dan

        with mock.patch.object(melding, "kanalen", lambda c: {}), \
                self.assertLogs("brms.alarms", "INFO") as log:
            eerst, dan = asyncio.run(proef())
        verzoek = [{"sensor_id": "kachel", "alarm_id": "vorst"}]
        self.assertEqual(eerst, [[]])
        self.assertEqual(dan, [[], verzoek])
        self.assertEqual(gestuurd(), [[], verzoek, verzoek])
        # De verandering staat één keer in het log; de herhaling niet.
        self.assertEqual(sum("verzoeken aan de schakellaag: kachel (regel vorst)" in r
                             for r in log.output), 1)


if __name__ == "__main__":
    unittest.main()
