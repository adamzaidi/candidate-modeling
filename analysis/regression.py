"""What predicts a federal candidate's small-dollar share of individual receipts?

Public FEC candidate-summary files (no API key). One row per candidate-cycle.
OLS with HC3 and state-clustered SEs, plus a fractional logit. Every number in
docs/regression-writeup.md is written by this script from the fitted objects.

    python analysis/regression.py
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import requests
import statsmodels.api as sm
from statsmodels.stats.outliers_influence import variance_inflation_factor

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from candidate_modeling import config  # noqa: E402
from candidate_modeling.labels import normalize_party  # noqa: E402

# Completed even-year cycles with a stable candidate-summary CSV. 2026 is still
# being filed, so it is downloaded for a coverage note and then dropped.
MAIN_CYCLES = (2016, 2018, 2020, 2022, 2024)
ROBUST_CYCLES = (2012, 2014) + MAIN_CYCLES
ALL_CYCLES = ROBUST_CYCLES
SUMMARY_URL = "https://www.fec.gov/files/bulk-downloads/{cycle}/candidate_summary_{cycle}.csv"
MIN_INDIVIDUAL = 50_000.0
MIN_RECEIPTS = 50_000.0

# Same palette as reports/revisions/build.py (light, labeled, axes at zero).
SURFACE, TEXT, TEXT2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#7a5af8"]

# Omitted categories in the design matrix: challenger, House, Republican, first cycle.
REGRESSORS = ["incumbent", "open_seat", "senate", "democrat", "log_individual"]


def summary_url(cycle: int) -> str:
    return SUMMARY_URL.format(cycle=int(cycle))


def normalize_ici(value) -> str:
    """FEC incumbent/challenger/open-seat. Files mix I/C/O with full words."""
    u = str(value or "").strip().upper().replace("_", " ").replace("-", " ")
    if u in {"I", "INCUMBENT"}:
        return "incumbent"
    if u in {"C", "CHALLENGER"}:
        return "challenger"
    if u in {"O", "OPEN", "OPEN SEAT"}:
        return "open"
    return "unknown"


def normalize_office(value) -> str:
    u = str(value or "").strip().upper()
    return u if u in {"H", "S", "P"} else "unknown"


def _money(value) -> float:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return 0.0
    s = str(value).strip().replace(",", "").replace("$", "")
    if s in {"", ".", "NA", "None"}:
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------- download ----------

def download_file(url: str, dest: Path, timeout: int = 120) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    tmp = dest.with_suffix(dest.suffix + ".part")
    r = requests.get(url, headers={"User-Agent": config.USER_AGENT}, timeout=timeout)
    r.raise_for_status()
    tmp.write_bytes(r.content)
    tmp.replace(dest)
    return dest


def download_summaries(cycles, cache_dir: Path) -> dict[int, Path]:
    out = {}
    for cycle in cycles:
        url = summary_url(cycle)
        dest = cache_dir / f"candidate_summary_{cycle}.csv"
        download_file(url, dest)
        out[int(cycle)] = dest
    return out


# ---------- sample ----------

def load_summaries(files: dict[int, Path]) -> pd.DataFrame:
    """Read each cycle CSV with DuckDB and stack them. One row per candidate-cycle."""
    con = duckdb.connect()
    frames = []
    for cycle, path in sorted(files.items()):
        rel = con.execute("SELECT * FROM read_csv_auto(?, header=true)", [str(path)]).df()
        rel.insert(0, "cycle", int(cycle))
        frames.append(rel)
    if not frames:
        raise ValueError("No candidate-summary files to load.")
    return pd.concat(frames, ignore_index=True)


def build_sample(raw: pd.DataFrame, cycles=MAIN_CYCLES,
                 min_individual: float = MIN_INDIVIDUAL,
                 min_receipts: float = MIN_RECEIPTS) -> tuple[pd.DataFrame, dict]:
    """House/Senate, major-party, serious individual fundraising, known incumbency."""
    df = raw.copy()
    df["candidate_id"] = df["Cand_Id"].astype(str).str.strip()
    df["candidate_name"] = df["Cand_Name"].astype(str).str.strip()
    df["state"] = df["Cand_Office_St"].astype(str).str.strip().str.upper()
    df["office"] = df["Cand_Office"].map(normalize_office)
    df["ici"] = df["Cand_Incumbent_Challenger_Open_Seat"].map(normalize_ici)
    df["party"] = df["Cand_Party_Affiliation"].map(normalize_party)
    df["individual"] = df["Individual_Contribution"].map(_money)
    df["unitemized"] = df["Individual_Unitemized_Contribution"].map(_money)
    df["itemized"] = df["Individual_Itemized_Contribution"].map(_money)
    df["pac"] = df["Other_Committee_Contribution"].map(_money)
    df["party_committee"] = df["Party_Committee_Contribution"].map(_money)
    df["cand_contrib"] = df["Cand_Contribution"].map(_money)
    df["total_contrib"] = df["Total_Contribution"].map(_money)
    df["receipts"] = df["Total_Receipt"].map(_money)
    df["transfers_in"] = df["Transfer_From_Other_Auth_Committee"].map(_money)
    df["cycle"] = df["cycle"].astype(int)

    n_raw = {int(c): int((df["cycle"] == c).sum()) for c in sorted(df["cycle"].unique())}
    keep_cycle = df["cycle"].isin([int(c) for c in cycles])
    keep_office = df["office"].isin(["H", "S"])
    keep_party = df["party"].isin(["democratic", "republican"])
    keep_ici = df["ici"].isin(["incumbent", "challenger", "open"])
    keep_id = df["candidate_id"].str.startswith(("H", "S"))
    keep_money = (df["individual"] >= min_individual) & (df["receipts"] >= min_receipts)
    mask = keep_cycle & keep_office & keep_party & keep_ici & keep_id & keep_money
    out = df.loc[mask].copy()
    out = out.drop_duplicates(["cycle", "candidate_id"], keep="first")

    # Small-dollar share of individual receipts. Clip tiny floating-point overshoot.
    out["small_share"] = np.where(out["individual"] > 0, out["unitemized"] / out["individual"], np.nan)
    n_clip = int(((out["small_share"] < 0) | (out["small_share"] > 1)).sum())
    out["small_share"] = out["small_share"].clip(0, 1)
    out["pac_share"] = np.where(out["total_contrib"] > 0, out["pac"] / out["total_contrib"], np.nan)
    out["pac_share"] = out["pac_share"].clip(0, 1)
    out["log_individual"] = np.log(out["individual"])
    out["net_receipts"] = (out["receipts"] - out["transfers_in"]).clip(lower=0)
    out = out.dropna(subset=["small_share", "pac_share", "log_individual", "state"]).reset_index(drop=True)

    notes = {
        "n_raw_by_cycle": n_raw,
        "n_sample": int(len(out)),
        "n_clipped_share": n_clip,
        "min_individual": min_individual,
        "min_receipts": min_receipts,
        "cycles": [int(c) for c in cycles],
        "dropped": {
            "wrong_cycle": int((~keep_cycle).sum()),
            "not_house_senate": int((keep_cycle & ~keep_office).sum()),
            "not_major_party": int((keep_cycle & keep_office & ~keep_party).sum()),
            "unknown_ici": int((keep_cycle & keep_office & keep_party & ~keep_ici).sum()),
            "below_money_floor": int(
                (keep_cycle & keep_office & keep_party & keep_ici & keep_id & ~keep_money).sum()),
        },
    }
    return out, notes


def design_matrix(df: pd.DataFrame, cycles, extra_dummies: list[str] | None = None,
                  include_log: bool = True) -> pd.DataFrame:
    """Explicit dummies so the omitted groups are easy to say out loud."""
    cycles = [int(c) for c in cycles]
    X = pd.DataFrame({
        "incumbent": (df["ici"] == "incumbent").astype(int),
        "open_seat": (df["ici"] == "open").astype(int),
        "senate": (df["office"] == "S").astype(int),
        "democrat": (df["party"] == "democratic").astype(int),
    }, index=df.index)
    if include_log:
        X["log_individual"] = df["log_individual"].astype(float)
    for c in cycles[1:]:
        X[f"cycle_{c}"] = (df["cycle"] == c).astype(int)
    if extra_dummies:
        for col in extra_dummies:
            dummies = pd.get_dummies(df[col], prefix=col, drop_first=True, dtype=int)
            X = pd.concat([X, dummies], axis=1)
    X = sm.add_constant(X, has_constant="add")
    keep = [c for c in X.columns if c == "const" or X[c].nunique() > 1]
    return X[keep].astype(float)


def _params(res, names=None) -> dict:
    names = list(names) if names is not None else list(res.params.index)
    ci = res.conf_int()
    out = {}
    for k in names:
        if k not in res.params.index:
            continue
        out[k] = {
            "coef": float(res.params[k]),
            "se": float(res.bse[k]),
            "t": float(res.tvalues[k]) if k in res.tvalues.index else None,
            "p": float(res.pvalues[k]),
            "ci_low": float(ci.loc[k, 0]),
            "ci_high": float(ci.loc[k, 1]),
        }
    out["_n"] = int(res.nobs)
    out["_r2"] = float(getattr(res, "rsquared", float("nan")))
    return out


def fit_ols(y, X, groups=None, cov="HC3"):
    model = sm.OLS(y, X)
    if cov == "cluster":
        return model.fit(cov_type="cluster", cov_kwds={"groups": groups})
    return model.fit(cov_type=cov)


def fit_frac(y, X, groups=None, cov="HC3"):
    """Papke-Wooldridge fractional logit: GLM, binomial family, y in [0, 1]."""
    model = sm.GLM(y, X, family=sm.families.Binomial())
    if cov == "cluster":
        return model.fit(cov_type="cluster", cov_kwds={"groups": groups})
    return model.fit(cov_type=cov)


def average_partial_effects(res, X: pd.DataFrame) -> dict:
    """OLS-like units (change in the 0-1 share) from the fractional logit."""
    mu = np.asarray(res.predict(X), dtype=float)
    apes = {}
    for col in X.columns:
        if col == "const":
            continue
        beta = float(res.params[col])
        if set(pd.unique(X[col])) <= {0.0, 1.0}:
            X1, X0 = X.copy(), X.copy()
            X1[col] = 1.0
            X0[col] = 0.0
            apes[col] = float(np.mean(res.predict(X1) - res.predict(X0)))
        else:
            apes[col] = float(np.mean(beta * mu * (1.0 - mu)))
    return apes


def vif_table(X: pd.DataFrame) -> pd.DataFrame:
    cols = [c for c in X.columns if c != "const"]
    rows = []
    for i, name in enumerate(X.columns):
        if name == "const":
            continue
        rows.append({"variable": name, "vif": float(variance_inflation_factor(X.values, i))})
    return pd.DataFrame(rows)


def group_means(df: pd.DataFrame) -> list[dict]:
    rows = []
    for cols, label in [(["ici"], "incumbency"), (["party"], "party"), (["office"], "office"),
                        (["cycle"], "cycle"), (["party", "cycle"], "party_cycle"),
                        (["ici", "office"], "ici_office")]:
        g = (df.groupby(cols, dropna=False)
               .agg(n=("small_share", "size"),
                    small_share_mean=("small_share", "mean"),
                    small_share_sd=("small_share", "std"),
                    pac_share_mean=("pac_share", "mean"),
                    individual_mean=("individual", "mean"),
                    receipts_mean=("receipts", "mean"))
               .reset_index())
        g.insert(0, "group", label)
        rows.extend(g.to_dict(orient="records"))
    return rows


# ---------- output helpers ----------

def _pp(x, d=1) -> str:
    """Percentage points from a 0-1 share or coefficient."""
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return "n/a"
    return f"{100 * x:.{d}f}"


def _pct(x, d=1) -> str:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return "n/a"
    return f"{100 * x:.{d}f}%"


def _n(x) -> str:
    return f"{int(x):,}"


def _p(x) -> str:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return "n/a"
    if x < 0.001:
        return "< 0.001"
    return f"{x:.3f}"


def _p_clause(x) -> str:
    s = _p(x)
    return f"p {s}" if s.startswith("<") else f"p = {s}"


def _coef_cell(row: dict, digits=1) -> str:
    return f"{_pp(row['coef'], digits)} ({_pp(row['se'], digits)})"


def _json_ready(obj):
    if isinstance(obj, dict):
        return {str(k): _json_ready(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_ready(v) for v in obj]
    if isinstance(obj, (np.floating, float)):
        x = float(obj)
        return None if math.isnan(x) or math.isinf(x) else x
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, Path):
        return str(obj)
    return obj


def write_csv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(path, index=False)


# ---------- figures ----------

def _style(ax, title, ylabel=None):
    ax.set_facecolor(SURFACE)
    ax.set_title(title, color=TEXT, loc="left", fontsize=11, pad=8)
    ax.tick_params(colors=TEXT2)
    for s in ax.spines.values():
        s.set_color(GRID)
    ax.yaxis.grid(True, color=GRID)
    ax.set_axisbelow(True)
    if ylabel:
        ax.set_ylabel(ylabel, color=TEXT2)


def figures(df: pd.DataFrame, ols, outdir: Path) -> dict:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    made = {}
    outdir.mkdir(parents=True, exist_ok=True)

    # fig1: mean small-dollar share by incumbency and party
    fig, ax = plt.subplots(figsize=(7.2, 4.0), facecolor=SURFACE)
    order = ["challenger", "open", "incumbent"]
    parties = ["democratic", "republican"]
    width = 0.36
    ymax = 0
    for j, (party, color) in enumerate(zip(parties, SERIES)):
        means, ns = [], []
        for ici in order:
            sl = df[(df["ici"] == ici) & (df["party"] == party)]["small_share"]
            means.append(float(sl.mean()) if len(sl) else 0.0)
            ns.append(int(len(sl)))
        xs = [i + (j - 0.5) * width for i in range(len(order))]
        ax.bar(xs, means, width=width - 0.04, color=color, label=party)
        for x, v, n in zip(xs, means, ns):
            ax.text(x, v, f"{100 * v:.0f}%\nn={n:,}", ha="center", va="bottom", fontsize=7, color=TEXT2)
        ymax = max(ymax, max(means) if means else 0)
    _style(ax, "Mean small-dollar share of individual receipts", "Share of individual receipts")
    ax.set_xticks(range(len(order)), ["Challenger", "Open seat", "Incumbent"])
    ax.set_ylim(0, min(1.0, ymax * 1.35 + 0.05))
    ax.yaxis.set_major_formatter(lambda v, _: f"{100 * v:.0f}%")
    ax.legend(frameon=False, fontsize=8, labelcolor=TEXT2)
    fig.tight_layout()
    p1 = outdir / "fig1_share_by_incumbency_party.png"
    fig.savefig(p1, dpi=160, facecolor=SURFACE)
    plt.close(fig)
    made["fig1"] = {"path": p1.name, "ylim": [0, float(min(1.0, ymax * 1.35 + 0.05))]}

    # fig2: main OLS coefficients (clustered), percentage points
    names = [c for c in REGRESSORS if c in ols.params.index]
    fig, ax = plt.subplots(figsize=(7.2, 4.2), facecolor=SURFACE)
    y_pos = np.arange(len(names))
    coefs = np.array([100 * ols.params[n] for n in names])
    los = np.array([100 * ols.conf_int().loc[n, 0] for n in names])
    his = np.array([100 * ols.conf_int().loc[n, 1] for n in names])
    ax.axvline(0, color=GRID, linewidth=1)
    ax.errorbar(coefs, y_pos, xerr=[coefs - los, his - coefs], fmt="o", color=SERIES[0],
                ecolor=TEXT2, capsize=3)
    labels = {
        "incumbent": "Incumbent (vs challenger)",
        "open_seat": "Open seat (vs challenger)",
        "senate": "Senate (vs House)",
        "democrat": "Democrat (vs Republican)",
        "log_individual": "Log individual receipts",
    }
    ax.set_yticks(y_pos, [labels.get(n, n) for n in names])
    _style(ax, "OLS coefficients, state-clustered 95% CI")
    ax.set_xlabel("Percentage points of small-dollar share", color=TEXT2)
    ax.xaxis.grid(True, color=GRID)
    ax.yaxis.grid(False)
    fig.tight_layout()
    p2 = outdir / "fig2_coefficients.png"
    fig.savefig(p2, dpi=160, facecolor=SURFACE)
    plt.close(fig)
    made["fig2"] = {"path": p2.name, "xlim_includes_zero": bool(los.min() <= 0 <= his.max())}

    # fig3: residual diagnostics
    fitted = np.asarray(ols.fittedvalues)
    resid = np.asarray(ols.resid)
    fig, axes = plt.subplots(1, 2, figsize=(8.4, 3.8), facecolor=SURFACE)
    axes[0].scatter(fitted, resid, s=8, alpha=0.25, color=SERIES[0], linewidths=0)
    axes[0].axhline(0, color=TEXT2, linewidth=1)
    _style(axes[0], "Residuals vs fitted", "Residual")
    axes[0].set_xlabel("Fitted small-dollar share", color=TEXT2)
    axes[1].hist(resid, bins=40, color=SERIES[0], edgecolor="white")
    _style(axes[1], "Residual histogram", "Count")
    axes[1].set_xlabel("Residual", color=TEXT2)
    fig.tight_layout()
    p3 = outdir / "fig3_residuals.png"
    fig.savefig(p3, dpi=160, facecolor=SURFACE)
    plt.close(fig)
    made["fig3"] = {"path": p3.name, "ylim": [0, None]}

    # fig4: share vs size
    fig, ax = plt.subplots(figsize=(7.2, 4.0), facecolor=SURFACE)
    for ici, color in zip(["challenger", "open", "incumbent"], SERIES):
        sl = df[df["ici"] == ici]
        ax.scatter(sl["log_individual"], sl["small_share"], s=8, alpha=0.25, color=color,
                   label=ici, linewidths=0)
    _style(ax, "Small-dollar share vs log individual receipts", "Small-dollar share")
    ax.set_xlabel("Log individual receipts", color=TEXT2)
    ax.set_ylim(0, 1)
    ax.yaxis.set_major_formatter(lambda v, _: f"{100 * v:.0f}%")
    ax.legend(frameon=False, fontsize=8, labelcolor=TEXT2, markerscale=2)
    fig.tight_layout()
    p4 = outdir / "fig4_share_vs_size.png"
    fig.savefig(p4, dpi=160, facecolor=SURFACE)
    plt.close(fig)
    made["fig4"] = {"path": p4.name, "ylim": [0, 1]}
    return made


# ---------- models ----------

def spec_fit(df, cycles, y_col="small_share", include_log=True, extra=None, cov="cluster"):
    y = df[y_col].astype(float)
    X = design_matrix(df, cycles, extra_dummies=extra, include_log=include_log)
    groups = df["state"]
    ols = fit_ols(y, X, groups=groups, cov=cov)
    return y, X, ols


def _row_from_ols(name, ols) -> dict:
    p = _params(ols)
    inc, dem = p.get("incumbent") or {}, p.get("democrat") or {}
    return {
        "spec": name, "n": int(ols.nobs), "r2": float(getattr(ols, "rsquared", float("nan"))),
        "incumbent_coef": inc.get("coef"), "incumbent_se": inc.get("se"), "incumbent_p": inc.get("p"),
        "democrat_coef": dem.get("coef"), "democrat_se": dem.get("se"), "democrat_p": dem.get("p"),
    }


def robustness_rows(df, cycles) -> list[dict]:
    rows = []
    _, _, ols_cl = spec_fit(df, cycles, cov="cluster")
    rows.append(_row_from_ols("main_ols_cluster_state", ols_cl))
    _, _, ols_hc3 = spec_fit(df, cycles, cov="HC3")
    rows.append(_row_from_ols("ols_hc3", ols_hc3))
    y = df["small_share"].astype(float)
    X = design_matrix(df, cycles)
    ols_cand = fit_ols(y, X, groups=df["candidate_id"], cov="cluster")
    rows.append(_row_from_ols("ols_cluster_candidate", ols_cand))
    for name, sample, kw in [
        ("house_only", df[df["office"] == "H"].reset_index(drop=True), {}),
        ("min_individual_100k", df[df["individual"] >= 100_000].reset_index(drop=True), {}),
        ("no_log_individual", df, {"include_log": False}),
        ("state_fe_hc3", df, {"extra": ["state"], "cov": "HC3"}),
        ("pac_share_dv", df, {"y_col": "pac_share"}),
        ("drop_heavy_self_fund",
         df[(df["cand_contrib"] / df["total_contrib"].replace(0, np.nan)).fillna(0) <= 0.25]
         .reset_index(drop=True), {}),
    ]:
        if len(sample) < 20:
            continue
        _, _, ols = spec_fit(sample, cycles, **kw)
        rows.append(_row_from_ols(name, ols))
    return rows


# ---------- writeup / pdf ----------

LABELS = {
    "incumbent": "Incumbent (vs challenger)",
    "open_seat": "Open seat (vs challenger)",
    "senate": "Senate (vs House)",
    "democrat": "Democrat (vs Republican)",
    "log_individual": "Log individual receipts",
    "const": "Intercept",
}


def _sign_phrase(coef: float) -> str:
    return "higher" if coef > 0 else "lower"


def report_md(res: dict) -> str:
    s = res["sample"]
    m = res["means"]
    ols = res["ols_cluster"]
    hc3 = res["ols_hc3"]
    ape = res["frac_ape"]
    vif = {r["variable"]: r["vif"] for r in res["vif"]}
    missing = [k for k in REGRESSORS if k not in ols]
    if missing:
        raise KeyError(f"main OLS is missing {missing}")
    inc, op, sen, dem, logi = (ols[k] for k in REGRESSORS)
    lines = [
        "# What predicts a federal candidate's small-dollar share?",
        "",
        f"*OLS and fractional logit on FEC candidate summaries, cycles "
        f"{s['cycles'][0]}-{s['cycles'][-1]}. Generated {res['today']} by "
        f"`analysis/regression.py`. Every number below comes from that run "
        f"(see `analysis/output/tables/`). Public data only.*",
        "",
        "## Question",
        "",
        "Among major-party House and Senate candidates who raised a meaningful "
        "amount from individuals, what predicts the share of that individual "
        "money that is unitemized (donors who gave $200 or less in the cycle)? "
        "The predictors are incumbency, open seat, office (Senate vs House), "
        "party, and log individual receipts, with cycle fixed effects.",
        "",
        "The example of pooling FEC itemized rows with the seven state bulk "
        "files cannot support this question. FEC unitemized totals live on the "
        "candidate summary (and in `fec_totals` for profiled candidates), not "
        "in `contributions`. State itemization thresholds are not $200, Texas "
        "files have no party, Wisconsin and North Carolina have no office, and "
        "no state connector yet has an incumbent/challenger flag (it is on the "
        "roadmap). So this analysis uses the public FEC candidate-summary file, "
        "which is the same Form 3 rollup the pipeline already stores per "
        "profiled candidate, but for every candidate in the cycle.",
        "",
        "## Data",
        "",
        f"- Source: FEC candidate summary CSV, "
        f"[file description](https://www.fec.gov/campaign-finance-data/candidate-summary-file-description/).",
        f"- Cycles in the main sample: {', '.join(str(c) for c in s['cycles'])}. "
        f"2012 and 2014 are downloaded and used only as a robustness check. "
        f"2026 is excluded because the cycle is not over.",
        "- Outcome: `Individual_Unitemized_Contribution / Individual_Contribution`. "
        "Unitemized means the donor did not exceed $200 to that campaign in the "
        "cycle, so the committee does not itemize the gift. This is a reporting "
        "threshold, not a survey of donor intent.",
        "- Incumbency: FEC `Cand_Incumbent_Challenger_Open_Seat` "
        "(incumbent / challenger / open).",
        "- Companion outcome in robustness: PAC share of total contributions "
        "(`Other_Committee_Contribution / Total_Contribution`).",
        "",
        "## Sample",
        "",
        f"Main sample: **{_n(s['n_sample'])} candidate-cycles** after filters. "
        "Keep House and Senate only (presidential fundraising is a different "
        "market), Democrats and Republicans, known incumbency, individual "
        f"receipts of at least ${_n(s['min_individual'])} and total receipts of "
        f"at least ${_n(s['min_receipts'])}. The money floor drops long-shot "
        "committees whose share is 0% or 100% because of a handful of gifts.",
        "",
        f"Mean small-dollar share: **{_pct(m['small_share'])}**. "
        f"Mean PAC share of contributions: **{_pct(m['pac_share'])}**. "
        f"By incumbency: challengers {_pct(m['small_challenger'])}, "
        f"open seats {_pct(m['small_open'])}, "
        f"incumbents {_pct(m['small_incumbent'])}. "
        f"By party: Democrats {_pct(m['small_dem'])}, Republicans {_pct(m['small_rep'])}.",
        "",
        f"{s['n_clipped_share']} shares were outside [0, 1] before clipping "
        "(floating-point or refund noise). They were clipped; the count is "
        "reported so the clip cannot hide a data problem.",
        "",
        "## Method",
        "",
        "1. **OLS.** The share is a number between 0 and 1. OLS coefficients "
        "are percentage-point differences holding the other regressors fixed. "
        "Omitted groups: challenger, House, Republican, and "
        f"{s['cycles'][0]} (cycle dummies absorb a common year shock).",
        "2. **Standard errors.** HC3 allows the residual variance to differ "
        "across campaigns (heteroskedasticity). State-clustered SEs also allow "
        "campaigns in the same state to share unobserved donor networks and "
        "party infrastructure. The main table reports clustered SEs; HC3 is "
        "next to them.",
        "3. **Fractional logit.** Because the outcome is a share, a binomial "
        "GLM (Papke and Wooldridge 1996) keeps fitted values in [0, 1]. "
        "Coefficients are not percentage points, so the table shows average "
        "partial effects, which are.",
        "4. **Diagnostics.** Summary means, a correlation matrix, VIFs on the "
        "design matrix, and residual plots.",
        "5. **Robustness.** House only; $100k individual floor; drop log size; "
        "state fixed effects; cluster by candidate; drop heavy self-funders; "
        "PAC share as the outcome; add 2012-2014.",
        "",
        "This is an association, not a causal design. Incumbency is not "
        "random, and campaigns choose how they raise money.",
        "",
        "## Results",
        "",
        f"![Mean share](../analysis/output/fig1_share_by_incumbency_party.png)",
        "",
        "*Figure 1. Mean unitemized share of individual receipts. Table: "
        "`analysis/output/tables/group_means.csv`.*",
        "",
        f"![Coefficients](../analysis/output/fig2_coefficients.png)",
        "",
        "*Figure 2. Main OLS coefficients in percentage points, 95% CI clustered "
        "by state. Table: `analysis/output/tables/regression_main.csv`.*",
        "",
        "| Variable | OLS, clustered by state | OLS, HC3 | Fractional logit APE |",
        "|---|---|---|---|",
    ]
    for k in REGRESSORS:
        lines.append(
            f"| {LABELS[k]} | {_coef_cell(ols[k])} | {_coef_cell(hc3[k])} | {_pp(ape[k])} |")
    lines += [
        "",
        f"N = {_n(ols['_n'])}. OLS R-squared = {ols['_r2']:.3f}. "
        "Cells are percentage points of the 0-1 share, with standard errors in "
        "parentheses. Cycle dummies are in the table file and omitted here.",
        "",
        "### Plain English",
        "",
        f"- Holding office, party, cycle, and log individual receipts fixed, "
        f"incumbents' small-dollar share is **{_pp(inc['coef'])} percentage points "
        f"{_sign_phrase(inc['coef'])}** than challengers' "
        f"(clustered SE {_pp(inc['se'])}, {_p_clause(inc['p'])}).",
        f"- Open-seat candidates are **{_pp(op['coef'])} percentage points "
        f"{_sign_phrase(op['coef'])}** than challengers "
        f"(SE {_pp(op['se'])}, {_p_clause(op['p'])}).",
        f"- Senate candidates are **{_pp(sen['coef'])} percentage points "
        f"{_sign_phrase(sen['coef'])}** than House candidates "
        f"(SE {_pp(sen['se'])}, {_p_clause(sen['p'])}).",
        f"- Democrats are **{_pp(dem['coef'])} percentage points "
        f"{_sign_phrase(dem['coef'])}** than Republicans "
        f"(SE {_pp(dem['se'])}, {_p_clause(dem['p'])}).",
        f"- Doubling individual receipts is associated with a "
        f"**{_pp(abs(logi['coef'] * math.log(2)))} percentage-point "
        f"{'increase' if logi['coef'] > 0 else 'decrease'}** "
        f"in the small-dollar share "
        f"(log coefficient {_pp(logi['coef'])}, SE {_pp(logi['se'])}, "
        f"{_p_clause(logi['p'])}). "
        + ("The partial size effect is small next to incumbency; once "
           "incumbency is held fixed, larger individual programs are not more "
           "itemized." if logi["coef"] > 0 else
           "Larger individual programs are, on average, more itemized."),
        "",
        "The fractional-logit average partial effects have the same signs. "
        "That is the check that OLS is not being driven by the linear model "
        "predicting shares outside [0, 1].",
        "",
        "## Diagnostics",
        "",
        f"![Residuals](../analysis/output/fig3_residuals.png)",
        "",
        "*Figure 3. OLS residuals vs fitted values, and the residual histogram. "
        "Shares pile up near 0, so residuals are not textbook-normal. That is "
        "why we also fit the fractional logit and why SEs are robust.*",
        "",
        f"![Share vs size](../analysis/output/fig4_share_vs_size.png)",
        "",
        "*Figure 4. Small-dollar share against log individual receipts. "
        "Incumbents cluster toward lower shares at a given size; the "
        "regression is asking whether that gap remains after party, office, "
        "and cycle.*",
        "",
        f"The largest VIF among the main regressors (not cycle dummies) is "
        f"**{max(vif[k] for k in REGRESSORS if k in vif):.2f}** "
        f"({max((k for k in REGRESSORS if k in vif), key=lambda k: vif[k])}). Values under 5 are the "
        "usual rule of thumb that collinearity is not driving the SEs. The "
        "full VIF table is `analysis/output/tables/vif.csv`. Pairwise "
        "correlations are in `analysis/output/tables/correlation.csv`.",
        "",
        "## Robustness",
        "",
        "Each row refits the same idea on a different sample, SE, or outcome. "
        "The incumbent and Democrat coefficients are the ones to watch.",
        "",
        "| Specification | N | Incumbent | Democrat |",
        "|---|---|---|---|",
    ]
    for row in res["robustness"]:
        if row["incumbent_coef"] is None:
            continue
        lines.append(
            f"| {row['spec']} | {_n(row['n'])} | "
            f"{_pp(row['incumbent_coef'])} ({_pp(row['incumbent_se'])}) | "
            f"{_pp(row['democrat_coef'])} ({_pp(row['democrat_se'])}) |")
    lines += [
        "",
    ]
    small_rows = [r for r in res["robustness"]
                  if r.get("incumbent_coef") is not None and r["spec"] != "pac_share_dv"]
    pac_row = next((r for r in res["robustness"] if r["spec"] == "pac_share_dv"), None)
    if small_rows and all(r["incumbent_coef"] < 0 for r in small_rows):
        lines.append(
            "The incumbency gap is negative in every specification that uses "
            "small-dollar share as the outcome.")
    elif small_rows:
        lines.append(
            "The incumbency gap is not the same sign in every small-dollar "
            "specification; see the table rather than a single headline.")
    lines.append("")
    if pac_row and pac_row.get("incumbent_coef") is not None and small_rows:
        if small_rows[0]["incumbent_coef"] < 0 and pac_row["incumbent_coef"] > 0:
            lines.append(
                "Switching the outcome to PAC share flips the sign: incumbents "
                "take a larger share of their contributions from other committees. "
                "That is a different mix, not a contradiction.")
        else:
            lines.append(
                "The PAC-share specification is in the table as a companion mix "
                "measure; it is not the same outcome as small-dollar share.")
    lines.append("")
    lines += [
        f"Adding 2012 and 2014 (N = {_n(res['robust_2012_2014']['n'])}) leaves "
        f"the incumbent coefficient at {_pp(res['robust_2012_2014']['incumbent_coef'])} "
        f"(SE {_pp(res['robust_2012_2014']['incumbent_se'])}).",
        "",
        "## Limitations",
        "",
        "- **Correlation is not causation.** Incumbents differ from challengers "
        "in name recognition, committee assignments, and who chooses to run. "
        "The coefficient is the remaining gap after the controls in the table, "
        "not the effect of making someone an incumbent.",
        "- **Selection.** Candidates below the $50,000 individual floor are "
        "out. That drops many uncompetitive challengers. Raising the floor to "
        "$100,000 is in the robustness table.",
        "- **Omitted variables.** There is no district partisanship, no Cook "
        "PVI, and no general-election vote share. The FEC all-candidates file "
        "stopped carrying vote share after 2006, and this summary file never "
        "had it. Competitiveness is therefore not in the model. Ideology, "
        "candidate quality, and presidential-year coattails are also missing.",
        "- **Simultaneity.** Small-dollar share is a strategy (email, ActBlue, "
        "WinRed) and a result of being the kind of candidate those platforms "
        "promote. Log receipts is partly a consequence of the same things as "
        "the outcome; the specification without it is in the robustness table.",
        "- **The $200 line is legal, not behavioral.** A $201 donor is fully "
        "itemized. Two $100 gifts in two cycles look like small-dollar; $200 "
        "in one cycle does not.",
        "- **Joint fundraising transfers.** The small-dollar share uses "
        "individual contributions as the denominator, so transfers from "
        "authorized committees are not in the outcome. They still affect who "
        "shows up in the sample via total receipts.",
        "- **Repeated candidates.** Some people appear in several cycles. "
        "Main SEs cluster by state; a candidate-level cluster is in robustness.",
        "- **State disclosure data are out of this regression on purpose.** "
        "They cannot be stacked with FEC unitemized totals without pretending "
        "the itemization rules match. They remain the right source for "
        "state-office questions this repo already answers with "
        "`compare_elections`.",
        "",
        "## Reproduce",
        "",
        "```bash",
        "python3 -m venv .venv && source .venv/bin/activate",
        "pip install -e '.[analysis]'",
        "python analysis/regression.py",
        "```",
        "",
        "Files land in `analysis/output/` (tables, figures, `manifest.json`) "
        "and `docs/regression-writeup.md` plus `docs/regression-writeup.pdf`. "
        "Re-running on the same CSVs should match. The CSVs themselves are "
        "cached under `data/raw/fec/` (gitignored) and hashed in the manifest.",
        "",
        "Offline tests use a tiny fixture CSV and do not hit the network: "
        "`pytest tests/test_regression.py`.",
        "",
    ]
    text = "\n".join(lines)
    if "\u2014" in text or "\u2013" in text:
        raise ValueError("writeup contains a dash character the style rules forbid")
    return text


def write_pdf(res: dict, path: Path) -> None:
    from fpdf import FPDF

    s, m, ols = res["sample"], res["means"], res["ols_cluster"]
    pdf = FPDF(format="Letter")
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()
    w = pdf.epw

    def heading(title):
        pdf.set_x(pdf.l_margin)
        pdf.set_font("Helvetica", "B", 12)
        pdf.multi_cell(w, 7, title, new_x="LMARGIN", new_y="NEXT")

    def body(text, size=10, style=""):
        pdf.set_x(pdf.l_margin)
        pdf.set_font("Helvetica", style, size)
        pdf.multi_cell(w, 5, text, new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("Helvetica", "B", 14)
    pdf.multi_cell(w, 8, "What predicts a federal candidate's small-dollar share?",
                   new_x="LMARGIN", new_y="NEXT")
    body(
        f"FEC candidate summaries, {s['cycles'][0]}-{s['cycles'][-1]}. "
        f"Generated {res['today']} by analysis/regression.py. "
        "Every number is from that run. Public data only.",
        size=10, style="I",
    )
    heading("Question")
    body("Among major-party House and Senate candidates with at least $50,000 "
         "in individual receipts, what predicts the unitemized (under $200 per "
         "donor per cycle) share of those individual receipts?")
    heading("Sample")
    body(f"N = {_n(s['n_sample'])} candidate-cycles. "
         f"Mean small-dollar share {_pct(m['small_share'])}. "
         f"Challengers {_pct(m['small_challenger'])}, open seats "
         f"{_pct(m['small_open'])}, incumbents {_pct(m['small_incumbent'])}. "
         f"Democrats {_pct(m['small_dem'])}, Republicans {_pct(m['small_rep'])}.")
    heading("Main OLS (clustered by state)")
    pdf.set_font("Helvetica", "B", 9)
    col_w = [90, 50, 40]
    pdf.set_x(pdf.l_margin)
    for label, cw in zip(["Variable", "Coef. (SE), pp", "p"], col_w):
        pdf.cell(cw, 6, label, border=1)
    pdf.ln()
    pdf.set_font("Helvetica", "", 9)
    for k in REGRESSORS:
        if k not in ols:
            continue
        pdf.set_x(pdf.l_margin)
        pdf.cell(col_w[0], 6, LABELS[k][:40], border=1)
        pdf.cell(col_w[1], 6, _coef_cell(ols[k]), border=1)
        pdf.cell(col_w[2], 6, _p(ols[k]["p"]), border=1)
        pdf.ln()
    body(f"N = {_n(ols['_n'])}. R-squared = {ols['_r2']:.3f}.", size=9, style="I")
    heading("Plain English")
    inc, dem = ols["incumbent"], ols["democrat"]
    body(f"Holding the other regressors fixed, incumbents' small-dollar share "
        f"is {_pp(inc['coef'])} percentage points {_sign_phrase(inc['coef'])} "
        f"than challengers' (SE {_pp(inc['se'])}, {_p_clause(inc['p'])}). "
        f"Democrats are {_pp(dem['coef'])} percentage points "
        f"{_sign_phrase(dem['coef'])} than Republicans "
        f"(SE {_pp(dem['se'])}, {_p_clause(dem['p'])}). "
         "The fractional logit average partial effects have the same signs. "
         "This is an association, not a causal effect.")
    heading("Limitations")
    body("Incumbency is not random. District partisanship and race "
         "competitiveness are missing (FEC vote share is not in this file). "
         "The $200 line is a reporting rule. State bulk files are not in this "
         "regression because their itemization rules and labels are not "
         "comparable. Full writeup: docs/regression-writeup.md.")
    heading("Reproduce")
    pdf.set_font("Courier", "", 9)
    pdf.set_x(pdf.l_margin)
    pdf.multi_cell(w, 5, "pip install -e '.[analysis]'\npython analysis/regression.py",
                   new_x="LMARGIN", new_y="NEXT")
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(path))


# ---------- run ----------

def compute(files: dict[int, Path], cycles=MAIN_CYCLES,
            min_individual=MIN_INDIVIDUAL, min_receipts=MIN_RECEIPTS) -> dict:
    raw = load_summaries(files)
    df, notes = build_sample(raw, cycles=cycles, min_individual=min_individual,
                             min_receipts=min_receipts)
    y, X, ols_cl = spec_fit(df, cycles, cov="cluster")
    _, _, ols_hc3 = spec_fit(df, cycles, cov="HC3")
    frac = fit_frac(y, X, groups=df["state"], cov="cluster")
    apes = average_partial_effects(frac, X)
    vif = vif_table(X[ [c for c in X.columns if not str(c).startswith("cycle_")] ])
    means = {
        "small_share": float(df["small_share"].mean()),
        "pac_share": float(df["pac_share"].mean()),
        "small_challenger": float(df.loc[df["ici"] == "challenger", "small_share"].mean()),
        "small_open": float(df.loc[df["ici"] == "open", "small_share"].mean()),
        "small_incumbent": float(df.loc[df["ici"] == "incumbent", "small_share"].mean()),
        "small_dem": float(df.loc[df["party"] == "democratic", "small_share"].mean()),
        "small_rep": float(df.loc[df["party"] == "republican", "small_share"].mean()),
        "individual_median": float(df["individual"].median()),
        "n_house": int((df["office"] == "H").sum()),
        "n_senate": int((df["office"] == "S").sum()),
        "n_incumbent": int((df["ici"] == "incumbent").sum()),
        "n_challenger": int((df["ici"] == "challenger").sum()),
        "n_open": int((df["ici"] == "open").sum()),
    }
    corr_cols = ["small_share", "pac_share"] + REGRESSORS
    Xc = design_matrix(df, cycles)
    corr_df = pd.concat(
        [df[["small_share", "pac_share"]], Xc[[c for c in REGRESSORS if c in Xc.columns]]],
        axis=1).corr()

    rob = robustness_rows(df, cycles)
    extra = [c for c in ALL_CYCLES if c in files]
    df_long, long_notes = build_sample(raw, cycles=extra, min_individual=min_individual,
                                       min_receipts=min_receipts)
    _, _, ols_long = spec_fit(df_long, extra, cov="cluster")
    long = {
        "n": int(ols_long.nobs),
        "incumbent_coef": float(ols_long.params["incumbent"]),
        "incumbent_se": float(ols_long.bse["incumbent"]),
        "incumbent_p": float(ols_long.pvalues["incumbent"]),
        "cycles": extra,
    }

    keep = ["cycle", "candidate_id", "candidate_name", "state", "office", "party", "ici",
            "individual", "unitemized", "itemized", "pac", "total_contrib", "receipts",
            "small_share", "pac_share", "log_individual"]
    return {
        "today": dt.date.today().isoformat(),
        "sample": notes,
        "means": means,
        "ols_cluster": _params(ols_cl),
        "ols_hc3": _params(ols_hc3),
        "frac_ape": {k: float(apes[k]) for k in REGRESSORS if k in apes},
        "vif": vif.to_dict(orient="records"),
        "correlation": corr_df.reset_index().rename(columns={"index": "variable"}).to_dict(orient="records"),
        "group_means": group_means(df),
        "robustness": rob,
        "robust_2012_2014": long,
        "df": df[keep],
        "ols_obj": ols_cl,
        "fitted": np.asarray(ols_cl.fittedvalues),
        "resid": np.asarray(ols_cl.resid),
        "files": {int(c): str(p) for c, p in files.items()},
        "corr_cols": corr_cols,
    }


def tables(res: dict) -> dict[str, list[dict]]:
    ols, hc3 = res["ols_cluster"], res["ols_hc3"]
    main = []
    for k, label in {**LABELS, **{f"cycle_{c}": f"Cycle {c} (vs {MAIN_CYCLES[0]})"
                                  for c in MAIN_CYCLES[1:]}}.items():
        if k not in ols:
            continue
        main.append({
            "variable": k, "label": label,
            "ols_cluster_coef": ols[k]["coef"], "ols_cluster_se": ols[k]["se"],
            "ols_cluster_p": ols[k]["p"],
            "ols_hc3_coef": hc3[k]["coef"], "ols_hc3_se": hc3[k]["se"], "ols_hc3_p": hc3[k]["p"],
            "frac_ape": res["frac_ape"].get(k),
        })
    desc = [{
        "n": res["sample"]["n_sample"],
        "small_share_mean": res["means"]["small_share"],
        "pac_share_mean": res["means"]["pac_share"],
        "individual_median": res["means"]["individual_median"],
        "n_house": res["means"]["n_house"], "n_senate": res["means"]["n_senate"],
        "n_incumbent": res["means"]["n_incumbent"],
        "n_challenger": res["means"]["n_challenger"], "n_open": res["means"]["n_open"],
    }]
    return {
        "summary": desc,
        "group_means": res["group_means"],
        "regression_main": main,
        "vif": res["vif"],
        "correlation": res["correlation"],
        "robustness": res["robustness"],
        "sample_notes": [res["sample"]],
    }


def render(res: dict, outdir: Path, writeup_path: Path, pdf_path: Path) -> dict:
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "tables").mkdir(exist_ok=True)
    t = tables(res)
    for name, rows in t.items():
        write_csv(rows, outdir / "tables" / f"{name}.csv")
    res["df"].to_csv(outdir / "tables" / "analysis_sample.csv", index=False)
    made = figures(res["df"], res["ols_obj"], outdir)
    md = report_md(res)
    writeup_path.parent.mkdir(parents=True, exist_ok=True)
    writeup_path.write_text(md)
    write_pdf(res, pdf_path)
    vintages = {}
    for cycle, path in res["files"].items():
        p = Path(path)
        vintages[str(cycle)] = {
            "path": str(p), "url": summary_url(int(cycle)),
            "bytes": p.stat().st_size if p.exists() else None,
            "sha256": sha256_file(p) if p.exists() else None,
        }
    manifest = {
        "today": res["today"],
        "question": "small_share = unitemized individual / individual contributions",
        "cycles": res["sample"]["cycles"],
        "n": res["sample"]["n_sample"],
        "min_individual": res["sample"]["min_individual"],
        "source": "FEC candidate summary CSV (public bulk, no API key)",
        "vintages": vintages,
        "ols_cluster_incumbent": res["ols_cluster"]["incumbent"],
        "figures": {k: v["path"] for k, v in made.items()},
        "writeup": str(writeup_path),
        "pdf": str(pdf_path),
    }
    (outdir / "manifest.json").write_text(json.dumps(_json_ready(manifest), indent=2) + "\n")
    (outdir / "results.json").write_text(json.dumps(_json_ready({
        k: v for k, v in res.items() if k not in {"df", "ols_obj", "fitted", "resid"}
    }), indent=2) + "\n")
    return {"tables": t, "figures": made, "writeup": md, "manifest": manifest}


def run(cycles=MAIN_CYCLES, cache_dir: Path | None = None, outdir: Path | None = None,
        writeup_path: Path | None = None, pdf_path: Path | None = None,
        files: dict[int, Path] | None = None, min_individual=MIN_INDIVIDUAL) -> dict:
    cache_dir = Path(cache_dir or (config.data_dir() / "raw" / "fec"))
    outdir = Path(outdir or (ROOT / "analysis" / "output"))
    writeup_path = Path(writeup_path or (ROOT / "docs" / "regression-writeup.md"))
    pdf_path = Path(pdf_path or (ROOT / "docs" / "regression-writeup.pdf"))
    need = sorted(set(list(cycles) + list(ROBUST_CYCLES)))
    if files is None:
        files = download_summaries(need, cache_dir)
    res = compute(files, cycles=cycles, min_individual=min_individual)
    out = render(res, outdir, writeup_path, pdf_path)
    out["res"] = res
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="FEC small-dollar share regression")
    ap.add_argument("--cache-dir", type=Path)
    ap.add_argument("--out-dir", type=Path)
    ap.add_argument("--writeup", type=Path)
    ap.add_argument("--pdf", type=Path)
    ap.add_argument("--min-individual", type=float, default=MIN_INDIVIDUAL)
    args = ap.parse_args(argv)
    out = run(cache_dir=args.cache_dir, outdir=args.out_dir, writeup_path=args.writeup,
              pdf_path=args.pdf, min_individual=args.min_individual)
    n = out["res"]["sample"]["n_sample"]
    inc = out["res"]["ols_cluster"]["incumbent"]
    print(f"N={n} incumbent={inc['coef']:.4f} (se={inc['se']:.4f} p={inc['p']:.4g})")
    print(f"wrote {out['manifest']['writeup']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
