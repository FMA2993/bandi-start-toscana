# bandi-start-toscana

Estrattore dei **bandi di gara aperti** dal portale acquisti di START Toscana
(`sanita.start.toscana.it/portalegare`, ex "Portale Gare" della sanità toscana).

Restituisce per ogni gara: oggetto, ente appaltante, importo, scadenza, criterio di
aggiudicazione, tipo appalto, CPV, CIG, PEC, date (quesiti / offerte / prima seduta),
lotti con relativo CIG, e **gli allegati ufficiali** con URL diretto, dimensione e
SHA256 dichiarato dalla stazione appaltante.

Nessuna API, nessun login, nessun proxy: il portale è ASP classico + Joomla e serve HTML
*server-side*, quindi si parla direttamente con le pagine e con l'endpoint degli allegati.

## Quickstart

```bash
make install          # .venv + dipendenze (usa uv se c'è, altrimenti venv+pip)
make update           # scrape completo -> output/ + allegati/
make list             # solo elenco, a video
make test             # pytest offline sulle fixture
```

Senza make:

```bash
uv venv .venv && uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/python start_toscana.py \
    --json output/bandi_aperti.json --csv output/bandi_aperti.csv \
    --html output/report_bandi.html --download allegati --workers 5 --timeout 90
```

> **Trappola ambiente (macOS)** — `/opt/homebrew/bin/python3` (Homebrew 3.14.6) crea il
> venv ma `ensurepip` fallisce: `python3 -m venv .venv` muore con *ensurepip returned
> non-zero exit status 1*. Usa `uv` (consigliato) oppure il Python di python.org
> (`/usr/local/bin/python3`, qui 3.15.2), che funziona.

## Output

```
output/bandi_aperti.json     54 bandi, dettaglio completo (struttura sotto)
output/bandi_aperti.csv      vista piatta per Excel/pivot
output/report_bandi.html     dashboard autonoma (nessuna CDN, apribile offline)
allegati/<id_bando>/...      203 file reali (PDF, DOCX, XLSX, ZIP) ~87 MB
```

La dashboard: ricerca full-text (oggetto/ente/CIG/CPV/id), filtri rapidi
(*solo con allegati*, *scade entro 3 giorni*, *oltre 1 M€*), ordinamento per colonna,
riga espandibile con l'elenco allegati (link al file locale se scaricato, più il
link al portale).

### Struttura di un record JSON

```json
{
  "id": "613416",
  "descrizione": "ESTARSDAFA03PR25 FORNITURA DI FARMACI URGENTI",
  "stato": "Bando",                      // Bando | Rettificato | Revocato | ...
  "ente_proponente": "Regione Toscana",
  "ente_appaltante": "Regione Toscana",
  "importo": 1452000.0,
  "scadenza": "2026-11-19T12:00:00",     // ISO, ordinabile
  "criterio": "Prezzo più basso",
  "tipo_appalto": "Forniture",
  "cig": "BD9E361B26",                   // primo CIG dei lotti se il bando non ne ha uno
  "cpv": "33690000-3 - Medicinali vari",
  "provincia": "Firenze", "comune": "Firenze",
  "pec": "estar@postacert.toscana.it",
  "scadenza_quesiti": "...", "scadenza_offerta": "...", "data_seduta": "...",
  "num_lotti": 3, "lotti": [{"Numero Lotto": "1", "CIG": "BD9E361B26", "…": "…"}],
  "num_documenti": 7,
  "documenti": [{
      "descrizione": "Determina Indizione",
      "filename": "Determinazione_0001310_2026.pdf",
      "ext": "pdf", "size_bytes": 365649,
      "sha256": "FFF6017698F2630D3830DFE9CBD509676613DBC529D704C2D7ACABBCF128215F",
      "data_pubblicazione": "2026-10-30T08:33:49",
      "url": "https://…/DisplayAttach.ASP?OPERATION=DISPLAY&…&TECHVALUE=…"
  }],
  "campi": { "…": "tutte le coppie chiave/valore della scheda" }
}
```

## CLI

```
--scaduti                 elenco storico dei bandi scaduti
--max-pages N             limite di pagine dell'elenco
--limit N                 max bandi da dettagliare
--min-importo / --max-importo
--ente SUBSTRING          filtra ente appaltante/proponente
--testo SUBSTRING         filtra l'oggetto
--solo-con-documenti      tieni solo le gare con allegati pubblici
--no-detail               solo elenco, nessuna richiesta di dettaglio
--workers N               parallelismo sul dettaglio (default 4)
--timeout N               timeout per richiesta (default 60; il portale è lento, 90 è meglio)
--json / --csv / --html   file di output
--download DIR            scarica gli allegati in DIR/<id_bando>/
--quiet
```

