"""Build the revisions report: every number, table, and chart comes from this script.

    .venv/bin/python reports/revisions/build.py            # writes reports/revisions/output/

Nothing in report.md is typed by hand: compute() produces all figures from the tested library functions
(revision_analysis, known_curve, compare_elections + BEA), render() writes CSV tables, PNG charts, report.md, and a
manifest with data vintages, rules applied, and the code version. Re-running on the same data gives identical output.
"""
import csv
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from candidate_modeling import elections, revisions, scope  # noqa: E402
from candidate_modeling.sources import bea, states  # noqa: E402

OUT = Path(__file__).resolve().parent / "output"
REVISION_STATES = ["TX", "PA"]          # states with filing dates and >= 2 comparable (even-year) past elections
REFERENCE_YEARS = [2022, 2024]
TARGET_YEAR = 2026
GOVERNOR_STATES = ["TX", "PA", "MI"]    # governor on the ballot in both 2022 and 2026, data covered both windows
LAGS = (-30, -7, 0, 7, 14, 30, 60, 90, 180)

# Chart styling: dataviz reference palette, light mode (validated: scripts/validate_palette.js, all-pairs PASS;
# aqua is below 3:1 contrast, so every series is direct-labeled and every figure has a table).
SURFACE, TEXT, TEXT2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
SERIES = ["#2a78d6", "#eb6834", "#1baf7a"]
STATE_NAMES = {"TX": "Texas", "PA": "Pennsylvania", "MI": "Michigan", "GA": "Georgia"}


# ---------- compute ----------

def compute(today: dt.date | None = None) -> dict:
    today = today or dt.date.today()
    res = {"today": today.isoformat(), "revision": {}, "curves": {}, "governor": {}, "excluded": []}
    for st in REVISION_STATES:
        r = revisions.revision_analysis(st, [*REFERENCE_YEARS, TARGET_YEAR], lags=LAGS, today=today)
        res["revision"][st] = r
        res["curves"][st] = {y: revisions.known_curve(st, y, today=today) for y in [*REFERENCE_YEARS, TARGET_YEAR]}
    try:
        ga = revisions.revision_analysis("GA", [2025, TARGET_YEAR], lags=LAGS, today=today)
        res["excluded"].append({"state": "GA", "reason": (ga["nowcasts"] or [{}])[0].get("reason", "")})
    except ValueError as e:  # not synced on this machine
        res["excluded"].append({"state": "GA", "reason": str(e)})
    for st in GOVERNOR_STATES:
        res["governor"][st] = elections.compare_elections([2022, TARGET_YEAR], state=st, office="governor",
                                                          today=today)
    res["vintages"] = {"bea": bea.vintage(),
                       **{st: _state_vintage(st) for st in sorted(set(REVISION_STATES + GOVERNOR_STATES))}}
    res["rules"] = sorted({rule for r in res["revision"].values() for rule in r["rules_applied"]}
                          | {rule for g in res["governor"].values() for rule in g["rules_applied"]})
    return res


def _state_vintage(st: str) -> dict:
    from candidate_modeling.sources.states._common import read_manifest
    m = states.get(st)
    d, _ = m._dirs()
    man = read_manifest(d)
    keep = {k: v for k, v in man.items() if k in ("zip_last_modified", "parquet_built_from")}
    if not keep:  # yearly manifests: record per-year stamps
        keep = {k: (v.get("last_modified") or v.get("extracted") or v) if isinstance(v, dict) else v
                for k, v in man.items() if k[:2] in ("20", "co")}
    return {"covered_years": m.covered_years(), "manifest": keep}


# ---------- tables ----------

