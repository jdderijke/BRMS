"""Decoder voor BTHome v2 (ARCHITECTURE.md §5.2.3).

BTHome is een open formaat waarin sensoren hun metingen in een Bluetooth-advertentie
uitzenden. De Xiaomi Monitor 3 met pvvx-firmware gebruikt BTHome v2. De gegevens staan in
de "service data" van de advertentie, onder UUID 0xFCD2.

Opbouw van de service data (zie https://bthome.io/format/):

    byte 0      apparaatinformatie
                  bit 0     = 1 als het bericht versleuteld is
                  bit 2     = 1 als het apparaat alleen bij een gebeurtenis zendt
                  bit 5..7  = BTHome-versie (moet 2 zijn)
    byte 1..    een reeks objecten, elk:
                  1 byte object-ID  (bijv. 0x02 = temperatuur)
                  n bytes waarde    (little-endian; n hangt af van het object-ID)

Voorbeeld van een pvvx-bericht:

    40 | 00 a4 | 01 64 | 02 ca 09 | 03 bf 13 | 0c 02 0c
    ^    ^       ^       ^          ^          ^
    |    |       |       |          |          batterijspanning 0x0c02 = 3074 x 0,001 = 3,074 V
    |    |       |       |          vocht      0x13bf = 5055 x 0,01 = 50,55 %
    |    |       |       temperatuur 0x09ca = 2506 x 0,01 = 25,06 °C
    |    |       batterij-percentage (wordt door BRMS niet gebruikt, zie §5.3.3)
    |    pakket-ID (volgnummer; niet gebruikt)
    v2, onversleuteld

Dit is een zuivere functie: bytes in, waarden uit. Daardoor is hij te testen zonder
Bluetooth en zonder Pi (tests/test_bthome.py).
"""

from dataclasses import dataclass

# Volledige 128-bit schrijfwijze van UUID 0xFCD2 zoals bleak de service data aanlevert.
FCD2_UUID = "0000fcd2-0000-1000-8000-00805f9b34fb"


class BTHomeFout(ValueError):
    """Het bericht is geen geldig, onversleuteld BTHome v2-bericht."""


# Object-ID -> (lengte van de waarde in bytes, met teken ja/nee).
#
# Een BTHome-bericht bevat geen lengte per object: wie een object wil overslaan, moet de
# lengte ervan kennen. Daarom staat hier de lengte van álle object-ID's uit de
# BTHome-specificatie, ook van objecten die BRMS niet gebruikt. Lengte None betekent
# variabel: dan geeft de eerste byte na het object-ID de lengte (tekst en ruwe data).
_LENGTE: dict[int, tuple[int | None, bool]] = {
    0x00: (1, False), 0x01: (1, False), 0x02: (2, True), 0x03: (2, False),
    0x04: (3, False), 0x05: (3, False), 0x06: (2, False), 0x07: (2, False),
    0x08: (2, True), 0x09: (1, False), 0x0A: (3, False), 0x0B: (3, False),
    0x0C: (2, False), 0x0D: (2, False), 0x0E: (2, False), 0x0F: (1, False),
    0x10: (1, False), 0x11: (1, False), 0x12: (2, False), 0x13: (2, False),
    0x14: (2, False),
    **{i: (1, False) for i in range(0x15, 0x2E)},  # binaire sensoren (aan/uit), 1 byte
    0x2E: (1, False), 0x2F: (1, False),
    0x3A: (1, False), 0x3C: (2, False), 0x3D: (2, False), 0x3E: (4, False),
    0x3F: (2, True), 0x40: (2, False), 0x41: (2, False), 0x42: (3, False),
    0x43: (2, False), 0x44: (2, False), 0x45: (2, True), 0x46: (1, False),
    0x47: (2, False), 0x48: (2, False), 0x49: (2, False), 0x4A: (2, False),
    0x4B: (3, False), 0x4C: (4, False), 0x4D: (4, False), 0x4E: (4, False),
    0x4F: (4, False), 0x50: (4, False), 0x51: (2, False), 0x52: (2, False),
    0x53: (None, False), 0x54: (None, False), 0x55: (4, False), 0x56: (2, False),
    0x57: (1, True), 0x58: (1, True), 0x59: (1, True), 0x5A: (2, True),
    0x5B: (4, True), 0x5C: (4, True), 0x5D: (2, True), 0x5E: (2, False),
    0x5F: (2, False), 0x60: (1, False), 0x61: (2, False),
    0xF0: (2, False), 0xF1: (4, False), 0xF2: (3, False),  # apparaat-type en firmware
}

