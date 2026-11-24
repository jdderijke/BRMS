/* =============================================================================
   Handbediening in het web van BRMS (ARCHITECTURE.md §5.6 "Relais in het web", §5.8).

   Eén bestand voor de twee plekken waar een grootheid te bedienen is: het venster met de
   grootheden van een sensor op de systeempagina, en een tekening op de pagina Overzicht
   (de tekst `sensor.grootheid.RW`). Zo ziet de bediening er op beide plekken hetzelfde
   uit en werkt ze hetzelfde.

   Twee vormen, vastgelegd door de decoder van het sensortype (§5.4.2 punt 8):

     schuif   een grootheid met twee standen (het relais): een kleine schuifknop met links
              AUTO en rechts de actieve stand van het relais (AAN bij de kachel, UIT bij
              een relais met veilige stand aan), en ernaast een stip voor de werkelijke
              stand (groen = aan, grijs = uit, leeg = onbekend);
     invoer   een grootheid met een in te voeren waarde (bijv. een thermostaat): een
              invoerveldje met de eenheid en een knop. Zo'n sensortype is er nog niet; het
              web is erop voorbereid.

   Het web schakelt niet zelf. Een klik stuurt de keuze naar de webservice (/api/schakel),
   die hem als opdracht over de bus aan de schakellaag geeft. De knop toont de nieuwe
   keuze pas als de schakellaag hem meldt (§5.6): tot dan staat hij op de oude keuze, en
   is hij even uitgeschakeld. Meldt de schakellaag niets (de service draait niet), dan is
   de knop uitgeschakeld.

   Gebruik:  const b = Bediening.schuif(sensor, "relais", opties);
             ergens.append(b.el);  en bij elke verversing:  b.toon(schakelstand, waarde);
   ============================================================================= */
