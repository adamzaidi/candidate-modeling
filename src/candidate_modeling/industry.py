"""Donor industry coding: employer/occupation free text -> NAICS 2022 sector (rule industry-coding).

Mirrors statistical-agency autocoding:
  1. Rules code the clear cases (employer patterns first, then occupation): "ISD" -> 61, "REALTOR" -> 53.
  2. A text classifier (character n-grams over employer + occupation, logistic regression) is trained on the
     rule-coded pairs and applied to the rest.
  3. Predictions below CONFIDENCE stay 'unclassified' (a review queue), never a forced guess.
  4. Evaluation against a hand-labeled sample (reference/industry_gold.csv): precision / recall per sector.

Coding happens once per distinct (employer, occupation) pair, which is ~500k pairs for ~13M gifts, and the
result joins back to contributions on the normalized pair.
"""
import csv
import datetime as _dt
import json
import re
from pathlib import Path

import duckdb

from . import store
from .config import ROOT, data_dir

SECTORS = {
    "11": "Agriculture, forestry, fishing and hunting", "21": "Mining, quarrying, and oil and gas extraction",
    "22": "Utilities", "23": "Construction", "31-33": "Manufacturing", "42": "Wholesale trade",
    "44-45": "Retail trade", "48-49": "Transportation and warehousing", "51": "Information",
    "52": "Finance and insurance", "53": "Real estate and rental and leasing",
    "54": "Professional, scientific, and technical services", "55": "Management of companies and enterprises",
    "56": "Administrative and support and waste management", "61": "Educational services",
    "62": "Health care and social assistance", "71": "Arts, entertainment, and recreation",
    "72": "Accommodation and food services", "81": "Other services (incl. unions, religious, civic)",
    "92": "Public administration",
}
NON_INDUSTRY = {"retired": "Retired", "not_employed": "Not employed", "student": "Student", "homemaker": "Homemaker",
                "self_employed": "Self-employed, industry not stated", "unknown": "No usable employer/occupation",
                "unclassified": "Could not be coded with confidence (review queue)"}
CONFIDENCE = 0.6
GOLD_PATH = ROOT / "reference" / "industry_gold.csv"

