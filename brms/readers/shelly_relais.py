"""Decoder en schakelopdracht voor een Shelly-relais (ARCHITECTURE.md §5.2.6).

Het sensortype `shelly-relais` is een Shelly-schakelaar met de Gen2-API en één uitgang
`switch:0`; het model is de Shelly Plug M Gen3, een tussenstekker met energiemeting. Hij
schakelt de kachel bij vorst.

Zuivere functies: het antwoord van de Shelly (JSON, al omgezet naar een dict) erin,
waarden eruit; en het adres voor een schakelopdracht. Geen netwerk, zodat alles te testen
is zonder plug (tests/test_shelly_relais.py). Het pollen en het schakelen zelf staan in
readers/shelly.py.

Dezelfde aanvraag als bij de Shelly Plus Uni (readers/shelly_uni.py),

    http://<adres>/rpc/Shelly.GetStatus

geeft de toestand van alle onderdelen. Gecontroleerd met de plug zelf (§5.2.6,
"Bevindingen met de plug"):

    {
      "switch:0": {"id": 0, "source": "HTTP_in", "output": false,   stand van het relais
                   "apower": 0.0,                                   vermogen in watt
                   "voltage": 221.3,                                netspanning in volt
                   "current": 0.000,                                stroom in ampère
                   "aenergy": {...}, "temperature": {...}, ...},
      "wifi":     {"sta_ip": "...", "rssi": -58, ...},
      ...
    }

De energieteller (aenergy), de netfrequentie en de temperatuur in de plug worden niet
opgeslagen (besluit OPEN-27).

Schakelen (§5.2.6 punt 3, §5.8 punt 5):

    http://<adres>/rpc/Switch.Set?id=0&on=true&toggle_after=120     aan, na 120 s vanzelf uit
    http://<adres>/rpc/Switch.Set?id=0&on=false                     uit

`toggle_after` is de terugschakeltijd die de Shelly zelf bewaakt: na die tijd keert hij de
stand om, ook als BRMS intussen is vastgelopen. Elk relais heeft een veilige stand (het
veld `veilige_stand` in brms.toml: de kachel uit, de walstroom aan). De andere, actieve
stand gaat nooit zonder terugschakeltijd; de veilige stand gaat altijd zonder.
"""

from brms import grootheden
from brms.readers.shelly_uni import ShellyFout, Veld

# De grootheden die dit sensortype levert (§5.2.6 punt 2 en 5, §5.4.2 punt 3). De reader
# voegt de signaalsterkte van de wifi (rssi) toe. 'walstroom' wordt niet gemeten maar
# afgeleid van de spanning (zie walstroom() hieronder).
LEVERT = ("relais", "vermogen", "spanning", "stroom", "walstroom")

# De grootheden van dit type die te bedienen zijn, met de vorm van de bediening (§5.4.2
# punt 8): alleen de stand van het relais, als aan/uit. Of BRMS het relais van een
# bepaalde plug ook echt mag bedienen, staat in brms.toml (controls = ["relais"]).
BEDIENBAAR = {"relais": grootheden.Bediening(grootheden.AANUIT)}

# De plug zit op de walstroom: staat er netspanning op, dan is er walstroom (§5.2.6 punt
# 5). Boven deze spanning (volt) geldt walstroom = 1, anders 0.
WALSTROOM_DREMPEL = 180.0

# De component van de Shelly waarin de uitgang zit, en zijn nummer in een opdracht.
COMPONENT = "switch:0"
UITGANG = 0

# Per grootheid het veld in de component, en de eenheid voor de trace.
_GETALLEN = (("vermogen", "apower", "W"), ("spanning", "voltage", "V"), ("stroom", "current", "A"))


def decodeer_velden(status: dict) -> list[Veld]:
    """Zet het antwoord van Shelly.GetStatus om in velden, één per grootheid.

    Ontbreekt de uitgang in het antwoord, dan is het geen relais (verkeerd adres of een
    ander apparaat): ShellyFout. Een los veld dat ontbreekt of geen bruikbare waarde
    heeft, krijgt waarde None en wordt niet gepubliceerd (§5.2.3 punt 4).
    """
    if not isinstance(status, dict):
        raise ShellyFout("het antwoord is geen JSON-object")
    deel = status.get(COMPONENT)
    if not isinstance(deel, dict):
        raise ShellyFout(f"geen '{COMPONENT}' in het antwoord; is dit wel een Shelly-relais?")

    velden = []
    # De stand van het relais: true = aan.
    ruw = deel.get("output")
    if isinstance(ruw, bool):
        velden.append(Veld("relais", f"{COMPONENT}.output", ruw, 1.0 if ruw else 0.0, "true = 1"))
    else:
        velden.append(Veld("relais", f"{COMPONENT}.output", ruw, None, "geen toestand (true/false)"))
    # De metingen. (bool is in Python een getal; true/false is hier geen meetwaarde.)
    for grootheid, veld, eenheid in _GETALLEN:
        ruw = deel.get(veld)
        if isinstance(ruw, bool) or not isinstance(ruw, (int, float)):
            velden.append(Veld(grootheid, f"{COMPONENT}.{veld}", ruw, None, "geen waarde gemeten"))
        else:
            velden.append(Veld(grootheid, f"{COMPONENT}.{veld}", ruw, float(ruw), eenheid))
    return velden


def walstroom(spanning: float) -> tuple[float, str]:
    """De afgeleide grootheid 'walstroom' van een plug (§5.2.6 punt 5): 1 als de
    (gecorrigeerde) netspanning boven de drempel ligt, anders 0. Geeft (waarde, uitleg
    voor de trace).

    Alleen voor de plug, niet als algemene afleiding uit 'spanning' (§5.2.5): de
    accumonitor levert ook 'spanning', en dat is geen netspanning.

    Zonder walstroom heeft de plug geen voeding en antwoordt hij niet; er komt dan geen 0
    maar helemaal geen waarde. Een 0 komt alleen voor bij een te lage netspanning.
    """
    aan = spanning > WALSTROOM_DREMPEL
    return (1.0 if aan else 0.0,
            f"spanning {spanning:g} V {'>' if aan else '<='} {WALSTROOM_DREMPEL:g} V")


def zet_url(adres: str, aan: bool, duur: float | None = None, veilig_aan: bool = False) -> str:
    """Het adres van de schakelopdracht.

    aan         True = relais aan, False = uit
    duur        de terugschakeltijd in seconden: na die tijd keert de Shelly de stand zelf
                weer om
    veilig_aan  de veilige stand van dit relais (§5.8 punt 2): False = uit (de kachel, de
                standaard), True = aan (een relais dat de walstroom schakelt)

    De veilige stand gaat altijd zonder terugschakeltijd: daar moet het relais blijven
    staan. De andere, actieve stand kan alleen mét (§5.8 punt 5): een kachel die aan
    blijft staan, of walstroom die uit blijft, als BRMS wegvalt, is een risico.
    ValueError bij de actieve stand zonder duur.
    """
    basis = f"http://{adres}/rpc/Switch.Set?id={UITGANG}&on={'true' if aan else 'false'}"
    if aan == veilig_aan:
        return basis
    if duur is None or duur <= 0:
        raise ValueError(f"'{'aan' if aan else 'uit'}' is hier de actieve stand en kan alleen "
                         f"met een terugschakeltijd (duur) groter dan 0")
    return f"{basis}&toggle_after={duur:g}"
