"""Tests van de simulatie (ARCHITECTURE.md §10.3) – zonder broker en zonder Bluetooth,
met een nep-bus en nep-klok."""

import contextlib
import io
import json
import random
import unittest

from brms import simulatie, trace
from brms.config import Config, MqttConfig, OpslagConfig, Sensor
from brms.readers import bthome, ruuvi, victron
from brms.readers.ble import Ontvanger, verwerk

MAC = "A4:C1:38:00:00:01"
RMAC = "CB:B8:33:4C:88:4F"
SENSOREN = {MAC: Sensor(MAC, "bthome", "kajuit"), RMAC: Sensor(RMAC, "ruuvi", "motorruim")}

# Een echte advertentie van de Xiaomi: 25,06 °C (zie tests/test_bthome.py).
PVVX = bytes.fromhex("40" "00a4" "0164" "02ca09" "03bf13" "0c020c")


class NepBus:
    """Onthoudt wat er gepubliceerd wordt."""

    def __init__(self):
        self.metingen = []
        self.berichten = []

    def abonneer(self, topic, qos, callback):
        pass

    def publiceer(self, topic, payload, qos=0):
        self.berichten.append((topic, json.loads(payload)))
        return True

    def publiceer_meting(self, m):
        self.metingen.append(m)
        return True


class NepKlok:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _opdracht(sim, topic, sensor_id, **extra):
    sim.bij_opdracht(topic, json.dumps({"sensor_id": sensor_id, **extra}).encode())


class TestTestreeks(unittest.TestCase):
    def test_begin_en_einde(self):
        self.assertEqual(simulatie.waarden(0.0)["temperatuur"], 25.0)
        self.assertEqual(simulatie.waarden(1.0)["temperatuur"], -5.0)
        self.assertEqual(simulatie.waarden(0.5)["vocht"], 67.5)

    def test_ruis_blijft_rond_de_testreeks(self):
        toeval = random.Random(1)
        strak = simulatie.waarden(0.5)
        reeks = [simulatie.waarden(0.5, toeval) for _ in range(500)]
        for g, afwijking in simulatie.RUIS.items():
            ws = [w[g] for w in reeks]
            # Er is ruis (niet elke waarde gelijk), maar hij blijft klein en zonder
            # verschuiving: het gemiddelde ligt op de testreeks.
            self.assertGreater(max(ws) - min(ws), afwijking)
            self.assertLess(max(abs(w - strak[g]) for w in ws), 6 * afwijking)
            self.assertAlmostEqual(sum(ws) / len(ws), strak[g], delta=afwijking / 2)

    def test_ruis_blijft_binnen_wat_een_sensor_kan_uitzenden(self):
        # Ook aan de randen van de testreeks moeten de bytes te maken en te decoderen zijn.
        toeval = random.Random(2)
        for fractie in (0.0, 1.0):
            for n in range(300):
                for sensor in SENSOREN.values():
                    sd, fd = simulatie.advertentie(sensor, fractie, n, toeval)
                    if sensor.type == "bthome":
                        w = bthome.decodeer(sd[bthome.FCD2_UUID])
                    else:
                        w = ruuvi.decodeer(fd[ruuvi.FABRIKANT_ID])
                    self.assertTrue(0 <= w["vocht"] <= 100)
                    self.assertIn("temperatuur", w)

    def test_bthome_bytes_door_de_echte_decoder(self):
        # Wat de simulator maakt, moet de echte decoder precies teruggeven.
        for fractie, verwacht in [
                (0.0, {"temperatuur": 25.0, "vocht": 40.0, "batterijspanning": 3.0}),
                (1.0, {"temperatuur": -5.0, "vocht": 95.0, "batterijspanning": 2.2})]:
            data = simulatie.bthome_bytes(simulatie.waarden(fractie), 7)
            self.assertEqual(bthome.decodeer(data), verwacht)

    def test_ruuvi_bytes_door_de_echte_decoder(self):
        data = simulatie.ruuvi_bytes(simulatie.waarden(1.0), 300, RMAC)
        self.assertEqual(len(data), 24)
        self.assertEqual(ruuvi.decodeer(data), {
            "temperatuur": -5.0, "vocht": 95.0, "luchtdruk": 980.0,
            "versnelling_x": 200, "versnelling_y": -100, "versnelling_z": 975,
            "batterijspanning": 2.2, ruuvi.TELLER: 300 % 255})
        self.assertEqual(data[18:].hex(":").upper(), RMAC)

    def test_advertentie_per_sensortype(self):
        sd, fd = simulatie.advertentie(SENSOREN[MAC], 0.0, 0)
        self.assertEqual((list(sd), fd), ([bthome.FCD2_UUID], {}))
        sd, fd = simulatie.advertentie(SENSOREN[RMAC], 0.0, 0)
        self.assertEqual((sd, list(fd)), ({}, [ruuvi.FABRIKANT_ID]))