# (field, regex, code). Checked in order; first match wins. Employer rules run before occupation rules.
_EMP = [  # order matters: first match wins
    (r"\b(RETIRED)\b$", "retired"),
    (r"^(NOT EMPLOYED|UNEMPLOYED|DISABLED)$", "not_employed"),
    (r"^(HOMEMAKER|HOME ?MAKER|HOUSEWIFE)$", "homemaker"),
    (r"^(STUDENT)$", "student"),
    (r"^(SELF|SELF[ -]?EMPLOYED|SELF EMPLOYEED|OWNER)$", "self_employed"),
    # specific names that a generic keyword below would misfile
    (r"\b(STATE FARM|UNITED ?HEALTH ?CARE|UNITEDHEALTH|HUMANA|AETNA|BLUE CROSS|CIGNA|CENTENE|ANTHEM)\b", "52"),
    (r"\b(CHARTER COMMUNICATIONS|SPECTRUM COMMUNICATIONS|TIME WARNER|CABLE|TELEPHONE|SOUTHWESTERN BELL|AT&T|VERIZON|"
     r"COMCAST|T-MOBILE)\b|^CHARTER$", "51"),
    (r"\b(CENTURY 21|KELLER WILLIAMS|COLDWELL|RE/?MAX|HILLWOOD)\b", "53"),
    (r"\b(TXU|ONCOR|ATMOS ENERGY|AEP|CENTERPOINT|DTE|CONSUMERS ENERGY|NRG ENERGY|CONSTELLATION ENERGY|"
     r"LUMINANT ENERGY)\b", "22"),
    (r"\b(REPUBLIC SERVICES|WASTE MANAGEMENT|WASTE)\b", "56"),
    (r"\b(RNDC|REPUBLIC NATIONAL DISTRIBUTING|SOUTHERN GLAZER|DISTRIBUTING)\b", "42"),
    # sectors
    (r"\b(ISD|SCHOOL|SCHOOLS|UNIVERSITY|COLLEGE|COMM COLL|ACADEMY|EDUCATION|UNIV)\b|\bU$", "61"),  # "ROOSEVELT U"
    (r"\b(HOSPITAL|HEALTH ?CARE|HEALTH SYSTEM|MEDICAL CENTER|MED CTR|MEDICAL CTR|CLINIC|PHYSICIANS|PEDIATRIC|DENTAL|"
     r"MEDICAL|HANGER)\b", "62"),
    (r"\b(CITY OF|COUNTY OF|STATE OF|COUNTY|POLICE|SHERIFF|FIRE DEPARTMENT|FIRE DEPT|FIRE RESCUE|DEPARTMENT OF|"
     r"US ARMY|U\.?S\.? ARMY|US NAVY|USAF|AIR FORCE|MARINE CORPS|GOVERNMENT|MUNICIPAL|TOWNSHIP|COSA|USPS)\b", "92"),
    (r"\b(LAW FIRM|LAW OFFICES?|LAW GROUP|LLP|ATTORNEYS?|LEGAL|ENGINEERING|ENGINEERS|CONSULTING|CONSULTANTS|"
     r"ACCOUNTING|CPAS?|ARCHITECTS?|HDR|HNTB)\b", "54"),
    (r"\b(BANK|BANCORP|CAPITAL|INVESTMENTS?|FINANCIAL|INSURANCE|SECURITIES|ASSET MANAGEMENT|WEALTH|USAA|"
     r"UNITED SERVICES AUTOMOBILE|PNC|COMERICA|CREDIT UNION|JPMORGAN)\b", "52"),
    (r"\b(REALTY|REAL ESTATE|REALTORS?|PROPERTIES|PROPERTY MANAGEMENT)\b", "53"),
    (r"\b(CONSTRUCTION|CONTRACTORS?|BUILDERS?|ROOFING|PLUMBING|ELECTRICIANS|PIPEFITTERS|CARPENTERS|IRONWORKERS|"
     r"LABORERS|SHEET METAL|PAVING|CONCRETE|ZACHRY|FLOOR COVERING)\b", "23"),
    (r"\b(VALERO|PHILLIPS 66|MARATHON PETROLEUM|REFINING|BOEING|FORD MOTOR|GENERAL MOTORS|STELLANTIS|ELI LILLY|"
     r"ALTRIA|LOCKHEED|RAYTHEON|MANUFACTURING|PHARMACEUTICALS?|CHEMICAL|AEROSPACE|STEEL|NUCOR|GLAXO\w*|PFIZER|"
     r"MERCK|ASTRAZENECA|ABBVIE|AMGEN|PEPSICO|SPECTRUM BRANDS|FOREST PRODUCTS)\b", "31-33"),
    (r"\b(PIPELINES?|MIDSTREAM|TRANSCANADA|KINDER MORGAN|ENTERPRISE PRODUCTS)\b", "48-49"),
    (r"\b(OIL|PETROLEUM|EXPLORATION|DRILLING|ENERGY TRANSFER|EXXON\w*|CHEVRON|CONOCO\w*|OCCIDENTAL|"
     r"PIONEER NATURAL|DEVON|DIAMONDBACK|EOG|ANADARKO|MINING)\b", "21"),
    (r"\b(ELECTRIC|UTILITIES|UTILITY|PUBLIC SERVICE|POWER|ENERGY|WATER COMPANY)\b", "22"),
    (r"\b(FRYS|FRY'S|SAFEWAY|KROGER|WALMART|WAL-MART|TARGET|H-?E-?B|COSTCO|HOME DEPOT|LOWE'S|LOWES|STORES?|RETAIL|"
     r"GROCERY|SUPERMARKET|PHARMACY|WALGREENS?|CVS|IKEA|NAPA|KLOTHES)\b", "44-45"),
    (r"\b(FEDEX|FEDERAL EXPRESS|UPS|UNITED PARCEL|AIRLINES?|RAILWAY|RAILROAD|BNSF|UNION PACIFIC|TRUCKING|"
     r"LOGISTICS|TRANSIT|FREIGHT|BUS CHARTER|CARGO)\b", "48-49"),
    (r"\b(MEDIA|PUBLISHING|SOFTWARE|MICROSOFT|GOOGLE|ALPHABET|META|ORACLE|TELECOM|COMMUNICATIONS|BROADCASTING|"
     r"NEWSPAPER)\b", "51"),
    (r"\b(RESTAURANTS?|HOTELS?|HILTON|MARRIOTT|HOSPITALITY|CATERING|CAFE|STEAKHOUSE|MCDONALD'?S|BRINKER)\b", "72"),
    (r"\b(CHURCH|MINISTRIES|UNION|LOCAL \d+|L/U|BROTHERHOOD|ASSOCIATION|FOUNDATION|NONPROFIT|NON-PROFIT|"
     r"FUNERAL)\b", "81"),
    (r"\b(FARMS?|RANCH|RANCHING|DAIRY|AGRICULTUR\w*|CATTLE)\b", "11"),
]
_OCC = [
    (r"\bRETIRED\b|^RETIREE\b", "retired"),
    (r"^(NOT EMPLOYED|UNEMPLOYED|NONE|DISABLED)$", "not_employed"),
    (r"^(UNKNOWN|N/?A|NA|NOT APPLICABLE|INFORMATION REQUESTED|REQUESTED)$", "unknown"),
    (r"^(HOMEMAKER|HOME ?MAKER|HOUSEWIFE|STAY AT HOME \w+)$", "homemaker"),
    (r"^(STUDENT)$", "student"),
    (r"\b(TEACHER|EDUCATOR|PROFESSOR|PRINCIPAL|SCHOOL \w+|SCHOOL SUPERINTENDENT|SUPERINTENDENT OF SCHOOLS|"
     r"PARAPROFESSIONAL|LIBRARIAN)\b", "61"),
    (r"\b(PHYSICIAN|NURSE|RN|MD|DOCTOR|SURGEON|DENTIST|THERAPIST|PSYCHOLOGIST|HOME HEALTHCARE|CAREGIVER|"
     r"PARAMEDIC|PHYSICIAN ASSISTANT|ANESTHESIOLOGIST|OPTOMETRIST|OPHTHALMOLOGIST|CHIROPRACTOR|VETERINARIAN|"
     r"PHLEBOTOM\w*)\b", "62"),
    (r"\b(FIREFIGHTER|FIRE FIGHTER|POLICE OFFICER|DEPUTY|SHERIFF|TROOPER|PUBLIC SERVANT|LEGISLATOR|"
     r"STATE REPRESENTATIVE|STATE SENATOR|JUDGE|CORRECTIONS OFFICER|MILITARY|COUNTY ATTORNEY|CITY ATTORNEY|"
     r"DISTRICT ATTORNEY|USPS)\b", "92"),
    (r"\b(ATTORNEY|LAWYER|PARALEGAL|CPA|ARCHITECT|CONSULTANT)\b", "54"),  # not ENGINEER/ACCOUNTANT: any industry
    (r"\b(BANKER|FINANCIAL ADVISOR|FINANCIAL PLANNER|INVESTOR|INVESTMENT \w+|INSURANCE \w+|UNDERWRITER|"
     r"PORTFOLIO MANAGER|ACTUARY|PRIVATE EQUITY|HEDGE FUND|VENTURE CAPITAL)\b", "52"),
    (r"\b(REALTOR|REAL ESTATE|PROPERTY MANAGER|LANDLORD)\b", "53"),
    (r"\b(CARPENTER|PLUMBER|PIPEFITTER|ELECTRICIAN|IRONWORKER|SHEET METAL WORKER|JOURNEYMAN|LABORER|BRICKLAYER|"
     r"CONTRACTOR|BUILDER|OPERATING ENGINEER|HVAC)\b", "23"),
    (r"\b(FACTORY WORKER|MACHINIST|ASSEMBLER|AUTO WORKER|AUTOWORKER|WELDER)\b", "31-33"),
    (r"\b(FOOD CLERK|COURTESY CLERK|CASHIER|GROCERY \w+|STOCKER|MEAT CUTTER|PHARMACY TECHNICIAN)\b", "44-45"),
    (r"\b(PILOT|TRUCK DRIVER|FLIGHT ATTENDANT|RAILROAD \w+|CONDUCTOR|LOCOMOTIVE)\b", "48-49"),
    (r"\b(FARMER|RANCHER|AGRICULTURE|GROWER)\b", "11"),
    (r"\b(PASTOR|MINISTER|CLERGY|PRIEST|RABBI|L/U REPRESENTATIVE|UNION REPRESENTATIVE|UNION ORGANIZER|"
     r"FUNERAL DIRECTOR)\b", "81"),  # not "union member": that says nothing about the member's industry
    (r"\b(CHEF|COOK|SERVER|BARTENDER|RESTAURANT \w+|HOTEL \w+)\b", "72"),
    (r"\b(SOFTWARE \w+|PROGRAMMER|DEVELOPER|JOURNALIST|REPORTER|EDITOR)\b", "51"),
    (r"\b(MUSICIAN|ARTIST|ACTOR|ACTRESS|WRITER|AUTHOR|PHOTOGRAPHER|DANCER|PERFORMER)\b", "71"),
    (r"\b(PEST CONTROL|JANITOR\w*|LANDSCAP\w*|SECURITY GUARD|STAFFING)\b", "56"),
]
_EMP_RX = [(re.compile(p), c) for p, c in _EMP]
_OCC_RX = [(re.compile(p), c) for p, c in _OCC]


