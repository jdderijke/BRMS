#!/bin/bash
# =============================================================================
# go2rtc.sh – het programma go2rtc ophalen (ARCHITECTURE.md §5.7 punt 18, §10.1 punt 6)
# =============================================================================
#
# go2rtc geeft het live-beeld van de camera's door aan de browser. Het is één los
# programma zonder afhankelijkheden, van een ander (github.com/AlexxIT/go2rtc), en het zit
# niet in apt. Dit script zet het in ~/BRMS/bin/go2rtc (staat in .gitignore).
#
# Wordt aangeroepen door install.sh en update.sh; los draaien mag ook:
#     bash ~/BRMS/deploy/go2rtc.sh
#
# Vaste versie: BRMS werkt go2rtc niet vanzelf bij. Een nieuwe versie = hieronder VERSIE en
# de controlegetallen aanpassen, testen op de test-Pi, en dan pas naar de boot.
#
# Het script doet niets als het juiste programma er al staat. Anders haalt het het op bij
# GitHub en controleert het met het controlegetal (SHA-256) hieronder. Klopt dat niet, dan
# wordt er niets geïnstalleerd: zo draait er nooit een ander programma dan het geteste.
# =============================================================================
set -euo pipefail

VERSIE="1.9.15"

# Het controlegetal (SHA-256) van het programma, per soort processor. Berekend van de
# bestanden van deze versie op GitHub (sha256sum go2rtc_linux_arm64).
declare -A SHA256=(
    # 64-bit Raspberry Pi OS (Pi 3B, Pi 4): 'uname -m' geeft aarch64.
    [arm64]="af416f22b21a24ab83b8d78642190b4932ae695b4e386853f11d1b0319781763"
)

BRMS="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
DOEL="$BRMS/bin/go2rtc"

case "$(uname -m)" in
    aarch64) SOORT="arm64" ;;
    *)
        echo "   go2rtc: geen programma voor deze processor ($(uname -m)); geen live-beeld." >&2
        exit 1 ;;
esac
VERWACHT="${SHA256[$SOORT]}"

# Het controlegetal van een bestand (alleen het getal, zonder de bestandsnaam).
controlegetal() { sha256sum "$1" | cut -d' ' -f1; }

# Staat het juiste programma er al? Dan is er niets te doen.
if [ -x "$DOEL" ] && [ "$(controlegetal "$DOEL")" = "$VERWACHT" ]; then
    echo "   go2rtc $VERSIE: aanwezig"
    exit 0
fi

echo "   go2rtc $VERSIE ophalen ..."
mkdir -p "$BRMS/bin"
# Eerst onder een tijdelijke naam: pas na de controle krijgt het de echte naam.
TIJDELIJK="$DOEL.nieuw"
trap 'rm -f "$TIJDELIJK"' EXIT
# -f: een foutpagina van de server is een fout, geen programma. -L: GitHub verwijst door.
curl -fsSL --max-time 300 -o "$TIJDELIJK" \
    "https://github.com/AlexxIT/go2rtc/releases/download/v$VERSIE/go2rtc_linux_$SOORT"

GEKREGEN="$(controlegetal "$TIJDELIJK")"
if [ "$GEKREGEN" != "$VERWACHT" ]; then
    echo "   go2rtc: het controlegetal klopt niet; niet geïnstalleerd." >&2
    echo "     verwacht: $VERWACHT" >&2
    echo "     gekregen: $GEKREGEN" >&2
    exit 1
fi
chmod 755 "$TIJDELIJK"
mv -f "$TIJDELIJK" "$DOEL"
echo "   go2rtc $VERSIE geïnstalleerd in $DOEL"
