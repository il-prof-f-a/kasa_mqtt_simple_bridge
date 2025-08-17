#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Kasa MQTT Bridge - Versione Stabile

Questo script funge da bridge tra i dispositivi smart TP-Link Kasa e un broker MQTT.
Scopre automaticamente i dispositivi sulla rete, pubblica il loro stato completo
come JSON e permette di controllarli tramite comandi MQTT.

Progettato per essere eseguito come servizio continuo.
"""

import asyncio
import yaml
import aiomqtt
import platform
import json
import re
import logging
from datetime import datetime, timedelta
from kasa import Discover, Credentials

# --- Configurazione del Logging ---
logging.basicConfig(
    level=logging.WARNING,
    format='%(asctime)s - %(levelname)s - %(message)s',
)

# --- Caricamento della Configurazione ---
try:
    with open('config.yaml', 'r') as f:
        config = yaml.safe_load(f)
except FileNotFoundError:
    logging.error("Errore critico: config.yaml non trovato. Assicurati che esista e sia nella stessa cartella.")
    exit()

# --- Funzioni di Utilità ---
def get_feature_value(feature):
    """Estrae il valore da un oggetto Feature in modo sicuro, convertendolo se necessario."""
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

# --- Task Principali del Bridge ---
async def discovery_task(kasa_devices, device_map, reverse_device_map):
    """
    Task che esegue periodicamente i "lavori" di scoperta definiti nel config.yaml
    per trovare nuovi dispositivi Kasa sulla rete.
    """
    creds = Credentials(username=config['kasa']['email'], password=config['kasa']['password'])
    jobs = config.get('discovery_jobs', [])
    next_scan_times = [datetime.now()] * len(jobs)

    while True:
        for i, job in enumerate(jobs):
            if datetime.now() >= next_scan_times[i]:
                job_type = job.get("type")
                logging.info(f"Esecuzione Job di Scoperta {i+1} (Tipo: {job_type})")
                
                temp_found_devices = {}
                
                if job_type == "broadcast":
                    timeout = job.get("timeout", 5)
                    try:
                        discovered = await Discover.discover(timeout=timeout)
                        for ip, dev in discovered.items(): temp_found_devices[ip] = dev
                    except Exception as e: logging.error(f"Errore durante il broadcast: {e}")
                
                elif job_type == "host":
                    target_ip = job.get("target")
                    if target_ip:
                        try:
                            dev = await Discover.discover_single(target_ip)
                            temp_found_devices[dev.host] = dev
                        except Exception:
                            logging.warning(f"Nessun dispositivo trovato a {target_ip} durante la scansione host.")

                for ip, found_dev in temp_found_devices.items():
                    if ip in [d.host for d in kasa_devices.values()]: continue

                    logging.info(f"Trovato potenziale NUOVO dispositivo a {ip} ({found_dev.model}). Tento l'autenticazione...")
                    try:
                        dev = await Discover.discover_single(ip, credentials=creds)
                        await dev.update()
                        
                        is_a_hub = "KH100" in dev.model
                        
                        if is_a_hub:
                            logging.info(f"  -> È un Hub ('{dev.alias}'). Cerco i dispositivi figli...")
                            for child in dev.children:
                                if child.device_id not in kasa_devices:
                                    alias = sanitize_alias(child.alias)
                                    unique_name = f"{alias}_{child.device_id[-8:]}"
                                    kasa_devices[child.device_id] = child
                                    device_map[child.device_id] = unique_name
                                    reverse_device_map[unique_name] = child.device_id
                                    logging.info(f"    -> NUOVO dispositivo figlio aggiunto: '{child.alias}' -> Topic: {unique_name}")
                        else:
                            device_id = dev.device_id
                            if device_id not in kasa_devices:
                                alias = sanitize_alias(dev.alias)
                                unique_name = f"{alias}_{device_id[-8:]}"
                                kasa_devices[device_id] = dev
                                device_map[device_id] = unique_name
                                reverse_device_map[unique_name] = dev.device_id
                                logging.info(f"  -> NUOVO dispositivo standalone aggiunto: '{dev.alias}' -> Topic: {unique_name}")

                    except Exception as e:
                        logging.error(f"  -> Autenticazione o aggiornamento fallito per {ip}: {e}")

                rescan_interval = job.get("rescan_interval")
                if rescan_interval:
                    next_scan_times[i] = datetime.now() + timedelta(seconds=rescan_interval)
                    logging.info(f"--- Job {i+1} completato. Prossimo scan schedulato tra {rescan_interval} secondi. ---")
                else:
                    next_scan_times[i] = datetime.max
        
        await asyncio.sleep(15) # Controlla se è ora di eseguire un job ogni 15 secondi

async def poll_kasa_devices(client, devices, dev_map):
    """
    Task che interroga periodicamente lo stato di tutti i dispositivi Kasa conosciuti
    e pubblica i dati su MQTT.
    """
    base_topic = config['mqtt']['base_topic']
    while True:
        poll_interval = config.get('poll_interval', 60)
        logging.info(f"Inizio ciclo di polling per {len(devices)} dispositivi...")
        
        for device_id in list(devices.keys()):
            if device_id in devices:
                valvola = devices[device_id]
                device_name = dev_map[device_id]
                try:
                    await valvola.update()
                    state_json = {n: get_feature_value(f) for n, f in valvola.features.items() if get_feature_value(f) is not None}
                    state_topic = f"{base_topic}/{device_name}/state"
                    payload = json.dumps(state_json)
                    await client.publish(state_topic, payload, retain=True)
                except Exception as e:
                    logging.error(f"Errore durante l'aggiornamento per {device_name}: {e}. Potrebbe essere offline.")
        
        logging.info(f"Fine ciclo di polling. Prossimo aggiornamento tra {poll_interval} secondi.")
        await asyncio.sleep(poll_interval)

async def handle_mqtt_messages(client, devices, rev_map):
    """
    Task che ascolta i messaggi MQTT in arrivo e invia i comandi corrispondenti
    ai dispositivi Kasa.
    """
    base_topic = config['mqtt']['base_topic']
    command_topic_wildcard = f"{base_topic}/+/set"
    await client.subscribe(command_topic_wildcard)
    logging.info(f"Sottoscritto al topic wildcard per i comandi: {command_topic_wildcard}")
    
    async for message in client.messages:
        try:
            topic = message.topic.value
            device_name = topic.split('/')[1]
            
            if device_name in rev_map:
                device_id = rev_map[device_name]
                valvola = devices[device_id]
                payload_str = message.payload.decode()
                logging.info(f"Messaggio ricevuto per '{device_name}': {payload_str}")
                commands = json.loads(payload_str)
                
                for feature_name, new_value in commands.items():
                    if feature_name in valvola.features and hasattr(valvola.features[feature_name], 'set_value'):
                        logging.info(f"  -> Invio comando: imposta '{feature_name}' a '{new_value}'")
                        await valvola.features[feature_name].set_value(new_value)
                        
                        if hasattr(valvola, 'parent') and valvola.parent:
                            hub_to_update = valvola.parent
                            logging.info(f"  -> Forzo l'aggiornamento dell'hub '{hub_to_update.alias}' per conferma...")
                            await asyncio.sleep(1) 
                            await hub_to_update.update()
                            logging.info(f"  -> Aggiornamento HUB completato.")
                            
                            state_json = {n: get_feature_value(f) for n, f in valvola.features.items() if get_feature_value(f) is not None}
                            state_topic = f"{base_topic}/{device_name}/state"
                            await client.publish(state_topic, json.dumps(state_json), retain=True)
                            logging.info(f"  -> Pubblicazione stato aggiornato per '{device_name}'")
            
        except json.JSONDecodeError:
            logging.warning(f"Payload ricevuto su {topic} non è un JSON valido: {payload_str}")
        except Exception:
            logging.exception(f"Errore non gestito in handle_mqtt_messages per il topic {topic}")

async def main():
    """
    Funzione principale che inizializza e orchestra tutti i task del bridge.
    """
    logging.info("Avvio del Kasa-MQTT Bridge...")
    
    # Dizionari condivisi che verranno popolati e usati dai task
    kasa_devices, device_map, reverse_device_map = {}, {}, {}
    
    main_tasks = []
    try:
        async with aiomqtt.Client(
            hostname=config['mqtt']['host'], port=config['mqtt']['port'],
            username=config['mqtt'].get('user'), password=config['mqtt'].get('password')
        ) as client:
            logging.info("Connesso al broker MQTT.")

            # Creiamo e avviamo i 3 task principali
            discovery = asyncio.create_task(discovery_task(kasa_devices, device_map, reverse_device_map))
            messages = asyncio.create_task(handle_mqtt_messages(client, kasa_devices, reverse_device_map))
            polling = asyncio.create_task(poll_kasa_devices(client, kasa_devices, device_map))
            
            main_tasks = [discovery, messages, polling]
            await asyncio.gather(*main_tasks)
            
    except asyncio.CancelledError:
        logging.info("Task principale cancellato, inizio spegnimento.")
    except Exception:
        logging.exception("Errore critico nel task principale. Il bridge si spegnerà.")
    finally:
        logging.info("Cancellazione dei task in corso...")
        for task in main_tasks:
            task.cancel()
        # Aspettiamo che tutti i task si chiudano correttamente
        await asyncio.gather(*main_tasks, return_exceptions=True)
        logging.info("Tutti i task sono stati terminati.")

if __name__ == "__main__":
    # Applica il fix per l'event loop solo su Windows
    if platform.system() == "Windows":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Ricevuto segnale di interruzione (CTRL+C). Spegnimento pulito...")
    
    logging.info("Kasa MQTT Bridge arrestato.")