def normalize(s) -> str | None:
    """Must match NORM_SQL exactly (SQL joins on it)."""
    if s is None:
        return None
    t = re.sub(r"\s+", " ", str(s).upper()).strip()
    return t or None


NORM_SQL = "nullif(trim(regexp_replace(upper({x}), '\\s+', ' ', 'g')), '')"


def rule_code(employer: str | None, occupation: str | None) -> tuple[str, str] | None:
    """(code, rule) or None (left for the classifier). Precedence:
    1. employer says retired / not employed / student / homemaker  -> that
    2. employer names an industry (a company is a better industry signal than a job title: an engineer at Boeing
       works in manufacturing)                                     -> that sector
    3. occupation matches                                          -> that code
    4. employer is 'self' with no usable occupation                -> self_employed
    5. both blank                                                  -> unknown"""
    emp, occ = normalize(employer), normalize(occupation)
    if emp and re.fullmatch(r"N/?A|NA|NONE|NOT APPLICABLE|INFORMATION REQUESTED|REQUESTED|UNKNOWN|-+|\.", emp):
        emp = None  # placeholder, not a statement about employment
    emp_hit = next(((c, f"employer:{rx.pattern[:40]}") for rx, c in _EMP_RX if emp and rx.search(emp)), None)
    occ_hit = next(((c, f"occupation:{rx.pattern[:40]}") for rx, c in _OCC_RX if occ and rx.search(occ)), None)
    if emp_hit and emp_hit[0] in NON_INDUSTRY and emp_hit[0] != "self_employed":
        return emp_hit
    if emp_hit and emp_hit[0] == "92" and occ_hit and occ_hit[0] in ("61", "62"):
        return occ_hit  # NAICS codes the establishment: a county clinic is 62, a state school 61
    if emp_hit and emp_hit[0] in SECTORS:
        return emp_hit
    if occ_hit:
        return occ_hit
    if emp_hit:  # self_employed
        return emp_hit
    if not emp and not occ:
        return ("unknown", "blank")
    return None


