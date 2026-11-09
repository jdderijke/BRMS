#!/bin/bash
# =============================================================================
# install.sh – eenmalige inrichting van een BRMS-Pi (ARCHITECTURE.md §10.1)
# =============================================================================
#
# Gebruik (op de Pi, als gewone gebruiker met sudo-rechten):
#
#   Eerste keer – de code staat nog niet op de Pi; kopieer alleen dit script, vanaf de pc:
#       scp deploy/install.sh brms:
#   en dan op de Pi:
#       sudo bash install.sh
#
#   Later (opnieuw inrichten):
#       sudo bash /opt/brms/deploy/install.sh
#
# Wat het doet, in volgorde:
#   1. systeempakketten installeren (Mosquitto, git, python3-venv)
#   2. systeemgebruiker 'brms' aanmaken; de services draaien onder die gebruiker
#   3. mappen aanmaken: /etc/brms (configuratie) en /var/lib/brms (database)
#   4. een deploy key maken waarmee de Pi de code van GitHub mag LEZEN
#   5. de code ophalen naar /opt/brms
#   6. een Python-venv maken en de vastgepinde pakketten installeren
#   7. de opdracht 'brms' installeren (/usr/local/bin/brms)
#   8. /etc/brms/brms.toml aanmaken uit het voorbeeld (alleen als hij nog niet bestaat)
#   9. Mosquitto inrichten volgens §3.1 (alleen lokaal, geen persistence)
#  10. de systemd-services installeren en starten
#
# Het script is veilig om opnieuw te draaien: elke stap kijkt eerst of hij al gedaan is.
#
# Eerste keer: bij stap 4 heeft GitHub de deploy key nog niet. Het script toont dan de
# publieke sleutel en stopt. Voeg die toe bij de repo (Settings > Deploy keys, zonder
# schrijfrechten) en draai het script opnieuw.
# =============================================================================

# Stop bij de eerste fout (-e), bij gebruik van een onbekende variabele (-u) en bij een
# fout ergens in een pijplijn (pipefail).
set -euo pipefail

# Repo via de SSH-alias 'github-brms' (zie stap 4), zodat git de deploy key gebruikt.
REPO="git@github-brms:jdderijke/BRMS.git"
BRMS=/opt/brms
SLEUTEL=/root/.ssh/brms_deploy_ed25519

# Het script moet als root draaien (pakketten, gebruikers, systemd).
[ "$EUID" -eq 0 ] || { echo "Draai dit script met sudo." >&2; exit 1; }

# -----------------------------------------------------------------------------
echo "== 1. Systeempakketten"
# Mosquitto: de lokale MQTT-broker (§3.1). git: code ophalen (§10.1). python3-venv:
# eigen Python-omgeving, los van de systeem-Python (§5.1).
apt-get update -q
apt-get install -y -q mosquitto git python3-venv

# -----------------------------------------------------------------------------
echo "== 2. Systeemgebruiker brms"
# Een systeemgebruiker zonder wachtwoord en zonder login-shell: de services krijgen alleen
# de rechten die ze nodig hebben (§10.1 punt 5), niet die van 'jandirk'.
if ! id brms >/dev/null 2>&1; then
    useradd --system --home-dir /var/lib/brms --no-create-home --shell /usr/sbin/nologin brms
fi
# Groep 'bluetooth' geeft via D-Bus toegang tot BlueZ, nodig voor de BLE-reader.
usermod -aG bluetooth brms

# -----------------------------------------------------------------------------
echo "== 3. Mappen"
# /etc/brms: configuratie. Eigenaar root, groep brms mag lezen (later staan hier de
# Victron-sleutels in, dus niet leesbaar voor iedereen).
install -d -o root -g brms -m 750 /etc/brms
# /var/lib/brms: database (en later opnames). Alleen brms mag hier schrijven.
install -d -o brms -g brms -m 750 /var/lib/brms

