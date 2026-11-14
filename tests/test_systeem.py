"""Tests van het systeemmodule (ARCHITECTURE.md §5.5.5, §5.6 systeempagina): regels uit
journald, de melding bij een fout, de status van de services en het herstarten – zonder
systemd, met nagemaakte uitvoer van journalctl en systemctl."""

import asyncio
import json
import logging
import subprocess
import sys
import unittest
from unittest import mock

from brms import systeem
from brms.config import Config, MqttConfig, OpslagConfig


def _bericht(tekst="iets", prioriteit=4, us=1_800_000_000_000_000, **velden) -> dict:
    """Eén bericht zoals journalctl --output=json het geeft (alles is tekst)."""
    return {"__REALTIME_TIMESTAMP": str(us), "PRIORITY": str(prioriteit), "MESSAGE": tekst, **velden}


class TestLeesRegel(unittest.TestCase):
    def test_regel_van_een_service(self):
        regel = systeem.lees_regel(_bericht("WARNING brms.readers.camera: foto mislukt", 4,
                                            _SYSTEMD_UNIT="brms-camera.service"))
        # Het niveau staat in een eigen veld; het woord ervoor is uit de tekst gehaald.
        self.assertEqual(regel, {"ts": 1_800_000_000, "us": 1_800_000_000_000_000,
                                 "service": "brms-camera", "niveau": "waarschuwing",
                                 "tekst": "brms.readers.camera: foto mislukt"})
        fout = systeem.lees_regel(_bericht("ERROR brms.storage: schrijven mislukt", 3,
                                           _SYSTEMD_UNIT="brms-opslag.service"))
        self.assertEqual((fout["niveau"], fout["tekst"]), ("fout", "brms.storage: schrijven mislukt"))

    def test_alleen_brms_mosquitto_en_de_kernel(self):
        self.assertEqual(systeem.lees_regel(_bericht(_SYSTEMD_UNIT="mosquitto.service"))["service"],
                         "mosquitto")
        self.assertEqual(systeem.lees_regel(_bericht("I/O error, dev sda", 3, _TRANSPORT="kernel")),
                         {"ts": 1_800_000_000, "us": 1_800_000_000_000_000, "service": "kernel",
                          "niveau": "fout", "tekst": "I/O error, dev sda"})
        # Andere eenheden, en berichten van onbekende herkomst, horen niet in het logboek.
        for velden in ({"_SYSTEMD_UNIT": "ssh.service"}, {"_SYSTEMD_UNIT": "cron.service"},
                       {"_TRANSPORT": "syslog"}, {}):
            self.assertIsNone(systeem.lees_regel(_bericht(**velden)), velden)

    def test_gewone_regels_tellen_niet(self):
        for prioriteit in (5, 6, 7):
            self.assertIsNone(systeem.lees_regel(
                _bericht(prioriteit=prioriteit, _SYSTEMD_UNIT="brms-web.service")))
        # Niveaus 0–3 zijn allemaal 'fout'.
        for prioriteit in (0, 2, 3):
            self.assertEqual(systeem.lees_regel(
                _bericht(prioriteit=prioriteit, _SYSTEMD_UNIT="brms-web.service"))["niveau"], "fout")

    def test_gecrashte_service_is_een_fout(self):
        # systemd meldt dit zelf (PID 1) als waarschuwing, met de service in het veld UNIT.
        regel = systeem.lees_regel(_bericht("brms-ble-reader.service: Failed with result 'exit-code'.",
                                            4, UNIT="brms-ble-reader.service",
                                            _SYSTEMD_UNIT="init.scope"))
        self.assertEqual((regel["service"], regel["niveau"]), ("brms-ble-reader", "fout"))
        # Andere meldingen van systemd over de service blijven een waarschuwing.
        regel = systeem.lees_regel(_bericht("brms-web.service: Main process exited", 4,
                                            UNIT="brms-web.service"))
        self.assertEqual(regel["niveau"], "waarschuwing")

    def test_onbruikbare_berichten(self):
        self.assertIsNone(systeem.lees_regel("tekst"))
        self.assertIsNone(systeem.lees_regel({"MESSAGE": "x", "_SYSTEMD_UNIT": "brms-web.service"}))
        self.assertIsNone(systeem.lees_regel(_bericht("", _SYSTEMD_UNIT="brms-web.service")))
        # Tekst die geen geldige UTF-8 is, levert journald als een lijst van bytes.
        regel = systeem.lees_regel(_bericht(list("kapot \xff".encode("latin-1")),
                                            _SYSTEMD_UNIT="brms-web.service"))
        self.assertTrue(regel["tekst"].startswith("kapot"))
        # Een heel lange regel wordt afgekapt.
        lang = systeem.lees_regel(_bericht("x" * 5000, _SYSTEMD_UNIT="brms-web.service"))
        self.assertEqual(len(lang["tekst"]), systeem.MAX_TEKST)


