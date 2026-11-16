#!/bin/bash
# =============================================================================
# install.sh – eenmalige inrichting van een BRMS-Pi (ARCHITECTURE.md §10.1)
# =============================================================================
#
# Indeling op de Pi (gelijk aan de pc):
#
#     ~/BRMS/                 git-clone van de repo
#         brms/               Python-code
#         config/brms.toml    configuratie (niet in git)
#         data/brms.db        database (niet in git)
#         deploy/             deze scripts en de systemd-units
#         bin/go2rtc          het programma voor het live-beeld (niet in git)
#         .venv/              Python-omgeving (niet in git)
#
# Gebruik – als gewone gebruiker (bijv. jandirk), NIET met sudo ervoor. Het script vraagt
# zelf om het sudo-wachtwoord voor de stappen die dat nodig hebben.
#
#   Eerste keer – de code staat nog niet op de Pi; haal alleen dit script op van GitHub:
#       curl -fsSLO https://raw.githubusercontent.com/jdderijke/BRMS/main/deploy/install.sh
#       bash install.sh
#
#   Later (opnieuw inrichten):
#       bash ~/BRMS/deploy/install.sh
#
# Wat het doet, in volgorde:
#   1. git installeren als het ontbreekt                                  (sudo)
#   2. de gebruiker toevoegen aan de groep 'bluetooth'                     (sudo)
#   3. de code ophalen naar ~/BRMS (via https; de repo is openbaar), daarna de
#      systeempakketten uit deploy/pakketten.txt installeren (alleen wat ontbreekt) (sudo)
#      en het programma go2rtc ophalen (het live-beeld, deploy/go2rtc.sh)
#   4. een Python-venv maken en de vastgepinde pakketten installeren
#   5. config/brms.toml aanmaken uit het voorbeeld (alleen als hij nog niet bestaat)
#   6. de opdracht 'brms' installeren (/usr/local/bin/brms)                (sudo)
#   7. Mosquitto inrichten volgens §3.1 (alleen lokaal, geen persistence)  (sudo)
#   8. de systemd-services installeren en starten                          (sudo)
#   9. Tailscale installeren en de aanmeldlink tonen (toegang van buiten)  (sudo)
#
# Het script is veilig om opnieuw te draaien: elke stap kijkt eerst of hij al gedaan is.
# =============================================================================

# Stop bij de eerste fout (-e), bij gebruik van een onbekende variabele (-u) en bij een
# fout ergens in een pijplijn (pipefail).
set -euo pipefail

# De repo is openbaar: lezen kan via https, zonder sleutel of account (§10.1 punt 1).
# Bij een eigen fork: zet hier het adres van die fork.
REPO="https://github.com/jdderijke/BRMS.git"
BRMS="$HOME/BRMS"
GEBRUIKER="$(id -un)"

# Niet als root: de bestanden in ~/BRMS moeten van de gebruiker zijn, en de services
# draaien als die gebruiker (§10.1 punt 5).
if [ "$EUID" -eq 0 ]; then
    echo "Draai dit script als gewone gebruiker, zonder sudo ervoor." >&2
    exit 1
fi

# -----------------------------------------------------------------------------
echo "== 1. git"
# De volledige lijst systeempakketten staat in deploy/pakketten.txt, maar die is er pas
# na het clonen (stap 3). Daarvoor is alleen git nodig.
if ! command -v git >/dev/null; then
    sudo apt-get update -q
    sudo apt-get install -y -q git
fi

# -----------------------------------------------------------------------------
echo "== 2. Groep bluetooth"
# Lid van 'bluetooth' geeft via D-Bus toegang tot BlueZ, nodig voor de BLE-reader.
# systemd leest de groepen bij elke start van een service, dus opnieuw inloggen is voor
# de services niet nodig.
sudo usermod -aG bluetooth "$GEBRUIKER"

# -----------------------------------------------------------------------------
echo "== 3. Code in $BRMS"
# Alleen de eerste keer clonen. Latere updates gaan via deploy/update.sh.
if [ ! -d "$BRMS/.git" ]; then
    git clone -q --branch main "$REPO" "$BRMS"
elif [ "$(git -C "$BRMS" remote get-url origin)" != "$REPO" ]; then
    # Een clone van vóór v0.74 haalt de code nog op via SSH met een deploy key. Het adres
    # van 'origin' omzetten naar https, zodat 'brms update' die sleutel niet meer nodig
    # heeft. De oude sleutel en de alias 'github-brms' in ~/.ssh blijven staan; die mogen
    # met de hand weg (ook bij GitHub: Settings > Deploy keys).
    git -C "$BRMS" remote set-url origin "$REPO"
    echo "   'origin' omgezet naar $REPO (de deploy key is niet meer nodig)."
fi

# Nu de code er is: de systeempakketten uit deploy/pakketten.txt (Mosquitto, sqlite3,
# tmux, python3-venv, ...). Alleen wat ontbreekt wordt geïnstalleerd.
echo "== 3b. Systeempakketten"
bash "$BRMS/deploy/pakketten.sh"

# go2rtc geeft het live-beeld van de camera's door (§5.7 punt 18). Het zit niet in apt;
# deploy/go2rtc.sh haalt een vaste versie op en controleert die. Lukt dat niet, dan gaat
# de inrichting door: alleen het live-beeld ontbreekt dan ('brms update' probeert het
# later opnieuw).
echo "== 3c. go2rtc"
bash "$BRMS/deploy/go2rtc.sh" \
    || echo "   LET OP: go2rtc is niet geïnstalleerd; er is geen live-beeld. Later opnieuw: brms update" >&2

