"""Meldingskanalen: een notificatie naar de telefoon sturen (ARCHITECTURE.md §5.5.4).

Twee kanalen, beide met alleen de standaardbibliotheek (P3):

- **Telegram** (eerste kanaal): een eigen bot stuurt het bericht via de HTTPS-API van
  Telegram. In brms.toml staan de sleutel van de bot (`token`) en het gesprek (`chat`).
- **E-mail** (tweede kanaal): via een mailaccount met SMTP.

Elke notificatie gaat naar elk kanaal dat is ingesteld; mislukt het ene, dan gaat het
andere gewoon door (§5.5.4 punt 1). Het opnieuw proberen staat in de Verzender.

De sleutel van de bot en het wachtwoord van de mail zijn geheimen: ze komen nooit in een
logregel of foutmelding (§5.5.4 punt 4). De token staat in het adres van de aanvraag aan
Telegram; elke foutmelding wordt daarom eerst geschoond (zie _schoon).

Een melding kan een foto bij zich hebben (§5.7 punt 15): Telegram toont die als
afbeelding met de tekst als onderschrift, de e-mail krijgt hem als bijlage.

Dit module bevat:
- Melding: wat er verstuurd wordt;
- telegram() en email(): één bericht over één kanaal (blokkerend; MeldFout bij mislukken);
- Verzender: de wachtrij die verstuurt en bij mislukken opnieuw probeert.
"""

import asyncio
import json
import logging
import smtplib
import ssl
import time
import uuid
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from email.message import EmailMessage

from brms.config import EmailConfig, MeldingConfig, TelegramConfig

log = logging.getLogger(__name__)

# Langste wachttijd op een dienst, in seconden.
TIMEOUT = 15.0

# Opnieuw proberen na een mislukte verzending (§5.5.4 punt 6): om de minuut, tot het
# bericht een dag oud is. Daarna heeft het geen nieuwswaarde meer en vervalt het.
OPNIEUW = 60.0
MAX_LEEFTIJD = 24 * 3600.0

# Vangnet: zoveel berichten wachten er hooguit. Bij lange uitval van het internet met veel
# alarmen vervallen de oudste.
MAX_WACHTRIJ = 200

# Het onderschrift van een foto in Telegram is hooguit zo lang.
MAX_ONDERSCHRIFT = 1024


class MeldFout(Exception):
    """Een bericht kon niet verstuurd worden. De tekst bevat geen geheimen."""


@dataclass(frozen=True)
class Melding:
    titel: str                   # één regel, bijv. "Vorst – kajuit: temperatuur 1,8 °C"
    tekst: str = ""              # eventuele toelichting eronder
    prioriteit: str = "normaal"  # urgent, hoog of normaal (§5.5.2 punt 8)
    # Een foto bij de melding (§5.7 punt 15): de bytes van een JPEG, en de bestandsnaam
    # voor de bijlage. repr=False: de bytes horen niet in een afgedrukte Melding.
    foto: bytes | None = field(default=None, repr=False)
    foto_naam: str = "foto.jpg"


def _schoon(tekst: str, *geheimen: str) -> str:
    """Haalt geheimen uit een foutmelding voordat die in het log komt."""
    for g in geheimen:
        if g:
            tekst = tekst.replace(g, "***")
    return tekst


def _formulier(velden: dict[str, str], naam: str, bestandsnaam: str, data: bytes) -> tuple[bytes, str]:
    """Bouwt een formulier met een bestand erin (multipart/form-data), zoals Telegram een
    foto wil ontvangen. Geeft (inhoud, Content-Type). De standaardbibliotheek heeft hier
    geen kant-en-klare functie voor."""
    grens = uuid.uuid4().hex
    delen = []
    for sleutel, waarde in velden.items():
        delen.append(f'--{grens}\r\nContent-Disposition: form-data; name="{sleutel}"\r\n\r\n'
                     f'{waarde}\r\n'.encode())
    delen.append(f'--{grens}\r\nContent-Disposition: form-data; name="{naam}"; '
                 f'filename="{bestandsnaam}"\r\nContent-Type: image/jpeg\r\n\r\n'.encode())
    delen.append(data)
    delen.append(f"\r\n--{grens}--\r\n".encode())
    return b"".join(delen), f"multipart/form-data; boundary={grens}"


