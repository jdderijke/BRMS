"""Tests van de alarmenpagina, de systeempagina en het gedeelde menu (ARCHITECTURE.md §5.6,
v0.52) – zonder broker en zonder systemd, met de testclient van Flask."""

import json
import os
import tempfile
import threading
import unittest
from unittest import mock

from brms import VERSION, config, systeem, web
from brms.config import Alarmregel, Camera, Config, MqttConfig, OpslagConfig, Sensor
from brms.storage import Database

MAC = "A4:C1:38:00:00:01"
MAC2 = "A4:C1:38:00:00:02"

ALARMEN = (
    Alarmregel("vorst", "Vorst", (MAC, MAC2), "grens", "temperatuur", onder=2.0, herstel=3.0,
               duur=900.0, melding="herhaald", herhaling=43200.0, prioriteit="hoog"),
    Alarmregel("beweging", "Beweging", (MAC2,), "aantal", "beweging", aantal=5, venster=60.0,
               melding="uit"),
    Alarmregel("camera-stil", "Camera stil", ("dek",), "stil", duur=900.0),
)


class NepBus:
    verbonden = True

    def __init__(self):
        self.berichten = []

    def publiceer(self, topic, payload, qos=0):
        if not self.verbonden:
            return False
        self.berichten.append((topic, json.loads(payload), qos))
        return True


class Basis(unittest.TestCase):
    def setUp(self):
        self._map = tempfile.TemporaryDirectory()
        self.pad = os.path.join(self._map.name, "brms.db")
        self.cfg = Config(MqttConfig(), OpslagConfig(self.pad),
                          {MAC: Sensor(MAC, "bthome", "kajuit"), MAC2: Sensor(MAC2, "ruuvi", "motorruim")},
                          alarmen=ALARMEN, cameras={"dek": Camera("dek", "Dek", "http://c/snap")})
        self.bus = NepBus()
        self.toml = os.path.join(self._map.name, "brms.toml")
        with open(self.toml, "w") as f:
            f.write("")                       # een lege configuratie is geldig
        self.client = web.maak_app(self.cfg, web.LiveBuffer(), self.bus, klok=lambda: 5000.2,
                                   config_pad=self.toml).test_client()

    def tearDown(self):
        self._map.cleanup()


