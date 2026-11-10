"""Tests van de webinterface (ARCHITECTURE.md §5.6) – zonder broker, met een tijdelijke
database en de testclient van Flask."""

import os
import pathlib
import sqlite3
import tempfile
import unittest

from brms import config, web
from brms.config import Config, MqttConfig, OpslagConfig, Sensor
from brms.meting import maak
from brms.storage import Database

MAC = "A4:C1:38:00:00:01"
MAC2 = "A4:C1:38:00:00:02"


def _config(database: str) -> Config:
    return Config(MqttConfig(), OpslagConfig(database),
                  {MAC: Sensor(MAC, "bthome", "kajuit"), MAC2: Sensor(MAC2, "ruuvi", "motorruim")})


def _vraag(sensor_id, grootheid, **extra):
    return {"sensor_id": sensor_id, "grootheid": grootheid, **extra}


class TestLiveBuffer(unittest.TestCase):
    def test_alleen_nieuwe_metingen_van_de_gevraagde_reeks(self):
        live = web.LiveBuffer()
        live.voeg_toe(maak(100, MAC, "temperatuur", 8.0))
        live.voeg_toe(maak(100, MAC2, "temperatuur", 3.0))
        live.voeg_toe(maak(100, MAC, "vocht", 50.0))
        # Meerdere reeksen in één keer, ook van verschillende sensoren.
        nr, punten = live.lees([_vraag(MAC, "temperatuur"), _vraag(MAC2, "temperatuur"),
                                _vraag(MAC, "vocht"), _vraag(MAC, "luchtdruk")])
        self.assertEqual(nr, 3)
        self.assertEqual(punten, [[[100, 8.0]], [[100, 3.0]], [[100, 50.0]], []])

        # Tweede vraag: alleen wat er sinds het vorige volgnummer bij kwam, ook als het in
        # dezelfde seconde valt.
        live.voeg_toe(maak(100, MAC, "temperatuur", 8.1))
        nr2, punten = live.lees([_vraag(MAC, "temperatuur", na=nr), _vraag(MAC, "vocht", na=nr)])
        self.assertEqual((nr2, punten), (4, [[[100, 8.1]], []]))

    def test_venster_in_tijd_en_in_aantal(self):
        live = web.LiveBuffer()
        for i in range(10):
            live.voeg_toe(maak(100 + i, MAC, "temperatuur", float(i)))

        def lees(**extra):
            return live.lees([_vraag(MAC, "temperatuur", **extra)])[1][0]

        self.assertEqual(lees(aantal=3), [[107, 7.0], [108, 8.0], [109, 9.0]])
        self.assertEqual(lees(vanaf=108), [[108, 8.0], [109, 9.0]])
        self.assertEqual(lees(vanaf=500), [])

    def test_oud_wordt_opgeruimd(self):
        # Bewaartijd 80 s: wat ouder is, verdwijnt (in stappen, niet bij elke meting).
        live = web.LiveBuffer(bewaar=80)
        for ts in range(0, 1000, 10):
            live.voeg_toe(maak(ts, MAC, "temperatuur", float(ts)))
        punten = live.lees([_vraag(MAC, "temperatuur")])[1][0]
        self.assertEqual(punten[-1], [990, 990.0])
        self.assertGreaterEqual(punten[0][0], 990 - 80 - 10 - 10)
        self.assertLess(len(punten), 12)

    def test_bovengrens_per_reeks(self):
        live = web.LiveBuffer(maximum=80)
        for i in range(1000):
            live.voeg_toe(maak(5, MAC, "temperatuur", float(i)))   # alles in dezelfde seconde
        punten = live.lees([_vraag(MAC, "temperatuur")])[1][0]
        self.assertLessEqual(len(punten), 90)
        self.assertEqual(punten[-1], [5, 999.0])

    def test_samenvatten_per_tijdvak(self):
        live = web.LiveBuffer()
        # Tijdvakken van 10 s: [100, 110) en [110, 120).
        for ts, w in [(101, 2.0), (105, 4.0), (112, 10.0)]:
            live.voeg_toe(maak(ts, MAC, "temperatuur", w))    # doorlopend: gemiddelde
            live.voeg_toe(maak(ts, MAC, "beweging", w))       # toename: som
        for ts, w in [(101, 0), (105, 1), (108, 0), (112, 0)]:
            live.voeg_toe(maak(ts, MAC, "bilgepomp", w))      # toestand: een 1 blijft zichtbaar
        _, punten = live.lees([_vraag(MAC, g, stap=10)
                               for g in ("temperatuur", "beweging", "bilgepomp")])
        self.assertEqual(punten, [[[100, 3.0], [110, 10.0]],
                                  [[100, 6.0], [110, 10.0]],
                                  [[100, 1.0], [110, 0.0]]])
        # Een vervolgvraag vanaf een tijdvak geeft dezelfde vakken terug.
        self.assertEqual(live.lees([_vraag(MAC, "temperatuur", vanaf=110, stap=10)])[1],
                         [[[110, 10.0]]])

    def test_bericht_van_de_bus(self):
        live = web.LiveBuffer()
        live.bij_bericht("brms/meting/x", maak(5, MAC, "vocht", 40).naar_json().encode())
        live.bij_bericht("brms/meting/x", b"kapot")   # wordt genegeerd
        self.assertEqual(live.lees([_vraag(MAC, "vocht")]), (1, [[[5, 40.0]]]))


