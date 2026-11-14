"""Systeem: logboek, status van de services, schijfruimte en herstarten
(ARCHITECTURE.md §5.5.5, §5.6 systeempagina).

Alle services schrijven hun logregels naar stderr; onder systemd komen die in journald.
journald is daarmee de ene bron voor systeemmeldingen (§5.5.5):

- de **webservice** leest er het logboek uit (logboek());
- de **alarmlaag** volgt het en verstuurt bij een fout een melding (volg() en Foutmelder).

Er wordt niets extra weggeschreven (P7), en het werkt ook als de database of de broker
zelf de storing is.

Welke regels tellen mee: die van de eenheden `brms-*` en `mosquitto`, wat systemd zelf
over die eenheden meldt (bijv. dat een service is gestopt met een fout), en de regels van
de **kernel** (lees- en schrijffouten van de schijf, onderspanning).

Niveaus (§5.5.5 punt 1). journald kent de niveaus van syslog, als getal:
    0–3  fout          (emerg, alert, crit, err)
    4    waarschuwing
    5–7  gewoon        (notice, info, debug) – niet in het logboek
Een service geeft het niveau door met `<N>` vóór elke regel (zie JournaldFormatter).

Verder in dit module: de status van de services (`systemctl show`), de schijfruimte, en
het herstarten van de services of van de Pi. Dat laatste mag de gebruiker van de services
door een polkit-regel (deploy/polkit/50-brms.rules); er is geen sudo voor nodig.

Op een systeem zonder systemd (de ontwikkel-pc) geven de functies hier een leeg resultaat
met een uitleg, in plaats van een fout.
"""

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import time

from brms import config
from brms.melding import Melding

log = logging.getLogger(__name__)

# Niveaus van journald (syslog).
FOUT, WAARSCHUWING = 3, 4
NIVEAUNAAM = {FOUT: "fout", WAARSCHUWING: "waarschuwing"}

# Eenheden buiten BRMS waarvan de regels ook in het logboek horen.
ANDERE_EENHEDEN = ("mosquitto",)
# Naam waaronder de regels van de kernel in het logboek staan.
KERNEL = "kernel"

# Zoveel regels leest het logboek hooguit terug uit journald (vangnet), en zoveel toont de
# pagina per keer.
MAX_TERUG = 5000
PER_PAGINA = 200
# Zoveel regels toont de pagina als ze opent ("laatste 10", §5.6 systeempagina punt 3).
STANDAARD_AANTAL = 10
# Langste tekst van één regel in het logboek en in een melding.
MAX_TEKST = 500
MAX_MELDING = 300

# Langste wachttijd op journalctl of systemctl, in seconden.
TIMEOUT = 10.0

# Velden die uit journald worden opgevraagd; de rest (tientallen per regel) is niet nodig.
_VELDEN = "MESSAGE,PRIORITY,_SYSTEMD_UNIT,UNIT,_TRANSPORT"

# Een melding per service hooguit eens per zoveel seconden (§5.5.5 punt 5).
TUSSENPOOS = 3600.0


# =============================================================================
# Logging van de services zelf
# =============================================================================

# Niveau van Python-logging -> niveau van journald.
_PRIORITEIT = {logging.CRITICAL: 2, logging.ERROR: 3, logging.WARNING: 4, logging.INFO: 6,
               logging.DEBUG: 7}


class JournaldFormatter(logging.Formatter):
    """Zet vóór elke logregel het niveau in de vorm die systemd herkent: `<3>` voor een
    fout, `<4>` voor een waarschuwing (§5.5.5 punt 2). journald bewaart dat niveau bij de
    regel, zodat het logboek erop kan filteren. In de tekst zelf is het niet te zien.

    Een fout met een uitzondering (log.exception) is meer dan één regel: de melding en
    daaronder de traceback. journald maakt van elke regel een eigen bericht. Daarom:
    - de eerste regel krijgt de soort en de tekst van de uitzondering erachter, zodat die
      ene regel in het logboek en in een melding genoeg zegt;
    - de regels van de traceback krijgen niveau 'gewoon': ze staan wel in journald
      (journalctl), maar niet tientallen keren als fout in het logboek.
    """

    def format(self, record: logging.LogRecord) -> str:
        niveau = _PRIORITEIT.get(record.levelno, 6 if record.levelno < logging.WARNING else 3)
        eerste, *rest = super().format(record).split("\n")
        if record.exc_info and record.exc_info[1] is not None:
            fout = record.exc_info[1]
            eerste += f" ({type(fout).__name__}: {fout})".replace("\n", " ")
        return "\n".join([f"<{niveau}>{eerste}"] + [f"<6>{regel}" for regel in rest])