class TestMenu(Basis):
    def test_elke_pagina_heeft_het_menu(self):
        for adres, titel in (("/", "Grafieken"), ("/opnames", "Opnames"), ("/alarmen", "Alarmen"),
                             ("/systeem", "Systeem")):
            r = self.client.get(adres)
            self.assertEqual(r.status_code, 200, adres)
            tekst = r.get_data(as_text=True)
            self.assertIn(f"<title>BRMS – {titel}</title>", tekst)
            # De vier pagina's in het menu, de eigen pagina gemarkeerd.
            for doel in ('href="/"', 'href="/opnames"', 'href="/alarmen"', 'href="/systeem"'):
                self.assertIn(doel, tekst, adres)
            self.assertEqual(tekst.count('aria-current="page"'), 1, adres)
            self.assertIn(f'href="{adres}" class="hier" aria-current="page"', tekst)
            # Het gedeelde stijlbestand, de menuknop, de themakeuze en de versie.
            for verwacht in ("brms.css", 'id="menuknop"', 'data-thema="donker"', 'id="stip"',
                             f"BRMS versie {VERSION}"):
                self.assertIn(verwacht, tekst, adres)
            # Lijn-iconen als SVG in de pagina; niets van buiten (P3).
            self.assertIn("<svg", tekst)
            self.assertNotIn("https://", tekst.split("<script>")[0])

    def test_stijlbestand(self):
        r = self.client.get("/static/brms.css")
        self.assertEqual(r.status_code, 200)
        tekst = r.get_data(as_text=True)
        r.close()
        # Licht en donker, ook als vaste keuze los van het toestel.
        for verwacht in ('[data-thema="donker"]', ':not([data-thema="licht"])', "prefers-color-scheme: dark",
                         "#zijbalk", "table.kaartjes"):
            self.assertIn(verwacht, tekst)

    def test_status(self):
        with mock.patch.object(systeem, "services", return_value=([], "systemctl is er niet")):
            # Zonder database en zonder systemd: onbekend, geen fout.
            self.assertEqual(self.client.get("/api/status").get_json(),
                             {"alarmen": None, "services": None, "broker": True,
                              "bedrijf": {"stand": "actief", "sensoren": []}})
        db = Database(self.pad)
        db.alarm_begin(100, "vorst", MAC, "temperatuur", 1.5)
        db.alarm_begin(200, "vorst", MAC2, "temperatuur", 1.0)
        db.alarm_einde(300, "vorst", MAC2, "herstel")
        db.sluit()
        draait = [{"naam": "brms-web", "actief": True}, {"naam": "brms-opslag", "actief": True}]
        client = web.maak_app(self.cfg, web.LiveBuffer(), self.bus).test_client()
        with mock.patch.object(systeem, "services", return_value=(draait, None)):
            self.assertEqual(client.get("/api/status").get_json(),
                             {"alarmen": 1, "services": True, "broker": True,
                              "bedrijf": {"stand": "actief", "sensoren": []}})
        # Een service die niet draait.
        client = web.maak_app(self.cfg, web.LiveBuffer(), self.bus).test_client()
        self.bus.verbonden = False
        with mock.patch.object(systeem, "services",
                               return_value=(draait + [{"naam": "brms-camera", "actief": False}], None)):
            self.assertEqual(client.get("/api/status").get_json(),
                             {"alarmen": 1, "services": False, "broker": False,
                              "bedrijf": {"stand": "actief", "sensoren": []}})

    def test_tegeltje_actief_simulatie_of_demo(self):
        # Wat de readers op de bus melden (§10.3 punt 10) bepaalt het tegeltje bovenaan.
        tijd = [1000.0]
        stand = web.SimulatieStand(klok=lambda: tijd[0])
        client = web.maak_app(self.cfg, web.LiveBuffer(), self.bus, simulatie=stand).test_client()

        def meld(dienst, *sensoren):
            stand.bij_bericht(f"brms/simulatie/{dienst}", json.dumps(
                {"ts": 1, "dienst": dienst,
                 "sensoren": [{"sensor_id": i, "soort": s, "einde": None} for i, s in sensoren]}).encode())

        def bedrijf():
            with mock.patch.object(systeem, "services", return_value=([], None)):
                return client.get("/api/status").get_json()["bedrijf"]

        naam = self.cfg.sensoren[MAC].naam
        self.assertEqual(bedrijf(), {"stand": "actief", "sensoren": []})
        meld("ble-reader", (MAC, "demo"))
        self.assertEqual(bedrijf(), {"stand": "demo", "sensoren": [naam]})
        # Een test gaat voor een demo; een onbekende sensor staat er met zijn ID.
        meld("shelly-reader", ("bilge-x", "test"))
        self.assertEqual(bedrijf(), {"stand": "simulatie", "sensoren": [naam, "bilge-x"]})
        # Een nieuwe melding van een reader vervangt zijn vorige.
        meld("shelly-reader")
        self.assertEqual(bedrijf()["stand"], "demo")
        # Meldt een reader zich niet meer (gestopt), dan vervalt wat hij meldde.
        tijd[0] += web.SIMULATIE_GELDIG + 1
        self.assertEqual(bedrijf(), {"stand": "actief", "sensoren": []})
        # Onzin op de bus wordt genegeerd.
        for onzin in (b"geen json", b"[]", b'{"dienst": "x"}', b'{"dienst": "x", "sensoren": [1]}'):
            stand.bij_bericht("brms/simulatie/x", onzin)
        self.assertEqual(bedrijf()["stand"], "actief")

    def test_status_van_de_services_wordt_kort_bewaard(self):
        # Elke open pagina vraagt de status elke 30 s; niet elke vraag start systemctl.
        with mock.patch.object(systeem, "services", return_value=([], None)) as vraag:
            for _ in range(5):
                self.client.get("/api/status")
            self.assertEqual(vraag.call_count, 1)
            # De systeempagina zelf krijgt altijd een verse stand.
            self.client.get("/api/systeem")
            self.assertEqual(vraag.call_count, 2)