def telegram(cfg: TelegramConfig, m: Melding, timeout: float = TIMEOUT) -> None:
    """Stuurt één bericht via de Telegram-bot. Blokkeert; MeldFout als het niet lukt.
    Met een foto: als afbeelding met de tekst als onderschrift (sendPhoto)."""
    tekst = m.titel + (f"\n{m.tekst}" if m.tekst else "")
    # 'normaal' komt binnen zonder geluid; 'hoog' en 'urgent' met (§5.5.4 punt 5).
    stil = m.prioriteit == "normaal"
    if m.foto is None:
        methode = "sendMessage"
        inhoud = json.dumps({"chat_id": cfg.chat, "text": tekst,
                             "disable_notification": stil}).encode()
        soort = "application/json"
    else:
        methode = "sendPhoto"
        inhoud, soort = _formulier(
            {"chat_id": cfg.chat, "caption": tekst[:MAX_ONDERSCHRIFT],
             "disable_notification": "true" if stil else "false"},
            "photo", m.foto_naam, m.foto)
    aanvraag = urllib.request.Request(
        f"https://api.telegram.org/bot{cfg.token}/{methode}", data=inhoud,
        headers={"Content-Type": soort})
    try:
        with urllib.request.urlopen(aanvraag, timeout=timeout) as r:
            antwoord = json.loads(r.read(65536))
    except urllib.error.HTTPError as e:
        # Telegram legt in het antwoord uit wat er mis is (verkeerde token, onbekend gesprek).
        try:
            uitleg = json.loads(e.read(65536)).get("description", "")
        except (ValueError, OSError, AttributeError):
            uitleg = ""
        raise MeldFout(_schoon(f"Telegram: fout {e.code} {uitleg}".strip(), cfg.token)) from None
    except (OSError, ValueError) as e:
        # Geen internet, timeout, of een onleesbaar antwoord.
        raise MeldFout(_schoon(f"Telegram: {e}", cfg.token)) from None
    if not isinstance(antwoord, dict) or not antwoord.get("ok"):
        raise MeldFout("Telegram: onverwacht antwoord")


def email(cfg: EmailConfig, m: Melding, timeout: float = TIMEOUT) -> None:
    """Stuurt één bericht per e-mail. Blokkeert; MeldFout als het niet lukt."""
    bericht = EmailMessage()
    # De prioriteit staat in het onderwerp (§5.5.4 punt 5), zodat erop te filteren is.
    extra = "" if m.prioriteit == "normaal" else f" {m.prioriteit.upper()}"
    bericht["Subject"] = f"[BRMS{extra}] {m.titel}"
    bericht["From"] = cfg.van
    bericht["To"] = ", ".join(cfg.aan)
    bericht.set_content(m.titel + (f"\n\n{m.tekst}" if m.tekst else "") + "\n")
    if m.foto is not None:
        bericht.add_attachment(m.foto, maintype="image", subtype="jpeg", filename=m.foto_naam)
    try:
        context = ssl.create_default_context()
        if cfg.poort == 465:
            # Poort 465: de verbinding is vanaf het begin versleuteld.
            server = smtplib.SMTP_SSL(cfg.server, cfg.poort, timeout=timeout, context=context)
        else:
            # Anders (meestal 587): eerst verbinden, dan versleutelen met STARTTLS. Zonder
            # versleuteling wordt er niet aangemeld.
            server = smtplib.SMTP(cfg.server, cfg.poort, timeout=timeout)
            server.starttls(context=context)
        with server:
            server.login(cfg.gebruiker, cfg.wachtwoord)
            server.send_message(bericht)
    except (OSError, smtplib.SMTPException) as e:
        raise MeldFout(_schoon(f"e-mail: {e}", cfg.wachtwoord)) from None


