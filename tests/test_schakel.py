"""Tests van de schakellaag (ARCHITECTURE.md §5.8) – zonder plug, zonder netwerk en zonder
broker.

De schakellaag beslist in welke stand elk relais hoort: de alarmlaag en het web vragen,
de schakellaag beslist, de Shelly-reader voert uit. Getest wordt:

- het lezen van de twee opdrachten (de verzoeken van de alarmlaag, de keuze van het web);
- de beslissing (punt 4): de voorwaarde gaat voor alles, dan de hand, dan een alarmregel,
  anders de veilige stand;
- de bewaking (punt 5): de actieve stand met een terugschakeltijd, elke minuut herhaald;
- de vragers (punt 3): verzoeken die vervallen als de alarmlaag zwijgt, en alleen een
  relais dat bediend mag worden (`controls`);
- de herstart (punt 7) en de melding voor het web (punt 9);
- een relais met de veilige stand aan (het walstroomrelais): alles omgekeerd;
- de service-lus.

De klok is nagemaakt, zodat minuten in de test geen minuten duren.
"""

import asyncio
import json
import unittest

from brms import config, schakel
from brms.config import Alarmregel, Config, MqttConfig, OpslagConfig, Sensor
from brms.meting import maak
from brms.readers import shelly
from brms.schakel import (HERHAAL, MELD_INTERVAL, TERUGSCHAKELTIJD, VERZOEK_GELDIG,
                          RelaisOpdracht, Schakellaag, Stand)

MAC = "A4:C1:38:00:00:01"
MULTI = "D3:00:00:00:00:03"

# De kachel: veilige stand uit, alleen aan als de MultiPlus walstroom meldt.
KACHEL = Sensor("kachel", "shelly-relais", "kachelplug", adres="192.168.1.121",
                voorwaarde=((MULTI, "walstroom", 1.0),), controls=("relais",))
# Een relais met de veilige stand aan (het walstroomrelais, §5.8): de actieve stand is uit.
WAL = Sensor("wal", "shelly-relais", "walrelais", adres="192.168.1.123", veilige_stand="aan",
             voorwaarde=(("wal", "walstroom", 1.0),), controls=("relais",))
# Een plug zonder 'controls': alleen te lezen, blijft in zijn veilige stand.
LOS = Sensor("los", "shelly-relais", "losse plug", adres="192.168.1.124",
             voorwaarde=(("los", "walstroom", 1.0),))

AAN = RelaisOpdracht("kachel", True, TERUGSCHAKELTIJD)
UIT = RelaisOpdracht("kachel", False)
VORST = [("kachel", "vorst")]


class NepKlok:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _config(*relais) -> Config:
    sensoren = {MAC: Sensor(MAC, "bthome", "kajuit"),
                MULTI: Sensor(MULTI, "victron-multiplus", "multiplus"),
                **{s.id: s for s in (relais or (KACHEL,))}}
    regels = (Alarmregel("vorst", "Vorst", (MAC,), "grens", "temperatuur", onder=2.0, herstel=3.0,
                         relais="kachel"),)
    return Config(MqttConfig(), OpslagConfig("x"), sensoren, alarmen=regels)


