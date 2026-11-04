"""Industry coding: rule precedence, classifier threshold, evaluation, and the join into comparisons."""
import csv
import datetime as dt

import duckdb
import pytest
import responses

from candidate_modeling import elections, industry
from candidate_modeling.sources.states import texas


@pytest.mark.parametrize("emp,occ,want", [
    ("AUSTIN ISD", "TEACHER", "61"),
    ("BOEING", "ENGINEER", "31-33"),                        # employer beats a generic job title
    ("RETIRED", "TEACHER", "retired"),
    ("N/A", "RETIRED", "retired"),                          # N/A employer is a placeholder, not "not employed"
    ("N/A", "N/A", "unknown"),
    ("SELF", "ATTORNEY", "54"),
    ("SELF", None, "self_employed"),
    ("STATE FARM", "AGENT", "52"),                          # not agriculture
    ("STARR BUS CHARTER & TOURS", "CO-OWNER", "48-49"),     # not Charter Communications
    ("STATE OF NC", "EDUCATOR", "61"),                      # NAICS codes the establishment, not the owner
    ("COUNTY OF LOS ANGELES", "LICENSED VOCATIONAL NURSE", "62"),
    ("PJ STEEL PIPELINE CONSTRUCTION", "UNION MEMBER", "23"),
    ("U S STEEL", "MAINTENANCE", "31-33"),                  # bare "U" is not a university
    (None, "UNION MEMBER", None),                           # membership says nothing about industry -> classifier
    (None, "ENGINEER", None),                               # job title alone -> classifier, not a forced 54
    (None, None, "unknown"),
])
def test_rule_precedence(emp, occ, want):
    hit = industry.rule_code(emp, occ)
    assert (hit[0] if hit else None) == want


def test_normalize_matches_sql():
    raw = "  austin\tisd   district "
    from candidate_modeling.labels import sql_str
    sql = duckdb.connect().execute(f"SELECT {industry.NORM_SQL.format(x=sql_str(raw))}").fetchone()[0]
    assert sql == industry.normalize(raw) == "AUSTIN ISD DISTRICT"


@pytest.fixture
def tx_with_jobs(tx_zip):
    with responses.RequestsMock() as rs:
        rs.head(texas.ZIP_URL, headers={"Last-Modified": "v1"})
        rs.get(texas.ZIP_URL, body=tx_zip)
        texas.sync()


def test_build_codes_every_pair_and_respects_threshold(tx_with_jobs, monkeypatch):
    meta = industry.build()
    rows = duckdb.connect().execute(f"SELECT emp, occ, code, method, confidence FROM read_parquet('{industry.out_path()}')"
                                    ).fetchall()
    assert meta["pairs"] == len(rows) and rows
    for emp, occ, code, method, conf in rows:
        assert method in ("rule", "model", "model_low_confidence", "no_model")
        if method == "model":
            assert conf >= industry.CONFIDENCE and code in industry.SECTORS
        if method == "model_low_confidence":
            assert code == "unclassified" and conf < industry.CONFIDENCE
    # fixture donors: "Austin ISD" + "Teacher" -> education
    assert ("AUSTIN ISD", "TEACHER", "61") in {(e, o, c) for e, o, c, _, _ in rows}


def test_evaluate_counts(tx_with_jobs, tmp_path):
    industry.build()
    gold = tmp_path / "gold.csv"
    with open(gold, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["employer", "occupation", "label", "note", "labeled_by"])
        w.writerows([["AUSTIN ISD", "TEACHER", "61", "", "t"], ["STATE FARM", "AGENT", "52", "", "t"],
                     ["STATE FARM", "AGENT", "11", "", "t"]])                      # one deliberately wrong label
    r = industry.evaluate(gold)
    assert r["n"] == 3 and r["accuracy_all"] == round(2 / 3, 3) and r["coverage"] == 1.0
    assert r["per_label"]["52"] == {"precision": 0.5, "recall": 1.0, "support": 1}


def test_compare_by_industry(tx_with_jobs):
    with pytest.raises(ValueError, match="not built"):
        elections.compare_elections([2024], state="TX", group_by="industry")
    industry.build()
    r = elections.compare_elections([2024], state="TX", group_by="industry", today=dt.date(2027, 1, 1))
    by = {g["industry"]: g["years"][2024]["total"] for g in r["by_industry"]}
    assert by["61"] == 100.0 + 250.0 + 77.0          # fixture individuals: Austin ISD teachers
    assert by["not_an_individual"] == 5000.0          # organizations are never industry-coded
