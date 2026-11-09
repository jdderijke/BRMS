#!/bin/bash
# =============================================================================
# update.sh – BRMS-code bijwerken of terugdraaien (ARCHITECTURE.md §10.1)
# =============================================================================
#
# Als gewone gebruiker (bijv. jandirk), NIET met sudo ervoor:
#
#     ~/BRMS/deploy/update.sh           bijwerken naar de nieuwste 'main' op GitHub
#     ~/BRMS/deploy/update.sh --terug   terug naar de commit van vóór de laatste update
#
# Stappen (§10.1 punt 3):
#   1. de huidige commit onthouden (om terug te kunnen)
#   2. 'main' ophalen en uitchecken
#   3. pakketten installeren volgens requirements.txt (vastgepinde versies)
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
SERVICES="brms-opslag brms-ble-reader"
GEBRUIKER="$(id -un)"

if [ "$EUID" -eq 0 ]; then
    echo "Draai dit script als gewone gebruiker, zonder sudo ervoor." >&2
    exit 1
fi
cd "$BRMS"

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
.venv/bin/pip install -q -r requirements.txt

# --- 4. Configuratie controleren tegen de nieuwe code --------------------------
brms check-config

# Vanaf hier niet meer terugdraaien bij een fout.
trap - ERR

# --- 5. Onthouden, units bijwerken, herstarten -------------------------------
echo "$huidig" > "$VORIGE"
# Units kunnen in een update veranderd zijn: plaatshouders invullen (zie install.sh) en
# systemd ze opnieuw laten lezen.
for unit in deploy/systemd/brms-*.service; do
    sed -e "s|@BRMS@|$BRMS|g" -e "s|@GEBRUIKER@|$GEBRUIKER|g" "$unit" \
        | sudo tee "/etc/systemd/system/$(basename "$unit")" >/dev/null
done
sudo systemctl daemon-reload
sudo systemctl restart $SERVICES
echo "Bijgewerkt en services herstart. Terug kan met: $BRMS/deploy/update.sh --terug"
exit 0
}
