import os
import sqlite3
import tempfile
import unittest

from brms.meting import maak
from brms.storage import INTERVAL, Aggregator, Database, volgend_moment

S = "A4:C1:38:00:00:01"


class TestAggregator(unittest.TestCase):
    def test_doorlopend_gemiddelde(self):
        a = Aggregator()
        for w in (1.0, 2.0, 4.0):
            a.voeg_toe(maak(10, S, "temperatuur", w))
        self.assertEqual(a.leeg(300), [(300, S, "temperatuur", 2.3333)])

    def test_interval_zonder_data_levert_niets(self):
        a = Aggregator()
        a.voeg_toe(maak(10, S, "vocht", 50))
        a.leeg(300)
        self.assertEqual(a.leeg(600), [])

    def test_toename_som(self):
        a = Aggregator()
        for w in (1, 0, 3):
            a.voeg_toe(maak(10, S, "beweging", w))
        self.assertEqual(a.leeg(300), [(300, S, "beweging", 4.0)])

    def test_toestand_wijzigingen_en_actuele_toestand(self):
        a = Aggregator()
        a.voeg_toe(maak(10, "M", "walstroom", 1))   # eerste waarde = verandering
        a.voeg_toe(maak(20, "M", "walstroom", 1))   # geen verandering
        a.voeg_toe(maak(30, "M", "walstroom", 0))   # verandering
        self.assertEqual(a.leeg(300), [(10, "M", "walstroom", 1.0), (30, "M", "walstroom", 0.0),
                                       (300, "M", "walstroom", 0.0)])
        # volgend interval: zelfde toestand -> alleen de actuele toestand, geen wijzigingsrij
        a.voeg_toe(maak(310, "M", "walstroom", 0))
        self.assertEqual(a.leeg(600), [(600, "M", "walstroom", 0.0)])
        # interval zonder bericht van de MultiPlus -> geen rij (geen 0!)
        self.assertEqual(a.leeg(900), [])


class TestDatabase(unittest.TestCase):
    def test_schrijven_en_wal(self):
        with tempfile.TemporaryDirectory() as d:
            pad = os.path.join(d, "sub", "brms.db")
            db = Database(pad)
            db.schrijf([(300, S, "temperatuur", 2.5), (300, S, "vocht", 60.0)])
            # zelfde sleutel nogmaals: vervangt, geen fout
            db.schrijf([(300, S, "temperatuur", 2.6)])
            db.sluit()
            con = sqlite3.connect(pad)
            self.assertEqual(con.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(con.execute("SELECT ts, sensor_id, grootheid, waarde FROM meting "
                                         "ORDER BY grootheid").fetchall(),
                             [(300, S, "temperatuur", 2.6), (300, S, "vocht", 60.0)])
            con.close()


class TestMoment(unittest.TestCase):
    def test_volgend_moment(self):
        self.assertEqual(volgend_moment(0), INTERVAL)
        self.assertEqual(volgend_moment(299.9), 300)
        self.assertEqual(volgend_moment(300), 600)


if __name__ == "__main__":
    unittest.main()