class TestVictron(unittest.TestCase):
    """De Victron-testreeks: versleuteld door de simulator, ontsleuteld door de decoder."""

    SLEUTEL = "aff4d0995b7d1e176c0c33ecb9e70dcd"
    VMAC = "D3:11:22:33:44:55"

    def _door_de_decoder(self, type_, fractie, n=7):
        w = simulatie.victron_waarden(type_, fractie)
        return victron.decodeer(simulatie.victron_bytes(type_, w, n, self.SLEUTEL),
                                self.SLEUTEL, type_)

    def test_accu_loopt_leeg(self):
        self.assertEqual(self._door_de_decoder("victron-bmv", 0.0), {
            "spanning": 12.8, "stroom": -2.0, "soc": 100.0, "verbruikt": 0.0,
            "resterende_tijd": 1200.0})
        self.assertEqual(self._door_de_decoder("victron-bmv", 1.0), {
            "spanning": 11.6, "stroom": -12.0, "soc": 20.0, "verbruikt": 160.0,
            "resterende_tijd": 60.0})

    def test_een_dag_zon(self):
        self.assertEqual(self._door_de_decoder("victron-mppt", 0.0),
                         {"zonnevermogen": 0.0, "laadstroom_zon": 0.0})
        self.assertEqual(self._door_de_decoder("victron-mppt", 0.5),
                         {"zonnevermogen": 45.0, "laadstroom_zon": 3.5})
        self.assertEqual(self._door_de_decoder("victron-mppt", 1.0),
                         {"zonnevermogen": 0.0, "laadstroom_zon": 0.0})

    def test_walstroom_valt_halverwege_weg(self):
        self.assertEqual(self._door_de_decoder("victron-multiplus", 0.49), {"walstroom": 1.0})
        self.assertEqual(self._door_de_decoder("victron-multiplus", 0.5), {"walstroom": 0.0})

    def test_elk_bericht_anders_versleuteld_en_alleen_met_de_goede_sleutel(self):
        w = simulatie.victron_waarden("victron-bmv", 0.3)
        een = simulatie.victron_bytes("victron-bmv", w, 1, self.SLEUTEL)
        twee = simulatie.victron_bytes("victron-bmv", w, 2, self.SLEUTEL)
        self.assertNotEqual(een[8:], twee[8:])
        self.assertTrue(victron.is_instant_readout(een))
        with self.assertRaises(victron.VictronFout):
            victron.decodeer(een, "00" + self.SLEUTEL[2:], "victron-bmv")

    def test_ruis_blijft_rond_de_testreeks(self):
        toeval = random.Random(1)
        for _ in range(200):
            w = simulatie.victron_waarden("victron-bmv", 0.5, toeval)
            self.assertAlmostEqual(w["spanning"], 12.2, delta=0.15)
            self.assertEqual((w["soc"], w["verbruikt"]), (60.0, 80.0))   # zonder ruis
            self.assertGreaterEqual(simulatie.victron_waarden("victron-mppt", 0.0, toeval)
                                    ["zonnevermogen"], 0.0)

    def test_door_de_reader(self):
        # Zoals de BLE-reader het doet: advertentie maken en via verwerk() tot metingen.
        for type_, verwacht in (("victron-bmv", {"soc": 60.0, "verbruikt": 80.0}),
                                ("victron-mppt", {"zonnevermogen": 45.0}),
                                ("victron-multiplus", {"walstroom": 0.0})):
            sensor = Sensor(self.VMAC, type_, "v", sleutel=self.SLEUTEL)
            sd, fd = simulatie.advertentie(sensor, 0.5, 3)
            self.assertEqual((sd, list(fd)), ({}, [victron.FABRIKANT_ID]))
            ms = {m.grootheid: m.waarde for m in verwerk(self.VMAC, sd, fd, -60, 1000,
                                                         {self.VMAC: sensor}, gesimuleerd=True)}
            for g, w in verwacht.items():
                self.assertEqual(ms[g], w)
            self.assertEqual(ms["rssi"], -60.0)
        self.assertIn("victron-bmv", simulatie.TYPES)
        self.assertEqual(simulatie.SERVICE["victron-mppt"], "brms-ble-reader")


