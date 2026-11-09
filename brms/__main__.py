"""Opdrachtregel van BRMS.

    python -m brms [--config PAD] [--debug] <opdracht>

Op de Pi installeert install.sh een korte opdracht `brms` die hetzelfde doet.

Opdrachten:
  check-config   configuratie controleren, bijv. vóór een herstart (§5.4.1)
  ble-reader     de BLE-reader-service starten (§5.2.1)
  opslag         de opslag-service starten (§5.3)

De services worden normaal door systemd gestart (deploy/systemd/), niet met de hand.
"""

import argparse
import asyncio
import logging
import signal
import sys

from brms import VERSION, config


def _check_config(cfg_pad: str) -> int:
    """Controleert de configuratie en meldt alle fouten. Exitcode 0 = in orde."""
    try:
        cfg = config.lees(cfg_pad)
    except config.ConfigFout as e:
        print(f"Configuratie {cfg_pad} bevat fouten:", file=sys.stderr)
        for f in e.fouten:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print(f"Configuratie {cfg_pad} is in orde: {len(cfg.sensoren)} sensoren.")
    return 0


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
        else:
            from brms import storage
            await storage.draai(cfg, bus, stop)
    finally:
        bus.stop()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="brms", description=f"BRMS {VERSION}")
    p.add_argument("--config", default=config.STANDAARD_PAD, help="pad naar brms.toml")
    p.add_argument("--debug", action="store_true", help="uitgebreide logging")
    p.add_argument("opdracht", choices=("check-config", "ble-reader", "opslag"))
    args = p.parse_args(argv)

    if args.opdracht == "check-config":
        return _check_config(args.config)

    # Logging naar stderr; onder systemd komt dat in journald, dat zelf tijd en
    # servicenaam toevoegt. Daarom hier alleen niveau, module en bericht.
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
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
    try:
        asyncio.run(_service(args.opdracht, cfg))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