def _journalctl(berichten):
    """Een nep-subprocess.run die deze berichten als uitvoer van journalctl geeft."""
    uitvoer = "\n".join(json.dumps(b) for b in berichten).encode()
    return mock.Mock(return_value=subprocess.CompletedProcess([], 0, uitvoer, b""))


class TestLogboek(unittest.TestCase):
    # De nieuwste eerst, zoals journalctl --reverse.
    BERICHTEN = [
        _bericht("ERROR a: vijf", 3, us=5_000_000, _SYSTEMD_UNIT="brms-opslag.service"),
        _bericht("gewoon", 6, us=4_500_000, _SYSTEMD_UNIT="brms-opslag.service"),
        _bericht("WARNING a: vier", 4, us=4_000_000, _SYSTEMD_UNIT="brms-camera.service"),
        _bericht("van iemand anders", 3, us=3_500_000, _SYSTEMD_UNIT="ssh.service"),
        _bericht("drie", 4, us=3_000_000, _TRANSPORT="kernel"),
        _bericht("WARNING a: twee", 4, us=2_000_000, _SYSTEMD_UNIT="brms-camera.service"),
        _bericht("ERROR a: een", 3, us=1_000_000, _SYSTEMD_UNIT="brms-camera.service"),
    ]

    def _lees(self, **keuze):
        with mock.patch.object(subprocess, "run", _journalctl(self.BERICHTEN)) as run:
            regels, eerder, vol, uitleg = systeem.logboek(**keuze)
        self.assertIsNone(uitleg)
        self.opdracht = run.call_args.args[0]
        self.vol = vol
        return [r["tekst"] for r in regels], eerder

    def test_alles_nieuwste_eerst(self):
        teksten, meer = self._lees()
        self.assertEqual(teksten, ["a: vijf", "a: vier", "drie", "a: twee", "a: een"])
        self.assertFalse(meer)
        # journalctl wordt gevraagd om JSON, de nieuwste eerst, vanaf niveau waarschuwing.
        self.assertEqual(self.opdracht[0], "journalctl")
        for vlag in ("--reverse", "--output=json", "--priority=4"):
            self.assertIn(vlag, self.opdracht)

    def test_keuze_van_niveau_en_service(self):
        self.assertEqual(self._lees(alleen_fouten=True)[0], ["a: vijf", "a: een"])
        self.assertEqual(self._lees(service="brms-camera")[0], ["a: vier", "a: twee", "a: een"])
        self.assertEqual(self._lees(service="kernel")[0], ["drie"])
        self.assertEqual(self._lees(service="brms-camera", alleen_fouten=True)[0], ["a: een"])

    def test_bladeren_per_aantal(self):
        # "Laatste 2", en dan telkens de 2 daarvoor (§5.6 systeempagina punt 3, v0.61).
        teksten, eerder = self._lees(aantal=2)
        self.assertEqual((teksten, eerder), (["a: vijf", "a: vier"], True))
        teksten, eerder = self._lees(aantal=2, overslaan=2)
        self.assertEqual((teksten, eerder), (["drie", "a: twee"], True))
        # Het laatste blad: de rest, en verder terug is er niets meer.
        teksten, eerder = self._lees(aantal=2, overslaan=4)
        self.assertEqual((teksten, eerder), (["a: een"], False))
        self.assertEqual(self._lees(aantal=2, overslaan=50), ([], False))
        # Het overslaan telt ná de filters: de fout vóór de nieuwste fout.
        self.assertEqual(self._lees(aantal=1, overslaan=1, alleen_fouten=True), (["a: een"], False))

    def test_periode(self):
        # Van (t/m) 2 s tot (niet t/m) 4 s: wat nieuwer is valt af, wat ouder is ook, maar
        # dat er iets ouders is, zegt `eerder`.
        self.assertEqual(self._lees(van=2, tot=4), (["drie", "a: twee"], True))
        self.assertFalse(self.vol)
        # Tot en met de oudste regel: verder terug is er niets.
        self.assertEqual(self._lees(van=0, tot=3), (["a: twee", "a: een"], False))
        # Een periode zonder regels.
        self.assertEqual(self._lees(van=100, tot=200), ([], True))
        # De filters gelden ook binnen een periode.
        self.assertEqual(self._lees(van=0, tot=10, service="kernel"), (["drie"], False))
        # Meer regels dan er passen: alleen de nieuwste, en `vol` meldt dat.
        with mock.patch.object(systeem, "PER_PAGINA", 2):
            self.assertEqual(self._lees(van=0, tot=10), (["a: vijf", "a: vier"], True))
        self.assertTrue(self.vol)

    def test_zonder_journalctl_of_met_een_fout(self):
        with mock.patch.object(subprocess, "run", side_effect=FileNotFoundError):
            regels, eerder, vol, uitleg = systeem.logboek()
        self.assertEqual((regels, eerder, vol), ([], False, False))
        self.assertIn("alleen op de Pi", uitleg)
        mislukt = mock.Mock(return_value=subprocess.CompletedProcess([], 1, b"", b"No journal files"))
        with mock.patch.object(subprocess, "run", mislukt):
            self.assertIn("No journal files", systeem.logboek()[3])
        with mock.patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("j", 10)):
            self.assertIn("kon niet gelezen worden", systeem.logboek()[3])


