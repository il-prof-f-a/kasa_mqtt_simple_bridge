
# Kasa MQTT Bridge

Un bridge Python semplice, robusto e auto-configurante per integrare i dispositivi TP-Link Kasa (incluso l'Hub KH100, le valvole termostatiche KE100, prese, lampadine, etc.) in qualsiasi sistema domotico tramite MQTT.

Questo progetto è nato da un'intensa sessione di debugging e rappresenta una soluzione completa per il controllo locale dei dispositivi Kasa, senza dipendere da servizi cloud esterni.

## Perché Usare Questo Bridge?

Molte piattaforme domotiche hanno un supporto limitato o inaffidabile per i dispositivi Kasa, specialmente per quelli che operano tramite un hub. Questo bridge risolve il problema creando un'interfaccia standard e universale tramite MQTT.

-   **Controllo Locale:** Comunica direttamente con i dispositivi sulla tua rete locale, riducendo la latenza e garantendo il funzionamento anche senza una connessione a Internet.
-   **Scoperta Automatica:** Scansiona la rete per trovare e configurare automaticamente tutti i tuoi dispositivi Kasa, senza bisogno di inserire manualmente indirizzi MAC o ID.
-   **Configurazione Flessibile:** Permette di definire strategie di scoperta ibride (broadcast, scansione di host specifici o di intere reti) per la massima affidabilità in qualsiasi tipo di rete.
-   **Struttura MQTT Parametrica:** Pubblica lo stato completo di ogni dispositivo come un singolo messaggio JSON, rendendo tutti i sensori e gli attributi immediatamente disponibili.
-   **Controllo a Ciclo Chiuso:** Quando invii un comando, il bridge verifica che il dispositivo abbia recepito la modifica e ripubblica immediatamente lo stato aggiornato, fornendo un feedback affidabile.
-   **Leggero e Asincrono:** Scritto in Python con `asyncio` e `aiomqtt` per la massima efficienza e un basso consumo di risorse, ideale per girare su un Raspberry Pi o un piccolo server.

## Prerequisiti

-   Python 3.11+
-   Un broker MQTT (es. [Mosquitto](https://mosquitto.org/)) in esecuzione sulla tua rete.
-   Git installato per clonare il repository.

## Installazione

1.  Clona questo repository sulla macchina che eseguirà il bridge:
    ```bash
    git clone https://github.com/il-prof-f-a/kasa_mqtt_simple_bridge
    cd kasa_mqtt_simple_bridge
    ```

2.  Crea e attiva un ambiente virtuale Python. Questo isola le dipendenze del progetto.
    ```bash
    python -m venv venv
    ```
    -   **Su Windows:**
        ```cmd
        .\venv\Scripts\activate
        ```
    -   **Su Linux/macOS:**
        ```bash
        source venv/bin/activate
        ```

3.  Installa tutte le dipendenze necessarie:
    ```bash
    pip install -r requirements.txt
    ```

## Configurazione

1.  Crea il tuo file di configurazione personale partendo dall'esempio fornito:
    ```bash
    # Su Windows
    copy config.yaml.example config.yaml
    # Su Linux/macOS
    cp config.yaml.example config.yaml
    ```

2.  Modifica il file `config.yaml` con i tuoi dati:
    -   **`mqtt`**: Inserisci i dettagli del tuo broker MQTT (`host`, `port`, `user`, `password`).
    -   **`kasa`**: Inserisci le credenziali del tuo account TP-Link Kasa (`email`, `password`). Queste sono necessarie per l'autenticazione locale.
    -   **`poll_interval`**: Definisce ogni quanti secondi lo script aggiorna lo stato dei dispositivi.
    -   **`discovery_jobs`**: Questa è la sezione più importante. Definisce come lo script deve cercare i dispositivi. La configurazione di default è la più raccomandata:
        -   Un job di tipo `broadcast` per trovare rapidamente i dispositivi su reti semplici.
        -   Un job di tipo `host` con l'IP specifico del tuo Hub Kasa per una scoperta garantita e veloce.
        -   Puoi aggiungere job di tipo `network` per scansionare intere sottoreti se hai dispositivi con IP dinamico.

## Esecuzione

Per un test iniziale o per l'esecuzione manuale, lancia semplicemente lo script:
```bash
python kasa_mqtt_bridge.py
```
Vedrai i log apparire nel terminale, mostrando la scoperta dei dispositivi e il ciclo di polling. Premi CTRL+C per fermarlo.

## API MQTT

### Topic di Stato

Lo stato completo di ogni dispositivo viene pubblicato come JSON sul topic:  
kasa/NOME_DISPOSITIVO/state

Il NOME_DISPOSITIVO è generato automaticamente come alias_ultimi8caratteriid.

Esempio di payload per una valvola:

codeJSON

```
{
    "state": true,
    "target_temperature": 22.0,
    "temperature": 21.5,
    "battery_level": 100,
    "thermostat_mode": "heating",
    "child_lock": false,
    "rssi": -55
}
```

### Topic di Comando
Per controllare un dispositivo, pubblica un messaggio JSON sul topic:
`kasa/NOME_DISPOSITIVO/set`

*Esempi di payload:*
-   **Impostare la temperatura:** `{"target_temperature": 21.5}`
-   **Accendere/Spegnere (modalità Riscaldamento/Off):** `{"state": true}` o `{"state": false}`
-   **Attivare blocco bambini:** `{"child_lock": true}`

**Nota per la riga di comando Windows:** Ricorda di "escapare" le virgolette doppie del JSON:
```cmd
mosquitto_pub -h IP_BROKER -t "kasa/salotto_.../set" -m "{\"target_temperature\": 22}"
```

----------

## Eseguire come Servizio Permanente

Per far girare il bridge 24/7, è necessario configurarlo come servizio che si avvii automaticamente con il sistema.

### Su Linux (con systemd)

1.  **Crea un file di servizio** con un editor di testo:
    
    codeBash
    
    ```
    sudo nano /etc/systemd/system/kasa-bridge.service
    ```
    
2.  **Incolla la seguente configurazione.**  **ATTENZIONE:** Sostituisci il_tuo_utente e i percorsi /home/il_tuo_utente/... con i tuoi dati reali!
    
    codeIni
    
    ```
    [Unit]
    Description=Kasa MQTT Bridge Service
    After=network-online.target
    
    [Service]
    Type=simple
    User=il_tuo_utente
    
    # Percorso all'eseguibile Python del venv e allo script
    ExecStart=/home/il_tuo_utente/kasa_mqtt_simple_bridge/venv/bin/python /home/il_tuo_utente/kasa_mqtt_simple_bridge/kasa_mqtt_bridge.py
    
    # Directory di lavoro del progetto
    WorkingDirectory=/home/il_tuo_utente/kasa_mqtt_simple_bridge
    
    Restart=on-failure
    RestartSec=15
    
    [Install]
    WantedBy=multi-user.target
    ```
    
3.  **Abilita e avvia il servizio:**
    
    codeBash
    
    ```
    sudo systemctl daemon-reload
    sudo systemctl start kasa-bridge.service
    sudo systemctl enable kasa-bridge.service
    ```
    
4.  **Per controllare i log:**
    
    codeBash
    
    ```
    sudo journalctl -u kasa-bridge.service -f
    ```
    

### Su Windows (con NSSM)

Il metodo più robusto è usare **NSSM (the Non-Sucking Service Manager)**.

1.  **Scarica NSSM** dal [sito ufficiale](https://www.google.com/url?sa=E&q=https%3A%2F%2Fnssm.cc%2Fdownload). Estrai nssm.exe in una cartella stabile (es. C:\NSSM).
    
2.  **Apri un Prompt dei comandi come Amministratore**.
    
3.  **Naviga alla cartella di NSSM** e lancia il comando di installazione:
    
    codeCmd
    
    ```
    cd C:\NSSM
    nssm.exe install KasaMqttBridge
    ```
    
4.  **Si aprirà una finestra grafica. Configurala come segue:**
    
    -   **Scheda Application:**
        
        -   **Path:**  C:\Users\franc\mio_progetto_kasa\venv\Scripts\python.exe (Usa il percorso esatto del tuo python.exe nel venv).
            
        -   **Startup directory:**  C:\Users\franc\mio_progetto_kasa (La cartella del tuo progetto).
            
        -   **Arguments:**  kasa_mqtt_bridge.py (Il nome del tuo script).
            
    -   **Scheda I/O (Opzionale ma raccomandato):**
        
        -   **Output (stdout):**  C:\Users\franc\mio_progetto_kasa\bridge.log (per salvare i log su file).
            
        -   **Error (stderr):**  C:\Users\franc\mio_progetto_kasa\bridge.error.log.
            
    -   **Scheda Shutdown:**
        
        -   Assicurati che Generate control-C sia spuntato per uno spegnimento pulito.
            
5.  **Clicca su "Install service"**.
    
6.  **Avvia il servizio** dal Prompt dei comandi (sempre come amministratore):
    
    codeCmd
    
    ```
    nssm.exe start KasaMqttBridge
    ```
    
    Puoi anche gestirlo dall'app "Servizi" di Windows.
    

## Licenza

Questo progetto è rilasciato sotto la Licenza MIT. Vedi il file LICENSE per maggiori dettagli.

## Ringraziamenti

Un ringraziamento speciale al team di [python-kasa](https://www.google.com/url?sa=E&q=https%3A%2F%2Fgithub.com%2Fpython-kasa%2Fpython-kasa) per aver creato e mantenuto una libreria fantastica.