def tables(res: dict) -> dict[str, list[dict]]:
    t = {"shares": [], "nowcast": [], "backtest": [], "governor": []}
    for st, r in res["revision"].items():
        for e in r["years"]:
            row = (e.get("rows") or [{}])[0]
            if e["status"] == "mature":
                for lag in LAGS:
                    t["shares"].append({"state": st, "election_year": e["election_year"], "days_after_election": lag,
                                        "known_usd": row["known_by_lag"][lag],
                                        "share_of_final": row["share_known_by_lag"][lag],
                                        "final_usd": row["total_now"]})
        for n in r["nowcasts"]:
            t["nowcast"].append({"state": st, "election_year": n["election_year"], "lag_days": n.get("lag_days"),
                                 "known_today_usd": n.get("known_today"), "nowcast_final_usd": n.get("nowcast_final"),
                                 "range_low_usd": (n.get("range") or [None, None])[0],
                                 "range_high_usd": (n.get("range") or [None, None])[1],
                                 "reference_years": " ".join(map(str, n.get("reference_years") or [])),
                                 "mean_share_known_at_lag": n.get("mean_share_known_at_lag")})
        for kind, by_lag in r["backtest_summary"].items():
            for lag, v in by_lag.items():
                t["backtest"].append({"state": st, "elections": kind, "days_after_election": lag,
                                      "mean_abs_error_pct": v["mean_abs_error_pct"], "n": v["n"]})
    for st, g in res["governor"].items():
        for y in g["years"]:
            b = y.get("bea") or {}
            t["governor"].append({"state": st, "election_year": y["election_year"], "nominal_usd": y.get("total"),
                                  "real_usd": b.get("real_total"), "real_base_year": b.get("real_dollars_base_year"),
                                  "real_per_resident_usd": b.get("real_per_capita_total"),
                                  "per_100k_personal_income_usd": b.get("per_100k_personal_income"),
                                  "income_year": b.get("income_year"), "pce_basis": b.get("pce_basis"),
                                  "coverage": y["coverage"]["status"]})
    return t


# ---------- charts ----------

def _style(ax, title: str | None = None):
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=TEXT2, labelsize=9)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    if title:
        ax.set_title(title, loc="left", color=TEXT, fontsize=11, fontweight="bold")