class TestAlarmen(Basis):
    def _overzicht(self, vraag=""):
        r = self.client.get("/api/alarmen/overzicht" + vraag)
        self.assertEqual(r.status_code, 200)
        return r.get_json()

    def test_regels_in_woorden(self):
        d = self._overzicht()         # er is nog geen database
        self.assertEqual((d["actief"], d["alarmen"], d["eerder"], d["vol"]), ([], [], False, False))
        self.assertEqual([r["id"] for r in d["regels"]], ["vorst", "beweging", "camera-stil"])
        vorst, beweging, camera = d["regels"]
        self.assertEqual(vorst, {
            "id": "vorst", "naam": "Vorst", "voorwaarde": "temperatuur onder 2 °C, 15 min lang",
            "sensoren": ["kajuit", "motorruim"], "melding": "melding, herhaald elke 12 u",
            "prioriteit": "hoog", "aan": True, "uit_sinds": None})
        self.assertEqual((beweging["voorwaarde"], beweging["melding"]),
                         ("beweging 5 of meer binnen 1 min", "geen melding"))
        # Een camera in een regel 'stil' staat er bij naam.
        self.assertEqual((camera["voorwaarde"], camera["sensoren"], camera["melding"]),
                         ("geen opnames gedurende 15 min", ["Dek"], "één melding"))

    def _vul(self, aantal):
        """Zet `aantal` afgelopen alarmen in de database: begin 1000, 2000, ..., elk 100 s lang."""
        db = Database(self.pad)
        for i in range(1, aantal + 1):
            db.alarm_begin(i * 1000, "vorst", MAC, "temperatuur", 1.0)
            db.alarm_einde(i * 1000 + 100, "vorst", MAC, "herstel")
        db.sluit()

    def test_laatste_tien_en_bladeren(self):
        # Zonder keuze: de laatste 10, de nieuwste eerst; er zijn er nog meer (eerder).
        self._vul(25)
        d = self._overzicht()
        self.assertEqual([a["begin"] for a in d["alarmen"]], list(range(25000, 15000, -1000)))
        self.assertEqual((d["eerder"], d["vol"]), (True, False))
        # Eén blad eerder: de 10 daarvoor.
        d = self._overzicht("?aantal=10&overslaan=10")
        self.assertEqual([a["begin"] for a in d["alarmen"]], list(range(15000, 5000, -1000)))
        self.assertTrue(d["eerder"])
        # Het laatste blad: de rest, en verder terug is er niets meer.
        d = self._overzicht("?aantal=10&overslaan=20")
        self.assertEqual([a["begin"] for a in d["alarmen"]], list(range(5000, 0, -1000)))
        self.assertFalse(d["eerder"])
        # Precies 10 alarmen: er is geen eerder blad.
        self.assertFalse(self._overzicht("?aantal=10&overslaan=15")["eerder"])
        # Onzin in de aanvraag valt terug op de standaard.
        d = self._overzicht("?aantal=x&overslaan=-5&van=abc")
        self.assertEqual(len(d["alarmen"]), 10)

    def test_periode(self):
        self._vul(25)
        # De alarmen die in de periode begonnen (van <= begin < tot), de nieuwste eerst.
        d = self._overzicht("?van=5000&tot=8000")
        self.assertEqual([a["begin"] for a in d["alarmen"]], [7000, 6000, 5000])
        # Er begon al eerder een alarm: de knop "eerder" heeft zin.
        self.assertEqual((d["eerder"], d["vol"]), (True, False))
        # De oudste periode: verder terug is er niets.
        d = self._overzicht("?van=0&tot=3000")
        self.assertEqual(([a["begin"] for a in d["alarmen"]], d["eerder"]), ([2000, 1000], False))
        # Een lege periode met oudere alarmen ervoor.
        d = self._overzicht("?van=90000&tot=99000")
        self.assertEqual((d["alarmen"], d["eerder"]), ([], True))
        # Een periode met meer dan het maximum: alleen de nieuwste, en `vol` zegt het.
        with mock.patch("brms.web.MAX_HISTORIE", 4):
            d = self._overzicht("?van=0&tot=99000")
        self.assertEqual(([a["begin"] for a in d["alarmen"]], d["vol"]),
                         ([25000, 24000, 23000, 22000], True))
        # Een omgekeerde periode is geen periode: dan de laatste 10.
        self.assertEqual(len(self._overzicht("?van=8000&tot=5000")["alarmen"]), 10)

    def test_pagina_heeft_de_keuze_en_de_bladerknoppen(self):
        # De alarmenpagina en de grafiekenpagina gebruiken dezelfde knoppen (_bladeren.html).
        alarmen = self.client.get("/alarmen").get_data(as_text=True)
        for verwacht in ['<option value="laatste">laatste 10</option>', '<option value="jaar">dit jaar</option>',
                         'class="pijl eerder"', 'class="pijl later"', 'class="pijl nu"']:
            self.assertIn(verwacht, alarmen)
        grafieken = self.client.get("/").get_data(as_text=True)
        for verwacht in ['class="pijl eerder"', 'class="pijl later"', 'class="pijl nu"']:
            self.assertIn(verwacht, grafieken)

    def test_actief_apart_en_de_historie(self):
        db = Database(self.pad)
        db.alarm_begin(100, "vorst", MAC, "temperatuur", 1.5)
        db.alarm_einde(400, "vorst", MAC, "herstel")
        db.alarm_begin(500, "camera-stil", "dek", None, None)
        db.alarm_begin(600, "vorst", MAC2, "temperatuur", 0.5)
        db.alarm_einde(700, "vorst", MAC2, "uitgezet")
        db.alarm_begin(800, "weg", "onbekend", "temperatuur", 1.0)     # regel bestaat niet meer
        db.alarm_schakel(900, "beweging", False)
        db.sluit()
        d = self._overzicht()
        self.assertEqual(d["nu"], 5000)
        # Wat nog actief is staat apart (nieuwste eerst), los van de keuze van de historie.
        self.assertEqual([(a["alarm_id"], a["begin"]) for a in d["actief"]],
                         [("weg", 800), ("camera-stil", 500)])
        self.assertEqual(d["actief"][1]["naam"], "Camera stil")
        # De historie: alle alarmen op volgorde van begin, de nieuwste eerst.
        self.assertEqual([(a["alarm_id"], a["begin"], a["einde"]) for a in d["alarmen"]],
                         [("weg", 800, None), ("vorst", 600, 700),
                          ("camera-stil", 500, None), ("vorst", 100, 400)])
        uitgezet, stil = d["alarmen"][1], d["alarmen"][2]
        self.assertEqual((stil["naam"], stil["sensor"], stil["waardetekst"]), ("Camera stil", "Dek", None))
        self.assertEqual((uitgezet["sensor"], uitgezet["waardetekst"], uitgezet["reden"]),
                         ("motorruim", "0,5 °C", "uitgezet"))
        # Een alarm van een regel of sensor die niet meer bestaat, staat er met zijn ID.
        self.assertEqual((d["alarmen"][0]["naam"], d["alarmen"][0]["sensor"]), ("weg", "onbekend"))
        # De schakelaar: de stand uit de database.
        self.assertEqual([(r["id"], r["aan"], r["uit_sinds"]) for r in d["regels"]],
                         [("vorst", True, None), ("beweging", False, 900), ("camera-stil", True, None)])

    def test_schakelen_gaat_als_opdracht_over_de_bus(self):
        with self.assertLogs("brms.web", "WARNING"):
            r = self.client.post("/api/alarmen/schakel", json={"alarm_id": "vorst", "aan": False})
        self.assertEqual((r.status_code, r.get_json()), (200, {"gevraagd": "vorst", "aan": False}))
        self.assertEqual(self.bus.berichten,
                         [("brms/opdracht/alarm/schakel", {"alarm_id": "vorst", "aan": False}, 1)])
        # Het web schrijft zelf niets: er is nog steeds geen database.
        self.assertFalse(os.path.exists(self.pad))

    def test_ongeldige_opdracht_of_geen_broker(self):
        for inhoud in ({"alarm_id": "bestaat-niet", "aan": False}, {"alarm_id": "vorst", "aan": "nee"},
                       {"alarm_id": "vorst"}, []):
            self.assertEqual(self.client.post("/api/alarmen/schakel", json=inhoud).status_code, 400)
        self.assertEqual(self.bus.berichten, [])
        self.bus.verbonden = False
        with self.assertLogs("brms.web", "WARNING"):
            r = self.client.post("/api/alarmen/schakel", json={"alarm_id": "vorst", "aan": True})
        self.assertEqual(r.status_code, 503)
        self.assertFalse(self._overzicht()["broker"])


