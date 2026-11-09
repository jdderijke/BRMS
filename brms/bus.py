"""Busmodule: al het MQTT-verkeer van BRMS loopt hierdoor (ARCHITECTURE.md §3.1–3.3).

Achtergrond:
- Alle services praten met elkaar via een lokale Mosquitto-broker (§3.1). Readers
  publiceren metingen; alarmlaag en opslag abonneren zich.
- paho-mqtt (versie 2.1.0, zoals in JSEM, §3.2) werkt met een eigen netwerkthread en
  callbacks, niet met asyncio. Die thread blijft binnen dit module: asyncio-services
  krijgen ontvangen berichten via een asyncio.Queue (zie `wachtrij`).
- paho verbindt op de achtergrond en probeert het bij uitval steeds opnieuw
  (1 s, oplopend tot 30 s). Een service hoeft daar zelf niets voor te doen.
- Is de broker onbereikbaar, dan worden metingen **weggegooid**, niet gebufferd
  (§3.1 punt 5). Na herstel gaat het verder met de dan ontvangen waarden.
"""

import asyncio
import logging
import threading

import paho.mqtt.client as mqtt

from brms import grootheden
from brms.meting import Meting

log = logging.getLogger(__name__)

# Maximale lengte van een ontvangstwachtrij. Normaal is die vrijwel leeg; alleen als een
# service vastloopt groeit hij, en dan gooien we liever berichten weg dan geheugen op te maken.
WACHTRIJ_MAX = 10000


class Bus:
    def __init__(self, naam: str, host: str = "localhost", port: int = 1883):
        """naam: korte naam van de service (bijv. 'opslag'), gebruikt als client-ID."""
        self._host, self._port = host, port
        # Vaste client-ID per service: in de broker-log is direct te zien wie er verbindt.
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"brms-{naam}")
        # Wachttijd tussen pogingen om opnieuw te verbinden.
        self._client.reconnect_delay_set(min_delay=1, max_delay=30)
        self._client.on_connect = self._bij_verbinden
        self._client.on_disconnect = self._bij_verbreken
        self._client.on_message = self._bij_bericht
        # Abonnementen worden bewaard, zodat ze na elke (her)verbinding opnieuw worden
        # aangemeld: de broker draait zonder persistence en onthoudt ze niet.
        self._abonnementen: list[tuple[str, int]] = []
        self._callbacks: list[tuple[str, callable]] = []
        # De lijsten worden gelezen in paho's thread en gevuld vanuit de service.
        self._lock = threading.Lock()
        # Teller van weggegooide metingen, om niet bij elke meting een logregel te schrijven.
        self._weggegooid = 0

    # --- levenscyclus -------------------------------------------------------------
    def start(self) -> None:
        """Start de netwerkthread. Verbinden (en opnieuw verbinden) gaat op de achtergrond;
        start() wacht daar niet op en faalt dus ook niet als de broker er (nog) niet is."""
        self._client.connect_async(self._host, self._port, keepalive=30)
        self._client.loop_start()

    def stop(self) -> None:
        self._client.disconnect()
        self._client.loop_stop()

    @property
    def verbonden(self) -> bool:
        return self._client.is_connected()

    # --- publiceren ---------------------------------------------------------------
    def publiceer_meting(self, m: Meting) -> bool:
        """Publiceert een meting op brms/meting/<sensor_id>/<grootheid> (§3.3).

        - QoS 1 voor toestanden (een verandering mag niet verloren gaan), QoS 0 voor
          doorlopende grootheden en toenames (de volgende komt zo).
        - Nooit retain: een bewaarde oude meting kan de alarmlaag na een herstart misleiden.
        - Geeft False terug als de meting is weggegooid (broker niet bereikbaar).
        """
        if not self.verbonden:
            # Alleen de eerste keer loggen; daarna tellen.
            if self._weggegooid == 0:
                log.warning("broker niet bereikbaar: metingen worden weggegooid")
            self._weggegooid += 1
            return False
        if self._weggegooid:
            log.info("broker weer bereikbaar; %d metingen weggegooid", self._weggegooid)
            self._weggegooid = 0
        qos = 1 if m.soort == grootheden.TOESTAND else 0
        info = self._client.publish(m.topic(), m.naar_json(), qos=qos, retain=False)
        return info.rc == mqtt.MQTT_ERR_SUCCESS

    def publiceer(self, topic: str, payload: str, qos: int = 0) -> bool:
        """Publiceert een willekeurig bericht (trace, opdracht). Nooit retain (§3.3).

        Geeft False terug als de broker niet bereikbaar is; het bericht is dan weggegooid.
        """
        if not self.verbonden:
            return False
        info = self._client.publish(topic, payload, qos=qos, retain=False)
        return info.rc == mqtt.MQTT_ERR_SUCCESS

    # --- abonneren ----------------------------------------------------------------
    def abonneer(self, topic: str, qos: int, callback) -> None:
        """Abonneert op een topic (wildcards toegestaan, bijv. 'brms/meting/#').

        callback(topic, payload) wordt aangeroepen in de netwerkthread van paho; gebruik
        in asyncio-services daarom `wachtrij`.
        """
        with self._lock:
            self._abonnementen.append((topic, qos))
            self._callbacks.append((topic, callback))
        if self.verbonden:
            self._client.subscribe(topic, qos)
        # Niet verbonden: _bij_verbinden meldt het abonnement aan zodra de verbinding er is.

    def wachtrij(self, topic: str, qos: int) -> asyncio.Queue:
        """Abonneert en levert berichten als (topic, payload) in een asyncio.Queue.

        Moet worden aangeroepen vanuit de draaiende asyncio-loop. paho's thread zet berichten
        thread-veilig in de wachtrij (call_soon_threadsafe). Is de wachtrij vol, dan wordt
        het bericht weggegooid.
        """
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue(maxsize=WACHTRIJ_MAX)

        def zet(item):
            # Draait in de asyncio-loop (niet in paho's thread).
            try:
                q.put_nowait(item)
            except asyncio.QueueFull:
                log.warning("wachtrij vol; bericht op %s weggegooid", item[0])

        self.abonneer(topic, qos, lambda t, p: loop.call_soon_threadsafe(zet, (t, p)))
        return q

    # --- paho-callbacks (draaien in paho's netwerkthread) ---------------------------
    def _bij_verbinden(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.warning("verbinden met broker mislukt: %s", reason_code)
            return
        log.info("verbonden met broker %s:%d", self._host, self._port)
        # Alle abonnementen (opnieuw) aanmelden.
        with self._lock:
            abonnementen = list(self._abonnementen)
        for topic, qos in abonnementen:
            client.subscribe(topic, qos)

    def _bij_verbreken(self, client, userdata, flags, reason_code, properties):
        # paho probeert zelf opnieuw te verbinden; hier alleen loggen.
        log.warning("verbinding met broker verbroken: %s", reason_code)

    def _bij_bericht(self, client, userdata, msg):
        # Bericht doorgeven aan elke callback waarvan het abonnement op de topic past.
        with self._lock:
            callbacks = list(self._callbacks)
        for topic, cb in callbacks:
            if mqtt.topic_matches_sub(topic, msg.topic):
                try:
                    cb(msg.topic, msg.payload)
                except Exception:
                    # Een fout in één callback mag paho's thread niet laten stoppen.
                    log.exception("fout bij verwerken van bericht op %s", msg.topic)
