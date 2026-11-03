import csv
import io
import zipfile

import pytest

from candidate_modeling.sources import fec
from candidate_modeling.sources.states import michigan, texas


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Every test gets its own data dir, profile dir, and a fake FEC key; no real sleeping."""
    monkeypatch.setenv("CM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CM_CANDIDATES_DIR", str(tmp_path / "candidates"))
    monkeypatch.setenv("FEC_API_KEY", "TEST_KEY")
    monkeypatch.setattr(fec, "_sleep", lambda s: None)
    return tmp_path


# ---------- Texas fixture zip ----------

TX_FILER_HEADER = ["recordType"] + texas.FILER_COLS + ["filerStreetCity"]
TX_CONTRIB_HEADER = ["recordType", "formTypeCd", "schedFormTypeCd", "receivedDt"] + texas.CONTRIB_COLS + ["contributorJobTitle"]


def _csv_bytes(header, rows) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=header, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow({h: r.get(h, "") for h in header})
    return buf.getvalue().encode()


def tx_contrib(i, filer="00081913", amount="100.00", state="TX", info="N", kind="INDIVIDUAL", **kw):
    base = {"recordType": "RCPT", "formTypeCd": "COH", "schedFormTypeCd": "A1", "reportInfoIdent": "1",
            "infoOnlyFlag": info, "filerIdent": filer, "filerTypeCd": "COH", "filerName": "Talarico, James",
            "contributionInfoId": str(i), "contributionDt": "20240115", "contributionAmount": amount,
            "contributorPersentTypeCd": kind, "contributorNameLast": "Doe", "contributorNameFirst": "Jane",
            "contributorNameOrganization": "Acme PAC" if kind == "ENTITY" else "",
            "contributorStreetCity": "Austin", "contributorStreetStateCd": state,
            "contributorStreetPostalCode": "78701-1234", "contributorEmployer": "Austin ISD",
            "contributorOccupation": "Teacher"}
    base.update(kw)
    return base


@pytest.fixture
def tx_zip(tmp_path) -> bytes:
    filers = [
        {"filerIdent": "00081913", "filerTypeCd": "COH", "filerName": "Talarico, James (The Honorable)",
         "filerPersentTypeCd": "INDIVIDUAL", "filerNameLast": "Talarico", "filerNameFirst": "James",
         "ctaSeekOfficeCd": "STATEREP", "ctaSeekOfficeDistrict": "50", "filerHoldOfficeCd": "STATEREP",
         "filerHoldOfficeDistrict": "50", "filerFilerpersStatusCd": "CURRENT_OFFICEHOLDER", "filerEffStartDt": "20230101"},
        {"filerIdent": "00099999", "filerTypeCd": "GPAC", "filerName": "Talarico Fan PAC"},  # not a candidate
        {"filerIdent": "00055555", "filerTypeCd": "COH", "filerName": "Smith, Someone"},
    ]
    contribs_01 = [
        tx_contrib(1, amount="100.00"),
        tx_contrib(2, amount="250.00", state="CA", contributorOccupation="Line one\nline two"),  # quoted newline
        tx_contrib(3, amount="5000.00", kind="ENTITY", contributorEmployer=""),
        tx_contrib(4, amount="999.00", info="Y"),  # superseded: must be dropped
        tx_contrib(5, amount="77.00", filer="00055555"),  # someone else
    ]
    contribs_02 = [tx_contrib(6, amount="1000.00", contributionDt="20250301")]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("filers.csv", _csv_bytes(TX_FILER_HEADER, filers))
        zf.writestr("contribs_01.csv", _csv_bytes(TX_CONTRIB_HEADER, contribs_01))
        zf.writestr("contribs_02.csv", _csv_bytes(TX_CONTRIB_HEADER, contribs_02))
        zf.writestr("cont_ss.csv", _csv_bytes(TX_CONTRIB_HEADER, [tx_contrib(7, amount="123456.00")]))  # ignored
        zf.writestr("expend_01.csv", b"recordType\nEXPN\n")
    return buf.getvalue()


# ---------- Michigan fixture zip ----------

def mi_line(cid, com="0521875", amount="50.00", state="MI", first=" Alice", last="Korach", com_type="Candidate",
            can=("JOCELYN", "BENSON"), date="09/11/2026", occupation="Teacher"):
    vals = {c: "" for c in michigan.COLUMNS}
    vals.update({"doc_seq_no": "26-1", "contribution_id": cid, "cont_detail_id": "0", "doc_stmnt_year": "2026",
                 "doc_type_desc": "October CS", "com_legal_name": "JOCELYN BENSON FOR GOVERNOR", "cfr_com_id": com,
                 "com_type": com_type, "can_first_name": can[0], "can_last_name": can[1],
                 "contribtype": "Direct Contributions", "contributor_f_name": first, "contributor_l_name_or_org": last,
                 "contributor_city": "Lansing", "contributor_state": state, "contributor_zip": "48933",
                 "contributor_occupation": occupation, "contributor_employer": "State of MI",
                 "received_date": date, "amount": amount, "can_office_sought": "Governor",
                 "can_district_sought": "Statewide", "can_political_party": "Democratic Party"})
    # real files: header has 28 fields, data rows have a trailing tab (29)
    return "\t".join(vals[c] for c in michigan.COLUMNS)


@pytest.fixture
def mi_zip() -> bytes:
    header = "\t".join(michigan.COLUMNS[:-1])
    part1 = "\n".join([header,
                       mi_line("26-1"),
                       mi_line("26-2", amount="1,250.00", state="OH"),
                       mi_line("26-3", first="", last='Big "Union" Local 1', amount="10000.00"),  # stray quotes
                       mi_line("26-4", com="0600001", com_type="Political", can=("", ""))]) + "\n"
    part2 = "\n".join([header, mi_line("26-5", amount="20.00", occupation="Nurse")]) + "\n"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("2026_mi_cfr_contributions1.txt", part1)
        zf.writestr("2026_mi_cfr_contributions2.txt", part2)
    return buf.getvalue()