class TestFoutmelder(unittest.TestCase):
    def test_een_melding_per_service_per_uur(self):
        nu = [1000.0]
        melder = systeem.Foutmelder(klok=lambda: nu[0])
        eerste = melder.bij_fout("brms-opslag", "brms.storage: schrijven naar database mislukt")
        self.assertEqual(eerste.titel, "🛑 Fout in brms-opslag: brms.storage: schrijven naar database mislukt")
        self.assertEqual(eerste.prioriteit, "hoog")
        self.assertNotIn("Sinds de vorige melding", eerste.tekst)
        # Binnen het uur: geen melding meer van deze service, wel van een andere.
        nu[0] += 600
        self.assertIsNone(melder.bij_fout("brms-opslag", "nog een"))
        self.assertIsNone(melder.bij_fout("brms-opslag", "en nog een"))
        self.assertIsNotNone(melder.bij_fout("kernel", "I/O error"))
        # Na het uur: weer een melding, die noemt wat er intussen bij kwam.
        nu[0] += 3000
        volgende = melder.bij_fout("brms-opslag", "weer mis")
        self.assertIn("weer mis", volgende.titel)
        self.assertIn("nog 2 fouten van brms-opslag", volgende.tekst)
        # De teller begint daarna opnieuw.
        nu[0] += 3600
        self.assertNotIn("Sinds de vorige melding", melder.bij_fout("brms-opslag", "x").tekst)

    def test_lange_tekst_wordt_afgekapt(self):
        melding = systeem.Foutmelder().bij_fout("kernel", "x" * 2000)
        self.assertLess(len(melding.titel), systeem.MAX_MELDING + 40)


