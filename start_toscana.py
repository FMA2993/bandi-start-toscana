"""
start_toscana.py — Client Python per il Portale Gare di START Toscana (Sanità).

Fonte: https://www.sanita.start.toscana.it/portalegare/index.php/bandi

Cosa fa:
  * elenca i bandi aperti (o scaduti) con importo, ente, scadenza
  * scarica il dettaglio di ogni bando: importo, criterio, tipo appalto, CIG, CPV,
    lotti, date (quesiti / offerte / seduta), indirizzo/PEC stazione appaltante
  * estrae gli allegati pubblici con URL diretto, dimensione e SHA256 dichiarati
  * scarica i PDF su disco

Endpoint scoperti (sito statico ASP, nessuna API JSON):
  GET /portalegare/index.php/bandi?&page=N                -> lista paginata (20/pagina)
  GET /portalegare/index.php/bandi?getdettaglio=yes&bando=<id>...  -> dettaglio
  GET /application/CTL_Library/functions/field/DisplayAttach.ASP
        ?OPERATION=DISPLAY&FIELD=Allegato&PATH=../&TECHVALUE=<token>&FORMAT=I,N
  GET /portale/rss_bandi.asp?...nPag=N                    -> feed RSS (54 record totali)

Uso:
  python start_toscana.py                          # lista bandi aperti (tabella)
  python start_toscana.py --json bandi.json        # + dump JSON completo con dettagli
  python start_toscana.py --min-importo 100000     # filtra per importo
  python start_toscana.py --ente ESTAR --download ./allegati
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, urljoin, urlsplit, parse_qs

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter

try:  # urllib3 v2 / v1 compat
    from urllib3.util.retry import Retry
except ImportError:  # pragma: no cover
    Retry = None  # type: ignore

BASE = "https://www.sanita.start.toscana.it"
LIST_PATH = "/portalegare/index.php/bandi"
ATTACH_PATH = "/application/CTL_Library/functions/field/DisplayAttach.ASP"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36"
)

_DATE_RE = re.compile(r"(\d{2})/(\d{2})/(\d{4})(?:\s+(?:ore\s+)?(\d{2}):(\d{2}))?")
_IMPORTO_RE = re.compile(r"([\d.]+),(\d{2})")
_STATO_RE = re.compile(r"Bando\s+(Rettificato|Revocato|Annullato|Sospeso)\s*-", re.I)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def parse_importo(text: str) -> float | None:
    """'39.900,00€' -> 39900.0 ; '150.558.322,48€' -> 150558322.48"""
    if not text:
        return None
    m = _IMPORTO_RE.search(text.replace("\xa0", " "))
    if not m:
        return None
    try:
        return float(m.group(1).replace(".", "") + "." + m.group(2))
    except ValueError:
        return None


def parse_data_it(text: str) -> str | None:
    """'19/11/2026 10:00' -> '2026-11-19T10:00:00' (ISO, ordinabile)."""
    m = _DATE_RE.search(text or "")
    if not m:
        return None
    d, mo, y, hh, mm = m.groups()
    return f"{y}-{mo}-{d}T{hh or '00'}:{mm or '00'}:00"


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()


def _session(timeout: int = 60) -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "user-agent": UA,
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "accept-language": "it-IT,it;q=0.9,en;q=0.8",
        }
    )
    if Retry is not None:
        retry = Retry(
            total=4,
            backoff_factor=1.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "HEAD"}),
            read=2,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_maxsize=16)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
    s.request_timeout = timeout  # type: ignore[attr-defined]
    return s


# --------------------------------------------------------------------------- #
# dataclasses
# --------------------------------------------------------------------------- #
@dataclass
class Documento:
    descrizione: str
    filename: str
    ext: str | None
    size_bytes: int | None
    sha256: str | None
    data_pubblicazione: str | None  # ISO
    techvalue: str
    url: str


@dataclass
class Bando:
    id: str
    descrizione: str
    tipo: str
    stato: str  # 'Bando' oppure 'Rettificato' / 'Revocato' ...
    ente_proponente: str
    ente_appaltante: str
    importo: float | None
    importo_raw: str
    scadenza: str | None  # ISO
    scadenza_raw: str
    url_dettaglio: str
    # popolati da detail()
    criterio: str | None = None
    tipo_appalto: str | None = None
    id_appalto_anac: str | None = None
    cig: str | None = None
    cpv: str | None = None
    provincia: str | None = None
    comune: str | None = None
    pec: str | None = None
    telefono: str | None = None
    data_pubblicazione: str | None = None
    scadenza_quesiti: str | None = None
    scadenza_offerta: str | None = None
    data_seduta: str | None = None
    lotti: list[dict[str, Any]] = field(default_factory=list)
    documenti: list[Documento] = field(default_factory=list)
    campi: dict[str, str] = field(default_factory=dict)
    errore: str | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Bando":
        """Ricostruisce un Bando da un record JSON (per rigenerare il report offline)."""
        doc_keys = (
            "descrizione",
            "filename",
            "ext",
            "size_bytes",
            "sha256",
            "data_pubblicazione",
            "techvalue",
            "url",
        )
        docs = [
            Documento(**{k: (x.get(k) or "") if k in ("descrizione", "filename", "techvalue", "url") else x.get(k) for k in doc_keys})
            for x in (d.get("documenti") or [])
        ]
        return cls(
            id=str(d.get("id", "")),
            descrizione=d.get("descrizione") or "",
            tipo=d.get("tipo") or "",
            stato=d.get("stato") or "Bando",
            ente_proponente=d.get("ente_proponente") or "",
            ente_appaltante=d.get("ente_appaltante") or "",
            importo=d.get("importo"),
            importo_raw=d.get("importo_raw") or "",
            scadenza=d.get("scadenza"),
            scadenza_raw=d.get("scadenza_raw") or "",
            url_dettaglio=d.get("url_dettaglio") or "",
            criterio=d.get("criterio"),
            tipo_appalto=d.get("tipo_appalto"),
            id_appalto_anac=d.get("id_appalto_anac"),
            cig=d.get("cig"),
            cpv=d.get("cpv"),
            provincia=d.get("provincia"),
            comune=d.get("comune"),
            pec=d.get("pec"),
            data_pubblicazione=d.get("data_pubblicazione"),
            scadenza_quesiti=d.get("scadenza_quesiti"),
            scadenza_offerta=d.get("scadenza_offerta"),
            data_seduta=d.get("data_seduta"),
            lotti=list(d.get("lotti") or []),
            documenti=docs,
            campi=dict(d.get("campi") or {}),
            errore=d.get("errore"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "descrizione": self.descrizione,
            "stato": self.stato,
            "tipo": self.tipo,
            "ente_proponente": self.ente_proponente,
            "ente_appaltante": self.ente_appaltante,
            "importo": self.importo,
            "scadenza": self.scadenza,
            "scadenza_raw": self.scadenza_raw,
            "criterio": self.criterio,
            "tipo_appalto": self.tipo_appalto,
            "cig": self.cig,
            "cpv": self.cpv,
            "provincia": self.provincia,
            "comune": self.comune,
            "pec": self.pec,
            "data_pubblicazione": self.data_pubblicazione,
            "scadenza_quesiti": self.scadenza_quesiti,
            "scadenza_offerta": self.scadenza_offerta,
            "data_seduta": self.data_seduta,
            "id_appalto_anac": self.id_appalto_anac,
            "num_lotti": len(self.lotti),
            "lotti": self.lotti,
            "documenti": [asdict(d) for d in self.documenti],
            "num_documenti": len(self.documenti),
            "url_dettaglio": self.url_dettaglio,
            "campi": self.campi,
            "errore": self.errore,
        }


# --------------------------------------------------------------------------- #
# client
# --------------------------------------------------------------------------- #
class StartToscanaClient:
    def __init__(self, timeout: int = 60, delay: float = 0.0):
        self.s = _session(timeout)
        self.timeout = timeout
        self.delay = delay

    # ---------------------------- lista ---------------------------------- #
    def listing_page(self, page: int = 1, scaduti: bool = False) -> str:
        params: dict[str, str] = {}
        if scaduti:
            params["scaduti"] = "yes"
        if page > 1:
            params["page"] = str(page)
        r = self.s.get(BASE + LIST_PATH, params=params, timeout=self.timeout)
        r.raise_for_status()
        return r.text

    def open_tenders(
        self,
        scaduti: bool = False,
        max_pages: int | None = None,
        stop_after_empty: int = 1,
    ) -> list[Bando]:
        """Elenca i bandi. Pagina finché trova righe (o fino a max_pages)."""
        out: list[Bando] = []
        seen: set[str] = set()
        page, empty_streak = 1, 0
        while True:
            if max_pages and page > max_pages:
                break
            try:
                html_text = self.listing_page(page, scaduti=scaduti)
            except requests.RequestException as exc:
                print(f"! pagina {page}: {exc}", file=sys.stderr)
                break
            batch = self._parse_listing(html_text)
            fresh = [b for b in batch if b.id not in seen]
            if not fresh:
                empty_streak += 1
                if empty_streak >= stop_after_empty:
                    break
            else:
                empty_streak = 0
            for b in fresh:
                seen.add(b.id)
                out.append(b)
            page += 1
            if self.delay:
                time.sleep(self.delay)
        return out

    @staticmethod
    def _parse_listing(html_text: str) -> list[Bando]:
        soup = BeautifulSoup(html_text, "html.parser")
        rows = soup.select("tr")
        bandi: list[Bando] = []
        for tr in rows:
            cells = tr.find_all("td", recursive=False)
            if len(cells) < 6:
                continue
            link = tr.find("a", href=re.compile(r"getdettaglio=yes"))
            if not link:
                continue
            href = html.unescape(link["href"])
            qs = parse_qs(urlsplit(href).query)
            bando_id = (qs.get("bando") or [""])[0]
            if not bando_id:
                continue
            descrizione = clean(cells[0].get_text(" ", strip=True))
            stato = "Bando"
            m = _STATO_RE.search(descrizione)
            if m:
                stato = m.group(1).capitalize()
            scadenza_raw = clean(cells[5].get_text(" ", strip=True))
            importo_raw = clean(cells[4].get_text(" ", strip=True))
            bandi.append(
                Bando(
                    id=bando_id,
                    descrizione=descrizione,
                    stato=stato,
                    tipo=clean(cells[1].get_text(" ", strip=True)),
                    ente_proponente=clean(cells[2].get_text(" ", strip=True)),
                    ente_appaltante=clean(cells[3].get_text(" ", strip=True)),
                    importo=parse_importo(importo_raw),
                    importo_raw=importo_raw,
                    scadenza=parse_data_it(scadenza_raw),
                    scadenza_raw=scadenza_raw,
                    url_dettaglio=BASE + href if href.startswith("/") else href,
                )
            )
        return bandi

    # --------------------------- dettaglio -------------------------------- #
    def detail(self, bando: Bando) -> Bando:
        try:
            r = self.s.get(bando.url_dettaglio, timeout=self.timeout)
            r.raise_for_status()
        except requests.RequestException as exc:
            bando.errore = f"dettaglio: {exc}"
            return bando
        self._parse_detail(bando, r.text)
        return bando

    @staticmethod
    def _parse_detail(b: Bando, html_text: str) -> None:
        soup = BeautifulSoup(html_text, "html.parser")

        # 1) tabella campi principali: quella che contiene "Descrizione breve"
        main_tbl = None
        for tbl in soup.find_all("table"):
            if "Descrizione breve" in tbl.get_text(" ", strip=True)[:2000]:
                main_tbl = tbl
                break
        rows_main = main_tbl.find_all("tr") if main_tbl else []
        for tr in rows_main:
            cells = tr.find_all(["th", "td"], recursive=False)
            if len(cells) < 2:
                continue
            k = clean(cells[0].get_text(" ", strip=True))
            v = clean(" ".join(c.get_text(" ", strip=True) for c in cells[1:]))
            if not k or k.lower() in {"descrizione", "allegato", "data pubblicazione"}:
                continue
            b.campi.setdefault(k, v)
        g = b.campi
        b.criterio = g.get("Criterio Aggiudicazione") or None
        b.tipo_appalto = g.get("Tipo Appalto") or None
        b.id_appalto_anac = g.get("Id Appalto ANAC") or None
        b.scadenza_quesiti = parse_data_it(g.get("Termine Richiesta Quesiti", ""))
        b.scadenza_offerta = parse_data_it(g.get("Presentare le offerte entro il", ""))
        if b.scadenza_offerta is None:
            for k, v in g.items():
                if "offerte entro" in k.lower():
                    b.scadenza_offerta = parse_data_it(v)
                    break
        b.data_seduta = parse_data_it(g.get("Data I Seduta", ""))

        # 2) tabella di indicizzazione (CPV, CIG, date, provincia...)
        for tbl in soup.select("table.list.infoIndicizzazione") or []:
            rows = tbl.find_all("tr")
            if len(rows) >= 3:
                heads = [clean(c.get_text(" ", strip=True)) for c in rows[1].find_all(["th", "td"])]
                vals = [clean(c.get_text(" ", strip=True)) for c in rows[2].find_all(["th", "td"])]
                idx = dict(zip(heads, vals))
                b.cpv = idx.get("Codice CPV") or b.cpv
                b.cig = idx.get("Codice CIG") or b.cig
                b.provincia = idx.get("Provincia Sede di Gara") or b.provincia
                b.comune = idx.get("Comune Sede di Gara") or b.comune
                b.data_pubblicazione = (
                    parse_data_it(idx.get("Data Pubblicazione", "")) or b.data_pubblicazione
                )
                if not b.scadenza:
                    b.scadenza = parse_data_it(idx.get("Data Scadenza Bando", ""))

        # 3) contatti stazione appaltante (footer)
        footer = soup.select_one("table.FooterBottom")
        if footer:
            ftxt = clean(footer.get_text(" ", strip=True))
            m = re.search(r"PEC\s+([^\s]+@[^\s]+)", ftxt, re.I)
            if m:
                b.pec = m.group(1).rstrip(".,;")
            m = re.search(r"Telefono\s*[–-]?\s*([+\d][\d\s/().-]{6,})", ftxt, re.I)
            if m:
                b.telefono = m.group(1).strip()

        # 4) lotti (CIG per lotto) - header può essere th o td
        for tbl in soup.find_all("table"):
            trs = tbl.find_all("tr")
            if not trs:
                continue
            heads = [
                clean(c.get_text(" ", strip=True))
                for c in trs[0].find_all(["th", "td"])
            ]
            if "Numero Lotto" in heads and "CIG" in heads:
                for tr in trs[1:]:
                    cells = [
                        clean(td.get_text(" ", strip=True)) for td in tr.find_all("td")
                    ]
                    if len(cells) >= 2 and cells[0].isdigit():
                        b.lotti.append(dict(zip(heads, cells)))
                break

        # CIG di comodo: se il bando non ne ha uno, usa quello del primo lotto
        if not b.cig and b.lotti:
            cigs = [l.get("CIG") for l in b.lotti if l.get("CIG")]
            if cigs:
                b.cig = cigs[0]
                b.campi.setdefault("CIG lotti", ", ".join(cigs))

        # 5) allegati pubblici
        b.documenti = StartToscanaClient._parse_allegati(soup)

    @staticmethod
    def _parse_allegati(soup: BeautifulSoup) -> list[Documento]:
        docs: list[Documento] = []
        for tbl in soup.find_all("table"):
            trs = tbl.find_all("tr")
            if not trs:
                continue
            # l'header può essere fatto di <th> oppure di <td>: usiamo la prima riga
            head_txt = " ".join(
                clean(c.get_text(" ", strip=True))
                for c in trs[0].find_all(["th", "td"])
            ).lower()
            if "allegato" not in head_txt:
                continue
            for tr in tbl.find_all("tr"):
                inp = tr.find("input", attrs={"name": "Allegato"})
                if not inp or not inp.get("value"):
                    continue
                techvalue = html.unescape(inp["value"]).strip()
                parts = techvalue.split("*")
                filename = parts[0] if parts else "allegato"
                ext = parts[1] if len(parts) > 1 else None
                size = None
                if len(parts) > 2 and parts[2].isdigit():
                    size = int(parts[2])
                sha = None
                for i, p in enumerate(parts[:-1]):
                    if p.upper() == "SHA256":
                        sha = parts[i + 1]
                        break
                doc_date = None
                for p in parts:
                    iso = parse_data_it(p)
                    if iso:
                        doc_date = iso
                        break
                if not doc_date:
                    for p in parts:
                        m_iso = re.search(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})", p)
                        if m_iso:
                            doc_date = (
                                f"{m_iso.group(1)}-{m_iso.group(2)}-{m_iso.group(3)}"
                                f"T{m_iso.group(4)}:{m_iso.group(5)}:00"
                            )
                            break
                # mappa colonne -> testo, così la descrizione non si confonde
                # con la data (nel blocco "Chiarimenti" la prima colonna è la data)
                heads = [
                    clean(c.get_text(" ", strip=True))
                    for c in trs[0].find_all(["th", "td"])
                ]
                cells = [
                    clean(td.get_text(" ", strip=True)) for td in tr.find_all("td")
                ]
                row = dict(zip(heads, cells))
                descr = ""
                for hname, htext in row.items():
                    if hname.lower().startswith("descrizion") and htext and htext != filename:
                        descr = htext
                        break
                if not descr:
                    for td in tr.find_all("td"):
                        t = clean(td.get_text(" ", strip=True))
                        if t and t != filename and not _DATE_RE.search(t):
                            descr = t
                            break
                # URL: ricostruito dalla onclick reale (rispetta PATH/OPERATION)
                url = ""
                label = tr.find("label", attrs={"onclick": True})
                if label:
                    m = re.search(
                        r"window\.open\('(?P<pre>[^']*)'\s*\+\s*escape\('(?P<tv>[^']*)'\)"
                        r"\s*\+\s*'(?P<post>[^']*)'",
                        label["onclick"],
                    )
                    if m:
                        url = (
                            html.unescape(m.group("pre"))
                            + quote(m.group("tv"), safe="")
                            + html.unescape(m.group("post"))
                        )
                if not url:
                    url = (
                        BASE
                        + ATTACH_PATH
                        + "?OPERATION=DISPLAY&FIELD=Allegato&PATH=../&TECHVALUE="
                        + quote(techvalue, safe="")
                        + "&FORMAT=I%2CN"
                    )
                docs.append(
                    Documento(
                        descrizione=descr,
                        filename=filename,
                        ext=ext,
                        size_bytes=size,
                        sha256=sha,
                        data_pubblicazione=doc_date,
                        techvalue=techvalue,
                        url=url,
                    )
                )
        # dedupe per techvalue (a volte la stessa riga compare 2 volte)
        seen, uniq = set(), []
        for d in docs:
            if d.techvalue in seen:
                continue
            seen.add(d.techvalue)
            uniq.append(d)
        return uniq

    # ------------------------- orchestrazione ----------------------------- #
    def enrich(
        self,
        bandi: Iterable[Bando],
        workers: int = 4,
        only_with_docs: bool = False,
    ) -> list[Bando]:
        bandi = list(bandi)

        def work(b: Bando) -> Bando:
            self.detail(b)
            if self.delay:
                time.sleep(self.delay)
            return b

        with ThreadPoolExecutor(max_workers=workers) as ex:
            out = list(ex.map(work, bandi))
        if only_with_docs:
            out = [b for b in out if b.documenti]
        return out

    # ---------------------------- download -------------------------------- #
    def download(self, doc: Documento, dest: Path) -> Path:
        dest.mkdir(parents=True, exist_ok=True)
        path = dest / doc.filename
        if path.exists() and doc.size_bytes and path.stat().st_size == doc.size_bytes:
            return path
        r = self.s.get(doc.url, timeout=max(self.timeout, 120), stream=True)
        r.raise_for_status()
        tmp = path.with_suffix(path.suffix + ".part")
        with tmp.open("wb") as fh:
            for chunk in r.iter_content(65536):
                fh.write(chunk)
        tmp.replace(path)
        if doc.size_bytes and path.stat().st_size != doc.size_bytes:
            raise IOError(
                f"{doc.filename}: attesi {doc.size_bytes} byte, ricevuti {path.stat().st_size}"
            )
        return path


_CSV_COLS = [
    "id",
    "scadenza",
    "importo",
    "stato",
    "tipo",
    "ente_appaltante",
    "criterio",
    "cpv",
    "cig",
    "num_documenti",
    "descrizione",
    "url_dettaglio",
]


def _write_csv(rows: list[dict[str, Any]], path: str | Path) -> Path:
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=_CSV_COLS, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow(row)
    return Path(path)


# --------------------------------------------------------------------------- #
# report HTML (dashboard offline, apribile con doppio clic)
# --------------------------------------------------------------------------- #
_HTML_HEAD = """<!doctype html>
<html lang="it"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
  *{box-sizing:border-box}
  html{background:#111315}
  body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
       color:#e6e6e6;background:#111315;font-size:13.5px;margin:0;padding:18px 22px 40px;line-height:1.4}
  h1{font-size:19px;margin:0 0 2px;font-weight:700;letter-spacing:-.01em}
  .sub{color:#8b9299;font-size:12px;margin-bottom:16px}
  .kpis{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:16px}
  .kpi{border:1px solid #2a2f34;border-radius:10px;padding:9px 13px;min-width:106px;background:#181b1e}
  .kpi .v{font-size:20px;font-weight:700;line-height:1.05}
  .kpi .l{font-size:10px;color:#8b9299;text-transform:uppercase;letter-spacing:.05em;margin-top:3px}
  .bar{display:flex;gap:8px;align-items:center;margin-bottom:10px;flex-wrap:wrap}
  input[type=search]{background:#181b1e;border:1px solid #2a2f34;color:#e6e6e6;border-radius:8px;
    padding:7px 11px;font:inherit;min-width:260px;outline:none}
  input[type=search]:focus{border-color:#4b6a8a}
  button{background:#181b1e;border:1px solid #2a2f34;color:#9aa3ab;border-radius:20px;padding:5px 12px;
    font:inherit;font-size:11.5px;cursor:pointer}
  button.on{color:#111315;border-color:#9fb6cc;background:#9fb6cc;font-weight:700}
  .hint{color:#8b9299;font-size:11.5px;margin-left:auto}
  table{border-collapse:collapse;width:100%}
  th{text-align:left;font-size:10px;text-transform:uppercase;letter-spacing:.06em;color:#8b9299;
     font-weight:700;padding:7px 8px;border-bottom:1px solid #2a2f34;cursor:pointer;white-space:nowrap;
     position:sticky;top:0;background:#111315;z-index:2}
  th:hover{color:#e6e6e6}
  td{padding:7px 8px;border-bottom:1px solid #1d2124;vertical-align:top}
  tr.r{cursor:pointer}
  tr.r:hover td{background:#171b1e}
  tr.r.open td{background:#1b2024}
  .num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
  .scad{white-space:nowrap;font-variant-numeric:tabular-nums}
  .gg{display:inline-block;min-width:34px;text-align:center;border-radius:5px;padding:1px 5px;font-size:10.5px;
      font-weight:700;margin-left:6px;background:#22272b;color:#9aa3ab}
  .gg.hot{background:#8c1d1d;color:#fff}
  .gg.soon{background:#8a5209;color:#fff}
  .og{max-width:520px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
  .ente{color:#9aa3ab;font-size:11.5px}
  .tag{font-size:9.5px;border-radius:4px;padding:1px 5px;margin-left:6px;white-space:nowrap;
       text-transform:uppercase;letter-spacing:.04em;font-weight:700}
  .tag.rett{background:#4a3708;color:#f0c674}
  .tag.rev{background:#4a1416;color:#f08a8a}
  .chev{color:#5d666e;font-size:10px;display:inline-block;width:10px}
  .det{background:#0d0f11}
  .det table{width:auto;min-width:60%}
  .det td{border-bottom:1px dotted #22272b;padding:5px 10px 5px 0}
  .det .lbl{color:#9aa3ab;min-width:190px}
  a{color:#9fb6cc;text-decoration:none}
  a:hover{text-decoration:underline}
  .docrow{display:flex;gap:10px;align-items:baseline;border-bottom:1px dotted #22272b;padding:4px 0}
  .docrow .fn{min-width:320px}
  .docrow .meta{color:#7d858c;font-size:11px}
  .none{color:#9aa3ab;font-style:italic}
  .foot{margin-top:14px;font-size:11px;color:#7d858c;line-height:1.6}
  .legend{font-size:11px;color:#8b9299;margin:2px 0 12px;line-height:1.7}
  .legend b{color:#e6e6e6}
</style></head><body>
<h1>__TITLE__</h1>
<div class="sub">__SUBTITLE__</div>
<div class="kpis">__KPIS__</div>
<div class="legend"><b>T-3</b> = giorni che mancano alla scadenza ·
<b>clicca una riga</b> per vedere e aprire gli allegati ·
<i>revocato / rettificato</i> = bando annullato o modificato</div>
<div class="bar">
  <input type="search" id="q" placeholder="cerca per oggetto, ente, CIG, CPV, numero bando…">
  <button data-f="doc">solo con allegati</button>
  <button data-f="hot">scade entro 3 giorni</button>
  <button data-f="big">oltre 1 milione €</button>
  <span class="hint" id="cnt"></span>
</div>
<table><thead><tr>
  <th data-s="scadenza">Scadenza</th>
  <th data-s="importo" class="num">Importo</th>
  <th data-s="ente_appaltante">Ente</th>
  <th data-s="descrizione">Oggetto</th>
  <th data-s="num_lotti" class="num">Lotti</th>
  <th data-s="num_documenti" class="num">All.</th>
</tr></thead><tbody id="tb"></tbody></table>
<div class="foot">__FOOT__</div>
"""

_HTML_SCRIPT = """
<script>
const D = __DATA__;
let f = new Set(), sortKey = "scadenza", asc = true;

function eur(v){ if(v==null||v===0) return "&mdash;";
  return "&euro;&nbsp;" + v.toLocaleString("it-IT",{minimumFractionDigits:2,maximumFractionDigits:2}); }
function kb(n){ if(!n) return ""; return n>1048576 ? (n/1048576).toFixed(1)+" MB" : Math.round(n/1024)+" KB"; }
function dt(s){ if(!s) return "&mdash;"; const p=s.split("T"); const d=p[0].split("-");
  return d[2]+"/"+d[1]+"/"+d[0]+(p[1]?" ore "+p[1].slice(0,5):""); }
function esc(s){ return (s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;"); }

function docRow(b, x){
  // se il file è già scaricato apre la copia locale, altrimenti il documento sul portale
  const href = x.local ? x.local : x.url;
  const extra = x.local ? '<a class="meta" href="' + x.url + '" target="_blank">&darr; portale</a>' : '';
  return '<div class="docrow"><span class="fn">'
       + '<a href="' + href + '" target="_blank">' + esc(x.filename) + '</a></span>'
       + '<span class="meta">' + esc(x.descrizione || x.ext || '') + '</span>'
       + '<span class="meta">' + kb(x.size_bytes) + '</span>'
       + extra + '</div>';
}

function detail(b){
  let h = '<div style="padding:6px 0 10px">';
  if(b.documenti.length){
    h += '<div style="font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:#8b9299;'
       + 'margin-bottom:6px">Allegati ufficiali (' + b.documenti.length + ')</div>';
    h += b.documenti.map(x => docRow(b,x)).join("");
  } else {
    h += '<div class="none">Nessun allegato pubblico: procedura riservata agli operatori invitati.</div>';
  }
  const info = [];
  if(b.criterio) info.push(["Criterio", b.criterio]);
  if(b.tipo_appalto) info.push(["Tipo appalto", b.tipo_appalto]);
  if(b.cig) info.push(["CIG", b.cig]);
  if(b.cpv) info.push(["CPV", b.cpv]);
  if(b.scadenza_quesiti) info.push(["Quesiti entro", dt(b.scadenza_quesiti)]);
  if(b.scadenza_offerta) info.push(["Offerte entro", dt(b.scadenza_offerta)]);
  if(b.data_seduta) info.push(["Prima seduta", dt(b.data_seduta)]);
  if(b.pec) info.push(["PEC ente", b.pec]);
  info.push(["Scheda sul portale", '<a href="' + b.url_dettaglio + '" target="_blank">apri &nearr;</a>']);
  if(info.length){
    h += '<table style="margin-top:10px">' + info.map(r =>
      '<tr><td class="lbl">' + r[0] + '</td><td>' + r[1] + '</td></tr>').join("") + '</table>';
  }
  return h + '</div>';
}

function render(){
  let rows = D.filter(function(b){
    if(f.has("doc") && !b.num_documenti) return false;
    if(f.has("hot") && !(b.giorni!==null && b.giorni<=3)) return false;
    if(f.has("big") && !(b.importo && b.importo>1e6)) return false;
    const q = document.getElementById("q").value.trim().toLowerCase();
    if(!q) return true;
    return (b.descrizione+" "+b.ente_appaltante+" "+(b.cig||"")+" "+(b.cpv||"")+" "+b.id)
           .toLowerCase().includes(q);
  });
  rows.sort(function(a,b){
    const A=a[sortKey], B=b[sortKey];
    if(typeof A==="number" && typeof B==="number") return asc?A-B:B-A;
    return asc ? String(A||"").localeCompare(String(B||"")) : String(B||"").localeCompare(String(A||""));
  });
  let out = "";
  rows.forEach(function(b, i){
    const g = b.giorni;
    const cls = (g===null) ? "" : (g<=1 ? "hot" : (g<=3 ? "soon" : ""));
    const badge = (g===null) ? "" : '<span class="gg ' + cls + '">' + (g<0 ? "scad." : "T-"+g) + '</span>';
    const tag = (b.stato && b.stato!=="Bando")
      ? '<span class="tag ' + (b.stato==="Revocato" ? "rev" : "rett") + '">' + b.stato + '</span>' : '';
    out += '<tr class="r" data-i="' + i + '">'
      + '<td class="scad"><span class="chev">&#9656;</span> ' + dt(b.scadenza) + badge + '</td>'
      + '<td class="num">' + eur(b.importo) + '</td>'
      + '<td><span class="ente">' + esc(b.ente_appaltante.split(" - ")[0]) + '</span></td>'
      + '<td><div class="og">' + esc(b.descrizione) + '</div>' + tag + '</td>'
      + '<td class="num">' + (b.num_lotti||"&mdash;") + '</td>'
      + '<td class="num">' + (b.num_documenti||"0") + '</td>'
      + '</tr>'
      + '<tr class="det" id="d'+i+'" style="display:none"><td colspan="6">' + detail(b) + '</td></tr>';
  });
  document.getElementById("tb").innerHTML = out;
  document.getElementById("cnt").textContent = rows.length + " bandi";
  document.querySelectorAll("tr.r").forEach(function(tr){
    tr.addEventListener("click", function(ev){
      if(ev.target.tagName === "A") return;
      const i = tr.dataset.i, d = document.getElementById("d"+i);
      const open = d.style.display !== "none";
      d.style.display = open ? "none" : "table-row";
      tr.classList.toggle("open", !open);
      tr.querySelector(".chev").innerHTML = open ? "&#9656;" : "&#9662;";
    });
  });
}
document.getElementById("q").addEventListener("input", render);
document.querySelectorAll("button[data-f]").forEach(function(btn){
  btn.addEventListener("click", function(){
    const k = btn.dataset.f;
    if(f.has(k)){ f.delete(k); btn.classList.remove("on"); } else { f.add(k); btn.classList.add("on"); }
    render();
  });
});
document.querySelectorAll("th[data-s]").forEach(function(th){
  th.addEventListener("click", function(){
    const k = th.dataset.s;
    if(k===sortKey){ asc = !asc; } else { sortKey = k; asc = true; }
    render();
  });
});
render();
</script>
</body></html>
"""


def render_html(
    bandi: list["Bando"],
    out_path: Path | str,
    allegati_dir: Path | str | None = None,
    title: str = "Bandi di gara aperti",
    subtitle: str = "",
) -> Path:
    """Scrive una dashboard HTML autonoma (nessuna dipendenza esterna)."""
    import datetime as _dt

    out_path = Path(out_path)
    adir = Path(allegati_dir) if allegati_dir else None
    now = _dt.datetime.now()
    rows = []
    for b in bandi:
        giorni = None
        if b.scadenza:
            try:
                giorni = (_dt.datetime.fromisoformat(b.scadenza) - now).days
            except ValueError:
                giorni = None
        docs = []
        for d in b.documenti:
            local = None
            if adir is not None:
                p = adir / b.id / d.filename
                if p.exists():
                    try:
                        local = os.path.relpath(
                            Path(p).resolve(), out_path.parent.resolve()
                        ).replace("\\", "/")
                    except ValueError:
                        local = str(Path(p).resolve())
            docs.append(
                {
                    "descrizione": d.descrizione,
                    "filename": d.filename,
                    "ext": d.ext,
                    "size_bytes": d.size_bytes,
                    "sha256": d.sha256,
                    "url": d.url,
                    "local": local,
                }
            )
        rows.append(
            {
                "id": b.id,
                "descrizione": b.descrizione,
                "stato": b.stato,
                "ente_appaltante": b.ente_appaltante,
                "importo": b.importo,
                "scadenza": b.scadenza,
                "giorni": giorni,
                "num_lotti": len(b.lotti),
                "num_documenti": len(docs),
                "criterio": b.criterio,
                "tipo_appalto": b.tipo_appalto,
                "cig": b.cig,
                "cpv": b.cpv,
                "scadenza_quesiti": b.scadenza_quesiti,
                "scadenza_offerta": b.scadenza_offerta,
                "data_seduta": b.data_seduta,
                "pec": b.pec,
                "url_dettaglio": b.url_dettaglio,
                "documenti": docs,
            }
        )
    rows.sort(key=lambda r: (r["scadenza"] or "9999"))

    tot_imp = sum(r["importo"] or 0 for r in rows)
    tot_doc = sum(r["num_documenti"] for r in rows)
    urgenti = sum(1 for r in rows if r["giorni"] is not None and 0 <= r["giorni"] <= 2)

    def kpi(v: str, label: str) -> str:
        return f'<div class="kpi"><div class="v">{v}</div><div class="l">{label}</div></div>'

    kpis = "".join(
        [
            kpi(str(len(rows)), "bandi aperti"),
            kpi(f"€ {tot_imp/1e6:,.1f} M".replace(",", "."), "valore totale"),
            kpi(str(tot_doc), "allegati"),
            kpi(str(urgenti), "scadono entro 48h"),
            kpi(str(sum(1 for r in rows if r["num_lotti"])), "con lotti"),
        ]
    )

    head = (
        _HTML_HEAD.replace("__TITLE__", html.escape(title))
        .replace("__SUBTITLE__", subtitle or f"Fonte: portale START Toscana · aggiornato il {now:%d/%m/%Y alle %H:%M}")
        .replace("__KPIS__", kpis)
        .replace(
            "__FOOT__",
            "Fonte: <b>sanita.start.toscana.it/portalegare</b> · "
            "gli allegati sono quelli pubblicati dalla stazione appaltante · "
            f"dati letti il {now:%d/%m/%Y %H:%M} · "
            "i bandi marcati <i>revocato</i> non sono più validi.",
        )
    )
    script = _HTML_SCRIPT.replace("__DATA__", json.dumps(rows, ensure_ascii=False))
    out_path.write_text(head + script, encoding="utf-8")
    return out_path


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Bandi gare START Toscana -> JSON/CSV/PDF")
    ap.add_argument("--scaduti", action="store_true", help="usa l'elenco dei bandi scaduti")
    ap.add_argument("--max-pages", type=int, default=None, help="limite pagine lista")
    ap.add_argument("--limit", type=int, default=None, help="max bandi da dettagliare")
    ap.add_argument("--min-importo", type=float, default=None)
    ap.add_argument("--max-importo", type=float, default=None)
    ap.add_argument("--ente", default=None, help="filtro substring su ente appaltante/proponente")
    ap.add_argument("--testo", default=None, help="filtro substring sulla descrizione")
    ap.add_argument("--solo-con-documenti", action="store_true")
    ap.add_argument("--no-detail", action="store_true", help="solo lista, nessun dettaglio")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--timeout", type=int, default=60)
    ap.add_argument("--json", dest="json_out", default=None, help="scrivi JSON qui")
    ap.add_argument("--csv", dest="csv_out", default=None, help="scrivi CSV qui")
    ap.add_argument("--html", dest="html_out", default=None, help="scrivi la dashboard HTML qui")
    ap.add_argument("--download", default=None, help="cartella dove scaricare gli allegati")
    ap.add_argument(
        "--from-json",
        dest="from_json",
        default=None,
        help="rigenera report/CSV da un JSON già salvato, senza toccare la rete",
    )
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)

    if a.from_json:
        raw = json.loads(Path(a.from_json).read_text(encoding="utf-8"))
        bandi = [Bando.from_dict(x) for x in raw]
        if not a.quiet:
            print(f"→ {len(bandi)} bandi da {a.from_json} (offline)", file=sys.stderr)
        if a.html_out:
            render_html(bandi, a.html_out, allegati_dir=a.download)
            if not a.quiet:
                print(f"✓ HTML -> {a.html_out}", file=sys.stderr)
        if a.csv_out:
            _write_csv([b.to_dict() for b in bandi], a.csv_out)
            if not a.quiet:
                print(f"✓ CSV -> {a.csv_out}", file=sys.stderr)
        if a.json_out:
            Path(a.json_out).write_text(
                json.dumps([b.to_dict() for b in bandi], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            if not a.quiet:
                print(f"✓ JSON -> {a.json_out}", file=sys.stderr)
        return 0

    client = StartToscanaClient(timeout=a.timeout)
    if not a.quiet:
        print("→ lettura elenco bandi...", file=sys.stderr)
    bandi = client.open_tenders(scaduti=a.scaduti, max_pages=a.max_pages)
    if not a.quiet:
        print(f"  {len(bandi)} bandi in elenco", file=sys.stderr)

    # filtri cheap prima del dettaglio
    if a.min_importo is not None:
        bandi = [b for b in bandi if b.importo is not None and b.importo >= a.min_importo]
    if a.max_importo is not None:
        bandi = [b for b in bandi if b.importo is not None and b.importo <= a.max_importo]
    if a.ente:
        needle = a.ente.lower()
        bandi = [
            b
            for b in bandi
            if needle in b.ente_appaltante.lower() or needle in b.ente_proponente.lower()
        ]
    if a.testo:
        needle = a.testo.lower()
        bandi = [b for b in bandi if needle in b.descrizione.lower()]
    if a.limit:
        bandi = bandi[: a.limit]

    if not a.no_detail and bandi:
        if not a.quiet:
            print(f"→ dettaglio di {len(bandi)} bandi ({a.workers} worker)...", file=sys.stderr)
        bandi = client.enrich(
            bandi, workers=a.workers, only_with_docs=a.solo_con_documenti
        )

    # output
    payload = [b.to_dict() for b in bandi]
    if a.json_out:
        Path(a.json_out).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if not a.quiet:
            print(f"✓ JSON -> {a.json_out}", file=sys.stderr)
    if a.csv_out:
        _write_csv(payload, a.csv_out)
        if not a.quiet:
            print(f"✓ CSV -> {a.csv_out}", file=sys.stderr)
    if a.download:
        dest = Path(a.download)
        ok = fail = 0
        for b in bandi:
            for doc in b.documenti:
                try:
                    client.download(doc, dest / b.id)
                    ok += 1
                except Exception as exc:  # noqa: BLE001
                    fail += 1
                    print(f"! {b.id}/{doc.filename}: {exc}", file=sys.stderr)
        if not a.quiet:
            print(f"✓ allegati: {ok} scaricati, {fail} falliti -> {dest}/", file=sys.stderr)

    if a.html_out:
        render_html(bandi, a.html_out, allegati_dir=a.download)
        if not a.quiet:
            print(f"✓ HTML -> {a.html_out}", file=sys.stderr)

    if not (a.json_out or a.csv_out or a.html_out):
        # stampa tabella
        fmt = "{:<8} {:<11} {:>13} {:<10} {:<40} {}"
        print(fmt.format("ID", "SCADENZA", "IMPORTO", "ALLEGATI", "ENTE", "OGGETTO"))
        for b in sorted(bandi, key=lambda x: (x.scadenza or "9999", -(x.importo or 0))):
            print(
                fmt.format(
                    b.id,
                    (b.scadenza or b.scadenza_raw)[:10],
                    f"{b.importo:,.2f}" if b.importo else "-",
                    str(len(b.documenti)),
                    b.ente_appaltante[:40],
                    b.descrizione[:80],
                )
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