class TestBerichten(unittest.TestCase):
    def test_opdracht_als_json(self):
        # De terugschakeltijd is 2 minuten, de herhaling 1 minuut (besluit OPEN-36).
        self.assertEqual((TERUGSCHAKELTIJD, HERHAAL, VERZOEK_GELDIG, MELD_INTERVAL),
                         (120.0, 60.0, 120.0, 60.0))
        self.assertEqual(json.loads(AAN.naar_json()), {"sensor_id": "kachel", "aan": True, "duur": 120.0})
        self.assertEqual(json.loads(UIT.naar_json()), {"sensor_id": "kachel", "aan": False})
        # Wat de schakellaag stuurt, kan de Shelly-reader lezen.
        self.assertEqual(shelly.lees_zet(AAN.naar_json().encode()), ("kachel", True, 120.0))
        self.assertEqual(shelly.lees_zet(UIT.naar_json().encode()), ("kachel", False, None))
        self.assertEqual(schakel.TOPIC_RELAIS_ZET, shelly.TOPIC_RELAIS_ZET)
        self.assertEqual(schakel.TOPIC_RELAIS_STAND_VRAAG, shelly.TOPIC_RELAIS_STAND_VRAAG)

    def test_stand_als_json(self):
        stand = Stand(1_800_000_000, "kachel", "auto", "aan", "alarmregel Vorst")
        self.assertEqual(stand.topic(), "brms/schakel/kachel")
        self.assertEqual(json.loads(stand.naar_json()),
                         {"ts": 1_800_000_000, "sensor_id": "kachel", "keuze": "auto", "stand": "aan",
                          "reden": "alarmregel Vorst"})

    def test_verzoeken_lezen(self):
        self.assertEqual(schakel.lees_verzoek(
            b'{"ts":1,"bron":"alarm","verzoeken":[{"sensor_id":"kachel","alarm_id":"vorst"}]}'), VORST)
        self.assertEqual(schakel.lees_verzoek(b'{"verzoeken":[]}'), [])
        for kapot in (b"niks", b"[]", b"{}", b'{"verzoeken":"kachel"}', b'{"verzoeken":[{}]}',
                      b'{"verzoeken":[{"sensor_id":"kachel","alarm_id":5}]}'):
            with self.assertRaises(ValueError):
                schakel.lees_verzoek(kapot)

    def test_keuze_lezen(self):
        self.assertEqual(schakel.lees_hand(b'{"ts":1,"sensor_id":"kachel","keuze":"aan"}'),
                         ("kachel", "aan", None, None))
        self.assertEqual(schakel.lees_hand(b'{"sensor_id":"kachel","keuze":"auto"}')[1], "auto")
        # Een ingevoerde waarde (het invoerveldje, §5.6).
        self.assertEqual(schakel.lees_hand(b'{"sensor_id":"cv","grootheid":"temperatuur","waarde":18.5}'),
                         ("cv", None, "temperatuur", 18.5))
        for kapot in (b"niks", b"[]", b'{"keuze":"aan"}', b'{"sensor_id":"kachel"}',
                      b'{"sensor_id":"kachel","keuze":"soms"}', b'{"sensor_id":"kachel","keuze":true}',
                      b'{"sensor_id":"cv","waarde":18.5}',
                      b'{"sensor_id":"cv","grootheid":"temperatuur","waarde":"warm"}',
                      b'{"sensor_id":"cv","grootheid":"temperatuur","waarde":true}'):
            with self.assertRaises(ValueError):
                schakel.lees_hand(kapot)


