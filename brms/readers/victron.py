"""Decoder voor Victron "Instant Readout" (ARCHITECTURE.md §5.2.2, §5.2.3).

Victron-apparaten met Bluetooth (ingebouwd, of via een VE.Direct / VE.Bus Smart dongle)
zenden hun belangrijkste gegevens ongeveer elke seconde uit in een advertentie, in de
"manufacturer data" onder fabrikant-ID 0x02E1. De gegevens zijn versleuteld (AES); elk
apparaat heeft een eigen sleutel, te vinden in VictronConnect:

    apparaat openen -> tandwiel -> Productinfo -> "Instant readout via Bluetooth" aan
    -> "Versleutelingsgegevens tonen": MAC-adres en sleutel (32 tekens)

Die sleutel staat per sensor in config/brms.toml (`sleutel = "..."`).

Het ontsleutelen en uitpakken doet de bibliotheek `victron-ble`: versleuteling en veel
recordtypes zelf schrijven is meer risico dan nut (§5.2.3). BRMS gebruikt de bibliotheek
alleen als decoder; scannen doet de BLE-reader zelf (§5.2.2 punt 1). Dit module kiest uit
wat de bibliotheek teruggeeft de grootheden van BRMS (§5.3.3).

Sensortypen in de configuratie, en wat ze leveren:

    victron-bmv        accumonitor (BMV-700 met dongle, SmartShunt)
                       spanning, stroom, soc, verbruikt, resterende_tijd
    victron-mppt       zonnelader (BlueSolar met dongle, SmartSolar)
                       zonnevermogen, laadstroom_zon
    victron-multiplus  MultiPlus met VE.Bus Smart dongle
                       walstroom (1 = spanning op de AC-ingang)

Het type staat ook in de advertentie zelf. Komt dat niet overeen met het type uit de
configuratie (verkeerd MAC-adres bij een apparaat gezet), dan is dat een VictronFout.

Net als de andere decoders zijn dit zuivere functies: bytes en sleutel in, waarden uit
(tests/test_victron.py).
"""

from dataclasses import dataclass

from victron_ble.devices import BatteryMonitor, SolarCharger, VEBus, detect_device_type
from victron_ble.devices.base import ACInState
from victron_ble.exceptions import AdvertisementKeyMismatchError

# Fabrikant-ID van Victron Energy; de sleutel in de manufacturer data van bleak.
FABRIKANT_ID = 0x02E1


class VictronFout(ValueError):
    """De advertentie is niet te ontsleutelen of past niet bij het sensortype."""


def _walstroom(staat) -> float | None:
    """AC-ingang van de MultiPlus -> 0/1. Onbekend (None) wordt niet gepubliceerd."""
    if staat in (ACInState.AC_IN_1, ACInState.AC_IN_2):
        return 1.0
    if staat == ACInState.NOT_CONNECTED:
        return 0.0
    return None


# Per sensortype: de klasse van de bibliotheek, en de grootheden van BRMS met
#   (grootheid, veld in de uitkomst van de bibliotheek, omrekening, uitleg voor de trace)
# De bibliotheek levert de waarden al in volt, ampère, procent enz.; een veld dat het
# apparaat als "onbekend" uitzendt, komt als None en wordt niet gepubliceerd (§5.2.3 punt 4).
_TYPEN = {
    "victron-bmv": (BatteryMonitor, (
        ("spanning", "voltage", float, "V"),
        ("stroom", "current", float, "A, negatief = ontladen"),
        ("soc", "soc", float, "%"),
        # De bibliotheek geeft het verbruik als negatief getal (-50 = 50 Ah verbruikt);
        # de grootheid `verbruikt` is positief.
        ("verbruikt", "consumed_ah", abs, "Ah, verbruikt sinds vol"),
        # Tijdens laden zendt de BMV "oneindig"; dan is er geen waarde (§5.3.3).
        ("resterende_tijd", "remaining_mins", float, "min"),
    )),
    "victron-mppt": (SolarCharger, (
        ("zonnevermogen", "solar_power", float, "W"),
        ("laadstroom_zon", "battery_charging_current", float, "A"),
    )),
    "victron-multiplus": (VEBus, (
        # Zie §5.2.2 punt 4: de veldnaam ac_in_state wordt met de echte MultiPlus bevestigd.
        ("walstroom", "ac_in_state", _walstroom, "AC-ingang: 1 = aanwezig"),
    )),
}

