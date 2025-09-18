#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Kasa MQTT Bridge - Versione Robust
- Reconnect loop con backoff
- Timeout su discover/update/set/publish
- Supervisore che riavvia i task interni se uno muore
- Heartbeat su MQTT e (opzionale) systemd watchdog (sdnotify)

Config richiesta (config.yaml):
mqtt:
  host: 127.0.0.1
  port: 1883
  base_topic: kasa
  user: ...
  password: ...
kasa:
  email: ...
  password: ...
poll_interval: 60
discovery_jobs:
  - { type: broadcast, timeout: 5, rescan_interval: 120 }
  - { type: host, target: "192.168.1.50", rescan_interval: 300 }
"""

import asyncio
import yaml
import aiomqtt
import platform
import json
import re
import logging
import sys
import signal
from datetime import datetime, timedelta
from kasa import Discover, Credentials

# --- Logging minimale (alzalo se vuoi: INFO/DEBUG) ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

# --- Caricamento & validazione config ---
try:
    with open('config.yaml', 'r') as f:
        config = yaml.safe_load(f)
    # Validazione minima per evitare KeyError successivi
    for path in [('mqtt', 'host'), ('mqtt', 'port'), ('mqtt', 'base_topic'),
                 ('kasa', 'email'), ('kasa', 'password')]:
        d = config
        for k in path:
            if not isinstance(d, dict) or k not in d:
                raise KeyError(f"Config mancante: {'.'.join(path)}")
            d = d[k]
except (FileNotFoundError, KeyError) as e:
    logging.error(f"Errore critico di configurazione: {e}")
    sys.exit(2)  # non-zero -> systemd Restart=on-failure

# --- Utilità ---
def get_feature_value(feature):
    """Estrae il valore da un oggetto Feature in modo sicuro."""
    if not hasattr(feature, 'value'):
        return None
    raw_value = feature.value
    if hasattr(raw_value, 'name') and not isinstance(raw_value, (str, int, float, bool)):
        return raw_value.name.lower()
    return raw_value

def sanitize_alias(alias):
    """Pulisce l'alias per usarlo in un topic MQTT."""
    s = str(alias or "").lower()
    s = re.sub(r'\s+', '_', s)
    s = re.sub(r'[^a-z0-9_-]', '', s)
    return s

async def a_wait_for(coro, timeout, what="operazione"):
    """Wrapper con timeout e messaggio chiaro."""
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        raise RuntimeError(f"Timeout {timeout}s durante {what}")

def _safe_topic(obj):
    """aiomqtt può dare topic come str o oggetto; normalizza a str."""
    return getattr(obj, "value", obj)

# --- Task Principali ---
async def discovery_task(kasa_devices, device_map, reverse_device_map):
    """Scoperta periodica dispositivi Kasa secondo discovery_jobs."""
    creds = Credentials(username=config['kasa']['email'], password=config['kasa']['password'])
    jobs = config.get('discovery_jobs', [])
    next_scan_times = [datetime.now()] * len(jobs)

    while True:
        now = datetime.now()
        for i, job in enumerate(jobs):
            if now < next_scan_times[i]:
                continue

            job_type = job.get("type")
            logging.info(f"Esecuzione Job di Scoperta {i+1} (Tipo: {job_type})")
            temp_found_devices = {}

            if job_type == "broadcast":
                timeout = job.get("timeout", 5)
                try:
                    discovered = await a_wait_for(Discover.discover(timeout=timeout),
                                                  timeout=timeout+2, what="discover broadcast")
                    for ip, dev in discovered.items():
                        temp_found_devices[ip] = dev
                except Exception as e:
                    logging.error(f"Errore discover broadcast: {e}")

            elif job_type == "host":
                target_ip = job.get("target")
                if target_ip:
                    try:
                        dev = await a_wait_for(Discover.discover_single(target_ip),
                                               timeout=10, what=f"discover host {target_ip}")
                        temp_found_devices[dev.host] = dev
                    except Exception:
                        logging.warning(f"Nessun dispositivo trovato a {target_ip}.")

            # Autenticazione e aggiunta mappa
            for ip, found_dev in temp_found_devices.items():
                if ip in [d.host for d in kasa_devices.values()]:
                    continue
                logging.info(f"Nuovo IP {ip} ({found_dev.model}). TENTO login...")
                try:
                    dev = await a_wait_for(Discover.discover_single(ip, credentials=creds),
                                           timeout=10, what=f"discover device {ip}")
                    await a_wait_for(dev.update(), timeout=10, what=f"update {ip}")

                    is_hub = "KH100" in getattr(dev, "model", "")
                    if is_hub:
                        logging.info(f"È un Hub '{dev.alias}'. Cerco i figli...")
                        for child in getattr(dev, "children", []):
                            if child.device_id not in kasa_devices:
                                alias = sanitize_alias(child.alias)
                                unique_name = f"{alias}_{child.device_id[-8:]}"
                                kasa_devices[child.device_id] = child
                                device_map[child.device_id] = unique_name
                                reverse_device_map[unique_name] = child.device_id
                                logging.info(f"Figlio aggiunto: '{child.alias}' -> {unique_name}")
                    else:
                        device_id = dev.device_id
                        if device_id not in kasa_devices:
                            alias = sanitize_alias(dev.alias)
                            unique_name = f"{alias}_{device_id[-8:]}"
                            kasa_devices[device_id] = dev
                            device_map[device_id] = unique_name
                            reverse_device_map[unique_name] = dev.device_id
                            logging.info(f"Standalone aggiunto: '{dev.alias}' -> {unique_name}")

                except Exception as e:
                    logging.error(f"Login/update fallito per {ip}: {e}")

            rescan = job.get("rescan_interval")
            next_scan_times[i] = (now + timedelta(seconds=rescan)) if rescan else datetime.max

        await asyncio.sleep(15)

