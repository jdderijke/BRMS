"""Busmodule: al het MQTT-verkeer van BRMS loopt hierdoor (ARCHITECTURE.md §3.1–3.3).

paho-mqtt draait een eigen netwerkthread; die blijft binnen dit module. Asyncio-services
krijgen ontvangen berichten via een asyncio.Queue. Is de broker onbereikbaar, dan worden
metingen weggegooid (niet gebufferd) en blijft paho opnieuw verbinden.
"""

import asyncio
import logging
import threading

import paho.mqtt.client as mqtt

from brms import grootheden
from brms.meting import Meting

log = logging.getLogger(__name__)

WACHTRIJ_MAX = 10000


class Bus:
    def __init__(self, naam: str, host: str = "localhost", port: int = 1883):
        self._host, self._port = host, port
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"brms-{naam}")
        self._client.reconnect_delay_set(min_delay=1, max_delay=30)
        self._client.on_connect = self._bij_verbinden
        self._client.on_disconnect = self._bij_verbreken
        self._client.on_message = self._bij_bericht
        self._abonnementen: list[tuple[str, int]] = []
        self._callbacks: list[tuple[str, callable]] = []
        self._lock = threading.Lock()
        self._weggegooid = 0

    # --- levenscyclus -------------------------------------------------------------
    def start(self) -> None:
        """Start de netwerkthread; verbinden (en opnieuw verbinden) gaat op de achtergrond."""
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
        """Publiceert een meting; False (weggegooid) als de broker niet bereikbaar is.

        QoS 1 voor toestanden, 0 voor doorlopend en toename; nooit retain (§3.1, §3.3).
        """
        if not self.verbonden:
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

    # --- abonneren ----------------------------------------------------------------
    def abonneer(self, topic: str, qos: int, callback) -> None:
        """callback(topic, payload) wordt aangeroepen in de netwerkthread van paho."""
        with self._lock:
            self._abonnementen.append((topic, qos))
            self._callbacks.append((topic, callback))
        if self.verbonden:
            self._client.subscribe(topic, qos)

    def wachtrij(self, topic: str, qos: int) -> asyncio.Queue:
        """Abonneert en levert berichten (topic, payload) in een asyncio.Queue.

        Aanroepen vanuit de draaiende asyncio-loop. Is de wachtrij vol, dan wordt het
        bericht weggegooid.
        """
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue(maxsize=WACHTRIJ_MAX)

        def zet(item):
            try:
                q.put_nowait(item)
            except asyncio.QueueFull:
                log.warning("wachtrij vol; bericht op %s weggegooid", item[0])

        self.abonneer(topic, qos, lambda t, p: loop.call_soon_threadsafe(zet, (t, p)))
        return q

    # --- paho-callbacks (netwerkthread) -------------------------------------------
    def _bij_verbinden(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.warning("verbinden met broker mislukt: %s", reason_code)
            return
        log.info("verbonden met broker %s:%d", self._host, self._port)
        with self._lock:
            abonnementen = list(self._abonnementen)
        for topic, qos in abonnementen:
            client.subscribe(topic, qos)

    def _bij_verbreken(self, client, userdata, flags, reason_code, properties):
        log.warning("verbinding met broker verbroken: %s", reason_code)

    def _bij_bericht(self, client, userdata, msg):
        with self._lock:
            callbacks = list(self._callbacks)
        for topic, cb in callbacks:
            if mqtt.topic_matches_sub(topic, msg.topic):
                try:
                    cb(msg.topic, msg.payload)
                except Exception:
                    log.exception("fout bij verwerken van bericht op %s", msg.topic)