# De sensortypen van dit module, en per type de grootheden die het kan leveren
# (ARCHITECTURE.md §5.4.2 punt 3). Gebruikt door `brms check-config` en `brms grootheden`.
LEVERT: dict[str, tuple[str, ...]] = {
    type_: tuple(g for g, *_ in velden) for type_, (_, velden) in _TYPEN.items()}


@dataclass(frozen=True)
class Veld:
    """Eén gedecodeerd veld, met de tussenstappen (voor trace, §10.2)."""
    grootheid: str
    bron: str             # naam van het veld in de bibliotheek, bijv. "voltage"
    ruw: object           # wat de bibliotheek teruggaf (getal, enum of None)
    uitleg: str
    waarde: float | None  # None = het apparaat meldt "onbekend"


def is_instant_readout(data: bytes) -> bool:
    """True als deze manufacturer data een Instant Readout-bericht is.

    Een Victron-apparaat zendt ook andere advertenties uit (bijv. voor VictronConnect);
    die beginnen niet met 0x10 en worden overgeslagen.
    """
    return len(data) > 8 and data[0] == 0x10


def decodeer(data: bytes, sleutel: str, sensortype: str) -> dict[str, float]:
    """Ontsleutelt en decodeert een Instant Readout-bericht naar {grootheid: waarde}.

    data        manufacturer data onder FABRIKANT_ID, zoals bleak die aanlevert
    sleutel     de versleutelingssleutel van dit apparaat (32 hexadecimale tekens)
    sensortype  het type uit de configuratie: victron-bmv, victron-mppt, victron-multiplus

    Velden die het apparaat als "onbekend" uitzendt, ontbreken in het resultaat.
    VictronFout bij een verkeerde sleutel, een ander soort apparaat dan het sensortype, of
    een bericht dat niet uit te pakken is.
    """
    return {v.grootheid: v.waarde for v in decodeer_velden(data, sleutel, sensortype)[1]
            if v.waarde is not None}


def decodeer_velden(data: bytes, sleutel: str, sensortype: str) -> tuple[bytes, list[Veld]]:
    """Als decodeer(), maar met de tussenstappen (voor trace): de ontsleutelde bytes en per
    veld wat de bibliotheek teruggaf. Onbekende velden staan er ook in, met waarde None."""
    klasse, velden = _TYPEN[sensortype]
    data = bytes(data)

    # Het soort apparaat staat onversleuteld in het bericht.
    gevonden = detect_device_type(data)
    if gevonden is not klasse:
        naam = gevonden.__name__ if gevonden else "onbekend soort apparaat"
        raise VictronFout(f"bericht is van een {naam}, niet van een {klasse.__name__} "
                          f"(type {sensortype} in de configuratie)")
    try:
        apparaat = klasse(sleutel)
        ontsleuteld = apparaat.decrypt(data)
        uitkomst = apparaat.parse_decrypted(ontsleuteld)
    except AdvertisementKeyMismatchError:
        raise VictronFout("de sleutel hoort niet bij dit apparaat (controleer 'sleutel' "
                          "in de configuratie met VictronConnect)") from None
    except Exception as e:
        # Een afgebroken bericht geeft in de bibliotheek een IndexError of ValueError; wat
        # het ook is, één kapot bericht mag de reader niet laten stoppen.
        raise VictronFout(f"bericht niet te decoderen: {type(e).__name__}: {e}") from None

    resultaat = []
    for grootheid, bron, reken, uitleg in velden:
        ruw = uitkomst.get(bron)
        resultaat.append(Veld(grootheid, bron, ruw, uitleg,
                              None if ruw is None else reken(ruw)))
    return ontsleuteld, resultaat
