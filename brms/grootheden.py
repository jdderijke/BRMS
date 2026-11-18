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
# resterende tijd; 1 voor temperatuur, vocht, laadtoestand, (zonne)vermogen en verbruik;
# 2 voor spanningen en stromen. Slagzij 0 (hele graden, v0.88); de alarmlaag gebruikt
# dit aantal ook voor de slagzij in een melding (v0.89, alarms.waardetekst).
GROOTHEDEN: dict[str, Grootheid] = {
    g.naam: g
    for g in (
        Grootheid("temperatuur", "°C", DOORLOPEND, 1),
        Grootheid("vocht", "%", DOORLOPEND, 1),
        Grootheid("luchtdruk", "hPa", DOORLOPEND, 0),
        Grootheid("versnelling_x", "mG", DOORLOPEND, 0),
        Grootheid("versnelling_y", "mG", DOORLOPEND, 0),
        Grootheid("versnelling_z", "mG", DOORLOPEND, 0),
        # Afgeleid uit de versnellingen (§5.2.5, brms/afleiden.py). Het getal heeft een
        # teken: positief = stuurboord, negatief = bakboord. Zie `zijde` hieronder.
        # Hele graden (v0.88): de hoek is op 1 à 2 graden nauwkeurig (§5.2.5), een decimaal
        # suggereert meer dan de sensor kan.
        Grootheid("slagzij", "graden", DOORLOPEND, 0),
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
        # Het Shelly-relais (§5.2.6): de stand van het relais, en wat het geschakelde
        # apparaat (de kachel) opneemt. Spanning en stroom van de plug gebruiken de
        # bestaande grootheden 'spanning' en 'stroom'.
        Grootheid("relais", "0/1", TOESTAND, 0),
        Grootheid("vermogen", "W", DOORLOPEND, 1),
    )
}


def soort(naam: str) -> str:
    """Soort van een grootheid; KeyError bij een onbekende grootheid."""
    return GROOTHEDEN[naam].soort


# Grootheden die getoond worden met een kant in plaats van een teken (§5.2.5 punt 10):
# naam -> (kant bij een positieve waarde, kant bij een negatieve waarde). De gebruiker
# rekent met stuurboord en bakboord, niet met plus en min; het teken staat alleen in
# brms.toml en in de database. De webpagina krijgt deze tabel mee (brms/web).
ZIJDEN: dict[str, tuple[str, str]] = {"slagzij": ("sb", "bb")}


def zijde(naam: str, waarde: float) -> str | None:
    """De kant waarmee een waarde getoond wordt ('sb' of 'bb'), of None als de grootheid
    gewoon met haar eenheid getoond wordt. Bij precies 0 is er geen kant: dan "" (leeg).

    Voorbeeld: zijde("slagzij", -1.8) -> "bb";  zijde("temperatuur", -1.8) -> None.
    """
    if naam not in ZIJDEN:
        return None
    positief, negatief = ZIJDEN[naam]
    return positief if waarde > 0 else negatief if waarde < 0 else ""