class TestSimulator(unittest.TestCase):
    def setUp(self):
        self.klok = NepKlok()
        self.sim = simulatie.Simulator(NepBus(), SENSOREN, klok=self.klok)

    def test_standaard_uit(self):
        self.assertFalse(self.sim.actief(MAC))
        self.assertEqual(self.sim.stappen(), [])

    def test_verloop_en_vanzelf_stoppen(self):
        _opdracht(self.sim, simulatie.TOPIC_AAN, MAC.lower(), duur=60)
        self.assertTrue(self.sim.actief(MAC))
        self.assertFalse(self.sim.actief(RMAC))

        # De reeks gaat binnen de duur twee keer heen en weer (§10.3 punt 5).
        self.assertEqual(self.sim.stappen(), [(SENSOREN[MAC], 0.0, 0)])
        self.klok.t += 7.5
        self.assertEqual(self.sim.stappen(), [(SENSOREN[MAC], 0.5, 1)])
        self.klok.t += 7.5    # een kwart van de duur: de eindwaarden
        self.assertEqual(self.sim.stappen(), [(SENSOREN[MAC], 1.0, 2)])
        self.klok.t += 15     # halverwege: terug op de beginwaarden
        self.assertEqual(self.sim.stappen(), [(SENSOREN[MAC], 0.0, 3)])
        self.klok.t += 15     # driekwart: weer de eindwaarden
        self.assertEqual(self.sim.stappen(), [(SENSOREN[MAC], 1.0, 4)])
        # Duur om: nog één advertentie met de beginwaarden, daarna is de simulatie voorbij.
        self.klok.t += 16
        self.assertEqual(self.sim.stappen(), [(SENSOREN[MAC], 0.0, 5)])
        self.assertFalse(self.sim.actief(MAC))
        self.assertEqual(self.sim.stappen(), [])

    def test_verloop_is_een_zaagtand(self):
        for voortgang, fractie in [(0.0, 0.0), (0.125, 0.5), (0.25, 1.0), (0.375, 0.5),
                                   (0.5, 0.0), (0.75, 1.0), (0.875, 0.5), (1.0, 0.0),
                                   (-1.0, 0.0), (2.0, 0.0)]:
            self.assertAlmostEqual(simulatie.verloop(voortgang), fractie)

    def test_uit(self):
        _opdracht(self.sim, simulatie.TOPIC_AAN, MAC, duur=60)
        _opdracht(self.sim, simulatie.TOPIC_UIT, MAC)
        self.assertFalse(self.sim.actief(MAC))

    def test_standaard_en_maximale_duur(self):
        _opdracht(self.sim, simulatie.TOPIC_AAN, MAC)                 # geen duur: 1 minuut
        _opdracht(self.sim, simulatie.TOPIC_AAN, RMAC, duur=10 ** 9)  # begrensd op het maximum
        self.klok.t += simulatie.STANDAARD_DUUR
        self.assertEqual([s.id for s, _, _ in self.sim.stappen()], [MAC, RMAC])
        self.assertFalse(self.sim.actief(MAC))
        self.assertTrue(self.sim.actief(RMAC))
        self.klok.t += simulatie.MAX_DUUR
        self.sim.stappen()
        self.assertFalse(self.sim.actief(RMAC))

    def test_sensor_blijft_nooit_genegeerd(self):
        # Ook als stappen() niet meer aangeroepen wordt, eindigt de bypass.
        _opdracht(self.sim, simulatie.TOPIC_AAN, MAC, duur=60)
        self.klok.t += 60 + 3 * simulatie.STAP + 1
        self.assertFalse(self.sim.actief(MAC))

    def test_onbekende_sensor_en_ongeldige_opdracht_genegeerd(self):
        _opdracht(self.sim, simulatie.TOPIC_AAN, "11:22:33:44:55:66", duur=60)
        for payload in [b"kapot", b"[1, 2]", b'{"sensor_id": 5}', b'{"duur": 60}']:
            self.sim.bij_opdracht(simulatie.TOPIC_AAN, payload)
        self.assertEqual(self.sim.stappen(), [])


