"""Decoder voor RuuviTag dataformaat 5, "RAWv2" (ARCHITECTURE.md §5.2.3).

Een RuuviTag (Pro) zendt zijn metingen uit in een Bluetooth-advertentie, in de
"manufacturer data" onder fabrikant-ID 0x0499 (Ruuvi Innovations). bleak levert die bytes
zonder het fabrikant-ID zelf: byte 0 is dan het dataformaat.

Opbouw van formaat 5 (24 bytes, alles big-endian), zie
https://docs.ruuvi.com/communication/bluetooth-advertisements/data-format-5-rawv2 :

    byte 0       dataformaat (moet 5 zijn)
    byte 1-2     temperatuur     int16   x 0,005 °C          ongeldig: 0x8000
    byte 3-4     vocht           uint16  x 0,0025 %          ongeldig: 0xFFFF
    byte 5-6     luchtdruk       uint16  + 50000 Pa          ongeldig: 0xFFFF
    byte 7-8     versnelling X   int16   mG                  ongeldig: 0x8000
    byte 9-10    versnelling Y   int16   mG                  ongeldig: 0x8000
    byte 11-12   versnelling Z   int16   mG                  ongeldig: 0x8000
    byte 13-14   voeding         bovenste 11 bits: batterijspanning in mV boven 1600 mV
                                                             ongeldig: 2047
                                 onderste 5 bits: zendvermogen (door BRMS niet gebruikt)
    byte 15      bewegingsteller uint8                       ongeldig: 255
    byte 16-17   volgnummer van de meting (niet gebruikt)
    byte 18-23   MAC-adres (niet gebruikt; bleak levert het adres al)

Voorbeeld (de officiële testvector "valid data" van Ruuvi):

    05 | 12 fc | 53 94 | c3 7c | 00 04 | ff fc | 04 0c | ac 36 | 42 | 00 cd | cb b8 33 4c 88 4f
         ^       ^       ^       ^       ^       ^       ^       ^
         |       |       |       |       |       |       |       bewegingsteller 66
         |       |       |       |       |       |       0xac36 >> 5 = 1377 + 1600 = 2977 mV = 2,977 V
         |       |       |       |       |       Z = 1036 mG
         |       |       |       |       Y = -4 mG
         |       |       |       X = 4 mG
         |       |       0xc37c = 50044 + 50000 = 100044 Pa = 1000,44 hPa
         |       0x5394 = 21396 x 0,0025 = 53,49 %
         0x12fc = 4860 x 0,005 = 24,3 °C

Ongeldige waarden: een sensor die een grootheid niet kan meten (bijv. luchtdruk bij een
RuuviTag Pro 3-in-1) zendt daarvoor een vaste "ongeldig"-waarde uit. Zo'n grootheid wordt
niet gepubliceerd (§5.2.3 punt 4).

Bewegingsteller: de decoder geeft de ruwe tellerstand terug onder de naam TELLER. Dat is
geen grootheid; de BLE-reader onthoudt per sensor de vorige stand en publiceert de toename
als grootheid `beweging` (§5.2.3 punt 5, §5.3.2). De functie `toename` hieronder rekent
die toename uit.

Dit zijn zuivere functies: bytes in, waarden uit. Daardoor zijn ze te testen zonder
Bluetooth en zonder Pi (tests/test_ruuvi.py, met de officiële testvectoren van Ruuvi).
"""

from dataclasses import dataclass

# Fabrikant-ID van Ruuvi Innovations; de sleutel in de manufacturer data van bleak.
FABRIKANT_ID = 0x0499

# Lengte van een compleet formaat 5-bericht (zonder het fabrikant-ID).
_LENGTE = 24

# Naam waaronder de ruwe bewegingsteller uit de decoder komt. Geen grootheid: de reader
# maakt er de toename `beweging` van.
TELLER = "bewegingsteller"

# De bewegingsteller loopt van 0 t/m 254 (255 betekent "ongeldig") en begint daarna weer
# bij 0. Er zijn dus 255 standen.
_TELLER_STANDEN = 255


class RuuviFout(ValueError):
    """Het bericht is geen geldig Ruuvi formaat 5-bericht."""