def _text(emp, occ) -> str:
    return f"E:{emp or ''} | O:{occ or ''}"


def _pairs_sql() -> str:
    from .elections import _inline
    from .sources import states
    parts = []
    for m in states.REGISTRY.values():
        if not m.is_synced():
            continue
        sql, p = m.normalized_sql(None, None)
        parts.append(f"SELECT {NORM_SQL.format(x='employer')} AS emp, {NORM_SQL.format(x='occupation')} AS occ "
                     f"FROM ({_inline(sql, p)}) b({', '.join(store.COLUMN_NAMES)}) "
                     "WHERE contributor_type = 'individual'")
    if not parts:
        raise ValueError("No state data synced.")
    return f"SELECT emp, occ, count(*) AS n FROM ({' UNION ALL '.join(parts)}) GROUP BY emp, occ"


def out_path() -> Path:
    d = data_dir() / "industry"
    d.mkdir(parents=True, exist_ok=True)
    return d / "pair_codes.parquet"


def build(min_train_count: int = 1, seed: int = 0) -> dict:
    """Code every distinct pair in synced data: rules, then a classifier trained on rule-coded industry pairs."""
    import numpy as np
    from sklearn.feature_extraction.text import HashingVectorizer
    from sklearn.linear_model import SGDClassifier

    con = duckdb.connect()
    pairs = con.execute(_pairs_sql()).fetchall()
    con.close()
    coded = []
    train_x, train_y, train_w = [], [], []
    for emp, occ, n in pairs:
        hit = rule_code(emp, occ)
        coded.append([emp, occ, n, hit[0] if hit else None, "rule" if hit else None, 1.0 if hit else None,
                      hit[1] if hit else None])
        if hit and hit[0] in SECTORS and n >= min_train_count:
            train_x.append(_text(emp, occ))
            train_y.append(hit[0])
            train_w.append(float(np.log1p(n)))
    vec = HashingVectorizer(analyzer="char_wb", ngram_range=(3, 5), n_features=2 ** 20, alternate_sign=False,
                            norm="l2")
    clf = SGDClassifier(loss="log_loss", alpha=2e-6, max_iter=20, tol=None, random_state=seed)
    todo = [i for i, r in enumerate(coded) if r[3] is None]
    if len(set(train_y)) < 2:  # too little rule-coded variety to learn from: leave the rest unclassified
        for i in todo:
            coded[i][3:7] = ["unclassified", "no_model", None, None]
        todo = []
    else:
        clf.fit(vec.transform(train_x), train_y, sample_weight=train_w)
    if todo:
        P = clf.predict_proba(vec.transform([_text(coded[i][0], coded[i][1]) for i in todo]))
        best = P.argmax(axis=1)
        for i, b, p in zip(todo, best, P.max(axis=1)):
            if p >= CONFIDENCE:
                coded[i][3:7] = [clf.classes_[b], "model", round(float(p), 3), None]
            else:
                coded[i][3:7] = ["unclassified", "model_low_confidence", round(float(p), 3), None]
    path = out_path()
    tmp = path.with_suffix(".json")
    tmp.write_text(json.dumps([dict(zip(["emp", "occ", "n", "code", "method", "confidence", "rule"], r))
                               for r in coded]))
    con = duckdb.connect()
    con.execute(f"COPY (SELECT * FROM read_json('{tmp}', format='array', columns={{'emp': 'VARCHAR', "
                "'occ': 'VARCHAR', 'n': 'BIGINT', 'code': 'VARCHAR', 'method': 'VARCHAR', 'confidence': 'DOUBLE', "
                f"'rule': 'VARCHAR'}})) TO '{path}' (FORMAT parquet)")
    stats = con.execute(f"""SELECT method, count(*) AS pairs, sum(n) AS gifts FROM read_parquet('{path}')
                            GROUP BY 1 ORDER BY 3 DESC""").fetchall()
    con.close()
    tmp.unlink()
    meta = {"built": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"), "pairs": len(coded),
            "training_pairs": len(train_x), "confidence_threshold": CONFIDENCE,
            "by_method": [{"method": m, "pairs": p, "gifts": g} for m, p, g in stats]}
    (path.parent / "build.json").write_text(json.dumps(meta, indent=2))
    return meta


