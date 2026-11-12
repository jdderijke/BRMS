"""Opdrachtregel van BRMS.

    python -m brms [--config PAD] [--debug] <opdracht>

Op de Pi installeert deploy/install.sh een korte opdracht `brms` die hetzelfde doet.
Zonder --config wordt config/brms.toml in de projectmap gelezen.

Opdrachten:
  check-config        configuratie controleren, bijv. vóór een herstart (§5.4.1)
  ble-reader          de BLE-reader-service starten (§5.2.1)
  shelly-reader       de Shelly-reader-service starten (§5.2.1, §5.2.4)
  opslag              de opslag-service starten (§5.3)
  alarm               de alarmlaag starten (§5.5)
  camera              de camera-reader-service starten (§5.7)
  web                 de webservice starten (§5.6)
  go2rtc              go2rtc starten: het live-beeld van de camera's (§5.7 punt 18)
  monitor [sensor..]  monitor in tmux: busverkeer, of met sensor(en) de trace (§10.2)
  monitor stop        het monitorvak sluiten; de shell blijft
  grootheden [type]   alle grootheden, of wat een sensortype levert (§5.4.2)
  sim <sensor ..>     gesimuleerde sensordata door de keten sturen (§10.3): een test (met
                      een duur) of een demo (zonder duur, loopt tot 'sim stop');
                      'all' = alle sensoren; 'sim stop <sensor ..>' stopt (ook: simuleer)
  start | stop | herstart   alle BRMS-services via systemd (§10.1 punt 9)
  update [--terug]    code bijwerken vanaf GitHub, of terug naar de vorige versie (§10.1)
  monitor-vak [..]    (intern) het vak onderin dat 'monitor' in tmux start

De services worden normaal door systemd gestart (deploy/systemd/), niet met de hand.
"""

import argparse
import asyncio
import logging
import os
import shutil
import signal
import subprocess
import sys

from brms import VERSION, config, systeem


def _check_config(cfg_pad: str) -> int:
    """Controleert de configuratie en meldt alle fouten. Exitcode 0 = in orde."""
    try:
        cfg = config.lees(cfg_pad)
    except config.ConfigFout as e:
        print(f"Configuratie {cfg_pad} bevat fouten:", file=sys.stderr)
        for f in e.fouten:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print(f"Configuratie {cfg_pad} is in orde: {len(cfg.sensoren)} sensoren, "
          f"{len(cfg.alarmen)} alarmregels, {len(cfg.cameras)} camera's.")
    # Geen fout, wel het melden waard (§5.5.4 punt 7).
    if not cfg.melding.ingesteld and any(r.melding != "uit" for r in cfg.alarmen):
        print("Let op: er is geen meldingskanaal ingesteld ([melding.telegram] of "
              "[melding.email]);\nalarmen worden vastgelegd, maar er gaat geen melding uit.")
    # Een video wordt opgenomen met ffmpeg (§5.7 punt 9).
    if any(c.video_url for c in cfg.cameras.values()) and shutil.which("ffmpeg") is None:
        print("Let op: er is een camera met 'video_url', maar ffmpeg is niet geïnstalleerd;\n"
              "video's mislukken tot ffmpeg er is (sudo apt install ffmpeg, of: brms update).")
    # Het live-beeld loopt via go2rtc (§5.7 punt 18). Alleen op de Pi: op de pc staat het
    # programma er nooit.
    if os.name == "posix" and any(c.video_url for c in cfg.cameras.values()):
        from brms import go2rtc
        if not os.path.isfile(go2rtc.PROGRAMMA):
            print("Let op: go2rtc is niet geïnstalleerd; er is geen live-beeld tot het er is\n"
                  "(brms update, of: bash deploy/go2rtc.sh). Opnames werken wel.")
    # Opmerkingen die geen fout zijn, bijvoorbeeld een veld dat niet meer bestaat.
    for w in cfg.waarschuwingen:
        print(f"Let op: {w}")
    return 0