class TestFormatter(unittest.TestCase):
    def _regel(self, niveau, tekst, exc_info=None):
        record = logging.LogRecord("brms.storage", niveau, __file__, 1, tekst, None, exc_info)
        return systeem.JournaldFormatter("%(levelname)s %(name)s: %(message)s").format(record)

    def test_niveau_voor_de_regel(self):
        self.assertEqual(self._regel(logging.INFO, "gestart"), "<6>INFO brms.storage: gestart")
        self.assertEqual(self._regel(logging.WARNING, "let op"), "<4>WARNING brms.storage: let op")
        self.assertEqual(self._regel(logging.ERROR, "mis"), "<3>ERROR brms.storage: mis")
        self.assertEqual(self._regel(logging.CRITICAL, "stopt")[:3], "<2>")
        self.assertEqual(self._regel(logging.DEBUG, "detail")[:3], "<7>")

    def test_fout_met_uitzondering(self):
        try:
            raise OSError("schijf vol")
        except OSError:
            regels = self._regel(logging.ERROR, "schrijven mislukt", sys.exc_info()).split("\n")
        # De eerste regel zegt genoeg voor het logboek en voor een melding...
        self.assertEqual(regels[0], "<3>ERROR brms.storage: schrijven mislukt (OSError: schijf vol)")
        # ...de traceback staat erachter als gewone regels: wel in journald, niet als fout.
        self.assertGreater(len(regels), 2)
        self.assertTrue(all(r.startswith("<6>") for r in regels[1:]))
        self.assertIn("Traceback", regels[1])

    def test_de_eerste_regel_komt_zo_in_het_logboek(self):
        # systemd haalt <3> eraf en zet het als PRIORITY bij het bericht; zo leest BRMS het terug.
        tekst = self._regel(logging.ERROR, "schrijven mislukt")
        regel = systeem.lees_regel(_bericht(tekst[3:], int(tekst[1]), _SYSTEMD_UNIT="brms-opslag.service"))
        self.assertEqual((regel["niveau"], regel["tekst"]), ("fout", "brms.storage: schrijven mislukt"))


SHOW = """Id=brms-web.service
LoadState=loaded
ActiveState=active
SubState=running
ActiveEnterTimestampMonotonic=22000000
NRestarts=0

Id=brms-camera.service
LoadState=loaded
ActiveState=activating
SubState=auto-restart
ActiveEnterTimestampMonotonic=30000000
NRestarts=7

Id=brms-oud.service
LoadState=not-found
ActiveState=inactive
SubState=dead
ActiveEnterTimestampMonotonic=0
NRestarts=0

Id=mosquitto.service
LoadState=loaded
ActiveState=failed
SubState=failed
ActiveEnterTimestampMonotonic=0
NRestarts=0
"""