def charts(res: dict, t: dict, outdir: Path) -> dict:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter, PercentFormatter

    outdir.mkdir(parents=True, exist_ok=True)
    made = {}

    # Figure 1: share of the final total that was public, by day relative to election day
    fig, axes = plt.subplots(1, len(res["curves"]), figsize=(10, 4.2), sharey=True, facecolor=SURFACE)
    axes = axes if hasattr(axes, "__iter__") else [axes]
    years = [*REFERENCE_YEARS, TARGET_YEAR]
    nowcast = {n["state"]: n for n in t["nowcast"] if n["nowcast_final_usd"]}
    for ax, (st, curves) in zip(axes, res["curves"].items()):
        _style(ax, STATE_NAMES.get(st, st))
        for color, y in zip(SERIES, years):
            pts = [p for p in curves[y] if p["share_known"] is not None]
            if not pts:
                continue
            xs, ys = [p["day"] for p in pts], [p["share_known"] for p in pts]
            label = str(y)
            if y == TARGET_YEAR:
                # A settling year's "final" is unknown: plot it against the nowcast, not against today's total
                # (which would reach 100% by construction and look complete).
                nc = nowcast.get(st)
                if not nc:
                    continue
                scale = nc["known_today_usd"] / nc["nowcast_final_usd"]
                ys = [v * scale for v in ys]
                label = f"{y} (vs. nowcast)"
                # label in the empty lower-right area, with a leader line to the end of the 2026 curve
                ax.annotate(f"{label}: {ys[-1]:.0%} so far", (xs[-1], ys[-1]), xytext=(95, 0.52), textcoords="data",
                            color=TEXT2, fontsize=8, va="center",
                            arrowprops={"arrowstyle": "-", "color": TEXT2, "linewidth": 0.6, "shrinkA": 2, "shrinkB": 2})
            ax.step(xs, ys, where="post", color=color, linewidth=2, label=label)
        ax.axvline(0, color=TEXT2, linewidth=1, linestyle=(0, (3, 3)))
        ax.text(1, 0.03, "Election day", color=TEXT2, fontsize=8)
        ax.set_ylim(0, 1.02)
        ax.set_xlim(-60, 200)
        ax.yaxis.set_major_formatter(PercentFormatter(1.0))
        ax.set_xlabel("Days relative to election day", color=TEXT2, fontsize=9)
    axes[0].set_ylabel("Share of final total already public", color=TEXT2, fontsize=9)
    axes[0].legend(frameon=False, fontsize=8, loc="lower right", labelcolor=TEXT2)
    fig.suptitle(f"Share of each election's final total that was public, by day ({TARGET_YEAR}: share of the nowcast)",
                 x=0.01, ha="left", color=TEXT, fontsize=10)
    fig.tight_layout()
    f1 = outdir / "fig1_share_known_by_day.png"
    fig.savefig(f1, dpi=200, facecolor=SURFACE)
    made["fig1"] = {"path": f1.name, "ylim": [a.get_ylim() for a in axes]}
    plt.close(fig)

    # Figure 2: 2026 known today vs nowcast final, with the reference-year range
    nc = [n for n in t["nowcast"] if n["nowcast_final_usd"]]
    fig, ax = plt.subplots(figsize=(7, 1.2 + 0.9 * max(len(nc), 1)), facecolor=SURFACE)
    _style(ax, f"{TARGET_YEAR}: money public so far vs. nowcast of the final total")
    ax.grid(axis="y", visible=False)
    ax.grid(axis="x", color=GRID, linewidth=0.8)
    for i, n in enumerate(nc):
        y0 = len(nc) - 1 - i
        ax.barh(y0 + 0.18, n["known_today_usd"] / 1e6, height=0.32, color=SERIES[0])
        ax.barh(y0 - 0.18, n["nowcast_final_usd"] / 1e6, height=0.32, color=SERIES[1])
        ax.errorbar(n["nowcast_final_usd"] / 1e6, y0 - 0.18, color=TEXT2, capsize=3, linewidth=1,
                    xerr=[[(n["nowcast_final_usd"] - n["range_low_usd"]) / 1e6],
                          [(n["range_high_usd"] - n["nowcast_final_usd"]) / 1e6]])
        ax.text(n["known_today_usd"] / 1e6, y0 + 0.18, f"  ${n['known_today_usd'] / 1e6:,.0f}M public",
                va="center", fontsize=8, color=TEXT2)
        ax.text(n["range_high_usd"] / 1e6, y0 - 0.18, f"  ${n['nowcast_final_usd'] / 1e6:,.0f}M nowcast",
                va="center", fontsize=8, color=TEXT2)
    ax.set_yticks(range(len(nc)), [STATE_NAMES.get(n["state"], n["state"]) for n in reversed(nc)])
    from matplotlib.patches import Patch
    ax.legend(handles=[Patch(color=SERIES[0], label=f"Public as of {res['today']}"),
                       Patch(color=SERIES[1], label="Nowcast of final total (min-max range)")],
              frameon=False, fontsize=8, labelcolor=TEXT2, loc="upper center", bbox_to_anchor=(0.5, -0.16), ncol=2)
    ax.set_xlim(0, max((n["range_high_usd"] for n in nc), default=1) / 1e6 * 1.3)
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"${v:,.0f}M"))
    fig.tight_layout()
    f2 = outdir / "fig2_nowcast_2026.png"
    fig.savefig(f2, dpi=200, facecolor=SURFACE)
    made["fig2"] = {"path": f2.name, "xlim": ax.get_xlim()}
    plt.close(fig)

    # Figure 3: governor money per resident, real dollars, 2022 vs 2026
    gov = t["governor"]
    sts = list(res["governor"])
    fig, ax = plt.subplots(figsize=(7, 3.6), facecolor=SURFACE)
    base_year = next((g["real_base_year"] for g in gov if g["real_base_year"]), "")
    _style(ax, f"Governor's-race money per resident, {base_year} dollars ({TARGET_YEAR} still settling)")
    width = 0.36
    for j, (color, y) in enumerate(zip(SERIES, [2022, TARGET_YEAR])):
        vals = [next((g["real_per_resident_usd"] for g in gov if g["state"] == s and g["election_year"] == y), 0) or 0
                for s in sts]
        xs = [i + (j - 0.5) * width for i in range(len(sts))]
        ax.bar(xs, vals, width=width - 0.04, color=color, label=str(y))
        for x, v in zip(xs, vals):
            ax.text(x, v, f"${v:,.2f}", ha="center", va="bottom", fontsize=8, color=TEXT2)
    ax.set_xticks(range(len(sts)), [STATE_NAMES.get(s, s) for s in sts])
    ax.set_ylim(0, max((g["real_per_resident_usd"] or 0) for g in gov) * 1.2 if gov else 1)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"${v:,.0f}"))
    ax.legend(frameon=False, fontsize=8, labelcolor=TEXT2)
    fig.tight_layout()
    f3 = outdir / "fig3_governor_real_per_resident.png"
    fig.savefig(f3, dpi=200, facecolor=SURFACE)
    made["fig3"] = {"path": f3.name, "ylim": ax.get_ylim()}
    plt.close(fig)
    return made


# ---------- report text (every number from the tables) ----------

def _m(v):
    return f"${v / 1e6:,.1f}M" if v is not None else "n/a"


def _pct(v):
    return f"{100 * v:.1f}%" if v is not None else "n/a"