def stel_logging_in(debug: bool = False) -> None:
    """Logging naar stderr. Onder systemd komt dat in journald, dat zelf tijd en
    servicenaam toevoegt; daarom hier alleen niveau, module en bericht. Alleen onder
    systemd (die zet JOURNAL_STREAM) komt het niveau er als `<N>` voor: in een gewone
    terminal zou dat alleen storen."""
    vorm = "%(levelname)s %(name)s: %(message)s"
    handler = logging.StreamHandler()
    handler.setFormatter(JournaldFormatter(vorm) if os.environ.get("JOURNAL_STREAM")
                         else logging.Formatter(vorm))
    logging.basicConfig(level=logging.DEBUG if debug else logging.INFO, handlers=[handler])


# =============================================================================
# Regels uit journald
# =============================================================================

def brms_services() -> list[str]:
    """Alle BRMS-services: de namen van de units in deploy/systemd/ (bijv. brms-opslag).

    Zo doet een nieuwe service vanzelf mee zodra zijn unit in deploy/systemd/ staat.
    """
    map_ = os.path.join(config.PROJECTMAP, "deploy", "systemd")
    return sorted(f[:-len(".service")] for f in os.listdir(map_)
                  if f.startswith("brms-") and f.endswith(".service"))


def _service(eenheid) -> str | None:
    """De naam van de service bij een eenheid van systemd ('brms-web.service' ->
    'brms-web'), of None als de eenheid niet bij BRMS hoort."""
    if not isinstance(eenheid, str) or not eenheid.endswith(".service"):
        return None
    naam = eenheid[:-len(".service")]
    return naam if naam.startswith("brms-") or naam in ANDERE_EENHEDEN else None


def _tekst(waarde) -> str:
    """De tekst van een bericht uit journald. Meestal een tekst; bij tekens die geen
    geldige UTF-8 zijn levert journald een lijst van bytes."""
    if isinstance(waarde, list):
        try:
            waarde = bytes(waarde).decode("utf-8", "replace")
        except (ValueError, TypeError):
            waarde = ""
    return waarde.strip() if isinstance(waarde, str) else ""


def lees_regel(d) -> dict | None:
    """Maakt van één bericht uit journald (JSON, als dict) een regel van het logboek:
    {"ts": Unix-seconden, "us": tijdstip in microseconden (voor het bladeren),
     "service", "niveau": "fout"|"waarschuwing", "tekst"}.
    None als het bericht niet in het logboek hoort: een andere eenheid, of niveau 'gewoon'.
    """
    if not isinstance(d, dict):
        return None
    try:
        us = int(d.get("__REALTIME_TIMESTAMP"))
        prioriteit = int(d.get("PRIORITY", 6))
    except (TypeError, ValueError):
        return None
    tekst = _tekst(d.get("MESSAGE"))
    # Van wie is het bericht? Van de service zelf (_SYSTEMD_UNIT), van systemd over de
    # service (UNIT, bijv. "Failed with result 'exit-code'"), of van de kernel.
    service = _service(d.get("_SYSTEMD_UNIT"))
    if service is None:
        service = _service(d.get("UNIT"))
        if service is not None and "Failed with result" in tekst:
            # systemd meldt een gecrashte service als waarschuwing; voor BRMS is dat een
            # fout: de service is gestopt zonder dat iemand daarom vroeg.
            prioriteit = min(prioriteit, FOUT)
    if service is None and d.get("_TRANSPORT") == "kernel":
        service = KERNEL
    if service is None or prioriteit > WAARSCHUWING or not tekst:
        return None
    # De services zetten het niveau als woord vóór hun tekst ("WARNING brms.bus: ...");
    # in het logboek staat het niveau al in een eigen kolom.
    for woord in ("CRITICAL ", "ERROR ", "WARNING "):
        if tekst.startswith(woord):
            tekst = tekst[len(woord):]
            break
    return {"ts": us // 1_000_000, "us": us, "service": service,
            "niveau": NIVEAUNAAM[FOUT if prioriteit <= FOUT else WAARSCHUWING],
            "tekst": tekst[:MAX_TEKST]}