## Uso come libreria

```python
from pathlib import Path
from start_toscana import StartToscanaClient, render_html

c = StartToscanaClient(timeout=90)
bandi = c.open_tenders()                       # -> list[Bando]
bandi = c.enrich(bandi, workers=5)             # dettaglio + documenti in parallelo

for b in bandi:
    print(b.id, b.scadenza, b.importo, len(b.documenti))
    for d in b.documenti:
        c.download(d, Path("allegati") / b.id)  # verifica size e sha256 dichiarati

render_html(bandi, "output/report.html", allegati_dir="allegati")
```

## Endpoint usati

| Metodo | URL | Note |
|---|---|---|
| GET | `/portalegare/index.php/bandi?&page=N` | elenco, 20 righe per pagina |
| GET | `/portalegare/index.php/bandi?getdettaglio=yes&bando=<id>&…` | scheda gara |
| GET | `/application/CTL_Library/functions/field/DisplayAttach.ASP?OPERATION=DISPLAY&FIELD=Allegato&PATH=../&TECHVALUE=<tv>&FORMAT=I%2CN` | binario allegato |
| GET | `/portale/rss_bandi.asp?…&nPag=N` | feed RSS, espone `<afs:totRecord>` (utile come sanity check) |

Il campo `TECHVALUE` è autodescrittivo:

```
<filename>*<ext>*<size_bytes>*<id>_<timestamp>*SHA256*<hash>*<data_pubblicazione>
```

Da qui si ricavano dimensione e hash **prima** di scaricare: il client li usa per
verificare ogni file (su 206 allegati: 206/206 hash combacianti, nessun file troncato).

## Caveat noti

- **Server lento**: ~3-4 minuti per 54 dettagli con 5 worker; read-timeout sporadici.
  Il client ha `Retry(total=4, backoff 1.5)` su 429/5xx + timeout per richiesta.
- **Bandi riservati**: le gare con `VisQ=NO/RicQ=NO` spesso non hanno allegati pubblici
  ("procedura riservata ai soli operatori invitati"). 17 bandi su 54 hanno documenti.
- **Allegati duplicati**: alcuni file sono pubblicati due volte con descrizioni diverse
  (stesso SHA256) → su disco restano 203 file per 206 voci. Il dedup è per `TECHVALUE`.
- **Nome file come chiave**: due documenti diversi con lo stesso nome nello stesso bando
  si sovrascriverebbero; finora non accade (i doppioni sono lo stesso file).
- L'elenco può cambiare durante la giornata (nel test è passato da 54 a 56 bandi in un'ora).

## Test

`tests/test_parse.py` gira **offline** sulle fixture in `fixtures/` (elenco + scheda
613416): importi, date, parsing dei lotti, 7 allegati con hash a 64 char, dedup,
generazione del report. Serve a intercettare i cambi di markup del portale senza
picchiare il sito.

```bash
make test        # oppure: .venv/bin/python -m pytest -q
```

## Automazione

```cron
# ogni giorno alle 7:15, aggiorna JSON + CSV + report e gli allegati nuovi
15 7 * * * cd /percorso/bandi-start-toscana && make update >> update.log 2>&1
```

`download()` salta i file già presenti con la dimensione corretta, quindi il giro
giornaliero scarica solo le novità.

## Come si condivide

Il repo contiene **solo codice + dati piccoli** (`output/` ≈ 1 MB). Gli allegati
(~85 MB di PDF/DOCX che cambiano ogni giorno) stanno fuori dal versioning:
si rigenerano con `make update` e in CI vengono caricati come *artifact*.

```bash
git clone <repo> && cd bandi-start-toscana
make install && make test
make sync            # git pull + apre il report aggiornato (uso quotidiano)
make update          # solo se servono anche i PDF in locale
```

La GitHub Action `.github/workflows/update.yml` gira ogni mattina alle 07:15 CET
(e on demand): test → scrape → commit di `output/` → artifact `allegati`.
Chi ha clonato fa `git pull` e trova il report del giorno senza eseguire nulla.
Gli allegati di ogni run si scaricano dalla pagina *Actions → run → Artifacts*
(30 giorni di retention, così non si gonfia la storia git).

## Licenza

MIT (vedi `LICENSE`). I dati sono pubblici e di proprietà degli enti appaltanti: la
fonte è citata nel report. Non ricaricare il sito in modo aggressivo (5 worker sono
già abbastanza per non farsi notare).
