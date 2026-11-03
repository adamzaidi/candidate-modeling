"""Derived labels shared by every source: party, self-funding, contribution kind, conduits.

Rules: party-labels, self-funding, contribution-kind, conduits (see scope.py).
"""


def sql_str(s) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def case_map(expr: str, mapping: dict[str, str], default: str = "'unknown'") -> str:
    """SQL CASE mapping raw values (compared trimmed and case-insensitively) to normalized categories."""
    whens = " ".join(f"WHEN {sql_str(k.strip().upper())} THEN {sql_str(v)}" for k, v in mapping.items())
    return f"CASE upper(trim({expr})) {whens} ELSE {default} END"

# ---------- party ----------

PARTIES = ("democratic", "republican", "libertarian", "green", "other", "cross_filed", "nonpartisan", "unknown",
           "not_applicable")
PARTY_MAP = {
    # PA codes
    "DEM": "democratic", "REP": "republican", "LIB": "libertarian", "GRN": "green", "IND": "other", "CST": "other",
    "KEY": "other", "OTH": "other", "NON": "nonpartisan", "D/R": "cross_filed", "R/D": "cross_filed",
    # MI labels
    "Democratic Party": "democratic", "Republican Party": "republican", "Libertarian Party": "libertarian",
    "Green Party": "green", "U.S. Taxpayers Party": "other", "No Party Affiliation": "nonpartisan",
    "Natural Law Party": "other", "Working Class Party": "other",
    # GA labels
    "Democrat": "democratic", "Republican": "republican", "Libertarian": "libertarian", "Other": "other",
    "Non-Partisan": "nonpartisan",
    # FEC party_full / profile free text
    "DEMOCRATIC PARTY": "democratic", "REPUBLICAN PARTY": "republican", "LIBERTARIAN PARTY": "libertarian",
    "GREEN PARTY": "green", "INDEPENDENT": "other", "NONPARTISAN": "nonpartisan",
}


def party_sql(expr: str) -> str:
    """Exact map first, then prefix rules (e.g. 'Democratic' -> democratic), else 'other'."""
    u = f"upper(trim({expr}))"
    return (f"CASE WHEN nullif(trim({expr}), '') IS NULL THEN 'unknown' "
            f"ELSE coalesce({case_map(expr, PARTY_MAP, 'NULL')}, "
            f"CASE WHEN {u} LIKE 'DEM%' THEN 'democratic' WHEN {u} LIKE 'REP%' OR {u} = 'GOP' THEN 'republican' "
            f"WHEN {u} LIKE 'LIB%' THEN 'libertarian' WHEN {u} LIKE 'GREEN%' THEN 'green' "
            f"WHEN {u} LIKE 'NON%' OR {u} LIKE 'NO PARTY%' OR {u} = 'NPA' THEN 'nonpartisan' ELSE 'other' END) END")


def committee_party_sql(party_expr: str, kind_expr: str, name_expr: str) -> str:
    """Candidates: their party (or unknown). Party committees: party from the name. Other committees: not_applicable."""
    n = f"upper(coalesce({name_expr}, ''))"
    return (f"CASE WHEN {kind_expr} = 'candidate' THEN {party_sql(party_expr)} "
            f"WHEN {kind_expr} = 'party_committee' AND regexp_matches({n}, '\\bDEMOCRAT') THEN 'democratic' "
            f"WHEN {kind_expr} = 'party_committee' AND regexp_matches({n}, '\\b(REPUBLICAN|GOP)\\b') THEN 'republican' "
            f"WHEN {party_sql(party_expr)} IN ('democratic', 'republican') THEN {party_sql(party_expr)} "
            f"ELSE 'not_applicable' END")  # PA PACs file 'OTH' meaning no party, not a third party


def normalize_party(value: str | None) -> str:
    if not value or not str(value).strip():
        return "unknown"
    v = str(value).strip()
    hit = PARTY_MAP.get(v) or PARTY_MAP.get(v.upper())
    if hit:
        return hit
    u = v.upper()
    for prefix, label in (("DEM", "democratic"), ("REP", "republican"), ("LIB", "libertarian"), ("GREEN", "green"),
                          ("NON", "nonpartisan"), ("NO PARTY", "nonpartisan")):
        if u.startswith(prefix):
            return label
    return "republican" if u == "GOP" else "other"


# ---------- conduits ----------

# Platforms that forward earmarked gifts. Their committees' receipts duplicate the gifts candidates itemize.
CONDUIT_PATTERN = r"\b(ACTBLUE|WINRED|DEMOCRACY ENGINE|ANEDOT)\b"


def conduit_sql(name_expr: str) -> str:
    return f"coalesce(regexp_matches(upper({name_expr}), {sql_str(CONDUIT_PATTERN)}), false)"


# ---------- contribution kind ----------

KINDS = ("monetary", "in_kind", "return", "transfer", "loan", "other_receipt")
EXCLUDED_KINDS = ("loan", "other_receipt")  # rule loans-excluded: not contributions


# ---------- self-funding ----------

def first_last(name: str | None) -> tuple[str | None, str | None]:
    """'James Talarico' / 'Talarico, James (The Honorable)' -> ('James', 'Talarico')."""
    if not name:
        return None, None
    import re
    n = re.sub(r"\(.*?\)", "", name).strip()
    if "," in n:
        last, rest = n.split(",", 1)
        first = rest.strip().split(" ")[0] if rest.strip() else None
        return first, last.strip()
    parts = [p for p in n.split() if p.upper() not in ("JR", "JR.", "SR", "SR.", "II", "III", "IV")]
    return (parts[0], parts[-1]) if len(parts) >= 2 else (None, parts[0] if parts else None)


# Self-funding = an individual contribution whose donor name contains the candidate's surname as a word and
# starts a word with the first 4 letters of their first name (Greg -> GREGORY ok; Josh vs JOSEPH rejected).
# Nicknames on one side only (legal "William" vs donor "BURT") are missed: a false negative, never a guess.
MACROS = r"""
CREATE OR REPLACE MACRO self_match(contributor, cand_last, cand_first) AS
    cand_last IS NOT NULL AND length(trim(cand_last)) > 1 AND contributor IS NOT NULL
    AND regexp_matches(upper(contributor), '\b' || regexp_escape(upper(trim(cand_last))) || '\b')
    AND (nullif(trim(coalesce(cand_first, '')), '') IS NULL
         OR regexp_matches(upper(contributor), '\b' || regexp_escape(upper(left(trim(cand_first), 4)))));
"""


def install_macros(con) -> None:
    con.execute(MACROS)


def _clean_name(expr: str) -> str:
    return f"trim(regexp_replace(coalesce({expr}, ''), '\\(.*?\\)', '', 'g'))"


def name_last_sql(expr: str) -> str:
    """'Talarico, James (The Honorable)' or 'James Talarico' -> 'Talarico'."""
    n = _clean_name(expr)
    return (f"nullif(CASE WHEN contains({n}, ',') THEN trim(split_part({n}, ',', 1)) "
            f"ELSE regexp_extract({n}, '(\\S+)$') END, '')")


def name_first_sql(expr: str) -> str:
    n = _clean_name(expr)
    return (f"nullif(CASE WHEN contains({n}, ',') THEN split_part(trim(split_part({n}, ',', 2)), ' ', 1) "
            f"ELSE split_part({n}, ' ', 1) END, '')")
