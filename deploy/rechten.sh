#!/bin/bash
# =============================================================================
# rechten.sh – wat de BRMS-services buiten hun eigen map mogen (ARCHITECTURE.md §5.5.5,
# §5.6 systeempagina)
# =============================================================================
#
# Aangeroepen door deploy/install.sh en deploy/update.sh, als gewone gebruiker (de
# gebruiker waaronder de services draaien). Mag altijd opnieuw; vraagt alleen om het
# sudo-wachtwoord als er iets te veranderen is.
#
#   1. De polkit-regel voor de knoppen "Herstart BRMS" en "Herstart de Pi": de gebruiker
#      mag de eenheden brms-* herstarten en de Pi opnieuw opstarten (zie
#      deploy/polkit/50-brms.rules voor de uitleg).
#   2. Het logboek: de gebruiker moet journald mogen lezen. Dat mag een lid van de groep
#      'adm' of 'systemd-journal'; op Raspberry Pi OS is de eerste gebruiker al lid van
#      'adm'. Zo niet, dan wordt hij aan 'systemd-journal' toegevoegd.
#
# Lukt een stap niet, dan gaat de installatie of update gewoon door: alleen de knoppen of
# het logboek werken dan niet, de rest van BRMS wel.
# =============================================================================

set -u

BRMS="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
GEBRUIKER="$(id -un)"
SJABLOON="$BRMS/deploy/polkit/50-brms.rules"
DOEL="/etc/polkit-1/rules.d/50-brms.rules"

# --- 1. polkit-regel ---------------------------------------------------------
if [ ! -d /etc/polkit-1/rules.d ] && ! sudo test -d /etc/polkit-1/rules.d; then
    echo "LET OP: polkit is er niet (/etc/polkit-1/rules.d ontbreekt); de herstartknoppen in het web werken niet." >&2
else
    # De regel met de gebruiker ingevuld. Alleen schrijven als hij ontbreekt of anders is:
    # de map is alleen voor root leesbaar, dus vergelijken gaat ook via sudo.
    nieuw="$(sed "s|@GEBRUIKER@|$GEBRUIKER|g" "$SJABLOON")"
    if ! echo "$nieuw" | sudo cmp -s - "$DOEL" 2>/dev/null; then
        if echo "$nieuw" | sudo tee "$DOEL" >/dev/null && sudo chmod 644 "$DOEL"; then
            echo "polkit-regel geplaatst: $GEBRUIKER mag de BRMS-services en de Pi herstarten (voor het web)."
        else
            echo "LET OP: de polkit-regel kon niet geplaatst worden; de herstartknoppen in het web werken niet." >&2
        fi
    fi
fi

# --- 2. journald lezen ---------------------------------------------------------
if ! id -nG "$GEBRUIKER" | tr ' ' '\n' | grep -qx -e adm -e systemd-journal; then
    if sudo usermod -aG systemd-journal "$GEBRUIKER"; then
        echo "$GEBRUIKER toegevoegd aan de groep systemd-journal (voor het logboek in het web)."
    else
        echo "LET OP: $GEBRUIKER mag journald niet lezen; het logboek in het web blijft leeg." >&2
    fi
fi

exit 0