def code_pairs(rows: list[tuple]) -> list[str]:
    """Look up codes for (employer, occupation) pairs from the built table (rules as fallback)."""
    path = out_path()
    con = duckdb.connect()
    con.execute("CREATE TEMP TABLE q (i INTEGER, emp VARCHAR, occ VARCHAR)")
    con.executemany("INSERT INTO q VALUES (?, ?, ?)", [(i, normalize(e), normalize(o)) for i, (e, o) in enumerate(rows)])
    got = dict(con.execute(f"""SELECT q.i, p.code FROM q LEFT JOIN read_parquet('{path}') p
                               ON p.emp IS NOT DISTINCT FROM q.emp AND p.occ IS NOT DISTINCT FROM q.occ""").fetchall()
               ) if path.exists() else {}
    con.close()
    out = []
    for i, (e, o) in enumerate(rows):
        c = got.get(i)
        if c is None:
            hit = rule_code(e, o)
            c = hit[0] if hit else "unclassified"
        out.append(c)
    return out


def evaluate(gold: Path = GOLD_PATH) -> dict:
    """Precision / recall per sector on the labeled sample, plus coverage. Gold labels: columns employer,
    occupation, label (sector code or non-industry key), labeled_by."""
    with open(gold) as f:
        rows = list(csv.DictReader(f))
    pred = code_pairs([(r["employer"] or None, r["occupation"] or None) for r in rows])
    labels = sorted({r["label"] for r in rows} | set(pred))
    per = {}
    for lab in labels:
        tp = sum(1 for r, p in zip(rows, pred) if p == lab and r["label"] == lab)
        fp = sum(1 for r, p in zip(rows, pred) if p == lab and r["label"] != lab)
        fn = sum(1 for r, p in zip(rows, pred) if p != lab and r["label"] == lab)
        if tp + fp + fn:
            per[lab] = {"precision": round(tp / (tp + fp), 3) if tp + fp else None,
                        "recall": round(tp / (tp + fn), 3) if tp + fn else None, "support": tp + fn}
    coded = [(r, p) for r, p in zip(rows, pred) if p != "unclassified"]
    acc_coded = sum(1 for r, p in coded if p == r["label"]) / len(coded) if coded else None
    return {"n": len(rows), "accuracy_all": round(sum(1 for r, p in zip(rows, pred) if p == r["label"]) / len(rows), 3),
            "accuracy_when_coded": round(acc_coded, 3) if acc_coded is not None else None,
            "coverage": round(len(coded) / len(rows), 3), "per_label": per,
            "labeled_by": sorted({r.get("labeled_by", "") for r in rows})}