def numbers(t: dict) -> dict:
    """The only source of numbers for report.md."""
    n: dict = {}
    for row in t["shares"]:
        n[f"share_{row['state']}_{row['election_year']}_{row['days_after_election']}"] = row["share_of_final"]
        n[f"final_{row['state']}_{row['election_year']}"] = row["final_usd"]
    for row in t["nowcast"]:
        for k in ("known_today_usd", "nowcast_final_usd", "range_low_usd", "range_high_usd"):
            n[f"nowcast_{row['state']}_{k}"] = row[k]
    for row in t["backtest"]:
        n[f"mae_{row['state']}_{row['elections']}_{row['days_after_election']}"] = row["mean_abs_error_pct"]
    for row in t["governor"]:
        n[f"gov_{row['state']}_{row['election_year']}_real_per_resident"] = row["real_per_resident_usd"]
        n[f"gov_{row['state']}_{row['election_year']}_real"] = row["real_usd"]
        n[f"gov_{row['state']}_{row['election_year']}_nominal"] = row["nominal_usd"]
    return n


def report_md(res: dict, t: dict, made: dict) -> str:
    n = numbers(t)
    sts = REVISION_STATES
    lines = [
        f"# How much do early filings understate final campaign totals?",
        "",
        f"*Revision analysis of state campaign-finance totals, {', '.join(STATE_NAMES[s] for s in sts)}, "
        f"{REFERENCE_YEARS[0]}-{TARGET_YEAR}. Generated {res['today']} by `reports/revisions/build.py`; every number "
        "below is computed by that script (see `tables/`).*",
        "",
        "## Summary",
        "",
    ]
    for st in sts:
        s0 = [n.get(f"share_{st}_{y}_0") for y in REFERENCE_YEARS]
        s90 = [n.get(f"share_{st}_{y}_90") for y in REFERENCE_YEARS]
        lines.append(f"- **{STATE_NAMES[st]}:** on election day, {_pct(min(s0))} to {_pct(max(s0))} of the eventual "
                     f"total had been reported ({' and '.join(map(str, REFERENCE_YEARS))}); by 90 days after, "
                     f"{_pct(min(s90))} to {_pct(max(s90))}. A nowcast built from those patterns puts {TARGET_YEAR} at "
                     f"{_m(n.get(f'nowcast_{st}_nowcast_final_usd'))} (range {_m(n.get(f'nowcast_{st}_range_low_usd'))}"
                     f" to {_m(n.get(f'nowcast_{st}_range_high_usd'))}), versus "
                     f"{_m(n.get(f'nowcast_{st}_known_today_usd'))} public today. Leave-one-out backtest error at "
                     f"election day: {n.get(f'mae_{st}_even_years_0', 'n/a')}%.")
    for st in GOVERNOR_STATES:
        a, b = n.get(f"gov_{st}_2022_real_per_resident"), n.get(f"gov_{st}_{TARGET_YEAR}_real_per_resident")
        if a and b:
            lines.append(f"- **{STATE_NAMES[st]} governor:** ${a:,.2f} per resident in 2022 vs ${b:,.2f} in "
                         f"{TARGET_YEAR} (inflation-adjusted; {TARGET_YEAR} still settling).")
    lines += [
        "",
        "## Data",
        "",
        "- Itemized contributions from state disclosure systems: Texas Ethics Commission (full database), Pennsylvania "
        "Department of State (yearly full exports). Loans, public funds, and other non-contribution receipts are excluded.",
        "- Filing dates: Texas report received date; Pennsylvania report submitted date. Each contribution is "
        "dated by when its report became public.",
        "- BEA reference data: SAINC1 state personal income and population; PCE price index (DPCERG). File vintages "
        "are listed in `manifest.json`.",
        "- Election windows: contributions dated after the previous November election day through this one.",
        "",
        "## Method",
        "",
        "1. **Vintage reconstruction.** For each election window, the total *as it would have been known* on day D is "
        "the sum of contributions whose report was filed by D. This mirrors comparing BEA's advance estimate with "
        "later vintages.",
        "2. **Revision measures.** For mature windows (180+ days after the election), the share of the final total "
        "known at each lag and the revision from each vintage to the final.",
        f"3. **Nowcast.** For {TARGET_YEAR}, the total public today divided by the mean share known at the same lag in "
        "past elections of the same kind (even-year generals). The range uses the reference elections' extremes; with "
        "two references it is a min-max band, not a confidence interval.",
        "4. **Backtest.** Each mature election is predicted from the others at every lag (leave-one-out); errors are "
        "reported separately for even- and odd-year elections because their reporting patterns differ.",
        "5. **Real dollars.** Nominal totals deflated by the PCE price index to the latest full year; per-resident "
        "figures use BEA state population.",
        "",
        "## Results",
        "",
        f"![Share known by day]({made['fig1']['path']})",
        "",
        f"*Figure 1. Share of each election's final total that was public, by day relative to election day. "
        f"{TARGET_YEAR} is plotted against its nowcast, since its final total is not known yet. "
        "Table: `tables/shares.csv`.*",
        "",
        "| State | Election | Election day | +30 days | +90 days | Final |",
        "|---|---|---|---|---|---|",
    ]
    for st in sts:
        for y in REFERENCE_YEARS:
            lines.append(f"| {STATE_NAMES[st]} | {y} | {_pct(n.get(f'share_{st}_{y}_0'))} | "
                         f"{_pct(n.get(f'share_{st}_{y}_30'))} | {_pct(n.get(f'share_{st}_{y}_90'))} | "
                         f"{_m(n.get(f'final_{st}_{y}'))} |")
    lines += ["", f"![Nowcast]({made['fig2']['path']})", "",
              f"*Figure 2. {TARGET_YEAR} total public today (blue) and the nowcast of the final total (orange) with the "
              "min-max range from reference elections. Table: `tables/nowcast.csv`.*", "",
              "| State | Elections | Election day | +30 days | +90 days |", "|---|---|---|---|---|"]
    for st in sts:
        for kind in ("even_years", "odd_years"):
            if f"mae_{st}_{kind}_0" in n:
                lines.append(f"| {STATE_NAMES[st]} | {kind.replace('_', '-')} | {n[f'mae_{st}_{kind}_0']}% | "
                             f"{n.get(f'mae_{st}_{kind}_30', 'n/a')}% | {n.get(f'mae_{st}_{kind}_90', 'n/a')}% |")
    lines += ["", "*Table: mean absolute backtest error of the nowcast by days after election "
              "(`tables/backtest.csv`).*", "", f"![Governor money]({made['fig3']['path']})", "",
              "*Figure 3. Money raised by governor's-race committees per resident, inflation-adjusted. Includes each "
              "committee's whole election window; candidate-linked committees (e.g. Texas specific-purpose committees) "
              "are counted under the candidate. Table: `tables/governor.csv`.*", "",
              "## Limitations", "",
              f"- Two reference elections per state is a small sample; the {TARGET_YEAR} ranges are indicative.",
              "- Vintages use report filing dates; amended reports carry their amendment date and appear as late "
              "revisions.",
              "- Itemized contributions only; unitemized small-dollar money is reported differently by each state.",
              "- Office is each committee's latest office on file.",
              f"- {TARGET_YEAR} is still settling, and BEA personal income for {TARGET_YEAR} is not yet published "
              "(the latest year is used and flagged in the tables)."]
    for ex in res["excluded"]:
        lines.append(f"- {STATE_NAMES.get(ex['state'], ex['state'])} is excluded from the nowcast: {ex['reason']}.")
    lines += ["", "## Reproducibility", "",
              "```bash", ".venv/bin/cm sync TX && .venv/bin/cm sync PA --years 2021 2022 2023 2024 2025 2026",
              ".venv/bin/cm sync BEA", ".venv/bin/python reports/revisions/build.py", "```", "",
              "Data vintages, rules applied, and the code version are in `manifest.json`. Rules are defined in "
              "`src/candidate_modeling/scope.py`.", ""]
    return "\n".join(lines)


# ---------- render ----------

def render(res: dict, outdir: Path = OUT) -> dict:
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "tables").mkdir(exist_ok=True)
    t = tables(res)
    for name, rows in t.items():
        with open(outdir / "tables" / f"{name}.csv", "w", newline="") as f:
            if rows:
                w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                w.writeheader()
                w.writerows(rows)
    made = charts(res, t, outdir)
    md = report_md(res, t, made)
    (outdir / "report.md").write_text(md)
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True,
                                text=True).stdout.strip()
    except OSError:
        commit = None
    manifest = {"generated": res["today"], "code_commit": commit, "vintages": res["vintages"],
                "rules_applied": {r: next((txt for items in scope.RULES.values() for rid, txt in items if rid == r), "")
                                  for r in res["rules"]},
                "figures": made, "tables": {k: len(v) for k, v in t.items()}}
    (outdir / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str))
    return {"tables": t, "figures": made, "report": md, "manifest": manifest}


if __name__ == "__main__":
    out = render(compute())
    print(json.dumps({"tables": {k: len(v) for k, v in out["tables"].items()},
                      "figures": list(out["figures"]), "output": str(OUT)}, indent=2))
