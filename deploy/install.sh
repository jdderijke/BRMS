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
#         .venv/              Python-omgeving (niet in git)
#
# Gebruik – als gewone gebruiker (bijv. jandirk), NIET met sudo ervoor. Het script vraagt
# zelf om het sudo-wachtwoord voor de stappen die dat nodig hebben.
#
#   Eerste keer – de code staat nog niet op de Pi; kopieer alleen dit script, vanaf de pc:
#       scp deploy/install.sh brms:
#   en dan op de Pi:
#       bash install.sh
#
#   Later (opnieuw inrichten):
#       bash ~/BRMS/deploy/install.sh
#
# Wat het doet, in volgorde:
#   1. git installeren als het ontbreekt                                  (sudo)
#   2. de gebruiker toevoegen aan de groep 'bluetooth'                     (sudo)
#   3. de deploy key klaarzetten waarmee de Pi de code van GitHub mag LEZEN
#   4. de code ophalen naar ~/BRMS, daarna de systeempakketten uit
#      deploy/pakketten.txt installeren (alleen wat ontbreekt)              (sudo)
#   5. een Python-venv maken en de vastgepinde pakketten installeren
#   6. config/brms.toml aanmaken uit het voorbeeld (alleen als hij nog niet bestaat)
#   7. de opdracht 'brms' installeren (/usr/local/bin/brms)                (sudo)
#   8. Mosquitto inrichten volgens §3.1 (alleen lokaal, geen persistence)  (sudo)
#   9. de systemd-services installeren en starten                          (sudo)
#
# Het script is veilig om opnieuw te draaien: elke stap kijkt eerst of hij al gedaan is.
#
# Deploy key: staat er nog geen sleutel in ~/.ssh, of heeft GitHub hem nog niet, dan toont
# het script de publieke sleutel en stopt. Voeg die toe bij de repo (Settings > Deploy
# keys, zonder schrijfrechten) en draai het script opnieuw.
# =============================================================================

# Stop bij de eerste fout (-e), bij gebruik van een onbekende variabele (-u) en bij een
# fout ergens in een pijplijn (pipefail).
set -euo pipefail

# Repo via de SSH-alias 'github-brms' (zie stap 3), zodat git de deploy key gebruikt.
REPO="git@github-brms:jdderijke/BRMS.git"
BRMS="$HOME/BRMS"
SLEUTEL="$HOME/.ssh/brms_deploy_ed25519"
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
# na het clonen (stap 4). Daarvoor is alleen git nodig.
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
echo "== 3. Deploy key"
# Een deploy key is een SSH-sleutel die alleen deze ene repo mag lezen. Bij diefstal van
# de Pi is alleen de BRMS-code leesbaar, niet de rest van het GitHub-account (§10.1).
install -d -m 700 "$HOME/.ssh"
if [ ! -f "$SLEUTEL" ]; then
    # ed25519-sleutel zonder wachtwoord (-N ""), want updates moeten onbeheerd kunnen.
    ssh-keygen -q -t ed25519 -N "" -C "brms-deploy@$(hostname)" -f "$SLEUTEL"
fi
# SSH-alias: 'github-brms' = github.com met precies deze sleutel. Zo raakt de deploy key
# niet verward met andere sleutels van de gebruiker.
if ! grep -q "^Host github-brms$" "$HOME/.ssh/config" 2>/dev/null; then
    cat >> "$HOME/.ssh/config" <<EOF

Host github-brms
    HostName github.com
    User git
    IdentityFile $SLEUTEL
    IdentitiesOnly yes
EOF
    chmod 600 "$HOME/.ssh/config"
fi
# De host-sleutel van GitHub vastleggen, zodat git niet interactief om bevestiging vraagt.
if ! grep -q "^github.com " "$HOME/.ssh/known_hosts" 2>/dev/null; then
    ssh-keyscan -t ed25519 github.com >> "$HOME/.ssh/known_hosts" 2>/dev/null
fi
# Proberen de repo te lezen. Lukt dat niet, dan is de deploy key nog niet toegevoegd.
if ! git ls-remote "$REPO" >/dev/null 2>&1; then
    echo
    echo "De Pi heeft nog geen toegang tot de repo. Voeg deze sleutel toe als deploy key"
    echo "via GitHub: repo BRMS > Settings > Deploy keys > Add deploy key"
    echo "('Allow write access' UIT laten):"
    echo
    cat "$SLEUTEL.pub"
    echo
    echo "Draai daarna dit script opnieuw."
    exit 2
fi

# -----------------------------------------------------------------------------
echo "== 4. Code in $BRMS"
# Alleen de eerste keer clonen. Latere updates gaan via deploy/update.sh.
if [ ! -d "$BRMS/.git" ]; then
    git clone -q --branch main "$REPO" "$BRMS"
fi

# Nu de code er is: de systeempakketten uit deploy/pakketten.txt (Mosquitto, sqlite3,
# tmux, python3-venv, ...). Alleen wat ontbreekt wordt geïnstalleerd.
echo "== 4b. Systeempakketten"
bash "$BRMS/deploy/pakketten.sh"

# -----------------------------------------------------------------------------
echo "== 5. Python-venv"
# Eigen venv in ~/BRMS/.venv (staat in .gitignore), net als op de pc. Pakketten exact
# volgens requirements.txt (vastgepind, P3).
[ -x "$BRMS/.venv/bin/python" ] || python3 -m venv "$BRMS/.venv"
"$BRMS/.venv/bin/pip" install -q -r "$BRMS/requirements.txt"

# -----------------------------------------------------------------------------
echo "== 6. Configuratie en data"
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
echo "== 7. Opdracht 'brms'"
# /usr/local/bin/brms wordt een verwijzing (symlink) naar deploy/brms in de repo. Een
# update van dat script komt dus vanzelf mee met git.
sudo ln -sfn "$BRMS/deploy/brms" /usr/local/bin/brms

# -----------------------------------------------------------------------------
echo "== 8. Mosquitto"
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
echo "== 9. systemd-services"
# De units in deploy/systemd/ bevatten @BRMS@ en @GEBRUIKER@ als plaatshouders; die worden
# hier ingevuld (bijv. /home/jandirk/BRMS en jandirk) en het resultaat gaat naar
# /etc/systemd/system/.
for unit in "$BRMS"/deploy/systemd/brms-*.service; do
    sed -e "s|@BRMS@|$BRMS|g" -e "s|@GEBRUIKER@|$GEBRUIKER|g" "$unit" \
        | sudo tee "/etc/systemd/system/$(basename "$unit")" >/dev/null
done
sudo systemctl daemon-reload
# Eerst de configuratie controleren; bij een fout stopt het script hier (set -e).
brms check-config
# 'enable' = starten bij het opstarten van de Pi; '--now' = ook nu direct (her)starten.
sudo systemctl enable -q --now brms-opslag brms-ble-reader brms-shelly-reader brms-web
sudo systemctl restart brms-opslag brms-ble-reader brms-shelly-reader brms-web

echo
echo "Klaar."
echo "  Status:    systemctl status 'brms-*'"
echo "  Logs:      journalctl -u 'brms-*' -f"
echo "  Live:      mosquitto_sub -v -t 'brms/meting/#'"
echo "  Web:       http://$(hostname):8080  (poort uit [web] in config/brms.toml)"
echo "  Monitor:   brms monitor            (met sensornaam: trace van de hele keten)"
echo "  Database:  sqlite3 -readonly $BRMS/data/brms.db"