EVAL_PATH = ROOT / "reference" / "industry_eval.json"
DEV_PATH = ROOT / "reference" / "industry_dev.csv"
TEST_PATH = ROOT / "reference" / "industry_test.csv"


def write_evaluation() -> dict:
    """Score the current coder on the development set (used to fix rules, so optimistic) and the held-out test set
    (labeled blind to predictions, never used for tuning). The README and reports cite this file, not typed numbers."""
    meta_path = out_path().parent / "build.json"
    coverage = None
    if out_path().exists():
        c = duckdb.connect()
        coverage = dict(zip(["share_of_gifts_coded", "share_of_pairs_coded"], c.execute(f"""
            SELECT round(sum(n) FILTER (WHERE code NOT IN ('unclassified', 'unknown')) / sum(n), 3),
                   round(count(*) FILTER (WHERE code NOT IN ('unclassified', 'unknown')) / count(*), 3)
            FROM read_parquet('{out_path()}')""").fetchone()))
        c.close()
    out = {"generated": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
           "coverage_all_synced_data": coverage,
           "note_on_dollars": "Coverage by dollars is lower than by gifts (large donors often list holding companies or "
                              "executive titles); use compare_elections(group_by='industry') for the dollar view.",
           "build": json.loads(meta_path.read_text()) if meta_path.exists() else None,
           "development": evaluate(DEV_PATH), "held_out_test": evaluate(TEST_PATH),
           "caveat": "Labels were assigned by Claude (test set blind to predictions) and need human review; the "
                     "development score is optimistic because rules were fixed from its errors."}
    EVAL_PATH.write_text(json.dumps(out, indent=2))
    return out


def industry_join_sql(alias: str = "b") -> str:
    """LEFT JOIN that adds `industry` (donor sector code) to a base with employer/occupation/contributor_type."""
    path = out_path()
    if not path.exists():
        return f"LEFT JOIN (SELECT NULL::VARCHAR AS emp, NULL::VARCHAR AS occ, NULL::VARCHAR AS code WHERE false) ic ON false"
    return (f"LEFT JOIN read_parquet('{path}') ic ON ic.emp IS NOT DISTINCT FROM {NORM_SQL.format(x=alias + '.employer')} "
            f"AND ic.occ IS NOT DISTINCT FROM {NORM_SQL.format(x=alias + '.occupation')}")