class TestSchakellaag(unittest.TestCase):
    def _laag(self, *relais, walstroom=1.0):
        """Een schakellaag met de kachel (of de gegeven relais). walstroom: de waarde die
        de MultiPlus vooraf meldt; None = nog niets. De eerste tik (de start) is al
        gedaan, zodat een test alleen ziet wat daarna gebeurt."""
        self.klok = NepKlok()
        with self.assertLogs("brms.schakel", "INFO"):
            laag = Schakellaag(_config(*relais), klok=self.klok, tijd=lambda: 1_800_000_000.4)
            self.start = laag.tik()
        if walstroom is not None:
            self._wal(laag, walstroom)
        return laag

    @staticmethod
    def _opdrachten(uit):
        return [x for x in uit if isinstance(x, RelaisOpdracht)]

    @staticmethod
    def _standen(uit):
        return [(x.sensor_id, x.keuze, x.stand, x.reden) for x in uit if isinstance(x, Stand)]

    def _wal(self, laag, waarde, sensor_id=MULTI):
        return self._opdrachten(laag.bij_meting(maak(1, sensor_id, "walstroom", waarde)))

    def _vraag(self, laag, verzoeken=VORST):
        return self._opdrachten(laag.bij_verzoeken(verzoeken))

    def _hand(self, laag, keuze, sensor_id="kachel"):
        return self._opdrachten(laag.bij_hand(sensor_id, keuze))

    def _tik(self, laag):
        return self._opdrachten(laag.tik())

    # --- herstart (punt 7) ---------------------------------------------------------------
    def test_bij_de_start_naar_de_veilige_stand(self):
        laag = self._laag(KACHEL, WAL, LOS, walstroom=None)
        # Elk relais krijgt de opdracht naar zijn veilige stand, zonder tijd; ook een
        # plug die niet bediend mag worden. De kachel uit, het walstroomrelais aan.
        self.assertEqual(self._opdrachten(self.start),
                         [UIT, RelaisOpdracht("wal", True), RelaisOpdracht("los", False)])
        # En elk relais staat op AUTO.
        self.assertEqual(self._standen(self.start),
                         [("kachel", "auto", "uit", "veilige stand"), ("wal", "auto", "aan", "veilige stand"),
                          ("los", "auto", "uit", "veilige stand")])
        self.assertEqual(laag.relais, ["kachel", "wal", "los"])
        # Daarna gebeurt er niets tot iemand iets vraagt.
        self.assertEqual(laag.tik(), [])

    def test_de_stand_opnieuw_als_de_reader_erom_vraagt(self):
        laag = self._laag()
        self.assertEqual(self._vraag(laag), [AAN])
        # De Shelly-reader start en vraagt om de stand: de opdracht van elk relais komt
        # bij de volgende tik opnieuw, zonder regel in het log en zonder nieuwe melding.
        laag.opnieuw()
        with self.assertNoLogs("brms.schakel", "INFO"):
            uit = laag.tik()
        self.assertEqual(uit, [AAN])
        # De herhaling begint daarmee opnieuw te tellen.
        self.klok.t += HERHAAL - 1
        self.assertEqual(self._tik(laag), [])
        self.klok.t += 1
        self.assertEqual(self._tik(laag), [AAN])
        # Ook een relais in zijn veilige stand krijgt zijn opdracht opnieuw.
        self.assertEqual(self._vraag(laag, []), [UIT])
        laag.opnieuw("kachel")
        self.assertEqual(self._tik(laag), [UIT])
        self.assertEqual(self._tik(laag), [])

    # --- een alarmregel (punt 3 en 4) -------------------------------------------------------
    def test_aan_op_verzoek_van_een_alarmregel_en_terug_bij_intrekken(self):
        laag = self._laag()
        with self.assertLogs("brms.schakel", "INFO") as log:
            uit = laag.bij_verzoeken(VORST)
        self.assertEqual(self._opdrachten(uit), [AAN])
        # De reden noemt de regel bij zijn naam.
        self.assertEqual(self._standen(uit), [("kachel", "auto", "aan", "alarmregel Vorst")])
        self.assertIn("relais kachelplug (kachel) aan: alarmregel Vorst", log.output[0])
        # Dezelfde lijst opnieuw (de alarmlaag stuurt hem elke minuut): er verandert niets.
        self.assertEqual(laag.bij_verzoeken(VORST), [])
        with self.assertLogs("brms.schakel", "INFO") as log:
            uit = laag.bij_verzoeken([])
        self.assertEqual(self._opdrachten(uit), [UIT])
        self.assertEqual(self._standen(uit), [("kachel", "auto", "uit", "veilige stand")])
        self.assertIn("relais kachelplug (kachel) uit: veilige stand", log.output[0])

    def test_de_actieve_stand_wordt_elke_minuut_herhaald(self):
        laag = self._laag()
        self.assertEqual(self._vraag(laag), [AAN])
        self.klok.t += HERHAAL - 1
        self._wal(laag, 1.0)
        self.assertEqual(self._tik(laag), [])
        self.klok.t += 1
        # De herhaling komt niet in het log (punt 10).
        with self.assertNoLogs("brms.schakel", "INFO"):
            self.assertEqual(self._tik(laag), [AAN])
        self.assertEqual(self._tik(laag), [])
        # De herhaling komt ruim binnen de terugschakeltijd van de Shelly.
        self.assertLess(HERHAAL, TERUGSCHAKELTIJD)
        # De veilige stand wordt niet herhaald: daar blijft de Shelly vanzelf staan.
        self._vraag(laag, [])
        self.klok.t += 10 * HERHAAL
        self._wal(laag, 1.0)
        self.assertEqual(self._tik(laag), [])

    def test_verzoeken_vervallen_als_de_alarmlaag_zwijgt(self):
        laag = self._laag()
        self.assertEqual(self._vraag(laag), [AAN])
        # De alarmlaag stuurt zijn lijst elke minuut. Blijft hij 2 minuten weg, dan
        # vervallen zijn verzoeken en gaat de kachel uit.
        # (De MultiPlus blijft intussen walstroom melden; elke meting werkt de stand bij.)
        opdrachten = []
        for _ in range(3):
            self.klok.t += VERZOEK_GELDIG / 4
            opdrachten += self._wal(laag, 1.0)
        self.klok.t += VERZOEK_GELDIG / 4
        with self.assertLogs("brms.schakel", "WARNING") as log:
            opdrachten += self._wal(laag, 1.0)
        self.assertEqual(opdrachten, [AAN, UIT])          # onderweg één herhaling, dan uit
        self.assertIn("geen bericht van de alarmlaag", log.output[0])
        # Meldt de alarmlaag zich weer, dan geldt zijn lijst weer.
        self.assertEqual(self._vraag(laag), [AAN])

    def test_twee_regels_op_hetzelfde_relais(self):
        laag = self._laag()
        uit = laag.bij_verzoeken([("kachel", "vorst"), ("kachel", "nacht"), ("kachel", "vorst")])
        self.assertEqual(self._opdrachten(uit), [AAN])
        # Een regel die niet (meer) in brms.toml staat, staat er met zijn ID.
        self.assertEqual(self._standen(uit)[0][3], "alarmregel Vorst, nacht")
        # Eén regel trekt zich terug: het relais blijft in de actieve stand.
        self.assertEqual(self._vraag(laag, [("kachel", "nacht")]), [])
        self.assertEqual(self._vraag(laag, []), [UIT])

    # --- de voorwaarde (punt 4.1) -------------------------------------------------------------
    def test_alleen_met_walstroom(self):
        laag = self._laag(walstroom=0.0)
        with self.assertLogs("brms.schakel", "INFO") as log:
            uit = laag.bij_verzoeken(VORST)
        self.assertEqual(self._opdrachten(uit), [])
        # Het relais volgt niet; het log en het web zeggen waarom.
        self.assertIn("blijft uit: voorwaarde geldt niet: multiplus.walstroom is 0, niet 1",
                      "\n".join(log.output))
        self.assertEqual(self._standen(uit), [(
            "kachel", "auto", "uit",
            "voorwaarde geldt niet: multiplus.walstroom is 0, niet 1 (gevraagd: alarmregel Vorst)")])
        # Het verzoek blijft staan: komt de walstroom terug, dan gaat het relais alsnog aan.
        # Valt hij weer weg: direct uit.
        self.assertEqual(self._wal(laag, 1.0), [AAN])
        self.assertEqual(self._wal(laag, 0.0), [UIT])

    def test_walstroom_onbekend_is_de_veilige_stand(self):
        # Nog geen enkele waarde van de MultiPlus.
        laag = self._laag(walstroom=None)
        self.assertEqual(self._vraag(laag), [])
        self.assertEqual(self._wal(laag, 1.0), [AAN])
        # De MultiPlus valt stil: na 2 minuten is de walstroom weer onbekend. (De alarmlaag
        # blijft zijn lijst sturen; elk bericht werkt de stand bij.)
        self.klok.t += TERUGSCHAKELTIJD - 1
        self.assertEqual(self._vraag(laag), [AAN])        # de herhaling, de waarde telt nog
        self.klok.t += 1
        self.assertEqual(self._vraag(laag), [UIT])
        self.assertEqual(self._tik(laag), [])

    def test_alleen_de_sensor_uit_de_voorwaarde_telt(self):
        laag = self._laag(walstroom=None)
        self.assertEqual(laag.volgt, {(MULTI, "walstroom")})
        # Walstroom van een andere sensor, of een andere grootheid: geen invloed.
        self.assertEqual(laag.bij_meting(maak(1, "kachel", "walstroom", 1.0)), [])
        self.assertEqual(laag.bij_meting(maak(1, MULTI, "spanning", 230.0)), [])
        self.assertEqual(self._vraag(laag), [])

    def test_alle_voorwaarden_moeten_gelden(self):
        plug = Sensor("kachel", "shelly-relais", "kachelplug", adres="x", controls=("relais",),
                      voorwaarde=((MULTI, "walstroom", 1.0), ("kachel", "walstroom", 1.0)))
        laag = self._laag(plug)
        self.assertEqual(self._vraag(laag), [])
        self.assertEqual(self._wal(laag, 1.0, "kachel"), [AAN])
        self.assertEqual(self._wal(laag, 0.0), [UIT])

    # --- de hand (punt 3, 4 en 6) -------------------------------------------------------------
    def test_met_de_hand_in_de_actieve_stand_en_terug_naar_auto(self):
        laag = self._laag()
        with self.assertLogs("brms.schakel", "INFO") as log:
            uit = laag.bij_hand("kachel", "aan")
        self.assertEqual(self._opdrachten(uit), [AAN])
        self.assertEqual(self._standen(uit), [("kachel", "aan", "aan", "met de hand")])
        self.assertIn("relais kachelplug (kachel) aan: met de hand", log.output[0])
        # Geen eigen tijdslimiet: de hand blijft gelden, en de opdracht wordt herhaald
        # (hier bij de eerstvolgende meting van de walstroom).
        self.klok.t += 10 * HERHAAL
        self.assertEqual(self._wal(laag, 1.0), [AAN])
        self.assertEqual(self._tik(laag), [])
        # Terug naar AUTO: zonder alarm is dat de veilige stand.
        uit = laag.bij_hand("kachel", "auto")
        self.assertEqual(self._opdrachten(uit), [UIT])
        self.assertEqual(self._standen(uit), [("kachel", "auto", "uit", "veilige stand")])

    def test_de_hand_gaat_voor_de_regel_en_de_voorwaarde_voor_de_hand(self):
        laag = self._laag()
        self.assertEqual(self._vraag(laag), [AAN])
        # Met de hand aan terwijl de regel al vraagt: de stand verandert niet, de keuze wel.
        uit = laag.bij_hand("kachel", "aan")
        self.assertEqual(self._opdrachten(uit), [])
        self.assertEqual(self._standen(uit), [("kachel", "aan", "aan", "met de hand")])
        # De regel trekt zich terug: de hand houdt de kachel aan.
        self.assertEqual(self._vraag(laag, []), [])
        # Maar de voorwaarde gaat voor alles, ook voor de hand (punt 4.1).
        self.assertEqual(self._wal(laag, 0.0), [UIT])
        # De keuze blijft staan: geldt de voorwaarde weer, dan gaat het relais alsnog aan.
        self.assertEqual(self._wal(laag, 1.0), [AAN])

    def test_terug_naar_auto_volgt_de_regel_weer(self):
        laag = self._laag()
        self._hand(laag, "aan")
        self.assertEqual(self._vraag(laag), [])
        # In AUTO vraagt de regel nog steeds: de kachel blijft aan, nu om het alarm.
        uit = laag.bij_hand("kachel", "auto")
        self.assertEqual(self._opdrachten(uit), [])
        self.assertEqual(self._standen(uit), [("kachel", "auto", "aan", "alarmregel Vorst")])

    def test_de_veilige_stand_met_de_hand_bestaat_niet(self):
        # Besluit v0.97: twee keuzes, AUTO en de actieve stand. De kachel tegen een alarm
        # in uit houden gaat via de schakelaar van de alarmregel.
        laag = self._laag()
        self._vraag(laag)
        with self.assertLogs("brms.schakel", "WARNING") as log:
            self.assertEqual(laag.bij_hand("kachel", "uit"), [])
        self.assertIn('de keuzes zijn "auto" en "aan"', log.output[0])

    def test_alleen_een_relais_dat_bediend_mag_worden(self):
        # §5.4.2 punt 8: zonder "relais" in 'controls' neemt de schakellaag geen keuze en
        # geen verzoek aan; het relais blijft in zijn veilige stand.
        laag = self._laag(KACHEL, LOS, walstroom=None)
        laag.bij_meting(maak(1, "los", "walstroom", 1.0))
        with self.assertLogs("brms.schakel", "WARNING") as log:
            self.assertEqual(laag.bij_hand("los", "aan"), [])
            self.assertEqual(laag.bij_verzoeken([("los", "vorst"), ("sauna", "vorst")]), [])
        self.assertEqual(sum("genegeerd" in r for r in log.output), 3)
        # Hetzelfde verzoek een minuut later: niet opnieuw in het log.
        with self.assertNoLogs("brms.schakel", "WARNING"):
            laag.bij_verzoeken([("los", "vorst"), ("sauna", "vorst")])
        with self.assertLogs("brms.schakel", "WARNING"):
            self.assertEqual(laag.bij_hand("kajuit", "aan"), [])

    # --- de melding voor het web (punt 9) ----------------------------------------------------
    def test_de_stand_wordt_elke_minuut_opnieuw_gemeld(self):
        laag = self._laag()
        self.klok.t += MELD_INTERVAL - 1
        self.assertEqual(laag.tik(), [])
        self.klok.t += 1
        uit = laag.tik()
        self.assertEqual(self._standen(uit), [("kachel", "auto", "uit", "veilige stand")])
        self.assertEqual(uit[0].ts, 1_800_000_000)
        self.assertEqual(laag.tik(), [])

    # --- een relais met de veilige stand aan (§5.8, het walstroomrelais) ---------------------
    def test_veilige_stand_aan(self):
        laag = self._laag(WAL, walstroom=None)
        laag.bij_meting(maak(1, "wal", "walstroom", 1.0))
        # Bij de start: aan, zonder tijd. Met de hand uit: de actieve stand, met de
        # terugschakeltijd; valt BRMS weg, dan zet de Shelly de walstroom zelf weer aan.
        self.assertEqual(self._opdrachten(self.start), [RelaisOpdracht("wal", True)])
        uit = laag.bij_hand("wal", "uit")
        self.assertEqual(self._opdrachten(uit), [RelaisOpdracht("wal", False, TERUGSCHAKELTIJD)])
        self.assertEqual(self._standen(uit), [("wal", "uit", "uit", "met de hand")])
        self.klok.t += HERHAAL
        self.assertEqual(self._tik(laag), [RelaisOpdracht("wal", False, TERUGSCHAKELTIJD)])
        laag.bij_meting(maak(1, "wal", "walstroom", 1.0))
        # De veilige stand als keuze bestaat ook hier niet.
        with self.assertLogs("brms.schakel", "WARNING"):
            self.assertEqual(laag.bij_hand("wal", "aan"), [])
        self.assertEqual(self._hand(laag, "auto", "wal"), [RelaisOpdracht("wal", True)])
        # De Shelly-reader kan beide opdrachten uitvoeren (zie tests/test_shelly_relais.py).
        self.assertEqual(config.actieve_stand(WAL), "uit")


