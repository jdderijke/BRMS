"""Afgeleide grootheden (ARCHITECTURE.md §5.2.5).

Een afgeleide grootheid wordt niet gemeten maar uit metingen berekend, bijvoorbeeld de
slagzij van de boot uit de versnellingen van een Ruuvi. Dat gebeurt hier, los van de
decoders: decoderen (bytes naar getallen) en afleiden (uit getallen nieuwe getallen
berekenen) zijn twee verschillende bezigheden (§5.2.5, redenering).

De plaats in de reader (§5.2.5 punt 4):

    decoderen -> corrigeren -> AFLEIDEN -> lijst 'grootheden' toepassen -> publiceren

Een afleiding rekent dus met de gecorrigeerde waarden, en haar uitkomst is daarna een
gewone meting: zelfde bus, opslag, alarmen en grafieken.

Deze module kent geen sensortypen. Een afleiding hangt af van haar invoer: elke sensor
die de invoer levert, kan de afgeleide waarde leveren (§5.2.5 punt 3).

Een nieuwe afleiding toevoegen (§5.2.5 punt 8): een regel in brms/grootheden.py, een regel
in AFLEIDINGEN hieronder, en de tabellen in ARCHITECTURE.md.
"""

import math
from dataclasses import dataclass
from typing import Callable

# De toegestane waarden van het veld 'stuurboord' van een sensor (§5.2.5 punt 10): de
# versnelling die positief wordt als de boot over stuurboord helt. De letter is de as van
# de sensor die dwarsscheeps ligt; een min ervoor betekent dat die as naar bakboord wijst.
STUURBOORD = ("x", "-x", "y", "-y")


@dataclass(frozen=True)
class Afleiding:
    """Eén regel uit de tabel van afleidingen (§5.2.5 punt 2)."""
    # De naam van de afgeleide grootheid (staat ook in brms/grootheden.py).
    grootheid: str
    # De grootheden die een sensortype moet leveren om deze afleiding te kunnen maken.
    invoer: tuple[str, ...]
    # Het veld van de sensor in brms.toml dat de afleiding nodig heeft, of None als ze
    # zonder instelling werkt. Zonder dat veld levert de sensor de grootheid niet (punt 3).
    instelling: str | None
    # De berekening: (gecorrigeerde waarden van één ontvangst, de sensor) -> (waarde,
    # uitleg voor de trace), of None als er voor deze ontvangst geen waarde is (punt 5).
    bereken: Callable[[dict, object], "tuple[float, str] | None"]


def _slagzij(waarden: dict, sensor) -> "tuple[float, str] | None":
    """De slagzij in graden: positief = stuurboord, negatief = bakboord (§5.2.5 punt 10).

    Een sensor die stil ligt meet alleen de zwaartekracht (ongeveer 1000 mG). Ligt hij
    plat, dan staat die bijna helemaal op de z-as. Helt de boot, dan komt een deel op de
    dwarsscheepse as terecht. De hoek volgt uit de verhouding van die twee:

        slagzij = atan2(dwars, |z|)

    - 'dwars' is de versnelling op de dwarsscheepse as, met het teken zo gedraaid dat
      stuurboord positief is. Welke as dat is en welke kant op, zegt het veld
      'stuurboord' van de sensor.
    - Van z wordt de grootte genomen: de sensor mag ook onderste boven gemonteerd zijn.

    De montagefout en de afwijking van de sensor zitten er nog in; die haalt de gewone
    correctie op 'slagzij' eraf (de kalibratie, toegepast door de reader).
    """
    keuze = sensor.stuurboord              # bijv. "-y"
    teken = -1 if keuze.startswith("-") else 1
    as_ = keuze[-1]                        # "x" of "y"
    dwars = waarden.get("versnelling_" + as_)
    z = waarden.get("versnelling_z")
    # Ontbreekt een invoerwaarde (ongeldig, of niet uitgezonden), dan is er voor deze
    # ontvangst geen slagzij (punt 5).
    if dwars is None or z is None:
        return None
    # Beide nul: er is geen richting om een hoek uit te berekenen. Komt in de praktijk
    # alleen voor bij een sensor in vrije val; liever geen waarde dan een verzonnen 0.
    if dwars == 0 and z == 0:
        return None
    # Afgerond op 2 decimalen: 1 mG is ongeveer 0,06 graden, meer cijfers zeggen niets.
    hoek = round(math.degrees(math.atan2(teken * dwars, abs(z))), 2)
    uitleg = (f"atan2({'-' if teken < 0 else ''}versnelling_{as_} = {teken * dwars:g},"
              f" |versnelling_z| = {abs(z):g}) = {hoek:g} graden")
    return hoek, uitleg


# De tabel van afleidingen (§5.2.5 punt 2). De volgorde is de volgorde waarin ze berekend
# en gepubliceerd worden.
AFLEIDINGEN: dict[str, Afleiding] = {
    a.grootheid: a
    for a in (
        Afleiding("slagzij", ("versnelling_x", "versnelling_y", "versnelling_z"),
                  "stuurboord", _slagzij),
    )
}


def voor_type(levert: tuple[str, ...]) -> tuple[str, ...]:
    """De afgeleide grootheden die een sensortype kan leveren, gegeven wat zijn decoder
    levert: alle afleidingen waarvan het type de hele invoer levert (§5.2.5 punt 3)."""
    return tuple(a.grootheid for a in AFLEIDINGEN.values()
                 if all(g in levert for g in a.invoer))


def ontbreekt(grootheid: str, sensor) -> str | None:
    """De naam van het veld dat de sensor mist om deze grootheid te leveren, of None als
    er niets ontbreekt (ook als de grootheid geen afgeleide grootheid is).

    Voorbeeld: ontbreekt("slagzij", sensor zonder 'stuurboord') -> "stuurboord".
    """
    a = AFLEIDINGEN.get(grootheid)
    if a is None or a.instelling is None:
        return None
    return a.instelling if getattr(sensor, a.instelling, None) is None else None


def leid_af(waarden: dict, sensor) -> tuple[dict, list[str]]:
    """Berekent de afgeleide waarden van één ontvangst.

    waarden   de gecorrigeerde waarden van deze ontvangst, {grootheid: waarde}
    sensor    de sensor uit de configuratie (voor de instelling van een afleiding)

    Geeft terug: ({afgeleide grootheid: waarde}, regels voor de trace). Een afleiding
    waarvoor de sensor de instelling mist, wordt stil overgeslagen: de sensor levert die
    grootheid dan niet. Ontbreekt alleen een invoerwaarde, dan staat dat in de trace.
    """
    nieuw: dict = {}
    regels: list[str] = []
    for a in AFLEIDINGEN.values():
        if a.instelling is not None and getattr(sensor, a.instelling, None) is None:
            continue
        uitkomst = a.bereken(waarden, sensor)
        if uitkomst is None:
            regels.append(f"{a.grootheid:<17} geen waarde: invoer ontbreekt")
            continue
        nieuw[a.grootheid], uitleg = uitkomst
        regels.append(f"{a.grootheid:<17} {uitleg}")
    return nieuw, regels