const Bediening = (() => {
  // Stuurt een keuze of een waarde naar de webservice. Geeft een fout (Error) met de
  // tekst van de webservice als het niet lukt, bijvoorbeeld "de MQTT-broker is niet
  // bereikbaar".
  async function stuur(inhoud) {
    const a = await fetch("/api/schakel", {method: "POST",
      headers: {"Content-Type": "application/json"}, body: JSON.stringify(inhoud)});
    if (!a.ok) {
      const d = await a.json().catch(() => ({}));
      throw new Error(d.fout || "fout " + a.status);
    }
  }

  function maak(soort, klasse, tekst) {
    const e = document.createElement(soort);
    if (klasse) e.className = klasse;
    if (tekst !== undefined) e.textContent = tekst;
    return e;
  }

  // De schuifknop van een relais.
  //   sensor     {id, naam, actieve_stand}: de sensor uit de configuratie; actieve_stand
  //              is "aan" of "uit" (de stand die niet de veilige is, §5.8 punt 2)
  //   grootheid  de bedienbare grootheid ("relais")
  //   opties     {naVerzoek: aanroepen na een geslaagde opdracht (om snel te verversen),
  //               bijFout:   aanroepen met een tekst als de opdracht niet verstuurd is}
  // Geeft {el, toon}: het element, en de functie die de stand erin zet.
  function schuif(sensor, grootheid, opties) {
    opties = opties || {};
    const el = maak("span", "bediening");
    const links = maak("span", "", "AUTO");
    const rechts = maak("span", "", sensor.actieve_stand.toUpperCase());
    const knop = maak("label", "schakelaar");
    const vink = maak("input");
    vink.type = "checkbox";
    vink.disabled = true;          // tot de eerste stand van de schakellaag er is
    vink.setAttribute("aria-label", sensor.naam + ": auto of met de hand " + sensor.actieve_stand);
    knop.append(vink, maak("i"));
    const stip = maak("i", "standstip");
    el.append(links, knop, rechts, stip);

    vink.addEventListener("change", async () => {
      // Naar rechts = met de hand in de actieve stand; naar links = AUTO.
      const keuze = vink.checked ? sensor.actieve_stand : "auto";
      // De knop toont de nieuwe keuze pas als de schakellaag hem meldt (§5.6): dus eerst
      // terug naar de oude keuze, en uitgeschakeld tot de volgende verversing.
      vink.checked = !vink.checked;
      vink.disabled = true;
      el.classList.add("wacht");
      try {
        await stuur({sensor: sensor.id, grootheid, keuze});
        if (opties.naVerzoek) opties.naVerzoek();
      } catch (e) {
        if (opties.bijFout) opties.bijFout(sensor.naam + " is niet geschakeld: " + e.message);
      }
    });

    // Zet de stand in de knop.
    //   schakel    wat de schakellaag over dit relais meldt: {keuze, stand, reden}, of
    //              null/undefined als hij niets meldt
    //   werkelijk  de laatste meting van het relais (1 = aan, 0 = uit), of null als de
    //              stand onbekend is (het relais is stil)
    function toon(schakel, werkelijk) {
      el.classList.remove("wacht");
      const metHand = !!schakel && schakel.keuze !== "auto";
      vink.disabled = !schakel;
      vink.checked = metHand;
      links.classList.toggle("gekozen", !!schakel && !metHand);
      rechts.classList.toggle("gekozen", metHand);
      // De stip: de werkelijke stand, los van de keuze. In AUTO kan het relais door een
      // alarmregel in zijn actieve stand staan; dat is hier te zien.
      const bekend = werkelijk === 0 || werkelijk === 1;
      stip.className = "standstip" + (bekend ? (werkelijk ? " aan" : " uit") : "");
      const nu = "Werkelijke stand: " + (bekend ? (werkelijk ? "aan" : "uit") : "onbekend");
      el.title = !schakel
        ? sensor.naam + ": de schakellaag meldt niets; de bediening is uitgeschakeld. " + nu
        : sensor.naam + ": " + (metHand ? "met de hand " + schakel.keuze : "auto")
          + ". De schakellaag wil: " + schakel.stand + " (" + schakel.reden + "). " + nu;
    }

    return {el, toon};
  }

  // Het invoerveldje van een grootheid met een in te voeren waarde.
  //   bediening  {laagste, hoogste, stap}: de grenzen van de decoder
  //   eenheid    de eenheid achter het veldje, bijv. "°C"
  // Geeft {el, toon}, zoals schuif.
  function invoer(sensor, grootheid, bediening, eenheid, opties) {
    opties = opties || {};
    const el = maak("span", "bediening invoer");
    const veld = maak("input");
    veld.type = "number";
    veld.min = bediening.laagste;
    veld.max = bediening.hoogste;
    veld.step = bediening.stap || "any";
    veld.setAttribute("aria-label", sensor.naam + " " + grootheid + ": nieuwe waarde");
    const knop = maak("button", "", "Zet");
    knop.type = "button";
    el.append(veld, maak("span", "", eenheid), knop);
    el.title = sensor.naam + " " + grootheid + ": tussen " + bediening.laagste + " en "
               + bediening.hoogste + " " + eenheid;
    // Heeft de gebruiker iets getypt dat nog niet verstuurd is? Dan blijft het staan bij
    // een verversing.
    let getypt = false;
    veld.addEventListener("input", () => { getypt = true; });

    async function zet() {
      const waarde = Number(veld.value);
      // De controle van de invoer (§5.6): een getal binnen de grenzen van de decoder. De
      // webservice controleert het nog eens.
      if (veld.value.trim() === "" || isNaN(waarde) || waarde < bediening.laagste || waarde > bediening.hoogste) {
        if (opties.bijFout) opties.bijFout(sensor.naam + " " + grootheid + ": geef een waarde tussen "
                                           + bediening.laagste + " en " + bediening.hoogste + " " + eenheid);
        return;
      }
      knop.disabled = true;
      try {
        await stuur({sensor: sensor.id, grootheid, waarde});
        getypt = false;
        if (opties.naVerzoek) opties.naVerzoek();
      } catch (e) {
        if (opties.bijFout) opties.bijFout(sensor.naam + " " + grootheid + " is niet ingesteld: " + e.message);
      } finally {
        knop.disabled = false;
      }
    }
    knop.addEventListener("click", zet);
    veld.addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); zet(); } });

    // Zet de laatst bekende waarde in het veldje; het veldje toont de nieuwe waarde dus
    // pas als de sensor haar meldt. Niet terwijl de gebruiker erin typt.
    function toon(schakel, waarde) {
      if (getypt || document.activeElement === veld) return;
      veld.value = waarde === null || waarde === undefined ? "" : waarde;
    }

    return {el, toon};
  }

  return {schuif, invoer};
})();
