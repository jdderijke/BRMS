"""Decoder voor de Shelly Plus Uni (ARCHITECTURE.md §5.2.4).

Zuivere functies: het antwoord van de Shelly (JSON, al omgezet naar een dict) erin,
waarden eruit. Geen netwerk, zodat alles te testen is zonder Shelly (tests/test_shelly.py).
Het pollen zelf staat in readers/shelly.py.

De Shelly Plus Uni is een apparaat van de tweede generatie ("Gen2"). Eén aanvraag

    http://<adres>/rpc/Shelly.GetStatus

geeft de toestand van alle onderdelen ("componenten") tegelijk, elk onder een eigen naam:

    {
      "input:0":       {"id": 0, "state": false},        digitale ingang (contact)
      "input:1":       {"id": 1, "state": true},
      "voltmeter:100": {"id": 100, "voltage": 12.61},    spanningsmeting, in volt
      "wifi":          {"sta_ip": "...", "rssi": -58, ...},
      ...
    }

- De twee digitale ingangen (input:0 en input:1) zitten er altijd in.
- De spanningsmeting is een randapparaat ("peripheral") dat eerst in de Shelly moet worden
  toegevoegd (in de app of webpagina van de Shelly: Add-on / Peripherals -> Voltmeter).
  Daarna verschijnt hij als voltmeter:<nummer>, meestal 100.

Welke component welke grootheid levert, staat in de configuratie ('bron', §5.2.4 punt 4):

    bron = { bilge = "input:0", bilgepomp = "voltmeter:100" }

Omzetten naar 0/1:
- input:      state true -> 1, false -> 0; met 'omgekeerd' andersom (voor een contact dat
              juist opengaat als de vlotter omhoog komt);
- voltmeter:  spanning >= drempel -> 1, anders 0 (de pomp krijgt spanning of niet).
"""

import re
from dataclasses import dataclass

# De grootheden die dit sensortype kan leveren (§5.4.2 punt 3). De reader voegt de
# signaalsterkte van de wifi (rssi) toe. Gepubliceerd wordt alleen wat een 'bron' heeft.
LEVERT = ("bilge", "bilgepomp")

# Een bron in de configuratie: soort component en nummer, bijv. "input:0", "voltmeter:100".
BRON = re.compile(r"^(input|voltmeter):(\d+)$")


class ShellyFout(Exception):
    """Het antwoord van de Shelly is niet te gebruiken."""


@dataclass(frozen=True)
class Veld:
    """Eén grootheid uit het antwoord, met de tussenstappen voor de trace (§10.2)."""
    grootheid: str
    bron: str            # de component, bijv. "voltmeter:100"
    ruw: object          # wat de Shelly meldt: true/false of de spanning; None als het ontbreekt
    waarde: float | None  # 0.0 of 1.0; None = niet te bepalen (wordt niet gepubliceerd)
    uitleg: str          # hoe ruw in waarde is omgezet, of waarom dat niet kon


def decodeer_velden(status: dict, bron: dict[str, str], drempel: float | None = None,
                    omgekeerd: tuple[str, ...] = ()) -> list[Veld]:
    """Zet het antwoord van Shelly.GetStatus om in velden, één per grootheid uit `bron`.

    status     het antwoord als dict
    bron       {grootheid: component}, bijv. {"bilge": "input:0"}
    drempel    spanning in volt vanaf waar een voltmeter-bron 1 geeft
    omgekeerd  grootheden waarvan 0 en 1 verwisseld worden

    Een grootheid waarvan de component ontbreekt of geen bruikbare waarde geeft, krijgt
    waarde None: die wordt niet gepubliceerd (§5.2.3 punt 4). Een verzonnen 0 zou een
    kapotte aansluiting verbergen.
    """
    if not isinstance(status, dict):
        raise ShellyFout("het antwoord is geen JSON-object")
    velden = []
    for grootheid, component in bron.items():
        soort = component.split(":")[0]
        deel = status.get(component)
        if not isinstance(deel, dict):
            velden.append(Veld(grootheid, component, None, None, "ontbreekt in het antwoord"))
            continue
        if soort == "input":
            ruw = deel.get("state")
            # Alleen een schakelingang meldt true/false; een analoge of tellende ingang niet.
            if not isinstance(ruw, bool):
                velden.append(Veld(grootheid, component, ruw, None, "geen toestand (true/false)"))
                continue
            aan, uitleg = ruw, "true = 1"
        else:
            ruw = deel.get("voltage")
            # De Shelly meldt null als hij de spanning niet kan meten. (bool is in Python
            # een getal; true/false is hier geen spanning.)
            if isinstance(ruw, bool) or not isinstance(ruw, (int, float)):
                velden.append(Veld(grootheid, component, ruw, None, "geen spanning gemeten"))
                continue
            if drempel is None:
                velden.append(Veld(grootheid, component, ruw, None, "geen drempel ingesteld"))
                continue
            aan = ruw >= drempel
            uitleg = f"{ruw:g} V {'>=' if aan else '<'} drempel {drempel:g} V"
        if grootheid in omgekeerd:
            aan, uitleg = not aan, uitleg + ", omgekeerd"
        velden.append(Veld(grootheid, component, ruw, 1.0 if aan else 0.0, uitleg))
    return velden


def decodeer(status: dict, bron: dict[str, str], drempel: float | None = None,
             omgekeerd: tuple[str, ...] = ()) -> dict[str, float]:
    """Als decodeer_velden, maar alleen {grootheid: waarde} van wat te bepalen was."""
    return {v.grootheid: v.waarde for v in decodeer_velden(status, bron, drempel, omgekeerd)
            if v.waarde is not None}


def rssi(status: dict) -> int | None:
    """De signaalsterkte van de wifi-verbinding van de Shelly in dBm, of None als hij
    ontbreekt. Een Shelly zonder wifi-verbinding meldt 0; dat is geen meting."""
    wifi = status.get("wifi") if isinstance(status, dict) else None
    waarde = wifi.get("rssi") if isinstance(wifi, dict) else None
    if isinstance(waarde, bool) or not isinstance(waarde, (int, float)) or waarde == 0:
        return None
    return round(waarde)