class TestWeb(unittest.TestCase):
    def setUp(self):
        self._map = tempfile.TemporaryDirectory()
        self.pad = os.path.join(self._map.name, "brms.db")
        db = Database(self.pad)
        db.schrijf([(300, MAC, "temperatuur", 8.0), (600, MAC, "temperatuur", 7.5),
                    (900, MAC, "temperatuur", 7.0), (600, MAC, "vocht", 55.0),
                    (600, MAC2, "temperatuur", 3.0)])
        db.sluit()
        self.live = web.LiveBuffer()
        # De klok van de Pi staat in deze tests op 1000.
        self.client = web.maak_app(_config(self.pad), self.live, klok=lambda: 1000.7).test_client()

    def tearDown(self):
        self._map.cleanup()

    def _live(self, *vragen):
        r = self.client.post("/api/live", json={"vragen": list(vragen)})
        self.assertEqual(r.status_code, 200)
        return r.get_json()

    def test_pagina(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        tekst = r.get_data(as_text=True)
        for verwacht in ["kajuit", "motorruim", MAC, "chart.umd.min.js", '"temperatuur"']:
            self.assertIn(verwacht, tekst)
        # De grootheden staan in de volgorde van de tabel (§5.3.3), niet alfabetisch.
        self.assertLess(tekst.index('"temperatuur": {'), tekst.index('"batterijspanning": {'))

    def test_pagina_biedt_per_sensor_zijn_eigen_grootheden(self):
        cfg = Config(MqttConfig(), OpslagConfig(self.pad), {
            MAC: Sensor(MAC, "bthome", "kajuit", {}, ("temperatuur", "rssi")),
            MAC2: Sensor(MAC2, "ruuvi", "motorruim")})
        tekst = web.maak_app(cfg, self.live).test_client().get("/").get_data(as_text=True)
        # kajuit: alleen wat in zijn lijst 'grootheden' staat; motorruim: alles van een Ruuvi.
        self.assertIn('"grootheden": ["temperatuur", "rssi"]', tekst)
        self.assertIn('"luchtdruk", "versnelling_x"', tekst)

    def test_chartjs_staat_lokaal(self):
        r = self.client.get("/static/chart.umd.min.js")
        self.assertEqual(r.status_code, 200)
        r.close()

    def test_historie(self):
        d = self.client.get(f"/api/historie?sensor={MAC}&grootheid=temperatuur&van=300&tot=600").get_json()
        self.assertEqual(d, {"van": 300, "tot": 600, "punten": [[300, 8.0], [600, 7.5]]})
        d = self.client.get(f"/api/historie?sensor={MAC2}&grootheid=temperatuur&van=0&tot=999").get_json()
        self.assertEqual(d["punten"], [[600, 3.0]])

    def test_historie_zonder_gegevens(self):
        d = self.client.get(f"/api/historie?sensor={MAC}&grootheid=vocht&van=5000&tot=6000").get_json()
        self.assertEqual(d["punten"], [])

    def test_historie_begrensd_op_de_langste_periode(self):
        tot = 10 * web.MAX_PERIODE
        d = self.client.get(f"/api/historie?sensor={MAC}&grootheid=vocht&van=0&tot={tot}").get_json()
        self.assertEqual(d["van"], tot - web.MAX_PERIODE)

    def test_historie_zonder_database(self):
        client = web.maak_app(_config(os.path.join(self._map.name, "bestaat-niet.db")),
                              self.live).test_client()
        d = client.get(f"/api/historie?sensor={MAC}&grootheid=vocht&van=0&tot=1000").get_json()
        self.assertEqual(d["punten"], [])

    def test_verbinding_is_alleen_lezen(self):
        # Zo opent het web de database (§5.6 punt 3): wijzigen kan dan niet.
        con = sqlite3.connect(pathlib.Path(self.pad).resolve().as_uri() + "?mode=ro", uri=True)
        with self.assertRaises(sqlite3.OperationalError):
            con.execute("DELETE FROM meting")
        con.close()

    def test_live_meerdere_reeksen_in_een_aanvraag(self):
        self.live.voeg_toe(maak(100, MAC, "temperatuur", 8.0))
        self.live.voeg_toe(maak(100, MAC2, "vocht", 60.0))
        d = self._live({"sensor": MAC.lower(), "grootheid": "temperatuur"},
                       {"sensor": MAC2, "grootheid": "vocht"},
                       {"sensor": "onbekend", "grootheid": "vocht"},
                       {"sensor": MAC, "grootheid": "onzin"}, "geen dict")
        self.assertEqual((d["nu"], d["nr"]), (1000, 2))
        self.assertEqual(d["punten"], [[[100, 8.0]], [[100, 60.0]], [], [], []])
        # Vervolgvraag: niets nieuws.
        self.assertEqual(self._live({"sensor": MAC, "grootheid": "temperatuur", "na": 2})["punten"], [[]])

    def test_live_venster(self):
        # Metingen op 100, 200, ... 1000; de klok van de Pi staat op 1000.
        for ts in range(100, 1001, 100):
            self.live.voeg_toe(maak(ts, MAC, "temperatuur", ts / 100))

        def haal(**extra):
            return self._live({"sensor": MAC, "grootheid": "temperatuur", **extra})["punten"][0]

        # Laatste 250 s, gerekend vanaf de klok van de Pi.
        self.assertEqual(haal(venster=250), [[800, 8.0], [900, 9.0], [1000, 10.0]])
        self.assertEqual(haal(aantal=2), [[900, 9.0], [1000, 10.0]])
        # Samengevat per 300 s: vakken [600, 900) en [900, 1200).
        self.assertEqual(haal(venster=450, stap=300), [[600, 7.0], [900, 9.5]])
        self.assertEqual(haal(vanaf=900, stap=300), [[900, 9.5]])
        # Onzin in de aanvraag geeft geen fout.
        self.assertEqual(len(haal(aantal="x", stap="y", venster="z")), 10)

    def test_fouten(self):
        self.assertEqual(self.client.post("/api/live", json={"vragen": "x"}).status_code, 400)
        self.assertEqual(self.client.post("/api/live", data="kapot").status_code, 400)
        te_veel = [{"sensor": MAC, "grootheid": "vocht"}] * (web.MAX_VRAGEN + 1)
        self.assertEqual(self.client.post("/api/live", json={"vragen": te_veel}).status_code, 400)
        hist = "/api/historie?sensor={}&grootheid={}&van={}&tot={}"
        self.assertEqual(self.client.get(hist.format("onbekend", "vocht", 0, 9)).status_code, 404)
        self.assertEqual(self.client.get(hist.format(MAC, "onzin", 0, 9)).status_code, 404)
        self.assertEqual(self.client.get(hist.format(MAC, "vocht", 9, 1)).status_code, 400)
        self.assertEqual(self.client.get(hist.format(MAC, "vocht", "x", "y")).status_code, 400)


class TestWebConfig(unittest.TestCase):
    def test_standaard_en_ingesteld(self):
        self.assertEqual(config.controleer({}).web, config.WebConfig("0.0.0.0", 8080))
        self.assertEqual(config.controleer({"web": {"host": "127.0.0.1", "port": 8000}}).web,
                         config.WebConfig("127.0.0.1", 8000))

    def test_fouten(self):
        with self.assertRaises(config.ConfigFout) as ctx:
            config.controleer({"web": {"host": "", "port": 0, "poort": 1}})
        fouten = "\n".join(ctx.exception.fouten)
        for verwacht in ["[web] host", "[web] port", "onbekende sleutel 'poort'"]:
            self.assertIn(verwacht, fouten)


if __name__ == "__main__":
    unittest.main()
