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


class TestLiveBuffer(unittest.TestCase):
    def test_alleen_nieuwe_metingen_van_de_gevraagde_sensor(self):
        live = web.LiveBuffer()
        live.voeg_toe(maak(100, MAC, "temperatuur", 8.0))
        live.voeg_toe(maak(100, MAC2, "temperatuur", 3.0))
        live.voeg_toe(maak(100, MAC, "vocht", 50.0))
        nr, reeksen = live.sinds(MAC)
        self.assertEqual(nr, 3)
        self.assertEqual(reeksen, {"temperatuur": [[100, 8.0]], "vocht": [[100, 50.0]]})

        # Tweede vraag: alleen wat er sinds het vorige volgnummer bij kwam, ook als het in
        # dezelfde seconde valt.
        live.voeg_toe(maak(100, MAC, "temperatuur", 8.1))
        nr2, reeksen = live.sinds(MAC, nr)
        self.assertEqual((nr2, reeksen), (4, {"temperatuur": [[100, 8.1]]}))
        self.assertEqual(live.sinds(MAC, nr2), (4, {}))

    def test_oudste_valt_eraf(self):
        live = web.LiveBuffer(punten=3)
        for i in range(5):
            live.voeg_toe(maak(i, MAC, "temperatuur", float(i)))
        self.assertEqual(live.sinds(MAC)[1], {"temperatuur": [[2, 2.0], [3, 3.0], [4, 4.0]]})

    def test_bericht_van_de_bus(self):
        live = web.LiveBuffer()
        live.bij_bericht("brms/meting/x", maak(5, MAC, "vocht", 40).naar_json().encode())
        live.bij_bericht("brms/meting/x", b"kapot")   # wordt genegeerd
        self.assertEqual(live.sinds(MAC), (1, {"vocht": [[5, 40.0]]}))


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
        self.client = web.maak_app(_config(self.pad), self.live).test_client()

    def tearDown(self):
        self._map.cleanup()

    def test_pagina(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        tekst = r.get_data(as_text=True)
        for verwacht in ["kajuit (bthome)", "motorruim (ruuvi)", MAC, "chart.umd.min.js",
                         '"temperatuur"']:
            self.assertIn(verwacht, tekst)
        # De grootheden staan in de volgorde van de tabel (§5.3.3), niet alfabetisch.
        self.assertLess(tekst.index('"temperatuur"'), tekst.index('"batterijspanning"'))

    def test_chartjs_staat_lokaal(self):
        r = self.client.get("/static/chart.umd.min.js")
        self.assertEqual(r.status_code, 200)
        r.close()

    def test_historie(self):
        d = self.client.get(f"/api/historie?sensor={MAC}&van=300&tot=600").get_json()
        self.assertEqual(d["reeksen"], {"temperatuur": [[300, 8.0], [600, 7.5]],
                                        "vocht": [[600, 55.0]]})
        self.assertEqual((d["van"], d["tot"]), (300, 600))

    def test_historie_zonder_gegevens(self):
        d = self.client.get(f"/api/historie?sensor={MAC}&van=5000&tot=6000").get_json()
        self.assertEqual(d["reeksen"], {})

    def test_historie_begrensd_op_de_langste_periode(self):
        tot = 10 * web.MAX_PERIODE
        d = self.client.get(f"/api/historie?sensor={MAC}&van=0&tot={tot}").get_json()
        self.assertEqual(d["van"], tot - web.MAX_PERIODE)

    def test_historie_zonder_database(self):
        client = web.maak_app(_config(os.path.join(self._map.name, "bestaat-niet.db")),
                              self.live).test_client()
        d = client.get(f"/api/historie?sensor={MAC}&van=0&tot=1000").get_json()
        self.assertEqual(d["reeksen"], {})

    def test_verbinding_is_alleen_lezen(self):
        # Zo opent het web de database (§5.6 punt 3): wijzigen kan dan niet.
        con = sqlite3.connect(pathlib.Path(self.pad).resolve().as_uri() + "?mode=ro", uri=True)
        with self.assertRaises(sqlite3.OperationalError):
            con.execute("DELETE FROM meting")
        con.close()

    def test_live(self):
        self.live.voeg_toe(maak(100, MAC, "temperatuur", 8.0))
        d = self.client.get(f"/api/live?sensor={MAC.lower()}").get_json()
        self.assertEqual((d["nr"], d["reeksen"]), (1, {"temperatuur": [[100, 8.0]]}))
        d = self.client.get(f"/api/live?sensor={MAC}&na=1").get_json()
        self.assertEqual(d["reeksen"], {})

    def test_fouten(self):
        self.assertEqual(self.client.get("/api/live?sensor=onbekend").status_code, 404)
        self.assertEqual(self.client.get("/api/historie?sensor=onbekend&van=0&tot=9").status_code, 404)
        self.assertEqual(self.client.get(f"/api/historie?sensor={MAC}&van=9&tot=1").status_code, 400)
        self.assertEqual(self.client.get(f"/api/historie?sensor={MAC}&van=x&tot=y").status_code, 400)


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
