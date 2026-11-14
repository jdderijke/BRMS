"""Tabel van grootheden (ARCHITECTURE.md §5.3.3).

Eén plek in de code; ARCHITECTURE.md bevat een kopie. Een reader kan alleen grootheden
uit deze tabel publiceren. De soort ligt hier vast (niet in de configuratie), zodat een
configfout een toestand nooit tot gemiddelde kan maken (§5.3.2).
"""

from dataclasses import dataclass

DOORLOPEND = "doorlopend"  # opslag: gemiddelde per interval
TOESTAND = "toestand"      # 0/1; opslag: bij elke verandering + actuele toestand per interval
TOENAME = "toename"        # opslag: som per interval

SOORTEN = (DOORLOPEND, TOESTAND, TOENAME)


@dataclass(frozen=True)
class Grootheid:
    naam: str
    eenheid: str
    soort: str
    # Het aantal decimalen waarmee het web een waarde van deze grootheid toont (§5.3.3,
    # v0.61). Alleen voor het tonen: de opslag en de alarmen rekenen met de volle waarde.
    # Een vast aantal per grootheid houdt de getallen op het scherm even breed, zodat de
    # tekst niet verspringt als een waarde verandert.
    decimalen: int


# De decimalen (§5.3.3): 0 voor luchtdruk, toestanden (0/1), versnelling, rssi, beweging en
# resterende tijd; 1 voor temperatuur, vocht, laadtoestand, zonnevermogen en verbruik;
# 2 voor spanningen en stromen.
GROOTHEDEN: dict[str, Grootheid] = {
    g.naam: g
    for g in (
        Grootheid("temperatuur", "°C", DOORLOPEND, 1),
        Grootheid("vocht", "%", DOORLOPEND, 1),
        Grootheid("luchtdruk", "hPa", DOORLOPEND, 0),
        Grootheid("versnelling_x", "mG", DOORLOPEND, 0),
        Grootheid("versnelling_y", "mG", DOORLOPEND, 0),
        Grootheid("versnelling_z", "mG", DOORLOPEND, 0),
        Grootheid("beweging", "aantal", TOENAME, 0),
        Grootheid("batterijspanning", "V", DOORLOPEND, 2),
        Grootheid("rssi", "dBm", DOORLOPEND, 0),
        Grootheid("spanning", "V", DOORLOPEND, 2),
        Grootheid("stroom", "A", DOORLOPEND, 2),
        Grootheid("soc", "%", DOORLOPEND, 1),
        Grootheid("verbruikt", "Ah", DOORLOPEND, 1),
        Grootheid("resterende_tijd", "min", DOORLOPEND, 0),
        Grootheid("zonnevermogen", "W", DOORLOPEND, 1),
        Grootheid("laadstroom_zon", "A", DOORLOPEND, 2),
        Grootheid("walstroom", "0/1", TOESTAND, 0),
        Grootheid("bilge", "0/1", TOESTAND, 0),
        Grootheid("bilgepomp", "0/1", TOESTAND, 0),
    )
}


def soort(naam: str) -> str:
    """Soort van een grootheid; KeyError bij een onbekende grootheid."""
    return GROOTHEDEN[naam].soort
