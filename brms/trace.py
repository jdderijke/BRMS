"""Trace: één meting door de hele keten volgen (ARCHITECTURE.md §10.2).

Standaard staat trace uit en doet dit module niets. De monitor (`brms monitor kajuit`)
zet trace aan voor één of meer sensoren met de opdracht

    brms/opdracht/trace/aan   {"sensor_ids": ["A4:C1:38:..."], "duur": 600}

Daarna publiceren de readers en de opslag voor die sensoren per stap wat ze doen, op

    brms/trace/<sensor_id>    {"ts", "sensor_id", "dienst", "stap", "tekst"[, "deel"]}

Trace gaat na `duur` seconden vanzelf uit (de monitor vernieuwt de opdracht zolang hij
draait), of direct met brms/opdracht/trace/uit. Zo kan trace nooit per ongeluk blijven
aanstaan.

Stappen (§10.2 punt 4): ruw, gedecodeerd, gecorrigeerd, gepubliceerd (readers),
opgenomen, opgeslagen (opslag).
"""

import json
import logging
import threading
import time

log = logging.getLogger(__name__)

TOPIC_AAN = "brms/opdracht/trace/aan"
TOPIC_UIT = "brms/opdracht/trace/uit"
TOPIC_PREFIX = "brms/trace"

# Looptijd als de opdracht geen duur noemt, en de maximale looptijd.
STANDAARD_DUUR = 600
MAX_DUUR = 3600


class Tracer:
    """Houdt bij voor welke sensoren trace aanstaat en publiceert traceberichten.

    Elke service (BLE-reader, Shelly-reader, opslag) heeft één Tracer. De opdrachten komen
    binnen in paho's netwerkthread, het traceren gebeurt in de asyncio-loop; daarom een lock.
    """

    def __init__(self, bus, dienst: str, klok=time.monotonic):
        """dienst: naam van de service in de traceberichten, bijv. 'ble-reader'.
        klok: monotone klok (§4.2); te vervangen in tests."""
        self._bus = bus
        self._dienst = dienst
        self._klok = klok
        self._lock = threading.Lock()
        self._tot: dict[str, float] = {}  # sensor_id -> einde van de trace (monotone klok)

    def start(self) -> None:
        """Luistert naar trace-opdrachten op de bus."""
        self._bus.abonneer(TOPIC_AAN, 1, self.bij_opdracht)
        self._bus.abonneer(TOPIC_UIT, 1, self.bij_opdracht)

    # --- opdrachten (draait in paho's thread) ---------------------------------------
    def bij_opdracht(self, topic: str, payload: bytes) -> None:
        try:
            d = json.loads(payload)
            ids = d.get("sensor_ids") or []
            if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
                raise ValueError("sensor_ids moet een lijst van teksten zijn")
        except (ValueError, AttributeError) as e:
            log.warning("ongeldige trace-opdracht op %s: %s", topic, e)
            return
        with self._lock:
            if topic == TOPIC_AAN:
                duur = d.get("duur", STANDAARD_DUUR)
                if isinstance(duur, bool) or not isinstance(duur, (int, float)) or duur <= 0:
                    duur = STANDAARD_DUUR
                einde = self._klok() + min(duur, MAX_DUUR)
                for i in ids:
                    if i not in self._tot:
                        log.info("trace aan voor %s", i)
                    self._tot[i] = einde
            else:
                for i in ids:
                    if self._tot.pop(i, None) is not None:
                        log.info("trace uit voor %s", i)

    # --- traceren -----------------------------------------------------------------
    def actief(self, sensor_id: str) -> bool:
        """True als trace voor deze sensor aanstaat (en niet verlopen is)."""
        with self._lock:
            einde = self._tot.get(sensor_id)
            if einde is None:
                return False
            if self._klok() >= einde:
                del self._tot[sensor_id]
                log.info("trace verlopen voor %s", sensor_id)
                return False
            return True

    def voor(self, sensor_id: str):
        """Geeft een functie stap(stap, tekst) als trace voor deze sensor aanstaat,
        anders None. Zo kost trace niets als hij uit staat: de aanroeper slaat het
        opbouwen van de tracetekst dan helemaal over."""
        if not self.actief(sensor_id):
            return None
        return lambda stap, tekst, deel="": self.stuur(sensor_id, stap, tekst, deel)

    def stuur(self, sensor_id: str, stap: str, tekst: str, deel: str = "") -> None:
        """deel: voor een stap die per meting uit meerdere berichten bestaat (de opslag
        stuurt 'opgenomen' per grootheid). De monitor geeft elk deel een eigen vaste plaats
        binnen de stap; zonder deel overschrijft een bericht de hele stap."""
        bericht = {"ts": int(time.time()), "sensor_id": sensor_id, "dienst": self._dienst,
                   "stap": stap, "tekst": tekst}
        if deel:
            bericht["deel"] = deel
        self._bus.publiceer(f"{TOPIC_PREFIX}/{sensor_id}",
                            json.dumps(bericht, ensure_ascii=False, separators=(",", ":")))
