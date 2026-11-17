"""Tests van het relais (ARCHITECTURE.md §5.2.6) – zonder plug, zonder netwerk en zonder
broker.

Drie delen, zoals de bouw:
- de decoder en de schakelopdracht (brms/readers/shelly_relais.py);
- het pollen en schakelen door de Shelly-reader (brms/readers/shelly.py);
- de actie `relais` en het veld `voorwaarde` in de configuratie (brms/config.py) en in
  de alarmlaag (brms/alarms.py): de thermostaat, de herhaling, de stille sensor en de
  voorwaarde (alleen aan met walstroom).

Het antwoord van de plug in deze tests is overgenomen van de echte Shelly Plug M Gen3
(§5.2.6, "Bevindingen met de plug"); het ophalen en het schakelen zelf zijn vervangen door
functies die zo'n antwoord teruggeven.
"""

import asyncio
import json
import os
import tempfile
import unittest

from brms import config
from brms.alarms import RELAIS_DUUR, RELAIS_HERHAAL, Alarmlaag, RelaisOpdracht
from brms.config import Alarmregel, Config, Correctie, MqttConfig, OpslagConfig, Sensor
from brms.meting import maak
from brms.readers import shelly, shelly_relais

MAC = "A4:C1:38:00:00:01"
MAC2 = "A4:C1:38:00:00:02"
MULTI = "D3:00:00:00:00:03"
# De plug van de kachel; hij mag alleen aan als de MultiPlus walstroom meldt (§5.2.6 punt 5).
KACHEL = Sensor("kachel", "shelly-relais", "kachel", adres="192.168.1.121", interval=30.0,
                voorwaarde=((MULTI, "walstroom", 1.0),))


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
        # Bij "uit" doet een duur er niet toe.
        self.assertEqual(shelly.lees_zet(b'{"sensor_id":"kachel","aan":false}'),
                         ("kachel", False, None))
        for kapot in (b"niks", b"[]", b'{"aan":true,"duur":600}', b'{"sensor_id":"kachel","aan":1}',
                      b'{"sensor_id":"kachel","aan":true}',            # aan zonder duur
                      b'{"sensor_id":"kachel","aan":true,"duur":0}',
                      b'{"sensor_id":"kachel","aan":true,"duur":true}'):
            with self.assertRaises(ValueError):
                shelly.lees_zet(kapot)

    def test_schakelen_en_direct_pollen(self):
        opdrachten = []

        def nep_schakel(adres, aan, duur, timeout):
            opdrachten.append((adres, aan, duur))
            return {"was_on": not aan}

        async def proef():
            poller = shelly.Poller(KACHEL, NepBus(), None, None, schakel=nep_schakel)
            gelukt = await poller.zet(True, 600.0)
            # Het wekken breekt de wachttijd tot de volgende poll af (zoals poll-nu).
            return gelukt, poller.wek.is_set()

        self.assertEqual(asyncio.run(proef()), (True, True))
        self.assertEqual(opdrachten, [("192.168.1.121", True, 600.0)])

    def test_mislukt_schakelen_is_geen_fout(self):
        def onbereikbaar(adres, aan, duur, timeout):
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
# De alarmlaag: de thermostaat
# =============================================================================

AAN = RelaisOpdracht("kachel", True, RELAIS_DUUR)
UIT = RelaisOpdracht("kachel", False)


def _regel(**velden) -> Alarmregel:
    """De vorstregel als thermostaat: aan onder 2 °C, uit vanaf 3 °C, zonder melding."""
    basis = {"id": "vorst", "naam": "Vorst", "sensoren": (MAC,), "soort": config.GRENS,
             "grootheid": "temperatuur", "onder": 2.0, "herstel": 3.0, "melding": "uit",
             "relais": "kachel"}
    return Alarmregel(**{**basis, **velden})


