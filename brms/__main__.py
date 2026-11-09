"""Opdrachtregel: python -m brms [--config PAD] <opdracht>

Opdrachten:
  check-config   configuratie controleren (vóór een herstart, §5.4.1)
  ble-reader     BLE-reader-service (§5.2.1)
  opslag         opslag-service (§5.3)
"""

import argparse
import asyncio
import logging
import signal
import sys

from brms import VERSION, config


def _check_config(cfg_pad: str) -> int:
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
    from brms.bus import Bus

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, AttributeError):  # Windows
            pass

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

    # journald voegt zelf tijd en servicenaam toe
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("brms")
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
