"""Labels: party, self-funding, contribution kind, conduits."""
import datetime as dt
import io
import zipfile

import duckdb
import pytest
import responses

from candidate_modeling import elections, labels, pipeline, store
from candidate_modeling.sources import fec
from candidate_modeling.sources.states import texas
from tests.conftest import TX_CONTRIB_HEADER, TX_FILER_HEADER, _csv_bytes, tx_contrib


# ---------- party ----------

@pytest.mark.parametrize("raw,want", [
    ("DEM", "democratic"), ("Democratic", "democratic"), ("Democratic Party", "democratic"), ("Democrat", "democratic"),
    ("REP", "republican"), ("Republican Party", "republican"), ("GOP", "republican"), ("LIB", "libertarian"),
    ("Green Party", "green"), ("D/R", "cross_filed"), ("Non-Partisan", "nonpartisan"),
    ("No Party Affiliation", "nonpartisan"), ("U.S. Taxpayers Party", "other"), ("", "unknown"), (None, "unknown"),
])
def test_party_normalization_python_and_sql_agree(raw, want):
    assert labels.normalize_party(raw) == want
    v = "NULL" if raw is None else labels.sql_str(raw)
    assert duckdb.connect().execute(f"SELECT {labels.party_sql(v)}").fetchone()[0] == want


@pytest.mark.parametrize("party,kind,name,want", [
    ("DEM", "candidate", "Friends of X", "democratic"),
    (None, "candidate", "Abbott, Greg", "unknown"),                     # TX: candidate with no party on file
    (None, "pac", "Texans for Lawsuit Reform PAC", "not_applicable"),
    (None, "party_committee", "Harris County Democratic Party", "democratic"),
    (None, "party_committee", "Republican Party of Texas", "republican"),
])
def test_committee_party(party, kind, name, want):
    p = "NULL" if party is None else labels.sql_str(party)
    sql = labels.committee_party_sql(p, labels.sql_str(kind), labels.sql_str(name))
    assert duckdb.connect().execute(f"SELECT {sql}").fetchone()[0] == want


# ---------- self-funding ----------

@pytest.mark.parametrize("name,want", [
    ("James Talarico", ("James", "Talarico")), ("Talarico, James (The Honorable)", ("James", "Talarico")),
    ("Jackson, Richard L.", ("Richard", "Jackson")), ("Martin Luther King Jr.", ("Martin", "King")), (None, (None, None)),
])
def test_first_last(name, want):
    assert labels.first_last(name) == want


@pytest.mark.parametrize("donor,last,first,want", [
    ("JACKSON, RICHARD", "Jackson", "Richard L.", True),
    ("ABBOTT, GREGORY", "Abbott", "Greg", True),            # 4-letter prefix: GREG -> GREGORY
    ("JOSEPH SHAPIRO", "Shapiro", "Josh", False),            # JOSH != JOSE
    ("SHAPIRO, LORI", "Shapiro", "Josh", False),             # relative
    ("JOHN JAMES", "James", "John", True),
    ("JAMES SMITH", "James", "John", False),
    ("ITOH, KEN", "Ito", "Ken", False),                      # surname must be a whole word
    ("BURT JONES", "Jones", "William", False),               # nickname on one side: missed (documented floor)
    (None, "Jones", "William", False),
])
def test_self_match(donor, last, first, want):
    c = duckdb.connect()
    labels.install_macros(c)
    d = "NULL" if donor is None else labels.sql_str(donor)
    assert c.execute(f"SELECT self_match({d}, {labels.sql_str(last)}, {labels.sql_str(first)})").fetchone()[0] is want


# ---------- contribution kind (FEC) ----------

@pytest.mark.parametrize("row,want", [
    ({"line_number": "11AI", "contribution_receipt_amount": 100}, "monetary"),
    ({"line_number": "11AI", "receipt_type_full": "IN-KIND CONTRIBUTION", "contribution_receipt_amount": 100}, "in_kind"),
    ({"line_number": "12", "contribution_receipt_amount": 5000}, "transfer"),
    ({"line_number": "13A", "contribution_receipt_amount": 50000}, "loan"),
    ({"line_number": "15", "contribution_receipt_amount": 20}, "other_receipt"),
    ({"line_number": "11AI", "contribution_receipt_amount": -50}, "return"),
])
def test_fec_receipt_kind(row, want):
    assert fec.receipt_kind(row) == want


