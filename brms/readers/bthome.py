"""Decoder voor BTHome v2 (ARCHITECTURE.md §5.2.3).

Zuivere functie: service-data (UUID 0xFCD2) in, grootheden uit. Formaat volgens
https://bthome.io/format/ – één byte apparaatinformatie, daarna objecten
(object-ID + waarde, little-endian). Alleen onversleutelde v2-berichten.
"""

FCD2_UUID = "0000fcd2-0000-1000-8000-00805f9b34fb"


class BTHomeFout(ValueError):
    """Bericht is geen geldig, onversleuteld BTHome v2-bericht."""


# Object-ID -> (lengte in bytes, signed). Nodig om objecten over te slaan die BRMS niet
# gebruikt. Lengte None = variabel (eerste byte is de lengte).
_LENGTE: dict[int, tuple[int | None, bool]] = {
    0x00: (1, False), 0x01: (1, False), 0x02: (2, True), 0x03: (2, False),
    0x04: (3, False), 0x05: (3, False), 0x06: (2, False), 0x07: (2, False),
    0x08: (2, True), 0x09: (1, False), 0x0A: (3, False), 0x0B: (3, False),
    0x0C: (2, False), 0x0D: (2, False), 0x0E: (2, False), 0x0F: (1, False),
    0x10: (1, False), 0x11: (1, False), 0x12: (2, False), 0x13: (2, False),
    0x14: (2, False),
    **{i: (1, False) for i in range(0x15, 0x2E)},  # binaire sensoren
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
    0xF0: (2, False), 0xF1: (4, False), 0xF2: (3, False),
}

# Object-ID -> (grootheid, factor, decimalen) voor de objecten die BRMS gebruikt.
_GROOTHEID: dict[int, tuple[str, float, int]] = {
    0x02: ("temperatuur", 0.01, 2),
    0x45: ("temperatuur", 0.1, 1),
    0x03: ("vocht", 0.01, 2),
    0x2E: ("vocht", 1, 0),
    0x0C: ("batterijspanning", 0.001, 3),
}


def decodeer(data: bytes) -> dict[str, float]:
    """Decodeert BTHome v2 service-data naar {grootheid: waarde}.

    Objecten die BRMS niet gebruikt worden overgeslagen. Bij een onbekend object-ID is
    de lengte onbekend; de waarden tot dat punt worden teruggegeven.
    """
    if not data:
        raise BTHomeFout("leeg bericht")
    info = data[0]
    if info & 0x01:
        raise BTHomeFout("versleuteld bericht")
    versie = info >> 5
    if versie != 2:
        raise BTHomeFout(f"BTHome-versie {versie} wordt niet ondersteund")

    waarden: dict[str, float] = {}
    i = 1
    while i < len(data):
        obj = data[i]
        i += 1
        if obj not in _LENGTE:
            break
        lengte, signed = _LENGTE[obj]
        if lengte is None:
            if i >= len(data):
                raise BTHomeFout("bericht afgebroken")
            lengte = data[i]
            i += 1
        if i + lengte > len(data):
            raise BTHomeFout("bericht afgebroken")
        ruw = data[i:i + lengte]
        i += lengte
        if obj in _GROOTHEID:
            naam, factor, decimalen = _GROOTHEID[obj]
            getal = int.from_bytes(ruw, "little", signed=signed)
            waarden[naam] = round(getal * factor, decimalen)
    return waarden