# De velden die BRMS gebruikt:
#   (naam, positie, met teken, bits naar rechts schuiven, "ongeldig"-waarde,
#    omrekening naar de eenheid van de grootheid (§5.3.3), uitleg voor de trace)
#
# Elk veld is 2 bytes, behalve de bewegingsteller (1 byte, zie decodeer_velden).
# Er wordt afgerond op het aantal decimalen van de resolutie, zodat 4860 x 0,005 netjes
# 24.3 wordt en niet 24.300000000000004.
_VELDEN = (
    ("temperatuur",      1, True,  0, -32768, lambda n: round(n * 0.005, 3),          "× 0.005"),
    ("vocht",            3, False, 0, 0xFFFF, lambda n: round(n * 0.0025, 4),         "× 0.0025"),
    # Pa -> hPa: delen door 100.
    ("luchtdruk",        5, False, 0, 0xFFFF, lambda n: round((n + 50000) / 100, 2),  "+ 50000 Pa, / 100"),
    ("versnelling_x",    7, True,  0, -32768, lambda n: n,                            "mG"),
    ("versnelling_y",    9, True,  0, -32768, lambda n: n,                            "mG"),
    ("versnelling_z",   11, True,  0, -32768, lambda n: n,                            "mG"),
    # Bovenste 11 bits van byte 13-14; mV -> V: delen door 1000.
    ("batterijspanning", 13, False, 5, 2047,  lambda n: round((n + 1600) / 1000, 3),  "+ 1600 mV, / 1000"),
)

# De grootheden die deze decoder kan leveren (ARCHITECTURE.md §5.4.2 punt 3). `beweging`
# hoort erbij: de reader berekent die uit de bewegingsteller van deze decoder.
# Gebruikt door `brms check-config` en `brms grootheden ruuvi`.
LEVERT = tuple(naam for naam, *_ in _VELDEN) + ("beweging",)


@dataclass(frozen=True)
class Veld:
    """Eén gedecodeerd veld, met de tussenstappen (voor trace, §10.2)."""
    grootheid: str        # naam van de grootheid, of TELLER voor de bewegingsteller
    ruw: bytes            # de bytes zoals ze in het bericht staan (big-endian)
    getal: int            # die bytes als geheel getal (na eventueel schuiven)
    uitleg: str           # de omrekening in woorden, bijv. "× 0.005"
    waarde: float | None  # omgerekende waarde; None = de sensor meldt "ongeldig"


def decodeer(data: bytes) -> dict[str, float]:
    """Decodeert Ruuvi formaat 5 naar {grootheid: waarde}.

    - Velden die de sensor als "ongeldig" uitzendt, ontbreken in het resultaat.
    - De ruwe bewegingsteller staat onder de sleutel TELLER (zie de uitleg bovenaan).
    - RuuviFout bij een leeg of te kort bericht, of een ander dataformaat dan 5.
    """
    return {v.grootheid: v.waarde for v in decodeer_velden(data) if v.waarde is not None}


def decodeer_velden(data: bytes) -> list[Veld]:
    """Als decodeer(), maar met de tussenstappen per veld (voor trace). Ongeldige velden
    staan er ook in, met waarde None."""
    if not data:
        raise RuuviFout("leeg bericht")
    if data[0] != 5:
        # Oudere RuuviTags (formaat 3) en andere Ruuvi-producten gebruiken een ander formaat.
        raise RuuviFout(f"dataformaat {data[0]} wordt niet ondersteund (alleen formaat 5)")
    if len(data) < _LENGTE:
        raise RuuviFout(f"bericht afgebroken ({len(data)} van {_LENGTE} bytes)")

    velden: list[Veld] = []
    for naam, pos, signed, schuif, ongeldig, reken, uitleg in _VELDEN:
        ruw = bytes(data[pos:pos + 2])
        getal = int.from_bytes(ruw, "big", signed=signed) >> schuif
        velden.append(Veld(naam, ruw, getal, uitleg,
                           None if getal == ongeldig else reken(getal)))

    # Bewegingsteller: één byte, zonder omrekening.
    teller = data[15]
    velden.append(Veld(TELLER, bytes(data[15:16]), teller, "tellerstand",
                       None if teller == 255 else teller))
    return velden


def toename(vorige: int, nieuw: int) -> int:
    """Toename van de bewegingsteller tussen twee ontvangen standen.

    De teller springt na 254 terug naar 0; van 253 naar 2 is dus een toename van 4.
    Mist de reader zoveel berichten dat de teller meer dan één keer rond is gegaan, dan
    valt de toename te laag uit; dat is niet te zien aan de teller zelf.
    """
    return (nieuw - vorige) % _TELLER_STANDEN
