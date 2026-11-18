"""Test offline: i parser lavorano sulle fixture HTML salvate, senza rete."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from start_toscana import (
    Bando,
    StartToscanaClient,
    parse_data_it,
    parse_importo,
    render_html,
)

FIX = Path(__file__).resolve().parent.parent / "fixtures"
LISTING = (FIX / "listing.html").read_text(encoding="utf-8")
DETAIL = (FIX / "detail_613416.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------- helpers --
def test_parse_importo() -> None:
    assert parse_importo("39.900,00\u20ac") == 39900.00
    assert parse_importo("150.558.322,48\u20ac") == 150558322.48
    assert parse_importo("985,00\u20ac") == 985.00
    assert parse_importo("") is None
    assert parse_importo("senza importo") is None


def test_parse_data_it() -> None:
    assert parse_data_it("19/11/2026 10:00") == "2026-11-19T10:00:00"
    assert parse_data_it("20/11/2026 ore 13:00 [Ora Italiana]") == "2026-11-20T13:00:00"
    assert parse_data_it("nessuna data") is None


# ----------------------------------------------------------------- lista ---
def test_listing_parsing() -> None:
    bandi = StartToscanaClient._parse_listing(LISTING)
    assert len(bandi) >= 15, f"solo {len(bandi)} righe riconosciute"
    for b in bandi:
        assert b.id.isdigit()
        assert b.url_dettaglio.startswith("https://www.sanita.start.toscana.it/")
        assert b.descrizione
        assert b.importo is not None and b.importo > 0
        assert b.scadenza is not None
        dt.datetime.fromisoformat(b.scadenza)  # solleva se non è ISO
    # gli stati sono mappati dal prefisso in maiuscolo della descrizione
    assert {b.stato for b in bandi} <= {"Bando", "Rettificato", "Revocato", "Annullato", "Sospeso"}


def test_listing_importi_plausibili() -> None:
    bandi = StartToscanaClient._parse_listing(LISTING)
    assert max(b.importo for b in bandi) > 1_000_000  # il portale ha gare grosse
    assert sum(1 for b in bandi if b.importo > 0) == len(bandi)


# -------------------------------------------------------------- dettaglio --
@pytest.fixture(scope="module")
def bando_613416() -> Bando:
    b = Bando(
        id="613416",
        descrizione="ESTARSDAFA03PR25 FORNITURA DI FARMACI URGENTI",
        tipo="Bando",
        stato="Bando",
        ente_proponente="Regione Toscana",
        ente_appaltante="Regione Toscana",
        importo=1452000.0,
        importo_raw="1.452.000,00\u20ac",
        scadenza="2026-11-19T12:00:00",
        scadenza_raw="19/11/2026 12:00",
        url_dettaglio="https://www.sanita.start.toscana.it/portalegare/index.php/bandi",
    )
    StartToscanaClient._parse_detail(b, DETAIL)
    return b


def test_dettaglio_campi(bando_613416: Bando) -> None:
    b = bando_613416
    assert b.errore is None
    assert b.criterio == "Prezzo più basso"
    assert b.tipo_appalto == "Forniture"
    assert b.cpv and b.cpv.startswith("33690000")
    assert b.provincia == "Firenze"
    assert b.pec and "@" in b.pec
    assert b.scadenza_offerta == "2026-11-19T12:00:00"
    assert b.data_seduta is not None
    assert b.cig == "BD9E361B26"  # preso dai lotti


def test_dettaglio_lotti(bando_613416: Bando) -> None:
    assert len(bando_613416.lotti) == 3
    for lotto in bando_613416.lotti:
        assert lotto["Numero Lotto"].isdigit()
        assert lotto["CIG"]


def test_dettaglio_allegati(bando_613416: Bando) -> None:
    docs = bando_613416.documenti
    assert len(docs) == 7, [d.filename for d in docs]
    for d in docs:
        assert d.url.startswith("https://www.sanita.start.toscana.it/application/CTL_Library/")
        assert "DisplayAttach.ASP" in d.url
        assert d.size_bytes and d.size_bytes > 0
        assert d.sha256 and len(d.sha256) == 64
        assert d.descrizione and d.descrizione != d.filename
    assert docs[0].filename == "Determinazione_0001310_2026.pdf"
    assert docs[0].size_bytes == 365649
    # gesture legata al TECHVALUE: se cambia il formato il test lo segnala
    assert "*SHA256*" in docs[0].techvalue
    assert docs[0].techvalue.split("*")[2] == str(docs[0].size_bytes)


def test_allegati_dedup() -> None:
    """Techvalue identici non devono produrre allegati duplicati."""
    soup_html = DETAIL.replace("Determinazione_0001310_2026.pdf", "Determinazione_0001310_2026.pdf")
    b = Bando(
        id="x", descrizione="x", tipo="Bando", stato="Bando",
        ente_proponente="", ente_appaltante="", importo=0, importo_raw="",
        scadenza=None, scadenza_raw="", url_dettaglio="",
    )
    StartToscanaClient._parse_detail(b, soup_html)
    techvalues = [d.techvalue for d in b.documenti]
    assert len(techvalues) == len(set(techvalues))


# ------------------------------------------------------------------ report --
def test_render_html(tmp_path: Path, bando_613416: Bando) -> None:
    out = tmp_path / "report.html"
    render_html([bando_613416], out)
    testo = out.read_text(encoding="utf-8")
    assert "const D = [" in testo
    assert "Determinazione_0001310_2026.pdf" in testo
    assert "Bandi di gara aperti" in testo
    assert out.stat().st_size > 8000


def test_from_dict_roundtrip(bando_613416: Bando) -> None:
    """to_dict/from_dict devono conservare i campi usati dal report."""
    d = bando_613416.to_dict()
    b2 = Bando.from_dict(d)
    assert b2.id == d["id"]
    assert b2.importo == d["importo"]
    assert b2.criterio == d["criterio"]
    assert b2.cig == d["cig"]
    assert len(b2.lotti) == len(bando_613416.lotti)
    assert [x.filename for x in b2.documenti] == [x.filename for x in bando_613416.documenti]
    assert [x.sha256 for x in b2.documenti] == [x.sha256 for x in bando_613416.documenti]