class TestServices(unittest.TestCase):
    def test_uitvoer_van_systemctl_show(self):
        # De Pi staat 100 s aan (monotone klok) en het is nu 1000000.
        lijst = systeem.lees_show(SHOW, nu=1_000_000.0, monotoon=100.0)
        self.assertEqual(lijst, [
            # 22 s na het opstarten gestart: 78 s geleden.
            {"naam": "brms-web", "actief": True, "toestand": "active (running)",
             "sinds": 999_922, "herstarts": 0},
            {"naam": "brms-camera", "actief": False, "toestand": "activating (auto-restart)",
             "sinds": None, "herstarts": 7},
            # brms-oud kent systemd niet: overgeslagen.
            {"naam": "mosquitto", "actief": False, "toestand": "failed (failed)",
             "sinds": None, "herstarts": 0}])

    def test_services(self):
        run = mock.Mock(return_value=subprocess.CompletedProcess([], 0, SHOW, ""))
        with mock.patch.object(subprocess, "run", run):
            lijst, uitleg = systeem.services()
        self.assertIsNone(uitleg)
        self.assertEqual([s["naam"] for s in lijst], ["brms-web", "brms-camera", "mosquitto"])
        opdracht = run.call_args.args[0]
        # Alle units uit deploy/systemd/ en Mosquitto worden gevraagd.
        self.assertEqual(opdracht[:2], ["systemctl", "show"])
        for naam in ("brms-web", "brms-opslag", "brms-alarm", "mosquitto"):
            self.assertIn(naam, opdracht)

    def test_zonder_systemd(self):
        with mock.patch.object(subprocess, "run", side_effect=FileNotFoundError):
            self.assertEqual(systeem.services(), (
                [], "systemctl is er niet op dit systeem; de status werkt alleen op de Pi"))

    def test_schijf(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as map_:
            pad = os.path.join(map_, "brms.db")
            with open(pad, "wb") as f:
                f.write(b"x" * 1000)
            with open(pad + "-wal", "wb") as f:
                f.write(b"x" * 24)
            s = systeem.schijf(Config(MqttConfig(), OpslagConfig(pad), {}))
        self.assertEqual(s["database"], 1024)           # het bestand plus het WAL-bestand
        self.assertGreater(s["totaal"], 0)
        self.assertEqual(s["gebruikt"] + s["vrij"] <= s["totaal"] * 1.01, True)


class TestHerstarten(unittest.TestCase):
    def _run(self, code=0, fout=""):
        return mock.Mock(return_value=subprocess.CompletedProcess([], code, "", fout))

    def test_services_herstarten(self):
        run = self._run()
        with mock.patch.object(subprocess, "run", run):
            self.assertIsNone(systeem.herstart_services(behalve="brms-web"))
        opdracht = run.call_args.args[0]
        # Zonder wachtwoordvraag (de toestemming komt van polkit) en zonder te wachten.
        self.assertEqual(opdracht[:4], ["systemctl", "--no-ask-password", "--no-block", "restart"])
        self.assertIn("brms-opslag", opdracht)
        self.assertNotIn("brms-web", opdracht)           # die herstart als laatste, apart
        self.assertTrue(all(naam.startswith("brms-") for naam in opdracht[4:]))

    def test_pi_herstarten(self):
        run = self._run()
        with mock.patch.object(subprocess, "run", run):
            self.assertIsNone(systeem.herstart_pi())
        self.assertEqual(run.call_args.args[0], ["systemctl", "--no-ask-password", "--no-wall", "reboot"])

    def test_geen_toestemming(self):
        run = self._run(1, "Failed to restart brms-opslag.service: Interactive authentication required.")
        with mock.patch.object(subprocess, "run", run):
            fout = systeem.herstart_service("brms-opslag")
        self.assertIn("Interactive authentication required", fout)
        self.assertIn("polkit-regel", fout)              # met de aanwijzing wat eraan te doen is

    def test_zonder_systemd(self):
        with mock.patch.object(subprocess, "run", side_effect=FileNotFoundError):
            self.assertIn("alleen op de Pi", systeem.herstart_pi())


class TestVolg(unittest.TestCase):
    def test_zonder_journalctl_doet_volgen_niets(self):
        async def proef():
            with mock.patch.object(asyncio, "create_subprocess_exec", side_effect=FileNotFoundError):
                await asyncio.wait_for(systeem.volg(lambda regel: None, asyncio.Event()), 2)
        with self.assertLogs("brms.systeem", "INFO"):
            asyncio.run(proef())

    def test_regels_komen_binnen_tot_het_stopverzoek(self):
        """Met een nagemaakt journalctl: twee regels, waarvan één in het logboek hoort."""
        gezien = []

        class NepUitvoer:
            def __init__(self):
                self.regels = [
                    json.dumps(_bericht("ERROR a: mis", 3, _SYSTEMD_UNIT="brms-opslag.service")).encode() + b"\n",
                    json.dumps(_bericht("van iemand anders", 3, _SYSTEMD_UNIT="ssh.service")).encode() + b"\n",
                    b"geen json\n"]

            async def readline(self):
                if self.regels:
                    return self.regels.pop(0)
                await asyncio.sleep(3600)        # journalctl --follow wacht op de volgende regel

        class NepProces:
            returncode = None

            def __init__(self):
                self.stdout = NepUitvoer()

            def terminate(self):
                self.returncode = -15

            async def wait(self):
                return self.returncode

        async def proef():
            stop = asyncio.Event()
            with mock.patch.object(asyncio, "create_subprocess_exec",
                                   mock.AsyncMock(return_value=NepProces())) as start:
                taak = asyncio.create_task(systeem.volg(gezien.append, stop))
                await asyncio.sleep(0.2)
                stop.set()
                await asyncio.wait_for(taak, 2)
            return start.call_args.args

        opdracht = asyncio.run(proef())
        self.assertEqual([(r["service"], r["tekst"]) for r in gezien], [("brms-opslag", "a: mis")])
        # Alleen wat er vanaf nu bij komt: oude fouten worden na een herstart niet opnieuw gemeld.
        self.assertIn("--follow", opdracht)
        self.assertIn("--lines=0", opdracht)


if __name__ == "__main__":
    unittest.main()