# -----------------------------------------------------------------------------
echo "== 4. Deploy key"
# Een deploy key is een SSH-sleutel die alleen deze ene repo mag lezen. Bij diefstal van
# de Pi is alleen de BRMS-code leesbaar, niet de rest van het GitHub-account (§10.1).
install -d -m 700 /root/.ssh
if [ ! -f "$SLEUTEL" ]; then
    # ed25519-sleutel zonder wachtwoord (-N ""), want updates moeten onbeheerd kunnen.
    ssh-keygen -q -t ed25519 -N "" -C "brms-deploy@$(hostname)" -f "$SLEUTEL"
fi
# SSH-alias: 'github-brms' = github.com met precies deze sleutel. Zo raakt de deploy key
# niet verward met andere sleutels die root eventueel heeft.
if ! grep -q "^Host github-brms$" /root/.ssh/config 2>/dev/null; then
    cat >> /root/.ssh/config <<EOF
Host github-brms
    HostName github.com
    User git
    IdentityFile $SLEUTEL
    IdentitiesOnly yes
EOF
    chmod 600 /root/.ssh/config
fi
# De host-sleutel van GitHub vastleggen, zodat git niet interactief om bevestiging vraagt.
if ! grep -q "^github.com " /root/.ssh/known_hosts 2>/dev/null; then
    ssh-keyscan -t ed25519 github.com >> /root/.ssh/known_hosts 2>/dev/null
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
echo "== 5. Code in $BRMS"
# Alleen de eerste keer clonen. Latere updates gaan via deploy/update.sh.
if [ ! -d "$BRMS/.git" ]; then
    git clone -q --branch main "$REPO" "$BRMS"
fi

# -----------------------------------------------------------------------------
echo "== 6. Python-venv"
# Eigen venv in /opt/brms/venv (staat in .gitignore). Pakketten exact volgens
# requirements.txt (vastgepind, P3).
[ -x "$BRMS/venv/bin/python" ] || python3 -m venv "$BRMS/venv"
"$BRMS/venv/bin/pip" install -q -r "$BRMS/requirements.txt"

# -----------------------------------------------------------------------------
echo "== 7. Opdracht 'brms'"
# Kort commando voor o.a. 'brms check-config' (§5.4.1).
install -m 755 "$BRMS/deploy/brms" /usr/local/bin/brms

# -----------------------------------------------------------------------------
echo "== 8. Configuratie"
# Nooit een bestaande configuratie overschrijven; alleen de eerste keer het voorbeeld
# neerzetten.
if [ ! -f /etc/brms/brms.toml ]; then
    install -o root -g brms -m 640 "$BRMS/config/brms.example.toml" /etc/brms/brms.toml
    echo "   /etc/brms/brms.toml aangemaakt uit het voorbeeld – pas de sensoren aan."
fi

# -----------------------------------------------------------------------------
echo "== 9. Mosquitto"
# De standaardconfiguratie van Debian schrijft persistence en een logbestand naar schijf.
# BRMS vervangt die door een eigen, korte configuratie (§3.1 punt 3). Het origineel
# wordt één keer bewaard als mosquitto.conf.orig.
if [ ! -f /etc/mosquitto/mosquitto.conf.orig ]; then
    cp /etc/mosquitto/mosquitto.conf /etc/mosquitto/mosquitto.conf.orig
fi
install -m 644 "$BRMS/deploy/mosquitto/mosquitto.conf" /etc/mosquitto/mosquitto.conf
systemctl enable -q mosquitto
systemctl restart mosquitto

# -----------------------------------------------------------------------------
echo "== 10. systemd-services"
install -m 644 "$BRMS"/deploy/systemd/brms-*.service /etc/systemd/system/
systemctl daemon-reload
# Eerst de configuratie controleren; bij een fout stopt het script hier (set -e).
brms check-config
# 'enable' = starten bij het opstarten van de Pi; '--now' = ook nu direct starten.
systemctl enable -q --now brms-opslag brms-ble-reader

echo
echo "Klaar."
echo "  Status:  systemctl status 'brms-*'"
echo "  Logs:    journalctl -u 'brms-*' -f"
echo "  Metingen live volgen: mosquitto_sub -v -t 'brms/meting/#'"