async def poll_kasa_devices(client, devices, dev_map):
    """Polling periodico di tutti i dispositivi + publish stato su MQTT."""
    base_topic = config['mqtt']['base_topic']
    while True:
        poll_interval = config.get('poll_interval', 60)
        logging.info(f"Polling per {len(devices)} dispositivi...")

        hubs_aggiornati = set()
        for device_id in list(devices.keys()):
            if device_id not in devices:
                continue
            dev = devices[device_id]
            device_name = dev_map.get(device_id, device_id[-8:])

            try:
                hub = getattr(dev, "parent", None)
                if hub is not None:
                    hub_key = id(hub)
                    if hub_key not in hubs_aggiornati:
                        await a_wait_for(hub.update(), timeout=10, what="update hub")
                        hubs_aggiornati.add(hub_key)

                await a_wait_for(dev.update(), timeout=10, what=f"update device {device_name}")

                state_json = {
                    n: get_feature_value(f)
                    for n, f in dev.features.items()
                    if get_feature_value(f) is not None
                }
                state_topic = f"{base_topic}/{device_name}/state"
                await a_wait_for(client.publish(state_topic, json.dumps(state_json), retain=True),
                                 timeout=10, what=f"publish state {device_name}")

            except Exception as e:
                logging.error(f"Errore aggiornamento '{device_name}': {e}")

        logging.info(f"Fine ciclo. Prossimo tra {poll_interval}s.")
        await asyncio.sleep(poll_interval)

async def handle_mqtt_messages(client, devices, rev_map):
    """Gestione comandi MQTT: .../base_topic/<device>/set con payload JSON."""
    base_topic = config['mqtt']['base_topic']
    command_topic_wildcard = f"{base_topic}/+/set"
    await client.subscribe(command_topic_wildcard)
    logging.info(f"Sottoscritto ai comandi su: {command_topic_wildcard}")

    async for message in client.messages:
        topic = "<unknown>"
        payload_str = ""
        try:
            topic = str(_safe_topic(message.topic))
            parts = topic.split('/')
            if len(parts) < 3:
                logging.warning(f"Topic non valido: {topic}")
                continue

            device_name = parts[1]
            if device_name not in rev_map:
                logging.warning(f"Device '{device_name}' non riconosciuto per topic {topic}")
                continue

            device_id = rev_map[device_name]
            dev = devices.get(device_id)
            if dev is None:
                logging.warning(f"Device '{device_name}' non presente in memoria.")
                continue

            payload_str = message.payload.decode(errors="replace")
            logging.info(f"Cmd per '{device_name}': {payload_str}")
            commands = json.loads(payload_str)

            for feature_name, new_value in commands.items():
                if feature_name in dev.features and hasattr(dev.features[feature_name], 'set_value'):
                    await a_wait_for(dev.features[feature_name].set_value(new_value),
                                     timeout=10, what=f"set {feature_name} on {device_name}")

                    # Aggiorna hub e ripubblica conferma
                    if hasattr(dev, 'parent') and dev.parent:
                        hub_to_update = dev.parent
                        await asyncio.sleep(1)
                        await a_wait_for(hub_to_update.update(), timeout=10, what="confirm hub update")

                        state_json = {n: get_feature_value(f) for n, f in dev.features.items()
                                      if get_feature_value(f) is not None}
                        state_topic = f"{base_topic}/{device_name}/state"
                        await a_wait_for(client.publish(state_topic, json.dumps(state_json), retain=True),
                                         timeout=10, what=f"publish confirm {device_name}")
                else:
                    logging.warning(f"Feature '{feature_name}' non impostabile su '{device_name}'")

        except json.JSONDecodeError:
            logging.warning(f"Payload non JSON su {topic}: {payload_str}")
        except Exception:
            logging.exception(f"Errore gestione messaggio (topic={topic})")