def kanalen(cfg: MeldingConfig) -> dict:
    """De ingestelde kanalen: {naam: functie(melding)}. Leeg als er niets is ingesteld."""
    uit = {}
    if cfg.telegram is not None:
        uit["Telegram"] = lambda m, c=cfg.telegram: telegram(c, m)
    if cfg.email is not None:
        uit["e-mail"] = lambda m, c=cfg.email: email(c, m)
    return uit


@dataclass
class _Post:
    """Eén melding in de wachtrij, met de kanalen waarover hij nog verstuurd moet worden."""
    melding: Melding
    open: list[str]
    sinds: float              # monotone klok: wanneer de melding ontstond
    volgende: float = 0.0     # monotone klok: niet eerder opnieuw proberen
    gemeld: set = field(default_factory=set)   # kanalen waarvoor al een waarschuwing gelogd is


class Verzender:
    """Verstuurt meldingen over alle kanalen en probeert het bij mislukken opnieuw.

    Het versturen blokkeert (netwerk) en gebeurt daarom in een thread; de alarmlaag loopt
    intussen door. Alles hier draait in de asyncio-loop; een lock is niet nodig.
    """

    def __init__(self, kanalen_: dict, klok=time.monotonic):
        """kanalen_: {naam: functie(melding)} (zie kanalen()); klok: te vervangen in tests."""
        self._kanalen = kanalen_
        self._klok = klok
        self._wachtrij: list[_Post] = []

    def voeg_toe(self, m: Melding) -> None:
        if not self._kanalen:
            return   # geen kanaal ingesteld: er gaat geen melding uit (§5.5.4 punt 7)
        if len(self._wachtrij) >= MAX_WACHTRIJ:
            oud = self._wachtrij.pop(0)
            log.warning("te veel wachtende meldingen; vervallen: %s", oud.melding.titel)
        self._wachtrij.append(_Post(m, list(self._kanalen), self._klok()))

    @property
    def wachtend(self) -> int:
        return len(self._wachtrij)

    async def verwerk(self) -> None:
        """Eén ronde: elke melding die aan de beurt is over haar open kanalen versturen."""
        nu = self._klok()
        for post in list(self._wachtrij):
            if post.volgende > nu:
                continue
            for naam in list(post.open):
                try:
                    await asyncio.to_thread(self._kanalen[naam], post.melding)
                except MeldFout as e:
                    # Eén waarschuwing per melding en kanaal; het opnieuw proberen is stil.
                    if naam not in post.gemeld:
                        post.gemeld.add(naam)
                        log.warning("melding niet verstuurd (%s); wordt opnieuw geprobeerd: %s",
                                    e, post.melding.titel)
                else:
                    post.open.remove(naam)
                    log.info("melding verstuurd via %s: %s", naam, post.melding.titel)
            if not post.open:
                self._wachtrij.remove(post)
            elif self._klok() - post.sinds > MAX_LEEFTIJD:
                self._wachtrij.remove(post)
                log.warning("melding na een dag nog niet verstuurd via %s; vervallen: %s",
                            ", ".join(post.open), post.melding.titel)
            else:
                post.volgende = self._klok() + OPNIEUW

    async def draai(self, stop: asyncio.Event) -> None:
        """Verstuurt tot `stop` gezet wordt."""
        while not stop.is_set():
            try:
                await self.verwerk()
            except Exception:
                # Een onverwachte fout mag het versturen niet voorgoed stoppen.
                log.exception("fout bij versturen van meldingen")
            try:
                await asyncio.wait_for(stop.wait(), 1.0)
            except asyncio.TimeoutError:
                pass