def _beheer(actie: str, cfg_pad: str) -> int:
    """brms start | stop | herstart: alle BRMS-services via systemd (§10.1 punt 9).

    - start en herstart controleren eerst de configuratie; bij een fout gebeurt er niets
      en draait de oude configuratie gewoon door.
    - stop geldt tot de volgende herstart van de Pi: de services zijn 'enabled' en starten
      bij het opstarten weer vanzelf.
    - sudo vraagt zo nodig om het wachtwoord.
    Na afloop wordt per service de status getoond.
    """
    # Alle BRMS-services: de units in deploy/systemd/. Een nieuwe service doet zo vanzelf mee.
    services = systeem.brms_services()
    if actie in ("start", "herstart"):
        if _check_config(cfg_pad) != 0:
            print("Niets gedaan; de services draaien (zo nodig) door met de oude configuratie.",
                  file=sys.stderr)
            return 1
    systemctl_actie = {"start": "start", "stop": "stop", "herstart": "restart"}[actie]
    print(f"{actie}: {', '.join(services)}")
    r = subprocess.run(["sudo", "systemctl", systemctl_actie, *services])

    # Status per service. 'is-active' geeft bijv. active, inactive, failed of activating.
    for s in services:
        status = subprocess.run(["systemctl", "is-active", s],
                                capture_output=True, text=True).stdout.strip()
        print(f"  {s:<20} {status}")
    if actie == "stop":
        print("Gestopt tot de volgende herstart van de Pi; weer starten met: brms start")
    return r.returncode


async def _service(naam: str, cfg: config.Config) -> None:
    """Start de bus en draait de gevraagde service tot een stopsignaal komt."""
    from brms.bus import Bus

    # systemd stopt een service met SIGTERM; Ctrl+C geeft SIGINT. Beide zetten `stop`,
    # waarna de service netjes afsluit (database dicht, scanner uit).
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, AttributeError):
            pass  # Windows kent dit niet; daar stopt Ctrl+C via KeyboardInterrupt

    # De bus verbindt op de achtergrond; de service start ook als de broker er nog niet is.
    bus = Bus(naam, cfg.mqtt.host, cfg.mqtt.port)
    bus.start()
    try:
        if naam == "ble-reader":
            from brms.readers import ble
            await ble.draai(cfg, bus, stop)
        elif naam == "shelly-reader":
            from brms.readers import shelly
            await shelly.draai(cfg, bus, stop)
        elif naam == "alarm":
            from brms import alarms
            await alarms.draai(cfg, bus, stop)
        elif naam == "camera":
            from brms.readers import camera
            await camera.draai(cfg, bus, stop)
        else:
            from brms import storage
            await storage.draai(cfg, bus, stop)
    finally:
        bus.stop()


def _grootheden(typen: list[str]) -> int:
    """Toont de tabel van grootheden, of per sensortype wat dat type levert (§5.4.2).

    Dit zijn de namen die in brms.toml bij 'grootheden' en 'correctie' horen."""
    from brms import grootheden

    if not typen:
        print(f"{'GROOTHEID':<18} {'EENHEID':<8} SOORT")
        for g in grootheden.GROOTHEDEN.values():
            print(f"{g.naam:<18} {g.eenheid:<8} {g.soort}")
        print()
        print("Sensortypen:", ", ".join(config.SENSOR_TYPES),
              "  (brms grootheden <type> toont wat een type levert)")
        return 0
    for t in typen:
        if t not in config.LEVERT:
            print(f"onbekend sensortype '{t}'; bekend: {', '.join(config.SENSOR_TYPES)}",
                  file=sys.stderr)
            return 1
        print(f"{t} levert:")
        for naam in config.LEVERT[t]:
            g = grootheden.GROOTHEDEN[naam]
            print(f"  {g.naam:<18} {g.eenheid:<8} {g.soort}")
    return 0