# -----------------------------------------------------------------------------
echo "== 4. Python-venv"
# Eigen venv in ~/BRMS/.venv (staat in .gitignore), net als op de pc. Pakketten exact
# volgens requirements.txt (vastgepind, P3).
[ -x "$BRMS/.venv/bin/python" ] || python3 -m venv "$BRMS/.venv"
"$BRMS/.venv/bin/pip" install -q -r "$BRMS/requirements.txt"

# -----------------------------------------------------------------------------
echo "== 5. Configuratie en data"
# Nooit een bestaande configuratie overschrijven; alleen de eerste keer het voorbeeld
# neerzetten. Alleen leesbaar voor de gebruiker (600): later staan hier de
# Victron-sleutels in.
if [ ! -f "$BRMS/config/brms.toml" ]; then
    install -m 600 "$BRMS/config/brms.example.toml" "$BRMS/config/brms.toml"
    echo "   config/brms.toml aangemaakt uit het voorbeeld – pas de sensoren aan."
fi
# data/ staat in git (met een leeg .gitkeep), maar voor de zekerheid:
mkdir -p "$BRMS/data"

# -----------------------------------------------------------------------------
echo "== 6. Opdracht 'brms'"
# /usr/local/bin/brms wordt een verwijzing (symlink) naar deploy/brms in de repo. Een
# update van dat script komt dus vanzelf mee met git.
sudo ln -sfn "$BRMS/deploy/brms" /usr/local/bin/brms

# -----------------------------------------------------------------------------
echo "== 7. Mosquitto"
# De standaardconfiguratie van Debian schrijft persistence en een logbestand naar schijf.
# BRMS vervangt die door een eigen, korte configuratie (§3.1 punt 3). Het origineel
# wordt één keer bewaard als mosquitto.conf.orig.
if [ ! -f /etc/mosquitto/mosquitto.conf.orig ]; then
    sudo cp /etc/mosquitto/mosquitto.conf /etc/mosquitto/mosquitto.conf.orig
fi
sudo install -m 644 "$BRMS/deploy/mosquitto/mosquitto.conf" /etc/mosquitto/mosquitto.conf
sudo systemctl enable -q mosquitto
sudo systemctl restart mosquitto

# -----------------------------------------------------------------------------
echo "== 8. systemd-services"
# De units in deploy/systemd/ bevatten @BRMS@ en @GEBRUIKER@ als plaatshouders; die worden
# hier ingevuld (bijv. /home/jandirk/BRMS en jandirk) en het resultaat gaat naar
# /etc/systemd/system/.
for unit in "$BRMS"/deploy/systemd/brms-*.service; do
    sed -e "s|@BRMS@|$BRMS|g" -e "s|@GEBRUIKER@|$GEBRUIKER|g" "$unit" \
        | sudo tee "/etc/systemd/system/$(basename "$unit")" >/dev/null
done
sudo systemctl daemon-reload
# De polkit-regel voor de herstartknoppen in het web, en het recht om journald te lezen
# voor het logboek (§5.5.5, §5.6 systeempagina).
bash "$BRMS/deploy/rechten.sh"
# Eerst de configuratie controleren; bij een fout stopt het script hier (set -e).
brms check-config
# Alle BRMS-services: de units in deploy/systemd/ (zoals in update.sh), zodat een nieuwe
# service hier niet apart genoemd hoeft te worden.
SERVICES=()
for unit in "$BRMS"/deploy/systemd/brms-*.service; do
    SERVICES+=("$(basename "$unit" .service)")
done
# 'enable' = starten bij het opstarten van de Pi; '--now' = ook nu direct (her)starten.
sudo systemctl enable -q --now "${SERVICES[@]}"
sudo systemctl restart "${SERVICES[@]}"

# -----------------------------------------------------------------------------
echo "== 9. Tailscale"
# Tailscale is de toegang van buiten: de boot zit achter het mobiele netwerk en is anders
# niet te bereiken (§9). Het zit niet in apt van Raspberry Pi OS; Tailscale levert een
# eigen installatiescript, dat zijn pakketbron toevoegt en het pakket installeert (sudo).
if ! command -v tailscale >/dev/null; then
    curl -fsSL https://tailscale.com/install.sh | sh
fi
# Aanmelden kan niet met een wachtwoord in een bestand: het gaat één keer via de browser.
# 'tailscale up' toont een link en wacht tot de Pi daar is goedgekeurd. Is de Pi al
# aangemeld, dan gebeurt er niets.
if ! tailscale status >/dev/null 2>&1; then
    echo "   Meld de Pi aan: open de link hieronder in een browser en keur de Pi goed."
    sudo tailscale up
fi
echo "   Tailscale-adres van deze Pi: $(tailscale ip -4 2>/dev/null || echo 'onbekend')"
echo "   LET OP: zet in https://login.tailscale.com/admin/machines bij deze Pi 'Disable key"
echo "   expiry' aan. Anders valt de Pi na een half jaar van het netwerk, en dat is op de"
echo "   boot niet op afstand te herstellen."

echo
echo "Klaar."
echo "  Status:    systemctl status 'brms-*'"
echo "  Logs:      journalctl -u 'brms-*' -f"
echo "  Live:      mosquitto_sub -v -t 'brms/meting/#'"
echo "  Web:       http://$(hostname):8080  (poort uit [web] in config/brms.toml)"
echo "  Monitor:   brms monitor            (met sensornaam: trace van de hele keten)"
echo "  Database:  sqlite3 -readonly $BRMS/data/brms.db"