class TestService(unittest.TestCase):
    """De service-lus: de eerste opdrachten pas als de broker bereikbaar is, de vraag aan de
    alarmlaag om zijn verzoeken, en het antwoord op de vraag van de Shelly-reader."""

    class Bus:
        def __init__(self, weigeren=0):
            self.berichten, self.callbacks = [], {}
            self.q = None
            self.verbonden = True
            self.weigeren = weigeren     # zo vaak mislukt een schakelopdracht nog

        def wachtrij(self, topic, qos):
            self.q = asyncio.Queue()
            return self.q

        def abonneer(self, topic, qos, callback):
            self.callbacks[topic] = callback

        def publiceer(self, topic, payload, qos=0):
            if topic == schakel.TOPIC_RELAIS_ZET and self.weigeren:
                self.weigeren -= 1
                return False
            self.berichten.append((topic, json.loads(payload)))
            return True

    def _draai(self, bus, tussendoor, cfg=None, wacht=1.4):
        async def proef():
            stop = asyncio.Event()
            taak = asyncio.create_task(schakel.draai(cfg or _config(), bus, stop))
            await asyncio.sleep(0.2)
            await tussendoor(bus)
            await asyncio.sleep(wacht)
            stop.set()
            await asyncio.wait_for(taak, 5)

        with self.assertLogs("brms.schakel", "INFO") as log:
            asyncio.run(proef())
        return log.output

    def _zet(self, bus):
        return [b[1] for b in bus.berichten if b[0] == schakel.TOPIC_RELAIS_ZET]

    def test_start_verzoek_en_hand(self):
        bus = self.Bus()
        uit = {"sensor_id": "kachel", "aan": False}
        aan = {"sensor_id": "kachel", "aan": True, "duur": 120.0}

        async def tussendoor(bus):
            # Bij de start: de veilige stand, de stand voor het web, en de vraag aan de
            # alarmlaag om zijn verzoeken.
            self.assertEqual(self._zet(bus), [uit])
            self.assertEqual([b[1]["stand"] for b in bus.berichten if b[0] == "brms/schakel/kachel"], ["uit"])
            self.assertEqual(sum(b[0] == schakel.TOPIC_ALARM_STAND_VRAAG for b in bus.berichten), 1)
            # De MultiPlus meldt walstroom; de alarmlaag antwoordt met een verzoek.
            m = maak(1, MULTI, "walstroom", 1.0)
            bus.q.put_nowait((m.topic(), m.naar_json().encode()))
            await asyncio.sleep(0.1)
            vraag = schakel.TOPIC_VERZOEK
            bus.callbacks[vraag](vraag, b'{"verzoeken":[{"sensor_id":"kachel","alarm_id":"vorst"}]}')
            await asyncio.sleep(0.1)
            self.assertEqual(self._zet(bus), [uit, aan])
            # De gebruiker zet de kachel met de hand aan; de regel trekt zich terug.
            hand = schakel.TOPIC_HAND
            bus.callbacks[hand](hand, b'{"sensor_id":"kachel","keuze":"aan"}')
            bus.callbacks[vraag](vraag, b'{"verzoeken":[]}')
            await asyncio.sleep(0.1)
            self.assertEqual(self._zet(bus), [uit, aan])
            # Ongeldige berichten en een ingevoerde waarde (OPEN-44) doen niets.
            bus.callbacks[hand](hand, b"niks")
            bus.callbacks[hand](hand, b'{"sensor_id":"cv","grootheid":"temperatuur","waarde":18.5}')
            bus.callbacks[vraag](vraag, b"{}")
            # De Shelly-reader start en vraagt om de stand: de opdracht komt opnieuw.
            stand = schakel.TOPIC_RELAIS_STAND_VRAAG
            bus.callbacks[stand](stand, b'{"ts": 1}')

        log = "\n".join(self._draai(bus, tussendoor))
        self.assertEqual(self._zet(bus), [uit, aan, aan])
        # De vraag aan de alarmlaag gaat één keer uit.
        self.assertEqual(sum(b[0] == schakel.TOPIC_ALARM_STAND_VRAAG for b in bus.berichten), 1)
        keuzes = [b[1]["keuze"] for b in bus.berichten if b[0] == "brms/schakel/kachel"]
        self.assertEqual(keuzes, ["auto", "auto", "aan"])
        for verwacht in ("schakellaag gestart; 1 relais: kachelplug (kachel), veilige stand uit",
                         "relais kachelplug (kachel) uit: start, veilige stand",
                         "relais kachelplug (kachel) aan: alarmregel Vorst",
                         "relais kachelplug (kachel) blijft aan: met de hand",
                         "niet uitgevoerd: er is nog geen sensortype met een bedienbare waarde",
                         "ongeldige opdracht op brms/opdracht/schakel/hand genegeerd",
                         "ongeldig bericht op brms/opdracht/schakel/verzoek genegeerd",
                         "schakellaag gestopt"):
            self.assertIn(verwacht, log)

    def test_een_opdracht_die_niet_op_de_bus_kan_volgt_opnieuw(self):
        bus = self.Bus(weigeren=1)

        async def tussendoor(bus):
            self.assertEqual(self._zet(bus), [])      # de eerste poging is mislukt

        log = "\n".join(self._draai(bus, tussendoor))
        # Bij de volgende tik, een seconde later, is de opdracht er alsnog.
        self.assertEqual(self._zet(bus), [{"sensor_id": "kachel", "aan": False}])
        self.assertIn("broker niet bereikbaar: relais kachel niet uit gezet", log)

    def test_zonder_relais_doet_de_service_niets(self):
        bus = self.Bus()
        cfg = Config(MqttConfig(), OpslagConfig("x"), {MAC: Sensor(MAC, "bthome", "kajuit")})

        async def tussendoor(bus):
            pass

        log = "\n".join(self._draai(bus, tussendoor, cfg, wacht=0.2))
        self.assertIn("geen relais (type shelly-relais) in de configuratie", log)
        self.assertEqual(bus.berichten, [])


if __name__ == "__main__":
    unittest.main()