class TestOntvanger(unittest.TestCase):
    """De ingang van de BLE-reader, met echte en gesimuleerde advertenties."""

    def setUp(self):
        self.bus, self.klok = NepBus(), NepKlok()
        self.sim = simulatie.Simulator(self.bus, SENSOREN, klok=self.klok)
        self.tracer = trace.Tracer(self.bus, "ble-reader", klok=self.klok)
        self.ontvang = Ontvanger(SENSOREN, self.bus, self.tracer, self.sim)

    def _temperaturen(self):
        return [m.waarde for m in self.bus.metingen if m.grootheid == "temperatuur"]

    def _simuleer_stap(self):
        for sensor, fractie, n in self.sim.stappen():
            sd, fd = simulatie.advertentie(sensor, fractie, n)
            self.ontvang(sensor.id, sd, fd, simulatie.RSSI, True)

    def test_bypass_tijdens_simulatie(self):
        # Zonder simulatie: de echte advertentie wordt verwerkt.
        self.ontvang(MAC, {bthome.FCD2_UUID: PVVX}, {}, -70)
        self.assertEqual(self._temperaturen(), [25.06])

        # Simulatie aan: de echte advertentie wordt genegeerd, de gesimuleerde verwerkt.
        _opdracht(self.sim, simulatie.TOPIC_AAN, MAC, duur=60)
        self.ontvang(MAC, {bthome.FCD2_UUID: PVVX}, {}, -70)
        self.assertEqual(self._temperaturen(), [25.06])
        self.klok.t += 7.5    # een achtste van de duur: halverwege de eerste heengang
        self._simuleer_stap()
        self.assertEqual(self._temperaturen(), [25.06, 10.0])
        self.assertEqual([m.waarde for m in self.bus.metingen if m.grootheid == "rssi"][-1],
                         simulatie.RSSI)

        # Simulatie voorbij (laatste advertentie: weer de beginwaarde); de echte sensor
        # telt weer mee.
        self.klok.t += 53
        self._simuleer_stap()
        self.ontvang(MAC, {bthome.FCD2_UUID: PVVX}, {}, -70)
        self.assertEqual(self._temperaturen(), [25.06, 10.0, 25.0, 25.06])

    def test_andere_sensor_loopt_door(self):
        _opdracht(self.sim, simulatie.TOPIC_AAN, RMAC, duur=60)
        self.ontvang(MAC, {bthome.FCD2_UUID: PVVX}, {}, -70)
        self.assertEqual(self._temperaturen(), [25.06])

    def test_onbekend_apparaat_genegeerd(self):
        self.ontvang("11:22:33:44:55:66", {bthome.FCD2_UUID: PVVX}, {}, -70)
        self.assertEqual(self.bus.metingen, [])

    def test_ruuvi_beweging_zonder_sprong_bij_de_overgang(self):
        def beweging():
            return [m.waarde for m in self.bus.metingen if m.grootheid == "beweging"]

        # Echte Ruuvi met tellerstand 200 en 201: één beweging.
        echt = bytearray(simulatie.ruuvi_bytes(simulatie.waarden(0.0), 0, RMAC))
        for stand in (200, 201):
            echt[15] = stand
            self.ontvang(RMAC, {}, {ruuvi.FABRIKANT_ID: bytes(echt)}, -80)
        self.assertEqual(beweging(), [1])

        # Simulatie: de teller begint bij 0. De sprong 201 -> 0 is geen beweging; daarna
        # telt elke gesimuleerde advertentie één beweging.
        _opdracht(self.sim, simulatie.TOPIC_AAN, RMAC, duur=60)
        for _ in range(3):
            self._simuleer_stap()
        self.assertEqual(beweging(), [1, 1, 1])

        # Terug naar de echte sensor: ook dan geen sprong.
        _opdracht(self.sim, simulatie.TOPIC_UIT, RMAC)
        for stand in (201, 203):
            echt[15] = stand
            self.ontvang(RMAC, {}, {ruuvi.FABRIKANT_ID: bytes(echt)}, -80)
        self.assertEqual(beweging(), [1, 1, 1, 2])

    def test_trace_vermeldt_simulatie(self):
        self.tracer.bij_opdracht(trace.TOPIC_AAN, json.dumps({"sensor_ids": [MAC]}).encode())
        _opdracht(self.sim, simulatie.TOPIC_AAN, MAC, duur=60)
        self._simuleer_stap()
        stappen = {d["stap"]: d["tekst"] for _, d in self.bus.berichten}
        self.assertEqual(list(stappen), ["ruw", "gedecodeerd", "gecorrigeerd", "gepubliceerd"])
        self.assertTrue(stappen["ruw"].startswith("SIMULATIE BTHome 40 00 00"))