def test_fec_candidate_entity_is_a_person():
    n = fec.normalize({"sub_id": 1, "entity_type": "CAN", "contributor_name": "TALARICO, JAMES",
                       "line_number": "11D", "contribution_receipt_amount": 1000}, "talarico")
    assert n["contributor_type"] == "individual" and n["contribution_kind"] == "monetary"


def test_loans_and_other_receipts_never_counted():
    pipeline.add_candidate("C", slug="c1", state="TX")
    base = {"candidate_slug": "c1", "source": "fec", "jurisdiction": "federal", "committee_id": "C1",
            "committee_name": "X", "date": "2026-01-01", "contributor_name": "D", "contributor_type": "individual",
            "city": None, "state": "TX", "zip": None, "employer": None, "occupation": None, "is_memo": False,
            "cycle": 2026}
    with store.connect() as con:
        store.upsert_contributions(con, [
            {**base, "transaction_id": "1", "amount": 100.0, "contribution_kind": "monetary"},
            {**base, "transaction_id": "2", "amount": 9999.0, "contribution_kind": "loan"},
            {**base, "transaction_id": "3", "amount": 50.0, "contribution_kind": "other_receipt"},
            {**base, "transaction_id": "4", "amount": 30.0, "contribution_kind": "in_kind"}])
        assert con.execute("SELECT sum(amount) FROM contributions_clean").fetchone()[0] == 130.0
    s = pipeline.finance_summary("c1")
    assert s["labels"]["loans"]["loans_total"] == 9999.0
    assert {k["kind"]: k["total"] for k in s["labels"]["by_kind"]} == {"monetary": 100.0, "in_kind": 30.0}


# ---------- statewide labels end to end (Texas fixture + extra 2027 rows) ----------

@pytest.fixture
def tx_labeled(tx_zip):
    src = zipfile.ZipFile(io.BytesIO(tx_zip))
    filers = src.read("filers.csv") + _csv_bytes(TX_FILER_HEADER, [
        {"filerIdent": "00059791", "filerTypeCd": "GPAC", "filerName": "ActBlue Texas"}]).split(b"\n", 1)[1]
    extra = _csv_bytes(TX_CONTRIB_HEADER, [
        tx_contrib(10, filer="00059791", amount="50.00", contributionDt="20270201"),                 # conduit
        tx_contrib(11, amount="25.00", contributionDt="20270201", contributorNameLast="Talarico",
                   contributorNameFirst="James"),                                                     # self
        tx_contrib(12, amount="40.00", contributionDt="20270201", schedFormTypeCd="A2"),              # in-kind
        tx_contrib(13, amount="10.00", contributionDt="20270201", contributorNameLast="Talarico",
                   contributorNameFirst="Jane"),                                                      # relative
    ])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for n in src.namelist():
            zf.writestr(n, filers if n == "filers.csv" else src.read(n))
        zf.writestr("contribs_03.csv", extra)
    with responses.RequestsMock() as rs:
        rs.head(texas.ZIP_URL, headers={"Last-Modified": "v1"})
        rs.get(texas.ZIP_URL, body=buf.getvalue())
        texas.sync()


def test_statewide_label_metrics(tx_labeled):
    y = elections.compare_elections([2027], state="TX", today=dt.date(2028, 1, 1))["years"][0]
    assert y["total"] == 125.0
    assert (y["conduit_recipient_total"], y["total_excluding_conduits"]) == (50.0, 75.0)
    assert y["self_funding_total"] == 25.0 and y["pct_self_funded"] == 20.0     # Jane Talarico is not self
    assert y["in_kind_total"] == 40.0


def test_group_by_party_texas_is_unknown_for_candidates(tx_labeled):
    r = elections.compare_elections([2027], state="TX", group_by="party", today=dt.date(2028, 1, 1))
    by = {g["party"]: g["years"][2027]["total"] for g in r["by_party"]}
    assert by == {"unknown": 75.0, "not_applicable": 50.0}
    assert any("no party" in n for n in r["notes"])


def test_profile_self_funding_and_party(tx_labeled):
    pipeline.add_candidate("James Talarico", slug="talarico", state="TX", party="Democratic",
                           state_ids={"TX": ["00081913"]})
    pipeline.fetch_finances("talarico")
    s = pipeline.finance_summary("talarico")
    assert s["labels"]["party"] == "democratic"
    assert s["labels"]["self_funding"]["total"] == 25.0
    assert {k["kind"] for k in s["labels"]["by_kind"]} == {"monetary", "in_kind"}
