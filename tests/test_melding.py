"""Tests van de meldingskanalen (ARCHITECTURE.md §5.5.4) – zonder internet: het netwerk
(urllib, smtplib) is vervangen. Er wordt in deze tests niets echt verstuurd."""

import asyncio
import io
import json
import smtplib
import unittest
import urllib.error
from unittest import mock

from brms import melding
from brms.config import EmailConfig, MeldingConfig, TelegramConfig
from brms.melding import MeldFout, Melding, Verzender

TOKEN = "123456:GEHEIME-token"
TG = TelegramConfig(TOKEN, "-100200300")
MAIL = EmailConfig("smtp.voorbeeld.nl", 587, "boot@voorbeeld.nl", "GEHEIM-wachtwoord",
                   "boot@voorbeeld.nl", ("ik@voorbeeld.nl", "jij@voorbeeld.nl"))
VORST = Melding("⚠️ Vorst – kajuit: temperatuur 1,6 °C (onder 2 °C)", prioriteit="hoog")


class _Antwoord(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


class TestTelegram(unittest.TestCase):
    def test_bericht(self):
        with mock.patch("urllib.request.urlopen", return_value=_Antwoord(b'{"ok": true}')) as u:
            melding.telegram(TG, VORST)
        aanvraag = u.call_args.args[0]
        self.assertEqual(aanvraag.full_url, f"https://api.telegram.org/bot{TOKEN}/sendMessage")
        self.assertEqual(json.loads(aanvraag.data), {
            "chat_id": "-100200300", "text": VORST.titel, "disable_notification": False})

    def test_normaal_zonder_geluid_en_met_toelichting(self):
        with mock.patch("urllib.request.urlopen", return_value=_Antwoord(b'{"ok": true}')) as u:
            melding.telegram(TG, Melding("Titel", "Toelichting"))
        inhoud = json.loads(u.call_args.args[0].data)
        self.assertEqual((inhoud["text"], inhoud["disable_notification"]),
                         ("Titel\nToelichting", True))

    def test_fout_van_telegram_zonder_de_token(self):
        fout = urllib.error.HTTPError(f"https://api.telegram.org/bot{TOKEN}/sendMessage", 401,
                                      "Unauthorized", {},
                                      io.BytesIO(b'{"ok": false, "description": "Unauthorized"}'))
        with mock.patch("urllib.request.urlopen", side_effect=fout):
            with self.assertRaises(MeldFout) as ctx:
                melding.telegram(TG, VORST)
        self.assertEqual(str(ctx.exception), "Telegram: fout 401 Unauthorized")

    def test_geen_internet_en_geheim_blijft_uit_de_melding(self):
        fout = urllib.error.URLError(f"geen verbinding met bot{TOKEN}")
        with mock.patch("urllib.request.urlopen", side_effect=fout):
            with self.assertRaises(MeldFout) as ctx:
                melding.telegram(TG, VORST)
        self.assertNotIn(TOKEN, str(ctx.exception))
        with mock.patch("urllib.request.urlopen", return_value=_Antwoord(b'{"ok": false}')):
            with self.assertRaises(MeldFout):
                melding.telegram(TG, VORST)

    def test_met_foto_als_afbeelding_met_onderschrift(self):
        jpeg = b"\xff\xd8\xff\xe0beeld\r\n--nog-meer"
        m = Melding("Titel", "Toelichting", prioriteit="hoog", foto=jpeg, foto_naam="dek-203000.jpg")
        with mock.patch("urllib.request.urlopen", return_value=_Antwoord(b'{"ok": true}')) as u:
            melding.telegram(TG, m)
        aanvraag = u.call_args.args[0]
        self.assertEqual(aanvraag.full_url, f"https://api.telegram.org/bot{TOKEN}/sendPhoto")
        soort = aanvraag.get_header("Content-type")
        self.assertTrue(soort.startswith("multipart/form-data; boundary="))
        # Het formulier is te lezen met de e-mailparser van Python: zo leest Telegram het ook.
        import email
        bericht = email.message_from_bytes(
            b"Content-Type: " + soort.encode() + b"\r\n\r\n" + aanvraag.data)
        delen = {d.get_param("name", header="content-disposition"): d for d in bericht.get_payload()}
        self.assertEqual(delen["chat_id"].get_payload(), "-100200300")
        self.assertEqual(delen["caption"].get_payload(decode=True).decode(), "Titel\nToelichting")
        self.assertEqual(delen["disable_notification"].get_payload(), "false")
        self.assertEqual(delen["photo"].get_filename(), "dek-203000.jpg")
        self.assertEqual(delen["photo"].get_content_type(), "image/jpeg")
        self.assertEqual(delen["photo"].get_payload(decode=True), jpeg)

    def test_foto_niet_in_repr(self):
        self.assertNotIn("beeld", repr(Melding("Titel", foto=b"beeld")))

    def test_token_niet_in_repr(self):
        self.assertNotIn(TOKEN, repr(MeldingConfig(TG, MAIL)))
        self.assertNotIn(MAIL.wachtwoord, repr(MeldingConfig(TG, MAIL)))


class TestEmail(unittest.TestCase):
    def test_starttls_op_587(self):
        with mock.patch("smtplib.SMTP") as smtp:
            melding.email(MAIL, VORST)
        server = smtp.return_value
        smtp.assert_called_once_with("smtp.voorbeeld.nl", 587, timeout=melding.TIMEOUT)
        server.starttls.assert_called_once()
        actief = server   # de code meldt zich aan op de server zelf
        actief.login.assert_called_once_with("boot@voorbeeld.nl", "GEHEIM-wachtwoord")
        bericht = actief.send_message.call_args.args[0]
        self.assertEqual(bericht["Subject"], f"[BRMS HOOG] {VORST.titel}")
        self.assertEqual((bericht["From"], bericht["To"]),
                         ("boot@voorbeeld.nl", "ik@voorbeeld.nl, jij@voorbeeld.nl"))
        self.assertIn("Vorst – kajuit", bericht.get_content())

    def test_met_foto_als_bijlage(self):
        jpeg = b"\xff\xd8\xff\xe0beeld"
        with mock.patch("smtplib.SMTP") as smtp:
            melding.email(MAIL, Melding("Titel", foto=jpeg, foto_naam="dek-203000.jpg"))
        bericht = smtp.return_value.send_message.call_args.args[0]
        (bijlage,) = list(bericht.iter_attachments())
        self.assertEqual((bijlage.get_content_type(), bijlage.get_filename()),
                         ("image/jpeg", "dek-203000.jpg"))
        self.assertEqual(bijlage.get_content(), jpeg)
        self.assertIn("Titel", bericht.get_body().get_content())

    def test_ssl_op_465(self):
        cfg = EmailConfig("smtp.voorbeeld.nl", 465, "u", "w", "a@b.nl", ("c@d.nl",))
        with mock.patch("smtplib.SMTP_SSL") as ssl_, mock.patch("smtplib.SMTP") as gewoon:
            melding.email(cfg, Melding("Titel"))
        ssl_.assert_called_once()
        gewoon.assert_not_called()
        bericht = ssl_.return_value.send_message.call_args.args[0]
        self.assertEqual(bericht["Subject"], "[BRMS] Titel")

    def test_fout(self):
        with mock.patch("smtplib.SMTP", side_effect=OSError("timed out")):
            with self.assertRaises(MeldFout) as ctx:
                melding.email(MAIL, VORST)
        self.assertEqual(str(ctx.exception), "e-mail: timed out")
        fout = smtplib.SMTPAuthenticationError(535, b"wachtwoord GEHEIM-wachtwoord geweigerd")
        with mock.patch("smtplib.SMTP") as smtp:
            smtp.return_value.login.side_effect = fout
            with self.assertRaises(MeldFout) as ctx:
                melding.email(MAIL, VORST)
        self.assertNotIn("GEHEIM-wachtwoord", str(ctx.exception))


class TestKanalen(unittest.TestCase):
    def test_alleen_wat_is_ingesteld(self):
        self.assertEqual(melding.kanalen(MeldingConfig()), {})
        self.assertEqual(list(melding.kanalen(MeldingConfig(telegram=TG))), ["Telegram"])
        self.assertEqual(list(melding.kanalen(MeldingConfig(TG, MAIL))), ["Telegram", "e-mail"])


class TestVerzender(unittest.TestCase):
    def setUp(self):
        self.t = 1000.0
        self.verstuurd = []          # (kanaal, titel)
        self.stuk = set()            # kanalen die nu niet werken

        def kanaal(naam):
            def stuur(m):
                if naam in self.stuk:
                    raise MeldFout(f"{naam}: geen verbinding")
                self.verstuurd.append((naam, m.titel))
            return stuur

        self.v = Verzender({"Telegram": kanaal("Telegram"), "e-mail": kanaal("e-mail")},
                           klok=lambda: self.t)

    def _ronde(self):
        asyncio.run(self.v.verwerk())

    def test_elke_melding_naar_elk_kanaal(self):
        self.v.voeg_toe(Melding("een"))
        self._ronde()
        self.assertEqual(self.verstuurd, [("Telegram", "een"), ("e-mail", "een")])
        self.assertEqual(self.v.wachtend, 0)

    def test_mislukt_kanaal_wordt_opnieuw_geprobeerd(self):
        self.stuk = {"Telegram"}
        self.v.voeg_toe(Melding("een"))
        with self.assertLogs("brms.melding", "WARNING") as log:
            self._ronde()                         # e-mail gaat door, Telegram niet
            self.assertEqual(self.verstuurd, [("e-mail", "een")])
            self._ronde()                         # nog geen minuut later: geen nieuwe poging
            self.t += melding.OPNIEUW
            self._ronde()                         # weer mislukt: geen tweede waarschuwing
        self.assertEqual(len(log.output), 1)
        self.assertEqual(self.v.wachtend, 1)
        # Telegram is terug: alleen daarover nog versturen, niet nog eens per e-mail.
        self.stuk = set()
        self.t += melding.OPNIEUW
        self._ronde()
        self.assertEqual(self.verstuurd, [("e-mail", "een"), ("Telegram", "een")])
        self.assertEqual(self.v.wachtend, 0)

    def test_na_een_dag_vervalt_de_melding(self):
        self.stuk = {"Telegram", "e-mail"}
        self.v.voeg_toe(Melding("een"))
        with self.assertLogs("brms.melding", "WARNING") as log:
            self._ronde()
            self.t += melding.MAX_LEEFTIJD + 1
            self._ronde()
        self.assertEqual(self.v.wachtend, 0)
        self.assertIn("vervallen", log.output[-1])

    def test_zonder_kanaal_gaat_er_niets_uit(self):
        v = Verzender({})
        v.voeg_toe(Melding("een"))
        self.assertEqual(v.wachtend, 0)

    def test_wachtrij_heeft_een_bovengrens(self):
        self.stuk = {"Telegram", "e-mail"}
        with self.assertLogs("brms.melding", "WARNING"):
            for i in range(melding.MAX_WACHTRIJ + 5):
                self.v.voeg_toe(Melding(str(i)))
        self.assertEqual(self.v.wachtend, melding.MAX_WACHTRIJ)


if __name__ == "__main__":
    unittest.main()