class TestVraagDuur(unittest.TestCase):
    def _vraag(self, *antwoorden):
        reeks = iter(antwoorden)
        return simulatie.vraag_duur(lambda tekst: next(reeks))

    def test_standaard_is_tien_minuten(self):
        self.assertEqual(self._vraag(""), 600)

    def test_minuten(self):
        self.assertEqual(self._vraag("5"), 300)
        self.assertEqual(self._vraag("0,5"), 30)

    def test_ongeldige_invoer_opnieuw_vragen(self):
        self.assertEqual(self._vraag("lang", "0", "-1", "999", "2"), 120)


CFG = Config(MqttConfig(), OpslagConfig(), SENSOREN)


class TestKiesSensoren(unittest.TestCase):
    def _ids(self, *namen):
        return [s.id for s in simulatie.kies_sensoren(CFG, list(namen))]

    def test_een_of_meer(self):
        self.assertEqual(self._ids("kajuit"), [MAC])
        self.assertEqual(self._ids("motorruim", "kajuit"), [RMAC, MAC])

    def test_met_kommas(self):
        self.assertEqual(self._ids("kajuit,", "motorruim"), [MAC, RMAC])
        self.assertEqual(self._ids("kajuit,motorruim"), [MAC, RMAC])

    def test_all(self):
        self.assertEqual(self._ids("all"), [MAC, RMAC])
        self.assertEqual(self._ids("ALL"), [MAC, RMAC])

    def test_dubbel_telt_een_keer(self):
        self.assertEqual(self._ids("kajuit", MAC.lower()), [MAC])

    def test_onbekend_of_niets(self):
        with self.assertRaises(ValueError) as ctx:
            self._ids("salon")
        self.assertIn("kajuit", str(ctx.exception))   # de melding noemt wat er wel is
        with self.assertRaises(ValueError):
            self._ids()


class TestOpdracht(unittest.TestCase):
    """`brms sim ...` zonder broker: _stuur is vervangen en onthoudt wat er verstuurd zou
    worden."""

    def setUp(self):
        self.verstuurd = []
        self._stuur, self._draait = simulatie._stuur, simulatie._reader_draait
        simulatie._stuur = lambda cfg, topic, sensoren, **inhoud: (
            self.verstuurd.append((topic, [s.id for s in sensoren], inhoud)) or True)
        simulatie._reader_draait = lambda service=None: None

    def tearDown(self):
        simulatie._stuur, simulatie._reader_draait = self._stuur, self._draait

    def _sim(self, argumenten, *antwoorden):
        reeks = iter(antwoorden)
        with contextlib.redirect_stdout(io.StringIO()) as uit:
            code = simulatie.opdracht(CFG, argumenten, lambda tekst: next(reeks))
        return code, uit.getvalue()

    def test_start_meteen_na_akkoord_en_duur(self):
        code, uit = self._sim(["kajuit"], "", "")      # twee keer Enter: ja, 10 minuten
        self.assertEqual(code, 0)
        self.assertEqual(self.verstuurd, [(simulatie.TOPIC_AAN, [MAC], {"duur": 600})])
        self.assertIn("genegeerd", uit)                # de waarschuwing over de bypass
        self.assertIn("brms sim stop kajuit", uit)

    def test_meerdere_sensoren_en_eigen_duur(self):
        code, _ = self._sim(["all"], "j", "2")
        self.assertEqual(code, 0)
        self.assertEqual(self.verstuurd, [(simulatie.TOPIC_AAN, [MAC, RMAC], {"duur": 120})])

    def test_niet_akkoord(self):
        code, uit = self._sim(["kajuit"], "n")
        self.assertEqual((code, self.verstuurd), (1, []))
        self.assertIn("Afgebroken", uit)

    def test_stop(self):
        # Stoppen vraagt niets.
        self.assertEqual(self._sim(["stop", "kajuit"])[0], 0)
        self.assertEqual(self._sim(["stop", "all"])[0], 0)
        self.assertEqual(self.verstuurd, [(simulatie.TOPIC_UIT, [MAC], {}),
                                          (simulatie.TOPIC_UIT, [MAC, RMAC], {})])

    def test_onbekende_sensor(self):
        code, uit = self._sim(["salon"])
        self.assertEqual((code, self.verstuurd), (1, []))
        self.assertIn("onbekende sensor", uit)

    def test_waarschuwing_als_de_reader_niet_draait(self):
        simulatie._reader_draait = lambda service=None: False
        code, uit = self._sim(["kajuit"], "", "")
        self.assertEqual(code, 1)
        self.assertIn("brms-ble-reader draait niet", uit)


if __name__ == "__main__":
    unittest.main()