def _monitor(args) -> int:
    """Monitor (§10.2): configuratie lezen voor de namen, sensoren opzoeken, en dan óf de
    tmux-sessie openen (monitor) óf het vak zelf draaien (monitor-vak, in tmux)."""
    from brms import monitor

    # 'brms monitor stop': het vak sluiten. Hiervoor is geen configuratie nodig.
    if args.opdracht == "monitor" and args.sensoren == ["stop"]:
        return monitor.sluit_vak()

    try:
        cfg = config.lees(args.config)
        sensoren = monitor.zoek_sensoren(args.sensoren, cfg)
    except config.ConfigFout as e:
        print("configuratiefout:", file=sys.stderr)
        for f in e.fouten:
            print(f"  - {f}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    if args.opdracht == "monitor":
        return monitor.open_sessie(args.sensoren, args.config)
    return monitor.draai_vak(cfg, sensoren)


def _update(terug: bool) -> int:
    """brms update [--terug]: de code bijwerken of terugdraaien (§10.1 punt 3).

    Het werk gebeurt in deploy/update.sh: nieuwe code ophalen, ontbrekende pakketten
    installeren, configuratie controleren en de services herstarten. Dit proces wordt
    vervangen door dat script (exec): de Python-code van BRMS verandert tijdens de update,
    en zo draait er geen half oude, half nieuwe code.
    """
    script = os.path.join(config.PROJECTMAP, "deploy", "update.sh")
    if os.name != "posix":
        print("brms update werkt alleen op de Pi (het voert deploy/update.sh uit).",
              file=sys.stderr)
        return 1
    try:
        os.execv("/bin/bash", ["bash", script] + (["--terug"] if terug else []))
    except OSError as e:
        print(f"update.sh kon niet gestart worden: {e}", file=sys.stderr)
        return 1


def _simuleer(args) -> int:
    """Simulatie (§10.3): configuratie lezen en de opdracht in brms.simulatie uitvoeren."""
    from brms import simulatie

    try:
        cfg = config.lees(args.config)
    except config.ConfigFout as e:
        print("configuratiefout:", file=sys.stderr)
        for f in e.fouten:
            print(f"  - {f}", file=sys.stderr)
        return 1
    return simulatie.opdracht(cfg, args.sensoren)


# Uitleg onder 'brms --help': eerst de opdrachten voor de beheerder, dan de services.
OPDRACHTEN = """opdrachten voor de beheerder:
  check-config           configuratie controleren (vóór een herstart)
  grootheden [type]      alle grootheden, of wat een sensortype levert
  monitor [sensor ...]   monitor onderin het scherm; met sensor: trace van de keten
  monitor stop           het monitorvak sluiten
  sim <sensor ...>       gesimuleerde data door de keten sturen (meerdere sensoren of
                         'all'); vraagt om akkoord en om de duur. Een duur = een test met
                         de testreeks; alleen Enter = een demo, die doorloopt tot 'sim stop'
  sim stop <sensor ...>  de simulatie of demo stoppen (ook: sim stop all)
  start                  alle BRMS-services starten (na controle van de configuratie)
  stop                   alle BRMS-services stoppen (tot de volgende herstart van de Pi)
  herstart               configuratie controleren en alle services herstarten,
                         bijv. na een wijziging van brms.toml
  update                 nieuwste versie ophalen, ontbrekende pakketten installeren,
                         configuratie controleren en de services herstarten
  update --terug         terug naar de versie van vóór de laatste update

services (worden door systemd gestart – niet met de hand; gebruik brms herstart):
  ble-reader             BLE-reader      (systemd-unit brms-ble-reader)
  shelly-reader          Shelly-reader   (systemd-unit brms-shelly-reader)
  opslag                 opslag-service  (systemd-unit brms-opslag)
  alarm                  alarmlaag       (systemd-unit brms-alarm)
  camera                 camera-reader   (systemd-unit brms-camera)
  web                    webinterface    (systemd-unit brms-web)
  go2rtc                 live-beeld      (systemd-unit brms-go2rtc)
"""


def _al_actief(opdracht: str) -> bool:
    """True als deze service al door systemd gedraaid wordt en deze aanroep niet zelf door
    systemd gestart is.

    Een tweede exemplaar naast de systemd-service botst: beide melden zich met dezelfde
    naam bij de broker en gooien elkaar er steeds uit, en bij de opslag ontstaan twee
    schrijvers op de database. systemd zet INVOCATION_ID voor processen die het zelf start.
    """
    if os.environ.get("INVOCATION_ID"):
        return False  # gestart door systemd
    try:
        r = subprocess.run(["systemctl", "is-active", "--quiet", f"brms-{opdracht}"], timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False  # geen systemd (bijv. Windows): niets te controleren
    return r.returncode == 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="brms", description=f"BRMS {VERSION}", epilog=OPDRACHTEN,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default=config.STANDAARD_PAD, help="pad naar brms.toml")
    p.add_argument("--debug", action="store_true", help="uitgebreide logging")
    p.add_argument("--toch", action="store_true",
                   help="een service toch met de hand starten naast die van systemd (voor testen)")
    p.add_argument("--terug", action="store_true",
                   help="bij update: terug naar de versie van vóór de laatste update")
    p.add_argument("opdracht",
                   choices=("check-config", "grootheden", "start", "stop", "herstart", "update",
                            "monitor", "sim", "simuleer", "ble-reader", "shelly-reader", "opslag", "alarm",
                            "camera",
                            "web", "go2rtc",
                            "monitor-vak"))
    p.add_argument("sensoren", nargs="*",
                   help="bij monitor: naam of ID van sensor(en) voor trace; "
                        "bij sim: naam of ID van sensor(en), 'all', of 'stop ...'; "
                        "bij grootheden: sensortype")
    args = p.parse_args(argv)

    if args.opdracht == "check-config":
        return _check_config(args.config)

    if args.opdracht == "grootheden":
        return _grootheden(args.sensoren)

    if args.opdracht in ("start", "stop", "herstart"):
        return _beheer(args.opdracht, args.config)

    if args.opdracht == "update":
        return _update(args.terug)

    if args.opdracht in ("monitor", "monitor-vak"):
        return _monitor(args)

    if args.opdracht in ("sim", "simuleer"):
        return _simuleer(args)

    # Beveiliging: een service niet met de hand starten als systemd hem al draait.
    if _al_actief(args.opdracht) and not args.toch:
        naam = f"brms-{args.opdracht}"
        print(f"{naam} draait al (systemd). Deze opdracht is voor systemd; gebruik", file=sys.stderr)
        for opdracht, doel in ((f"sudo systemctl restart {naam}", "om te herstarten"),
                               (f"journalctl -u {naam} -f", "om mee te kijken"),
                               ("brms monitor", "om de metingen te volgen")):
            print(f"  {opdracht:<40} {doel}", file=sys.stderr)
        print("(Toch starten, alleen voor testen: --toch)", file=sys.stderr)
        return 1

    # Logging naar stderr; onder systemd komt dat in journald, met het niveau erbij zodat
    # het logboek in het web erop kan filteren (§5.5.5 punt 2; zie brms/systeem.py).
    systeem.stel_logging_in(args.debug)
    log = logging.getLogger("brms")

    # Configuratie controleren vóór het starten (§5.4.1 punt 5). Bij een fout stopt de
    # service met een duidelijke melding in het log.
    try:
        cfg = config.lees(args.config)
    except config.ConfigFout as e:
        for f in e.fouten:
            log.error("configuratiefout: %s", f)
        return 1

    log.info("BRMS %s, %s", VERSION, args.opdracht)
    # Vervallen velden en dergelijke ook in het log van de service (§5.4.1).
    for w in cfg.waarschuwingen:
        log.warning("configuratie: %s", w)
    try:
        if args.opdracht == "go2rtc":
            # go2rtc is een los programma: BRMS schrijft zijn instellingen en start het dan
            # in plaats van zichzelf (§5.7 punt 18). Komt alleen terug bij een fout.
            from brms import go2rtc
            return go2rtc.start(cfg)
        if args.opdracht == "web":
            # Het web is Flask (geen asyncio, §3.2) en heeft zijn eigen hoofdlus.
            from brms import web
            web.draai(cfg, args.config)
        else:
            asyncio.run(_service(args.opdracht, cfg))
    except KeyboardInterrupt:
        pass
    except Exception:
        # Een onverwachte fout waardoor de service stopt: eerst als fout loggen, zodat hij
        # in het logboek staat en een melding geeft (§5.5.5 punt 2). systemd start de
        # service daarna opnieuw (§3.1 punt 1).
        log.critical("onverwachte fout; de service %s stopt", args.opdracht, exc_info=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
