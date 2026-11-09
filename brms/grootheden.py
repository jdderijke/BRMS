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


GROOTHEDEN: dict[str, Grootheid] = {
    g.naam: g
    for g in (
        Grootheid("temperatuur", "°C", DOORLOPEND),
        Grootheid("vocht", "%", DOORLOPEND),
        Grootheid("luchtdruk", "hPa", DOORLOPEND),
        Grootheid("versnelling_x", "mG", DOORLOPEND),
        Grootheid("versnelling_y", "mG", DOORLOPEND),
        Grootheid("versnelling_z", "mG", DOORLOPEND),
        Grootheid("beweging", "aantal", TOENAME),
        Grootheid("batterijspanning", "V", DOORLOPEND),
        Grootheid("rssi", "dBm", DOORLOPEND),
        Grootheid("spanning", "V", DOORLOPEND),
        Grootheid("stroom", "A", DOORLOPEND),
        Grootheid("soc", "%", DOORLOPEND),
        Grootheid("verbruikt", "Ah", DOORLOPEND),
        Grootheid("resterende_tijd", "min", DOORLOPEND),
        Grootheid("zonnevermogen", "W", DOORLOPEND),
        Grootheid("laadstroom_zon", "A", DOORLOPEND),
        Grootheid("walstroom", "0/1", TOESTAND),
        Grootheid("bilge", "0/1", TOESTAND),
        Grootheid("bilgepomp", "0/1", TOESTAND),
    )
}


def soort(naam: str) -> str:
    """Soort van een grootheid; KeyError bij een onbekende grootheid."""
    return GROOTHEDEN[naam].soort
