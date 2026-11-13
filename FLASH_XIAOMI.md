# Xiaomi Monitor 3 flashen met pvvx-firmware

Korte handleiding om een Xiaomi Monitor 3 (MJWSD05MMC) geschikt te maken voor BRMS.
De fabrieksfirmware zendt versleuteld in Xiaomi's eigen formaat. De pvvx-firmware zendt
openbaar en onversleuteld **BTHome v2** uit; daar gaat de decoder van uit
(ARCHITECTURE.md §5.2.3).

Flashen gaat draadloos; de sensor hoeft niet open. Reken op ongeveer vijf minuten per sensor.

## Nodig

- Windows-pc of laptop met Bluetooth en **Chrome** (of Edge). Firefox en iPhone werken niet.
- Verse batterij in de sensor.
- De flasher-pagina: <https://pvvx.github.io/ATC_MiThermometer/TelinkMiFlasher.html>

Doe de sensoren **één voor één** en leg de sensor naast de laptop.
Koppel de sensor niet via de Bluetooth-instellingen van Windows; dat doet de pagina.

## Stap 1 – Sensor in koppelmodus zetten

Zonder deze stap maakt de sensor wel verbinding, maar blijft de pagina hangen op
"Activating now, please wait...".

1. Houd **beide knoppen tegelijk** ingedrukt tot het scherm knippert; laat los.
2. Druk **kort op de bovenste knop**, daarna **kort op de onderste knop**.
3. Druk nogmaals op bovenste knop
4. Het bluetooth-icoontje **knippert** nu: de sensor staat in koppelmodus.
   (Een icoontje dat stil aan staat betekent alleen "verbonden".)

## Stap 2 – Verbinden

1. Open de flasher-pagina (of ververs hem met F5).
2. Vul bij **BLE device name prefix filter(s)** in: `BLE`
   Dan staat alleen de sensor in de lijst, niet alle bluetooth-apparaten uit de buurt.
3. Klik **Connect**, kies de BLE sensor en klik **Koppelen**.
   Staat er "Gekoppeld" achter de naam, dan kent Chrome hem van een eerdere poging;
   je moet hem toch aanklikken en koppelen.
4. De statusregel bovenaan meldt dat de sensor verbonden is.

## Stap 3 – Activeren en flashen

1. Klik **Do Activation**. Binnen enkele seconden zijn *Mi Token* en *Mi Bind Key* gevuld.
2. Klik op de aangeboden **Custom Firmware**. De pagina kiest zelf het bestand dat bij de
   sensor past; kies niet zelf een ander bestand.
3. Klik **Start Flashing** en laat de sensor ongeveer twee minuten liggen.
4. De sensor herstart en de pagina meldt "Disconnected". Dat hoort zo.

## Stap 4 – Instellen

De sensor heet nu `BTE_xxxxxx` (Engelse uitvoering) of `BTH_xxxxxx`. De zes tekens zijn de
laatste helft van het MAC-adres.

1. Zet het filter op `BTE` (of `BTH`, of maak het leeg), klik **Connect**, kies de sensor
   en klik **Koppelen**.
2. Zet **Advertising type** op **BTHome v2**. Laat encryptie (bind key) uit.
3. Klik **Send Config**.
4. Klik **Set Time** om de klok op het scherm gelijk te zetten.
5. Klik **Get MAC**. In het veld *Device MAC [+ 2 Rand]* staan 16 tekens; alleen de
   **eerste 12** zijn het MAC-adres. Voorbeeld: `A4C13812ABCD5F3E` → `A4:C1:38:12:AB:CD`.
6. Noteer het MAC-adres met de plek aan boord en plak een sticker op de sensor.

Niet aankomen:

- **!Set MAC!** en **!Clean MAC!** – die wijzigen het adres van de sensor.
- De naam van de sensor. BRMS herkent de sensor aan het MAC-adres; de plek aan boord staat
  in de configuratie van BRMS.

## Als het niet lukt

| Wat je ziet | Oorzaak en oplossing |
|---|---|
| `TypeError: Cannot read properties of null (reading 'substr')`, status "disconnected" | Er is een apparaat zonder naam gekozen. Vul het filter `MJWSD05` in en probeer opnieuw. Er is niets geflasht. |
| Blijft staan op "Activating now, please wait..." | De sensor stond niet in koppelmodus. Klik **Disconnect**, doe stap 1 opnieuw en verbind daarna. |
| Sensor staat niet in de lijst | Dichter bij de laptop leggen; batterij eruit en erin; controleren of Bluetooth in Windows aan staat. |
| Geen verbinding met de geflashte sensor | Reset met beide knoppen (stap 1.1) en opnieuw verbinden. |