async def heartbeat_task(client, base_topic, stop_event, sd_notifier=None):
    """Heartbeat su MQTT e (se presente) sd_notify watchdog."""
    status_topic = f"{base_topic}/_bridge/status"
    try:
        await client.publish(status_topic, json.dumps({"status": "online", "ts": datetime.utcnow().isoformat()}), retain=True)
    except Exception:
        logging.warning("Impossibile pubblicare stato 'online' all'avvio.")
    while not stop_event.is_set():
        try:
            await client.publish(f"{base_topic}/_bridge/heartbeat",
                                 json.dumps({"ts": datetime.utcnow().isoformat()}),
                                 retain=False)
            if sd_notifier:
                try:
                    sd_notifier.notify("WATCHDOG=1")
                except Exception:
                    pass
        except Exception:
            logging.debug("Heartbeat fallito (broker down?).")
        await asyncio.sleep(15)
    try:
        await client.publish(status_topic, json.dumps({"status": "offline", "ts": datetime.utcnow().isoformat()}), retain=True)
    except Exception:
        pass

# --- Supervisione sessione MQTT (con backoff) ---
async def run_once(stop_event):
    """Una sessione completa: connessione MQTT + task fino a disconnessione/errore."""
    kasa_devices, device_map, reverse_device_map = {}, {}, {}

    # opzionale: systemd watchdog
    sd_notifier = None
    try:
        from sdnotify import SystemdNotifier  # pip install sdnotify
        sd_notifier = SystemdNotifier()
        sd_notifier.notify("READY=1")
    except Exception:
        sd_notifier = None

    async with aiomqtt.Client(
        hostname=config['mqtt']['host'], port=config['mqtt']['port'],
        username=config['mqtt'].get('user'), password=config['mqtt'].get('password')
    ) as client:
        logging.info("Connesso al broker MQTT.")
        base_topic = config['mqtt']['base_topic']
        discovery = asyncio.create_task(discovery_task(kasa_devices, device_map, reverse_device_map), name="discovery")
        messages  = asyncio.create_task(handle_mqtt_messages(client, kasa_devices, reverse_device_map), name="mqtt_messages")
        polling   = asyncio.create_task(poll_kasa_devices(client, kasa_devices, device_map), name="polling")
        heartbeat = asyncio.create_task(heartbeat_task(client, base_topic, stop_event, sd_notifier), name="heartbeat")
        tasks = [discovery, messages, polling, heartbeat]

        try:
            # ritorna appena uno dei task termina con eccezione o completion inaspettata
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            # se un task è terminato (errore o chiusura), cancelliamo il resto
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            # Propaga eventuali eccezioni
            for t in done:
                exc = t.exception()
                if exc:
                    raise exc
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

async def main():
    """Supervisore: mantiene vivo il bridge e gestisce riconnessioni con backoff."""
    logging.info("Avvio del Kasa-MQTT Bridge (supervisor)...")
    stop_event = asyncio.Event()

    def _handle_stop(signame):
        logging.warning(f"Segnale {signame} ricevuto, arresto in corso...")
        stop_event.set()
    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_running_loop().add_signal_handler(s, _handle_stop, s.name)
        except NotImplementedError:
            pass  # Windows

    backoff = 3
    while not stop_event.is_set():
        try:
            await run_once(stop_event)
            if not stop_event.is_set():
                logging.warning("Sessione terminata; reconnessione tra %ss", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logging.exception(f"Errore run_once: {e}")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
    logging.info("Supervisor terminato.")

if __name__ == "__main__":
    if platform.system() == "Windows":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    try:
        asyncio.run(main())
        sys.exit(0)
    except KeyboardInterrupt:
        logging.info("Interruzione da tastiera, spegnimento pulito...")
        sys.exit(0)
    except Exception as e:
        logging.exception(f"Uscita inattesa: {e}")
        sys.exit(2)  # non-zero -> systemd Restart=on-failure