# De objecten die BRMS wél gebruikt: object-ID -> (grootheid, factor, decimalen).
#
# De ruwe waarde is een geheel getal; vermenigvuldigd met de factor geeft dat de waarde in
# de eenheid van de grootheid (§5.3.3). Er wordt afgerond op het aantal decimalen van de
# factor, zodat 2506 x 0,01 netjes 25.06 wordt en niet 25.060000000000002.
# Sommige grootheden kunnen in twee resoluties binnenkomen (bijv. temperatuur in 0,01 of
# in 0,1 °C); beide komen uit op dezelfde grootheid.
_GROOTHEID: dict[int, tuple[str, float, int]] = {
    0x02: ("temperatuur", 0.01, 2),
    0x45: ("temperatuur", 0.1, 1),
    0x03: ("vocht", 0.01, 2),
    0x2E: ("vocht", 1, 0),
    0x0C: ("batterijspanning", 0.001, 3),
}

# De grootheden die deze decoder kan leveren (ARCHITECTURE.md §5.4.2 punt 3), afgeleid uit
# de tabel hierboven. Gebruikt door `brms check-config` en `brms grootheden bthome`.
# Een nieuwe grootheid toevoegen = een regel in _GROOTHEID hierboven (en in grootheden.py).
LEVERT = tuple(dict.fromkeys(naam for naam, _, _ in _GROOTHEID.values()))


@dataclass(frozen=True)
class Object:
    """Eén gedecodeerd object, met de tussenstappen (voor trace, §10.2)."""
    grootheid: str
    object_id: int
    ruw: bytes      # de bytes van de waarde zoals ze in het bericht staan (little-endian)
    getal: int      # die bytes als geheel getal
    factor: float   # schaalfactor uit de BTHome-specificatie
    waarde: float   # getal x factor, afgerond


def decodeer(data: bytes) -> dict[str, float]:
    """Decodeert BTHome v2 service data naar {grootheid: waarde}.

    - Objecten die BRMS niet gebruikt, worden overgeslagen.
    - Bij een object-ID dat niet in de specificatie staat, is de lengte onbekend en kan
      de rest van het bericht niet gelezen worden. De waarden die tot dat punt gelezen zijn,
      zijn wel geldig en worden teruggegeven.
    - BTHomeFout bij een leeg, versleuteld, verkeerde-versie of afgebroken bericht.
    """
    return {o.grootheid: o.waarde for o in decodeer_objecten(data)}


def decodeer_objecten(data: bytes) -> list[Object]:
    """Als decodeer(), maar met de tussenstappen per object (voor trace)."""
    if not data:
        raise BTHomeFout("leeg bericht")

    # Byte 0: apparaatinformatie.
    info = data[0]
    if info & 0x01:
        # Versleuteling gebruikt BRMS niet (pvvx kan het, maar staat uit).
        raise BTHomeFout("versleuteld bericht")
    versie = info >> 5
    if versie != 2:
        raise BTHomeFout(f"BTHome-versie {versie} wordt niet ondersteund")

    objecten: list[Object] = []
    i = 1  # positie in het bericht; begint na de apparaatinformatie
    while i < len(data):
        # Object-ID lezen.
        obj = data[i]
        i += 1
        if obj not in _LENGTE:
            break  # onbekend object: lengte onbekend, verder lezen kan niet

        # Lengte van de waarde bepalen (vast, of variabel via een lengtebyte).
        lengte, signed = _LENGTE[obj]
        if lengte is None:
            if i >= len(data):
                raise BTHomeFout("bericht afgebroken")
            lengte = data[i]
            i += 1
        if i + lengte > len(data):
            raise BTHomeFout("bericht afgebroken")

        # Ruwe bytes van de waarde, en door naar het volgende object.
        ruw = data[i:i + lengte]
        i += lengte

        # Alleen objecten die BRMS gebruikt worden omgerekend.
        if obj in _GROOTHEID:
            naam, factor, decimalen = _GROOTHEID[obj]
            getal = int.from_bytes(ruw, "little", signed=signed)
            objecten.append(Object(naam, obj, bytes(ruw), getal, factor,
                                   round(getal * factor, decimalen)))
    return objecten
