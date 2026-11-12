#!/bin/bash
# =============================================================================
# update.sh – BRMS-code bijwerken of terugdraaien (ARCHITECTURE.md §10.1)
# =============================================================================
#
# Als gewone gebruiker (bijv. jandirk), NIET met sudo ervoor:
#
#     brms update           bijwerken naar de nieuwste 'main' op GitHub
#     brms update --terug   terug naar de commit van vóór de laatste update
#
# 'brms update' voert dit script uit; rechtstreeks kan ook: ~/BRMS/deploy/update.sh [--terug]
#
# Stappen (§10.1 punt 3):
#   1. de huidige commit onthouden (om terug te kunnen)
#   2. 'main' ophalen en uitchecken
#   3. ontbrekende systeempakketten uit deploy/pakketten.txt installeren (apt, alleen
#      als er iets ontbreekt), Python-pakketten volgens requirements.txt (pip), en het
#      programma go2rtc voor het live-beeld (deploy/go2rtc.sh)
#   4. 'brms check-config' – bij een fout stopt de update en draait de oude versie door
#   5. systemd-units bijwerken en de services herstarten (vraagt het sudo-wachtwoord)
#
# Bij een fout in stap 2–4 wordt de code teruggezet naar de oude commit. De services zijn
# dan niet herstart en draaien gewoon de oude versie door.
#
# config/brms.toml, data/ en .venv/ staan in .gitignore: een update raakt ze niet.
# =============================================================================

set -euo pipefail

# Dit script staat zelf in de repo en kan dus door 'git checkout' (hieronder) veranderen
# terwijl het draait. Bash leest een script regel voor regel; een wijziging halverwege
# kan dan rare fouten geven. Daarom staat alles tussen { en }: bash leest het hele blok
# in voordat het iets uitvoert.
{

# De projectmap is de map boven deploy/, waar dit script staat.
BRMS="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
# Hier staat de commit van vóór de laatste geslaagde update (voor --terug).
VORIGE="$BRMS/data/vorige-commit"
GEBRUIKER="$(id -un)"

if [ "$EUID" -eq 0 ]; then
    echo "Draai dit script als gewone gebruiker, zonder sudo ervoor." >&2
    exit 1
fi
cd "$BRMS"

# Alle BRMS-services: de units in deploy/systemd/ (bijv. brms-opslag). De lijst wordt
# gelezen op het moment van aanroepen, dus ná het uitchecken zit een service die met de
# update nieuw is er vanzelf bij.
services() {
    for unit in deploy/systemd/brms-*.service; do
        basename "$unit" .service
    done
}

# Zet de units uit deploy/systemd/ in systemd en zorgt dat elke service bij het opstarten
# van de Pi start ('enable'). Mag altijd opnieuw; vraagt het sudo-wachtwoord.
installeer_units() {
    # De units bevatten plaatshouders (zie install.sh); die worden hier ingevuld.
    for unit in deploy/systemd/brms-*.service; do
        sed -e "s|@BRMS@|$BRMS|g" -e "s|@GEBRUIKER@|$GEBRUIKER|g" "$unit" \
            | sudo tee "/etc/systemd/system/$(basename "$unit")" >/dev/null
    done
    sudo systemctl daemon-reload
    sudo systemctl enable -q $(services)
    # De polkit-regel voor de herstartknoppen in het web, en het recht om journald te
    # lezen voor het logboek (deploy/rechten.sh). Doet niets als het al in orde is.
    bash deploy/rechten.sh
}

# Haalt het programma go2rtc op als het ontbreekt of de versie niet klopt (deploy/go2rtc.sh).
# Lukt dat niet (geen verbinding met GitHub, verkeerd controlegetal), dan gaat de update
# gewoon door: alleen het live-beeld ontbreekt dan, de rest van BRMS werkt. Een volgende
# 'brms update' probeert het opnieuw.
haal_go2rtc() {
    bash deploy/go2rtc.sh || echo "LET OP: go2rtc is niet geïnstalleerd; er is geen live-beeld. Probeer later opnieuw: brms update" >&2
}

# --- 1. Huidige commit en doel bepalen ---------------------------------------
huidig=$(git rev-parse HEAD)
if [ "${1:-}" = "--terug" ]; then
    [ -s "$VORIGE" ] || { echo "Geen vorige commit bekend." >&2; exit 1; }
    doel=$(cat "$VORIGE")
else
    # Nieuwste 'main' van GitHub ophalen (nog niet uitchecken).
    git fetch -q origin main
    doel=$(git rev-parse origin/main)
fi

if [ "$doel" = "$huidig" ]; then
    echo "Al op $(git log -1 --format='%h %s' "$huidig")."
    # Toch controleren of er systeempakketten ontbreken (bijv. na een handmatige wijziging).
    bash deploy/pakketten.sh
    haal_go2rtc
    # En of elke service aanstaat en draait. Dat is niet zo als een service nieuw is en de
    # update nog door een oudere versie van dit script is uitgevoerd, die hem niet kende.
    # Dan volstaat nog een keer 'brms update'; een los systemctl-commando is niet nodig.
    ontbreekt=""
    for s in $(services); do
        if ! systemctl -q is-enabled "$s" 2>/dev/null || ! systemctl -q is-active "$s"; then
            ontbreekt="$ontbreekt $s"
        fi
    done
    if [ -n "$ontbreekt" ]; then
        echo "Niet actief:$ontbreekt – wordt aangezet en gestart."
        brms check-config
        installeer_units
        sudo systemctl start $ontbreekt
        echo "Gestart:$ontbreekt"
    else
        # Ook zonder nieuwe code: de rechten voor het web controleren (zie rechten.sh).
        bash deploy/rechten.sh
    fi
    exit 0
fi

# Bij een fout in de stappen hieronder wordt deze functie aangeroepen (trap ERR).
terug() {
    echo "FOUT – code terug naar $(git log -1 --format='%h' "$huidig"); services niet herstart." >&2
    git checkout -q -B main "$huidig"
    .venv/bin/pip install -q -r requirements.txt || true
    exit 1
}
trap terug ERR

# --- 2. Uitchecken -----------------------------------------------------------
echo "$(git log -1 --format='%h %s' "$huidig")"
echo "  ->  $(git log -1 --format='%h %s' "$doel")"
# -B: lokale branch 'main' wijst daarna naar het doel (ook bij teruggaan).
git checkout -q -B main "$doel"

# --- 3. Pakketten ------------------------------------------------------------
# Systeempakketten (apt) uit de nieuwe deploy/pakketten.txt; vraagt alleen om het
# sudo-wachtwoord als er iets ontbreekt.
bash deploy/pakketten.sh
# Python-pakketten in de venv, exact volgens requirements.txt.
.venv/bin/pip install -q -r requirements.txt
# go2rtc (het live-beeld, §5.7 punt 18): ophalen als het ontbreekt of de versie niet klopt.
haal_go2rtc

# --- 4. Configuratie controleren tegen de nieuwe code --------------------------
brms check-config

# Vanaf hier niet meer terugdraaien bij een fout.
trap - ERR

# --- 5. Onthouden, units bijwerken, herstarten -------------------------------
echo "$huidig" > "$VORIGE"
# Units kunnen in een update veranderd of nieuw zijn: opnieuw in systemd zetten en
# aanzetten. Daarna alle services herstarten, ook een service die nieuw is.
installeer_units
sudo systemctl restart $(services)
echo "Bijgewerkt en services herstart. Terug kan met: brms update --terug"
exit 0
}