def logboek(alleen_fouten: bool = False, service: str | None = None, aantal: int = PER_PAGINA,
            overslaan: int = 0, van: int | None = None, tot: int | None = None,
            ) -> tuple[list[dict], bool, bool, str | None]:
    """Leest het logboek uit journald: de nieuwste regels eerst.

    Twee filters, die altijd gelden:
      alleen_fouten  True = geen waarschuwingen
      service        alleen deze service (bijv. "brms-camera" of "kernel")
    Welk deel (§5.6 systeempagina punt 3, v0.61; dezelfde opzet als de alarmenpagina):
      van, tot       een periode in Unix-seconden: de regels van `van` tot (niet tot en
                     met) `tot`, hooguit PER_PAGINA;
      aantal, overslaan
                     zonder periode: de laatste `aantal` regels, na het overslaan van de
                     `overslaan` nieuwste (bladeren: 10 eerder, nog 10 eerder, ...).
    Het overslaan en de periode tellen ná de filters: "de 10 daarvoor" zijn de 10 fouten
    daarvoor als alleen fouten gekozen is.

    Geeft (regels, eerder, vol, uitleg):
      eerder  er zijn nog oudere regels dan de getoonde (voor de knop "eerder");
      vol     de periode heeft meer regels dan er getoond worden;
      uitleg  een tekst als het logboek niet gelezen kon worden (dan zijn de regels leeg),
              anders None.
    """
    opdracht = ["journalctl", "--no-pager", "--reverse", "--output=json",
                f"--output-fields={_VELDEN}", f"--priority={WAARSCHUWING}",
                f"--lines={MAX_TERUG}"]
    try:
        r = subprocess.run(opdracht, capture_output=True, timeout=TIMEOUT)
    except FileNotFoundError:
        return [], False, False, ("journalctl is er niet op dit systeem; het logboek werkt "
                                  "alleen op de Pi")
    except (OSError, subprocess.SubprocessError) as e:
        return [], False, False, f"het logboek kon niet gelezen worden: {e}"
    if r.returncode != 0:
        return [], False, False, ("het logboek kon niet gelezen worden: "
                                  + r.stderr.decode("utf-8", "replace").strip()[:200])
    periode = van is not None and tot is not None
    regels: list[dict] = []
    eerder = vol = False
    overgeslagen = 0
    # journalctl geeft de nieuwste regel eerst: de lus loopt dus terug in de tijd en kan
    # stoppen zodra het gevraagde deel compleet is.
    for ruw in r.stdout.splitlines():
        try:
            regel = lees_regel(json.loads(ruw))
        except ValueError:
            continue
        if regel is None:
            continue
        if alleen_fouten and regel["niveau"] != NIVEAUNAAM[FOUT]:
            continue
        if service is not None and regel["service"] != service:
            continue
        if periode:
            if regel["ts"] >= tot:
                continue            # nieuwer dan de periode: nog verder terug zoeken
            if regel["ts"] < van:
                eerder = True       # ouder dan de periode: er valt terug te bladeren
                break
            if len(regels) >= PER_PAGINA:
                # Meer dan er passen: alleen de nieuwste staan er. De rest is ouder.
                vol = eerder = True
                break
        else:
            if overgeslagen < overslaan:
                overgeslagen += 1
                continue
            if len(regels) >= aantal:
                eerder = True
                break
        regels.append(regel)
    return regels, eerder, vol, None


# =============================================================================
# Melding bij een fout (alarmlaag)
# =============================================================================

class Foutmelder:
    """Maakt van fouten uit het logboek meldingen, hooguit één per service per uur
    (§5.5.5 punt 5). Zuiver: geen klok en geen netwerk; de tijd komt van `klok`.

    Zonder die rem zou een service die elke 5 s crasht en herstart de telefoon
    overspoelen. De fouten die niet gemeld zijn, worden geteld; de eerstvolgende melding
    noemt het aantal. Alles staat in het logboek.
    """

    def __init__(self, tussenpoos: float = TUSSENPOOS, klok=time.monotonic):
        self._tussenpoos = tussenpoos
        self._klok = klok
        # Per service: [tijdstip van de laatste melding, aantal niet-gemelde fouten sindsdien].
        self._stand: dict[str, list] = {}

    def bij_fout(self, service: str, tekst: str) -> Melding | None:
        """Een fout van `service`; geeft de melding die uit moet, of None als deze service
        het afgelopen uur al een melding gaf."""
        nu = self._klok()
        stand = self._stand.get(service)
        if stand is not None and nu - stand[0] < self._tussenpoos:
            stand[1] += 1
            return None
        gemist = stand[1] if stand is not None else 0
        self._stand[service] = [nu, 0]
        toelichting = "Zie het logboek op de pagina Systeem."
        if gemist:
            toelichting = (f"Sinds de vorige melding waren er nog {gemist} "
                           f"{'fout' if gemist == 1 else 'fouten'} van {service}. " + toelichting)
        return Melding(f"🛑 Fout in {service}: {tekst[:MAX_MELDING]}", toelichting,
                       prioriteit="hoog")


