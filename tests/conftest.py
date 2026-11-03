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
        {"filerIdent": "00077777", "filerTypeCd": "SPAC", "filerName": "Texans for Talarico"},
        {"filerIdent": "00088888", "filerTypeCd": "SPAC", "filerName": "Stop Talarico"},
    ]
    spacs = [
        {"spacFilerIdent": "00077777", "spacFilerTypeCd": "SPAC", "spacPositionCd": "SUPPORT",
         "candidateFilerIdent": "00081913", "candidateSeekOfficeCd": ""},
        {"spacFilerIdent": "00088888", "spacFilerTypeCd": "SPAC", "spacPositionCd": "OPPOSE",
         "candidateFilerIdent": "00081913", "candidateSeekOfficeCd": "STATEREP"},
    ]
    contribs_01 = [
        tx_contrib(1, amount="100.00"),
        tx_contrib(2, amount="250.00", state="CA", contributorOccupation="Line one\nline two"),  # quoted newline
        tx_contrib(3, amount="5000.00", kind="ENTITY", contributorEmployer=""),
        tx_contrib(4, amount="999.00", info="Y"),  # superseded: must be dropped
        tx_contrib(5, amount="77.00", filer="00055555"),  # someone else
    ]
    contribs_02 = [tx_contrib(6, amount="1000.00", contributionDt="20250301"),
                   tx_contrib(8, filer="00077777", amount="700.00", contributionDt="20260201"),
                   tx_contrib(9, filer="00088888", amount="300.00", contributionDt="20260201")]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("filers.csv", _csv_bytes(TX_FILER_HEADER, filers))
        zf.writestr("spacs.csv", _csv_bytes(["recordType"] + __import__("candidate_modeling.sources.states.texas",
                    fromlist=["SPAC_COLS"]).SPAC_COLS, spacs))
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


# ---------- Pennsylvania fixture zip ----------

def _pa_filer(report_id, filer_id, name, ftype="2", cycle="1", submitted="2026-04-07", amend="N", office="GOV"):
    return (f'{report_id},"{filer_id}",2026,{submitted},{cycle},"{amend}","N","{ftype}","{name}","{office}","-1","DEM",'
            f'"","","Philadelphia","PA","19110","46","2025520221",0.00,0.00,0.00')


def _pa_contrib(report_id, filer_id, contributor, amt1="100.00", d1="20260115", amt2="0", d2="0", amt3="0", d3="0",
                section="ID", state="PA", occupation="Teacher", employer="School District"):
    return (f'{report_id},"{filer_id}",2026,2026-04-07,1,"{section}","{contributor}","","","Philadelphia","{state}",'
            f'"191101234","{occupation}","{employer}","","","","","","{d1}",{amt1},"{d2}",{amt2},"{d3}",{amt3},""')


@pytest.fixture
def pa_zip() -> bytes:
    from candidate_modeling.sources.states import pennsylvania as pa
    filers = "\r\n".join([",".join(pa.FILER_COLS),
        _pa_filer(100, "20160016", "Shapiro for Pennsylvania"),
        _pa_filer(101, "2026C0403", "SHAPIRO, JOSHUA D", ftype="1"),
        _pa_filer(200, "20160016", "Shapiro for Pennsylvania", cycle="2", submitted="2026-05-08"),
        _pa_filer(201, "20160016", "Shapiro for Pennsylvania", cycle="2", submitted="2026-06-01", amend="Y"),
        _pa_filer(300, "99999999", "UPPER DARBY FIREFIGHTERS \"PAC\"", office=""),
    ]) + "\r\n"
    contribs = [",".join(pa.CONTRIB_COLS),
        _pa_contrib(100, "20160016", "Jane Doe"),
        _pa_contrib(100, "20160016", "Jane Doe"),  # identical second gift: must be kept
        _pa_contrib(100, "20160016", 'ILLEGIBLE, CHESTER "CHET" SMITH', amt1="50.00", amt2="25.00", d2="20260201",
                    section="IB"),  # unescaped quotes + 2 slots
        _pa_contrib(100, "20160016", "IBEW Local 98 COPE", amt1="500000.00", section="IC", occupation="", employer=""),
        _pa_contrib(100, "20160016", "Out Of Stater", amt1="250.00", state="NY", employer="Flitter Milz PC\r\n"),  # newline
        _pa_contrib(200, "20160016", "Superseded Original", amt1="9999.00"),  # report 200 replaced by 201
        _pa_contrib(201, "20160016", "Amended Version", amt1="300.00"),
        _pa_contrib(300, "99999999", "Someone Else", amt1="1.00"),
    ]
    body = ("\r\n".join(contribs) + "\r\n").encode("utf-8")
    body += _pa_contrib(100, "20160016", "José Caf\xe9", amt1="10.00", section="IB").encode("cp1252") + b"\r\n"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("filer_2026.txt", filers)
        zf.writestr("contrib_2026.txt", body)
        zf.writestr("expense_2026.txt", "x\r\n")
    return buf.getvalue()


# ---------- Georgia fixture CSV ----------

def ga_row(txn, entity="101091", name="Burt Jones for Georgia", ttype="Contribution", sub="Itemized Contribution",
           ctype="Individual", last="Doe", first="Jane", state="GA", zip_='="30342-3429"', amount='"$1,000.00"',
           tdate="01/11/2026", amended="N", filed="02/05/2026", employer="Acme"):
    from candidate_modeling.sources.states import georgia
    vals = {c: "" for c in georgia.COLUMNS}
    vals.update({"filing_entity_id": entity, "filing_entity_name": name, "committee_name": name,
                 "registration_type": "Candidate/Candidate Campaign Committee", "transaction_id": txn,
                 "transaction_type": ttype, "transaction_subtype": sub, "contributor_type": ctype,
                 "contributor_last": last, "contributor_first": first, "contributor_city": "Atlanta",
                 "contributor_state": state, "contributor_zip": zip_, "employer": employer, "occupation": "Owner",
                 "transaction_date": tdate, "transaction_amount": amount, "amended": amended,
                 "report_name": "2026 Jan 31 CCDR", "report_filed_date": filed})
    return ",".join(vals[c] for c in georgia.COLUMNS)


@pytest.fixture
def ga_csv() -> bytes:
    from candidate_modeling.sources.states import georgia
    header = ",".join(c.replace("_", " ").title() for c in georgia.COLUMNS)
    rows = [
        ga_row("1"),
        ga_row("2", ctype="Corporation / Business / Unregistered Committee", last="GTBA - Georgia's Rural PAC", first=""),
        ga_row("3", name='"Barton, Madison "Matt" F."', entity="100599"),            # unescaped quotes, other filer
        ga_row("4", sub="Unitemized Contribution", ctype="", last="", first="", state="", zip_="", amount='"$250.00"'),
        ga_row("5", ttype="Return Contribution", amount='"($500.00)"'),
        ga_row("6", ttype="Loan Received", sub="", amount='"$100,000.00"'),           # loans excluded
        ga_row("7", amount='"$100.00"', filed="02/05/2026"),                          # original...
        ga_row("7", amount='"$150.00"', filed="05/04/2026", amended="Y"),             # ...amended wins
        ga_row("8", state="FL", amount='"$2,000.00"'),
    ]
    return ("\r\n".join([header, *rows]) + "\r\n").encode()
