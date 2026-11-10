"""Offline tests for the FEC small-dollar regression. No network."""
import importlib.util

import pandas as pd
import pytest

from candidate_modeling import config

spec = importlib.util.spec_from_file_location("regression", config.ROOT / "analysis" / "regression.py")
reg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reg)


HEADER_COLS = [
    "Link_Image", "Cand_Name", "Cand_Id", "Cand_Office", "Cand_Office_St", "Cand_Office_Dist",
    "Cand_Party_Affiliation", "Cand_Incumbent_Challenger_Open_Seat", "Total_Receipt",
    "Total_Disbursement", "Cash_On_Hand_COP", "Debt_Owed_By_Committee", "Coverage_End_Date",
    "Cand_Street_1", "Cand_Street_2", "Cand_City", "Cand_State", "Cand_Zip",
    "Individual_Itemized_Contribution", "Individual_Unitemized_Contribution",
    "Individual_Contribution", "Other_Committee_Contribution", "Party_Committee_Contribution",
    "Cand_Contribution", "Total_Contribution", "Transfer_From_Other_Auth_Committee",
]


def _row(i, cycle, office="H", state="TX", party="DEM", ici="CHALLENGER",
         individual=100_000, unitemized=50_000, pac=10_000, receipts=120_000):
    itemized = individual - unitemized
    cand = 0
    total_contrib = individual + pac + cand
    cid = f"{office}{i:08d}"
    return {
        "Link_Image": "", "Cand_Name": f"CAND {i}", "Cand_Id": cid, "Cand_Office": office,
        "Cand_Office_St": state, "Cand_Office_Dist": "01", "Cand_Party_Affiliation": party,
        "Cand_Incumbent_Challenger_Open_Seat": ici, "Total_Receipt": receipts,
        "Total_Disbursement": 0, "Cash_On_Hand_COP": 0, "Debt_Owed_By_Committee": 0,
        "Coverage_End_Date": f"12/31/{cycle}", "Cand_Street_1": "", "Cand_Street_2": "",
        "Cand_City": "X", "Cand_State": state, "Cand_Zip": "00000",
        "Individual_Itemized_Contribution": itemized,
        "Individual_Unitemized_Contribution": unitemized,
        "Individual_Contribution": individual, "Other_Committee_Contribution": pac,
        "Party_Committee_Contribution": 0, "Cand_Contribution": cand,
        "Total_Contribution": total_contrib, "Transfer_From_Other_Auth_Committee": 0,
    }


def write_cycle(path, cycle, n_per_cell=6):
    """Challengers have a high small-dollar share; incumbents a low one."""
    rows = []
    k = 0
    for state in ["TX", "PA", "MI", "GA", "AZ", "WI", "NC", "OH"]:
        for party, ici, base_share, pac in [
            ("DEM", "CHALLENGER", 0.55, 5_000),
            ("REP", "CHALLENGER", 0.45, 8_000),
            ("DEM", "INCUMBENT", 0.12, 40_000),
            ("REP", "INCUMBENT", 0.08, 50_000),
            ("DEM", "OPEN", 0.35, 15_000),
            ("REP", "OPEN SEAT", 0.30, 18_000),
        ]:
            for _ in range(n_per_cell):
                office = "S" if k % 11 == 0 else "H"
                individual = 80_000 + (k % 9) * 20_000
                jitter = ((k * 17 + cycle) % 13 - 6) / 100.0
                share = min(0.95, max(0.02, base_share + jitter))
                rows.append(_row(k, cycle, office=office, state=state, party=party, ici=ici,
                                 individual=individual, unitemized=round(share * individual, 2),
                                 pac=pac, receipts=individual + pac + 20_000))
                k += 1
    pd.DataFrame(rows)[HEADER_COLS].to_csv(path, index=False)
    return path


@pytest.mark.parametrize("raw,want", [
    ("I", "incumbent"), ("INCUMBENT", "incumbent"), ("challenger", "challenger"),
    ("OPEN", "open"), ("OPEN SEAT", "open"), ("", "unknown"),
])
def test_normalize_ici(raw, want):
    assert reg.normalize_ici(raw) == want


def test_build_sample_filters_president_and_tiny_campaigns(tmp_path):
    rows = [
        _row(1, 2016, office="P", state="US", individual=1_000_000, unitemized=400_000),
        _row(2, 2016, office="H", state="TX", individual=1_000, unitemized=900, receipts=1_000),
        _row(3, 2016, office="H", state="TX", party="IND", individual=80_000, unitemized=20_000),
        _row(4, 2016, office="H", state="TX", ici="INCUMBENT", individual=80_000, unitemized=10_000),
    ]
    raw = pd.DataFrame(rows)
    raw["cycle"] = 2016
    sample, notes = reg.build_sample(raw, cycles=[2016], min_individual=50_000)
    assert notes["n_sample"] == 1
    assert sample.iloc[0]["ici"] == "incumbent"
    assert sample.iloc[0]["small_share"] == pytest.approx(10_000 / 80_000)


@pytest.fixture
def built(tmp_path):
    files = {}
    for cycle in (2016, 2018):
        p = tmp_path / f"candidate_summary_{cycle}.csv"
        write_cycle(p, cycle)
        files[cycle] = p
    outdir = tmp_path / "out"
    writeup = tmp_path / "writeup.md"
    pdf = tmp_path / "writeup.pdf"
    return reg.run(cycles=(2016, 2018), files=files, outdir=outdir,
                   writeup_path=writeup, pdf_path=pdf)


def test_outputs_exist(built, tmp_path):
    outdir = tmp_path / "out"
    for name in ["manifest.json", "tables/regression_main.csv",
                 "tables/vif.csv", "tables/correlation.csv", "tables/robustness.csv",
                 "tables/analysis_sample.csv", "fig1_share_by_incumbency_party.png",
                 "fig2_coefficients.png", "fig3_residuals.png", "fig4_share_vs_size.png"]:
        assert (outdir / name).exists(), name
    assert (tmp_path / "writeup.md").exists()
    assert (tmp_path / "writeup.pdf").exists()


def test_incumbents_have_lower_small_dollar_share(built):
    inc = built["res"]["ols_cluster"]["incumbent"]
    assert inc["coef"] < 0
    assert inc["p"] < 0.05
    means = built["res"]["means"]
    assert means["small_incumbent"] < means["small_challenger"]


def test_writeup_numbers_come_from_the_run(built, tmp_path):
    md = (tmp_path / "writeup.md").read_text()
    n = built["res"]["sample"]["n_sample"]
    inc = built["res"]["ols_cluster"]["incumbent"]
    assert f"{n:,}" in md or str(n) in md
    assert reg._pp(inc["coef"]) in md
    assert reg._pp(inc["se"]) in md
    assert "\u2014" not in md and "—" not in md
    assert "{" not in md.replace("```bash", "")
    assert "None" not in md


def test_figure_axes_start_at_zero(built):
    assert built["figures"]["fig1"]["ylim"][0] == 0
    assert built["figures"]["fig4"]["ylim"][0] == 0


def test_vif_is_finite(built):
    vifs = [r["vif"] for r in built["res"]["vif"] if r["variable"] in reg.REGRESSORS]
    assert vifs and all(v == v and v < 50 for v in vifs)


def test_fractional_ape_matches_sign(built):
    ols = built["res"]["ols_cluster"]["incumbent"]["coef"]
    ape = built["res"]["frac_ape"]["incumbent"]
    assert ols * ape > 0