async def volg(bij_regel, stop: asyncio.Event, opnieuw: float = 10.0) -> None:
    """Volgt journald en roept bij_regel(regel) aan voor elke nieuwe regel van het
    logboek (zie lees_regel); loopt tot `stop` gezet wordt.

    Alleen wat er vanaf nu bij komt (`--lines=0`): na een herstart van de alarmlaag
    worden oude fouten niet opnieuw gemeld. Stopt journalctl, dan wordt het na `opnieuw`
    seconden opnieuw gestart. Is journalctl er niet (de ontwikkel-pc), dan doet deze
    functie niets.
    """
    opdracht = ["journalctl", "--follow", "--lines=0", "--output=json",
                f"--output-fields={_VELDEN}", f"--priority={WAARSCHUWING}"]
    while not stop.is_set():
        try:
            proces = await asyncio.create_subprocess_exec(
                *opdracht, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        except FileNotFoundError:
            log.info("journalctl is er niet op dit systeem; fouten uit het logboek worden "
                     "niet gemeld")
            return
        except OSError as e:
            log.warning("het logboek kan niet gevolgd worden: %s", e)
            proces = None
        if proces is not None:
            wacht_stop = asyncio.create_task(stop.wait())
            try:
                while not stop.is_set():
                    lees = asyncio.create_task(proces.stdout.readline())
                    await asyncio.wait({lees, wacht_stop}, return_when=asyncio.FIRST_COMPLETED)
                    if not lees.done():
                        lees.cancel()
                        break
                    ruw = lees.result()
                    if not ruw:
                        break                  # journalctl is gestopt
                    try:
                        regel = lees_regel(json.loads(ruw))
                    except ValueError:
                        continue
                    if regel is not None:
                        bij_regel(regel)
            except ValueError:
                # Een regel langer dan de buffer van asyncio; opnieuw beginnen.
                pass
            finally:
                wacht_stop.cancel()
                if proces.returncode is None:
                    proces.terminate()
                await proces.wait()
            if not stop.is_set():
                log.warning("het volgen van het logboek is gestopt; wordt opnieuw gestart")
        # Even wachten voor een nieuwe poging, maar een stopverzoek direct opmerken.
        try:
            await asyncio.wait_for(stop.wait(), opnieuw)
        except asyncio.TimeoutError:
            pass


# =============================================================================
# Status van de services en schijfruimte (webservice)
# =============================================================================

def lees_show(uitvoer: str, nu: float, monotoon: float) -> list[dict]:
    """Maakt van de uitvoer van `systemctl show` (blokken 'Naam=waarde', gescheiden door
    een lege regel) per eenheid:
    {"naam", "actief": bool, "toestand": bijv. "active (running)", "sinds": Unix-seconden
     of None, "herstarts": aantal}.

    `sinds` wordt berekend uit ActiveEnterTimestampMonotonic (microseconden sinds het
    opstarten van de Pi): het verschil met de monotone klok van nu, afgetrokken van de
    tijd van nu. Dat hangt niet af van de taal of de tijdzone van de uitvoer. Een eenheid
    die systemd niet kent (LoadState=not-found) wordt overgeslagen.
    """
    uit = []
    for blok in uitvoer.strip().split("\n\n"):
        v = dict(regel.split("=", 1) for regel in blok.splitlines() if "=" in regel)
        naam = v.get("Id", "")
        if not naam or v.get("LoadState") == "not-found":
            continue
        try:
            us = int(v.get("ActiveEnterTimestampMonotonic", "0"))
            herstarts = int(v.get("NRestarts", "0"))
        except ValueError:
            us, herstarts = 0, 0
        actief = v.get("ActiveState") == "active"
        uit.append({
            "naam": naam.removesuffix(".service"),
            "actief": actief,
            "toestand": f"{v.get('ActiveState', '?')} ({v.get('SubState', '?')})",
            "sinds": int(nu - (monotoon - us / 1e6)) if actief and us else None,
            "herstarts": herstarts})
    return uit


def services() -> tuple[list[dict], str | None]:
    """De status van alle BRMS-services en Mosquitto (zie lees_show). Geeft
    (services, uitleg); `uitleg` is een tekst als systemd niet te bevragen is."""
    namen = brms_services() + list(ANDERE_EENHEDEN)
    try:
        r = subprocess.run(
            ["systemctl", "show", "--property=Id,LoadState,ActiveState,SubState,"
             "ActiveEnterTimestampMonotonic,NRestarts", *namen],
            capture_output=True, text=True, timeout=TIMEOUT)
    except FileNotFoundError:
        return [], "systemctl is er niet op dit systeem; de status werkt alleen op de Pi"
    except (OSError, subprocess.SubprocessError) as e:
        return [], f"de status van de services kon niet worden opgevraagd: {e}"
    if r.returncode != 0:
        return [], f"de status van de services kon niet worden opgevraagd: {r.stderr.strip()[:200]}"
    return lees_show(r.stdout, time.time(), time.monotonic()), None


def schijf(cfg: config.Config) -> dict:
    """De schijfruimte (§5.6 systeempagina): grootte, gebruikt en vrij van de schijf waarop
    de database staat, en de omvang van de database zelf, alles in bytes. Een waarde die
    niet te bepalen is, is None."""
    uit = {"totaal": None, "gebruikt": None, "vrij": None, "database": None}
    pad = cfg.opslag.database
    try:
        ruimte = shutil.disk_usage(os.path.dirname(pad) or ".")
        uit.update(totaal=ruimte.total, gebruikt=ruimte.used, vrij=ruimte.free)
    except OSError:
        pass
    # De database is het bestand zelf plus de hulpbestanden van WAL.
    grootte = None
    for bestand in (pad, pad + "-wal", pad + "-shm"):
        try:
            grootte = (grootte or 0) + os.path.getsize(bestand)
        except OSError:
            pass
    uit["database"] = grootte
    return uit


# =============================================================================
# Herstarten (webservice)
# =============================================================================

def _systemctl(*argumenten: str) -> str | None:
    """Voert een opdracht van systemctl uit zonder om een wachtwoord te vragen. Geeft None
    als het gelukt is, anders de foutmelding. De toestemming komt van polkit
    (deploy/polkit/50-brms.rules)."""
    try:
        r = subprocess.run(["systemctl", "--no-ask-password", *argumenten],
                           capture_output=True, text=True, timeout=TIMEOUT)
    except FileNotFoundError:
        return "systemctl is er niet op dit systeem; herstarten werkt alleen op de Pi"
    except (OSError, subprocess.SubprocessError) as e:
        return str(e)
    if r.returncode != 0:
        fout = r.stderr.strip()[:300] or f"systemctl gaf code {r.returncode}"
        if "auth" in fout.lower() or "denied" in fout.lower():
            fout += ("  (De polkit-regel van BRMS ontbreekt waarschijnlijk; voer op de Pi "
                     "'brms update' uit.)")
        return fout
    return None


def herstart_services(behalve: str | None = None) -> str | None:
    """Herstart alle BRMS-services (behalve eventueel één). `--no-block`: systemd krijgt
    de opdracht en deze functie wacht niet tot alles weer draait. Geeft None of de
    foutmelding."""
    namen = [s for s in brms_services() if s != behalve]
    return _systemctl("--no-block", "restart", *namen)


def herstart_service(naam: str) -> str | None:
    """Herstart één BRMS-service."""
    return _systemctl("--no-block", "restart", naam)


def herstart_pi() -> str | None:
    """Start de Pi opnieuw op. `--no-wall`: geen bericht naar aangemelde terminals."""
    return _systemctl("--no-wall", "reboot")


if __name__ == "__main__":
    # Hulpje bij het testen op de Pi: toont het logboek zoals de pagina het krijgt.
    regels_, meer_, _vol, uitleg_ = logboek()
    for regel_ in regels_:
        print(time.strftime("%d-%m %H:%M:%S", time.localtime(regel_["ts"])),
              f"{regel_['service']:<18} {regel_['niveau']:<13} {regel_['tekst']}")
    print(uitleg_ or f"{len(regels_)} regels{' (er zijn er meer)' if meer_ else ''}", file=sys.stderr)