class TestSysteem(Basis):
    def test_services_en_schijf(self):
        db = Database(self.pad)
        db.opname(1000, "dek", "foto", None, "dek/1970/01/01/001640.jpg", 300, "interval")
        db.opname(1200, "dek", "video", 60, "dek/1970/01/01/002000.mp4", 4000, "handmatig")
        db.sluit()
        lijst = [{"naam": "brms-web", "actief": True, "toestand": "active (running)", "sinds": 4000,
                  "herstarts": 0}]
        with mock.patch.object(systeem, "services", return_value=(lijst, None)):
            d = self.client.get("/api/systeem").get_json()
        self.assertEqual((d["nu"], d["services"], d["uitleg"]), (5000, lijst, None))
        self.assertEqual(d["schijf"]["opnames"], 4300)
        self.assertGreater(d["schijf"]["database"], 0)
        self.assertGreater(d["schijf"]["totaal"], d["schijf"]["vrij"])

    def test_logboek(self):
        regels = [{"ts": 10, "us": 10_000_000, "service": "brms-camera", "niveau": "fout", "tekst": "mis"}]
        # De laatste N met bladeren (v0.61): aantal en overslaan gaan door naar het logboek.
        with mock.patch.object(systeem, "logboek", return_value=(regels, True, False, None)) as lees:
            d = self.client.get("/api/logboek?fouten=1&service=brms-camera&aantal=50&overslaan=100").get_json()
        lees.assert_called_once_with(alleen_fouten=True, service="brms-camera", aantal=50,
                                     overslaan=100, van=None, tot=None)
        self.assertEqual((d["regels"], d["eerder"], d["vol"], d["uitleg"]), (regels, True, False, None))
        # De keuzelijst: alle services van BRMS, Mosquitto en de kernel.
        for naam in ("brms-opslag", "brms-web", "mosquitto", "kernel"):
            self.assertIn(naam, d["services"])
        # Een periode (laatste 3 uur, vandaag): van en tot.
        with mock.patch.object(systeem, "logboek", return_value=(regels, False, True, None)) as lees:
            d = self.client.get("/api/logboek?van=1000&tot=2000").get_json()
        lees.assert_called_once_with(alleen_fouten=False, service=None, aantal=10, overslaan=0,
                                     van=1000, tot=2000)
        self.assertTrue(d["vol"])
        # Zonder iets: de laatste 10. Onzin en een omgekeerde periode vallen daarop terug.
        for vraag in ("", "?aantal=x&overslaan=-5&van=abc", "?van=2000&tot=1000", "?aantal=99999"):
            with mock.patch.object(systeem, "logboek",
                                   return_value=([], False, False, "geen journalctl")) as lees:
                d = self.client.get("/api/logboek" + vraag).get_json()
            verwacht = systeem.PER_PAGINA if "99999" in vraag else 10
            lees.assert_called_once_with(alleen_fouten=False, service=None, aantal=verwacht,
                                         overslaan=0, van=None, tot=None)
            self.assertEqual(d["uitleg"], "geen journalctl")

    def test_pagina_vraagt_om_bevestiging(self):
        tekst = self.client.get("/systeem").get_data(as_text=True)
        for verwacht in ('id="herstart-brms"', 'id="herstart-pi"', '<dialog id="zeker">',
                         "Weet je het zeker?", 'const GESTART = "5000.200";'):
            self.assertIn(verwacht, tekst)

    def test_herstart_brms(self):
        """Eerst de configuratie controleren, dan de andere services, dan de webservice zelf."""
        gestart = threading.Event()
        with mock.patch.object(systeem, "herstart_services", return_value=None) as andere, \
                mock.patch.object(systeem, "herstart_service",
                                  side_effect=lambda naam: gestart.set()) as zelf, \
                mock.patch.object(web, "HERSTART_WACHT", 0.05), \
                self.assertLogs("brms.web", "WARNING") as log:
            r = self.client.post("/api/systeem/herstart", json={"wat": "brms"})
            self.assertEqual((r.status_code, r.get_json()), (200, {"gevraagd": "brms"}))
            andere.assert_called_once_with(behalve="brms-web")
            # De webservice herstart kort ná het antwoord.
            self.assertTrue(gestart.wait(2))
            zelf.assert_called_once_with("brms-web")
        # In het logboek is terug te vinden dat het via het web gevraagd is.
        self.assertIn("gevraagd via het web", log.output[0])

    def test_herstart_brms_met_een_fout_in_de_configuratie(self):
        with open(self.toml, "w") as f:
            f.write('[[sensor]]\nid = "x"\ntype = "bestaat-niet"\nnaam = "k"\n')
        with mock.patch.object(systeem, "herstart_services") as andere:
            r = self.client.post("/api/systeem/herstart", json={"wat": "brms"})
        self.assertEqual(r.status_code, 409)
        d = r.get_json()
        self.assertIn("er is niets herstart", d["fout"])
        self.assertTrue(any("'type' moet een van" in f for f in d["fouten"]))
        andere.assert_not_called()

    def test_herstart_zonder_toestemming(self):
        with mock.patch.object(systeem, "herstart_services", return_value="Access denied"), \
                mock.patch.object(systeem, "herstart_service") as zelf, \
                self.assertLogs("brms.web", "WARNING"):
            r = self.client.post("/api/systeem/herstart", json={"wat": "brms"})
        self.assertEqual(r.status_code, 500)
        self.assertIn("Access denied", r.get_json()["fout"])
        zelf.assert_not_called()

    def test_herstart_pi(self):
        with mock.patch.object(systeem, "herstart_pi", return_value=None) as pi, \
                self.assertLogs("brms.web", "WARNING"):
            r = self.client.post("/api/systeem/herstart", json={"wat": "pi"})
        self.assertEqual((r.status_code, r.get_json()), (200, {"gevraagd": "pi"}))
        pi.assert_called_once_with()
        with mock.patch.object(systeem, "herstart_pi", return_value="geen systemctl"), \
                self.assertLogs("brms.web", "WARNING"):
            self.assertEqual(self.client.post("/api/systeem/herstart", json={"wat": "pi"}).status_code, 500)

    def test_herstart_alleen_op_een_duidelijke_opdracht(self):
        with mock.patch.object(systeem, "herstart_pi") as pi, \
                mock.patch.object(systeem, "herstart_services") as brms:
            for inhoud in ({"wat": "alles"}, {}, [], {"wat": None}):
                self.assertEqual(self.client.post("/api/systeem/herstart", json=inhoud).status_code, 400)
            # Zonder JSON (een gewoon formulier van een andere website) gebeurt er niets.
            self.assertEqual(self.client.post("/api/systeem/herstart", data={"wat": "pi"}).status_code, 400)
            self.assertEqual(self.client.get("/api/systeem/herstart").status_code, 405)
        pi.assert_not_called()
        brms.assert_not_called()


if __name__ == "__main__":
    unittest.main()
