#!/bin/bash
# =============================================================================
# pakketten.sh – ontbrekende systeempakketten uit deploy/pakketten.txt installeren
# =============================================================================
#
# Wordt aangeroepen door install.sh en update.sh (niet bedoeld om los te draaien, maar
# dat mag wel):  bash ~/BRMS/deploy/pakketten.sh
#
# Kijkt per pakket of het al geïnstalleerd is. Alleen als er iets ontbreekt, wordt
# 'sudo apt-get' aangeroepen (en dus om het wachtwoord gevraagd).
# =============================================================================
set -euo pipefail

LIJST="$(dirname "$(readlink -f "$0")")/pakketten.txt"

# Pakketnamen uit de lijst: commentaar (#...) en lege regels weglaten.
mapfile -t pakketten < <(sed -e 's/#.*//' -e 's/[[:space:]]//g' "$LIJST" | grep -v '^$')

# Welke ontbreken er? dpkg-query meldt per geïnstalleerd pakket "install ok installed".
ontbrekend=()
for p in "${pakketten[@]}"; do
    if ! dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q "install ok installed"; then
        ontbrekend+=("$p")
    fi
done

if [ ${#ontbrekend[@]} -eq 0 ]; then
    echo "   systeempakketten: alles aanwezig"
    exit 0
fi

echo "   systeempakketten installeren: ${ontbrekend[*]}"
sudo apt-get update -q
sudo apt-get install -y -q "${ontbrekend[@]}"
