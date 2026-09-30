# HTTPS per la web UI: questione aperta

Stato: **non deciso**, annotato il 2026-09-30. Da riprendere quando ci saranno altre funzioni che lo richiedono.

## Il problema

La web UI si apre da `http://cucu-XXXX.local`, senza HTTPS. I browser riservano alle pagine sicure alcune funzioni che oggi ci mancano:

| Funzione | Cosa ci servirebbe | Senza HTTPS |
|---|---|---|
| Service worker | Pagina "Cucù non è raggiungibile" quando Cucù è spento o fuori rete e l'app viene aperta dalla Home | Il telefono mostra una pagina bianca: non c'è niente di salvato da mostrare |
| `beforeinstallprompt` (Chrome Android) | Pulsante "Aggiungi alla Home" che apre la finestra di installazione con un tocco | Solo una guida con i passaggi da fare nel menu del browser |
| Notifiche push, accesso da fuori casa | Idee future | Non possibili |

Su iPhone il pulsante di installazione non esiste nemmeno in HTTPS: Safari permette solo Condividi → Aggiungi alla schermata Home. Il service worker invece funzionerebbe anche lì.

Vale solo per le pagine già aperte: la schermata di riconnessione della UI funziona già quando Cucù sparisce **con l'app aperta**. La pagina bianca compare solo aprendo l'app da chiusa.

## Alternative scartate

- **Certificato per `.local`**: nessuna autorità lo rilascia. Un certificato autofirmato fa comparire l'avviso di sicurezza del browser: inaccettabile per un genitore.
- **Guscio su un sito HTTPS pubblico** (con service worker) che poi apre Cucù in HTTP: una pagina HTTPS non può interrogare un indirizzo HTTP (mixed content), quindi non può sapere se Cucù è raggiungibile prima di mandarci il telefono.
- **Cache HTTP / `stale-if-error`**: Safari e Chrome non mostrano la copia vecchia di una pagina quando il server non risponde. AppCache non esiste più.

## L'unica strada vera

Un dominio pubblico per ogni dispositivo che punta all'indirizzo di Cucù nella rete di casa, con certificati validi rinnovati in automatico. È lo schema di Plex (`*.plex.direct`):

- es. `192-168-1-23.<id-dispositivo>.<dominio-cucu>` → risolve a `192.168.1.23`
- certificato Let's Encrypt emesso con la sfida DNS-01, gestito da un piccolo servizio centrale (il dispositivo non è raggiungibile da internet, quindi niente HTTP-01)
- il dispositivo rinnova e installa il certificato, e uvicorn serve HTTPS

Costi e rischi:
- un dominio e un servizio da tenere in piedi, cioè una dipendenza esterna per un prodotto che oggi funziona solo in rete locale;
- alcuni router bloccano i nomi pubblici che risolvono in indirizzi privati (protezione dal DNS rebinding, per esempio su certi Fritz!Box): serve un piano B;
- al primo setup il telefono è collegato all'hotspot di Cucù senza internet, quindi niente DNS pubblico: lì resterebbe l'accesso HTTP da `10.42.0.1`;
- l'indirizzo cambia se cambia l'IP del dispositivo: va aggiornato il DNS, oppure si usa un nome che contiene l'IP come fa Plex.

## Quando riprenderla

Ha senso se si aggiungono altre funzioni che richiedono HTTPS (notifiche, accesso da fuori casa, installazione con un tocco su Android). Solo per la pagina bianca il costo è sproporzionato.