class TestAlarmlaag(unittest.TestCase):
    def _laag(self, *regels, walstroom=1.0):
        """Een alarmlaag met een kajuitsensor, een tweede sensor, een MultiPlus en de
        kachel. walstroom: de waarde die de MultiPlus vooraf meldt; None = nog niets."""
        self.klok = NepKlok()
        sensoren = {MAC: Sensor(MAC, "bthome", "kajuit"),
                    MAC2: Sensor(MAC2, "bthome", "vooronder"),
                    MULTI: Sensor(MULTI, "victron-multiplus", "multiplus"),
                    "kachel": KACHEL}
        cfg = Config(MqttConfig(), OpslagConfig("x"), sensoren, alarmen=tuple(regels or (_regel(),)))
        laag = Alarmlaag(cfg, klok=self.klok, tijd=lambda: 1_800_000_000.4)
        if walstroom is not None:
            self._wal(laag, walstroom)
        return laag

    def _meet(self, laag, waarde, sensor_id=MAC, grootheid="temperatuur"):
        """Eén meting; geeft alleen de opdrachten aan het relais terug."""
        return [x for x in laag.bij_meting(maak(1, sensor_id, grootheid, waarde))
                if isinstance(x, RelaisOpdracht)]

    def _wal(self, laag, waarde):
        return self._meet(laag, waarde, MULTI, "walstroom")

    def _tik(self, laag):
        return [x for x in laag.tik() if isinstance(x, RelaisOpdracht)]

    def test_opdracht_als_json(self):
        # De uitschakeltijd is 2 minuten, de herhaling 1 minuut (besluit OPEN-36).
        self.assertEqual((RELAIS_DUUR, RELAIS_HERHAAL), (120.0, 60.0))
        self.assertEqual(json.loads(AAN.naar_json()), {"sensor_id": "kachel", "aan": True, "duur": 120.0})
        self.assertEqual(json.loads(UIT.naar_json()), {"sensor_id": "kachel", "aan": False})
        # Wat de alarmlaag stuurt, kan de Shelly-reader lezen.
        self.assertEqual(shelly.lees_zet(AAN.naar_json().encode()), ("kachel", True, 120.0))

    def test_uit_bij_de_start(self):
        # Na een herstart is er geen actief alarm: elk relais krijgt "uit" (§5.2.6 punt 6).
        laag = self._laag()
        self.assertEqual(laag.relais_uit_bij_start(), [UIT])
        # Heeft een nieuw alarm het relais intussen aangezet, dan blijft het aan.
        self.assertEqual(self._meet(laag, 1.5), [AAN])
        self.assertEqual(laag.relais_uit_bij_start(), [])

    def test_de_service_zet_de_relais_uit_bij_de_start(self):
        """De service-lus geeft "uit" bij de start en nog een keer kort daarna, voor als
        de Shelly-reader de eerste keer nog niet luisterde."""
        from unittest import mock
        from brms import alarms, melding

        class Bus(NepBus):
            def wachtrij(self, topic, qos):
                return asyncio.Queue()

            def abonneer(self, topic, qos, callback):
                pass

        sensoren = {MAC: Sensor(MAC, "bthome", "kajuit"),
                    MULTI: Sensor(MULTI, "victron-multiplus", "multiplus"), "kachel": KACHEL}
        cfg = Config(MqttConfig(), OpslagConfig("x"), sensoren, alarmen=(_regel(),))
        bus = Bus()

        async def proef():
            stop = asyncio.Event()
            taak = asyncio.create_task(alarms.draai(cfg, bus, stop))
            await asyncio.sleep(0.3)
            eerst = [b for b in bus.berichten if b[0] == alarms.TOPIC_RELAIS_ZET]
            await asyncio.sleep(2.0)
            stop.set()
            await asyncio.wait_for(taak, 5)
            return eerst

        with mock.patch.object(melding, "kanalen", lambda c: {}), \
                mock.patch.object(alarms, "RELAIS_UIT_BIJ_START", (0.0, 1.0)), \
                self.assertLogs("brms.alarms", "INFO") as log:
            eerst = asyncio.run(proef())
        uit = ("brms/opdracht/relais/zet", {"sensor_id": "kachel", "aan": False})
        self.assertEqual(eerst, [uit])
        self.assertEqual([b for b in bus.berichten if b[0] == alarms.TOPIC_RELAIS_ZET], [uit, uit])
        # Eén regel in het log, niet twee.
        self.assertEqual(sum("start: relais uit (kachel)" in r for r in log.output), 1)

    def test_aan_bij_alarm_en_uit_bij_herstel(self):
        laag = self._laag()
        self.assertEqual(self._meet(laag, 4.0), [])
        self.assertEqual(self._meet(laag, 1.5), [AAN])
        # Tussen de grens en de herstelgrens blijft de kachel aan (de hysterese).
        self.assertEqual(self._meet(laag, 2.5), [])
        self.assertEqual(self._meet(laag, 3.0), [UIT])
        self.assertEqual(self._meet(laag, 3.5), [])

    def test_aan_wordt_elke_minuut_herhaald(self):
        laag = self._laag()
        self.assertEqual(self._meet(laag, 1.5), [AAN])
        self.klok.t += RELAIS_HERHAAL - 1
        self._wal(laag, 1.0)
        self.assertEqual(self._meet(laag, 1.4), [])
        self.klok.t += 1
        self.assertEqual(self._tik(laag), [AAN])
        self.assertEqual(self._tik(laag), [])
        # De herhaling komt ruim binnen de uitschakeltijd van de Shelly.
        self.assertLess(RELAIS_HERHAAL, RELAIS_DUUR)

    def test_stille_sensor_zet_het_relais_uit(self):
        laag = self._laag()
        self.assertEqual(self._meet(laag, 1.5), [AAN])
        # De MultiPlus blijft melden, de kajuitsensor zwijgt. Het alarm blijft actief (geen
        # data is geen herstel), maar het relais gaat uit.
        opdrachten = []
        for _ in range(3):
            self.klok.t += RELAIS_DUUR / 3
            opdrachten += self._wal(laag, 1.0)
        # Onderweg één herhaling van "aan" (na een minuut), en "uit" zodra de laatste
        # waarde van de sensor zo oud is als de uitschakeltijd.
        self.assertEqual(opdrachten, [AAN, UIT])
        self.assertEqual(self._tik(laag), [])
        self.assertEqual(laag.actief(), [["vorst", MAC]])
        # Levert de sensor weer een waarde onder de herstelgrens, dan gaat het weer aan.
        self.assertEqual(self._meet(laag, 1.2), [AAN])

    def test_alleen_met_walstroom(self):
        laag = self._laag(walstroom=0.0)
        with self.assertLogs("brms.alarms", "INFO") as log:
            self.assertEqual(self._meet(laag, 1.5), [])
        self.assertIn("blijft uit: multiplus.walstroom is 0, niet 1", "\n".join(log.output))
        # Het alarm zelf hangt niet van de walstroom af.
        self.assertEqual(laag.actief(), [["vorst", MAC]])
        # De walstroom komt terug terwijl het alarm nog actief is: aan. Valt hij weer weg:
        # direct uit.
        self.assertEqual(self._wal(laag, 1.0), [AAN])
        self.assertEqual(self._wal(laag, 0.0), [UIT])

    def test_walstroom_onbekend_is_uit(self):
        # Nog geen enkele waarde van de MultiPlus.
        laag = self._laag(walstroom=None)
        self.assertEqual(self._meet(laag, 1.5), [])
        self.assertEqual(self._wal(laag, 1.0), [AAN])
        # De MultiPlus valt stil: na de uitschakeltijd is de walstroom weer onbekend.
        self.klok.t += RELAIS_DUUR
        self.assertEqual(self._meet(laag, 1.4), [UIT])

    def test_walstroom_van_een_andere_sensor_telt_niet(self):
        # Alleen een sensor die volgens de configuratie 'walstroom' levert.
        laag = self._laag(walstroom=None)
        self._meet(laag, 1.0, MAC2, "walstroom")
        self.assertEqual(self._meet(laag, 1.5), [])

    def _eigen_laag(self, voorwaarde, *extra):
        """Een alarmlaag met de kajuitsensor en een plug met de gegeven voorwaarde."""
        plug = Sensor("kachel", "shelly-relais", "kachel", adres="x", voorwaarde=voorwaarde)
        self.klok = NepKlok()
        sensoren = {MAC: Sensor(MAC, "bthome", "kajuit"), "kachel": plug,
                    **{s.id: s for s in extra}}
        cfg = Config(MqttConfig(), OpslagConfig("x"), sensoren, alarmen=(_regel(),))
        return Alarmlaag(cfg, klok=self.klok, tijd=lambda: 1_800_000_000.4)

    def test_de_plug_als_eigen_voorwaarde(self):
        # Zonder MultiPlus (§5.2.6 punt 5): de voorwaarde kijkt naar de walstroom van de
        # plug zelf.
        laag = self._eigen_laag((("kachel", "walstroom", 1.0),))
        self.assertEqual(self._meet(laag, 1.5), [])                       # nog onbekend
        self.assertEqual(self._meet(laag, 1.0, "kachel", "walstroom"), [AAN])
        self.assertEqual(self._meet(laag, 0.0, "kachel", "walstroom"), [UIT])

    def test_alle_voorwaarden_moeten_gelden(self):
        laag = self._eigen_laag(((MULTI, "walstroom", 1.0), ("kachel", "walstroom", 1.0)),
                                Sensor(MULTI, "victron-multiplus", "multiplus"))
        self._meet(laag, 1.5)
        self.assertEqual(self._wal(laag, 1.0), [])
        self.assertEqual(self._meet(laag, 1.0, "kachel", "walstroom"), [AAN])
        self.assertEqual(self._wal(laag, 0.0), [UIT])

    def test_regel_uitzetten_zet_het_relais_uit(self):
        laag = self._laag()
        self.assertEqual(self._meet(laag, 1.5), [AAN])
        uit = laag.schakel("vorst", False)
        self.assertIn(UIT, uit)
        # Weer aan: de beoordeling begint opnieuw, en bij de volgende lage waarde gaat de
        # kachel weer aan.
        self.assertEqual([x for x in laag.schakel("vorst", True) if isinstance(x, RelaisOpdracht)], [])
        self.assertEqual(self._meet(laag, 1.5), [AAN])

    def test_een_van_meer_sensoren_is_genoeg(self):
        laag = self._laag(_regel(sensoren=(MAC, MAC2)))
        self.assertEqual(self._meet(laag, 1.5, MAC2), [AAN])
        self.assertEqual(self._meet(laag, 1.0, MAC), [])      # staat al aan
        self.assertEqual(self._meet(laag, 5.0, MAC2), [])     # de kajuit is nog te koud
        self.assertEqual(self._meet(laag, 5.0, MAC), [UIT])

    def test_duur_van_de_regel_geldt_ook_voor_het_relais(self):
        # Het relais volgt het alarm: pas aan als de voorwaarde de duur heeft gegolden.
        laag = self._laag(_regel(duur=900))
        self.assertEqual(self._meet(laag, 1.5), [])
        self.klok.t += 900
        self._wal(laag, 1.0)
        self.assertEqual(self._meet(laag, 1.5), [AAN])

    def test_zonder_relais_verandert_er_niets(self):
        laag = self._laag(_regel(relais=None))
        self.assertEqual(self._meet(laag, 1.5), [])
        self.assertEqual(self._tik(laag), [])


if __name__ == "__main__":
    unittest.main()
