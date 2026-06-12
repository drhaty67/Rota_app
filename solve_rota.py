#!/usr/bin/env python3
from __future__ import annotations
import argparse
import re
import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Set, Tuple
from openpyxl import load_workbook
from ortools.sat.python import cp_model

# ---------------------------------------------------------------------------
# Allocation model (18-week cycle)
# ---------------------------------------------------------------------------
# 1 WTE  -> 5 allocations, exactly one of each block type:
#           AB1, AB2, DMonThu, WeekendAB, WeekendMixed
# <1 WTE -> round(wte * 5) allocations; block types reduced by BLOCK_DROP_ORDER.
#           If special_circumstances (col G) is populated, those rules ARE the
#           complete spec — e.g. "1 AB1 or AB2, 1 WeekendAB or WeekendMixed,
#           1/2 DMonThu" gives exactly 2-3 allocations as specified.
# >1 WTE -> 5 base allocations + extras from special_circumstances rules.
#
# Half-D blocks (from "1/2 DMonThu" in special_circumstances):
#   DMonTue  = D shift covering Mon + Tue only
#   DWedThu  = D shift covering Wed + Thu only
# The solver picks one of these two per cycle for the consultant.
# ---------------------------------------------------------------------------

BLOCK_TYPES = ["AB1", "AB2", "DMonThu", "DMonTue", "DWedThu", "WeekendAB", "WeekendMixed"]
# Drop order for <1 WTE consultants with NO special_circumstances rules.
# Half-D types (DMonTue, DWedThu) are never in the default allowed set —
# they only appear when explicitly specified via special_circumstances.
BLOCK_DROP_ORDER = ["WeekendMixed", "WeekendAB", "DMonThu", "AB2", "AB1"]
# Default block types for consultants with no special_circumstances rules
DEFAULT_BLOCK_TYPES = ["AB1", "AB2", "DMonThu", "WeekendAB", "WeekendMixed"]

# Block types that count as "D-slot" for cardiac constraint purposes
D_BLOCK_TYPES = {"DMonThu", "DMonTue", "DWedThu"}
# Block types that count as "weekend" for fairness/consecutive-weekend constraints
WEEKEND_BLOCK_TYPES = {"WeekendAB", "WeekendMixed"}
# Block types that count as "A-slot"
A_BLOCK_TYPES = {"AB1", "AB2", "WeekendAB", "WeekendMixed"}


def excel_date(v) -> Optional[date]:
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        try:
            return datetime.fromisoformat(s).date()
        except Exception:
            pass
        try:
            from dateutil import parser as _parser
            return _parser.parse(s, dayfirst=True).date()
        except Exception:
            return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, (int, float)):
        try:
            return date(1899, 12, 30) + timedelta(days=int(v))
        except Exception:
            return None
    return None


def fetch_period_dates_from_supabase(period_name: str):
    import os
    from datetime import datetime as _dt
    try:
        from supabase import create_client
    except Exception as e:
        raise RuntimeError(f"Supabase client not available: {e}.") from e

    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_ANON_KEY")
    if not url or not key:
        raise RuntimeError("Missing SUPABASE_URL and/or SUPABASE_KEY environment variables.")

    sb = create_client(url, key)
    resp = sb.table("rota_periods").select("start_date,end_date").eq("name", period_name).limit(1).execute()
    data = resp.data or []
    if not data:
        raise RuntimeError(f"No rota_periods row found with name={period_name!r}.")
    row = data[0]

    def _to_date(v):
        if v is None:
            return None
        if hasattr(v, "date") and not isinstance(v, str):
            try:
                return v.date()
            except Exception:
                pass
        if isinstance(v, str):
            s = v.strip()
            if not s:
                return None
            try:
                return _dt.fromisoformat(s.replace('Z', '+00:00')).date()
            except Exception:
                from dateutil import parser as _parser
                return _parser.parse(s, dayfirst=True).date()
        raise RuntimeError(f"Unparseable date from Supabase: {v!r}")

    s = _to_date(row.get("start_date"))
    e = _to_date(row.get("end_date"))
    if not s or not e:
        raise RuntimeError(f"rota_periods row '{period_name}' missing start_date/end_date.")
    return s, e


def _parse_unique_tags(val) -> frozenset:
    if val is None:
        return frozenset()
    s = str(val).strip().lower().replace(";", ",").replace("\n", ",")
    return frozenset(p.strip() for p in s.split(",") if p.strip())


def _has_tag(tags: frozenset, needle: str) -> bool:
    needle = needle.strip().lower()
    return any(needle in t for t in tags)


def _parse_special_circumstances(val) -> list:
    """Parse the special_circumstances column (col G) into a list of block rules.

    Each rule is a dict:
        {"count": int, "blocks": [str, ...], "fractional": bool}

    "count"      – exact number of allocations of this type required across the cycle.
    "blocks"     – list of block types that satisfy this rule (OR semantics: any one counts).
    "fractional" – if True, count was expressed as a fraction (e.g. "1/2 D") meaning
                   the solver should treat it as a *soft* target (0 or 1 allowed, aim for 1).
                   If False, it is a hard exact count.

    Supported cell formats (case-insensitive, comma or semicolon separated clauses):
        "1 AB1 or AB2"      -> 1 allocation from {AB1, AB2}
        "1/2 D"             -> 0 or 1 DMonThu (fractional, soft)
        "1 D"               -> exactly 1 DMonThu
        "2 WeekendAB"       -> exactly 2 WeekendAB
        "AB1, AB2"          -> 1 each of AB1 and AB2 (legacy: bare block names)
        "2"                 -> 2 extra allocations of any allowed type (legacy: plain int)

    Block name aliases (all case-insensitive):
        A, AB1              -> AB1
        B, AB2              -> AB2
        D, DMonThu          -> DMonThu
        WeekendAB, WAB      -> WeekendAB
        WeekendMixed, WM    -> WeekendMixed
    """
    if val is None:
        return []
    raw = str(val).strip()
    if not raw:
        return []

    # Normalise aliases to canonical block type names
    _ALIASES = {
        "a": "AB1", "ab1": "AB1",
        "b": "AB2", "ab2": "AB2",
        "d": "DMonThu", "dmonth": "DMonThu", "dmonthu": "DMonThu", "dmonthur": "DMonThu",
        "weekendab": "WeekendAB", "wab": "WeekendAB",
        "weekendmixed": "WeekendMixed", "wm": "WeekendMixed",
        # half-D aliases
        "dmontue": "DMonTue", "dmt": "DMonTue",
        "dwedthu": "DWedThu", "dwt": "DWedThu",
    }
    # Also accept exact canonical names
    for bt in BLOCK_TYPES:
        _ALIASES[bt.lower()] = bt

    def _resolve(token: str) -> Optional[str]:
        return _ALIASES.get(token.strip().lower())

    # Legacy: plain integer -> N extra blocks of any type
    try:
        n = int(raw)
        if n > 0:
            return [{"count": n, "blocks": list(BLOCK_TYPES), "fractional": False}]
        return []
    except ValueError:
        pass

    rules = []
    # Split on commas/semicolons; "or" within a clause is handled below
    clauses = [c.strip() for c in re.split(r"[,;]", raw) if c.strip()]

    for clause in clauses:
        clause = clause.strip()
        if not clause:
            continue

        # Match: [count[/denom]] <block> [or <block>]*
        # e.g. "1 AB1 or AB2", "1/2 D", "2 WeekendAB", "1 A or B"
        m = re.match(
            r"^(\d+(?:/\d+)?)\s+(.+)$",
            clause,
            re.IGNORECASE,
        )
        if m:
            count_str = m.group(1)
            blocks_str = m.group(2)

            # Parse count (may be fractional like 1/2)
            fractional = False
            if "/" in count_str:
                num, denom = count_str.split("/", 1)
                try:
                    frac = int(num) / int(denom)
                    count = max(1, round(frac))  # round to nearest int for hard constraint
                    fractional = True
                except Exception:
                    count = 1
            else:
                count = int(count_str)

            # Parse block names (split on "or")
            block_tokens = [t.strip() for t in re.split(r"\bor\b", blocks_str, flags=re.IGNORECASE)]
            blocks = [_resolve(t) for t in block_tokens]
            blocks = [b for b in blocks if b is not None]

            if blocks:
                # "1/2 DMonThu" -> fractional D -> use half-D block types instead
                if fractional and blocks == ["DMonThu"]:
                    blocks = ["DMonTue", "DWedThu"]
                rules.append({"count": count, "blocks": blocks, "fractional": fractional})
            continue

        # Fallback: bare block name(s) separated by "or" (no leading count -> count=1)
        block_tokens = [t.strip() for t in re.split(r"\bor\b", clause, flags=re.IGNORECASE)]
        blocks = [_resolve(t) for t in block_tokens]
        blocks = [b for b in blocks if b is not None]
        if blocks:
            rules.append({"count": 1, "blocks": blocks, "fractional": False})

    return rules


@dataclass(frozen=True)
class Consultant:
    name: str
    cardiac: bool
    wte: float
    eligible_a: bool
    eligible_d: bool
    active: bool
    unique: str = ""
    unique_tags: frozenset = frozenset()
    weekend_wte_cap: bool = False
    no_weekend: bool = False
    no_weekendab: bool = False
    no_d: bool = False
    # Parsed special_circumstances rules: tuple of dicts
    # Each dict: {"count": int, "blocks": [str,...], "fractional": bool}
    block_rules: tuple = ()  # tuple of rule dicts from _parse_special_circumstances


def daterange(d0: date, d1: date) -> List[date]:
    out, d = [], d0
    while d <= d1:
        out.append(d)
        d += timedelta(days=1)
    return out


def read_preferred_shifts_from_workbook(wb) -> List[Dict]:
    prefs: List[Dict] = []
    if "preferred_shifts" not in wb.sheetnames:
        return prefs
    ws = wb["preferred_shifts"]
    header = {}
    for c in range(1, ws.max_column + 1):
        v = ws.cell(1, c).value
        if v is not None:
            header[str(v).strip().lower()] = c

    def col(*names):
        for n in names:
            if n in header:
                return header[n]
        return None

    c_name = col("consultant_name", "consultant", "name")
    c_s = col("start_date", "startdate", "start")
    c_e = col("end_date", "enddate", "end")
    c_t = col("shift_type", "shifttype", "shift")
    c_w = col("weight", "pref_weight", "priority")

    if not (c_name and c_s and c_e and c_t):
        return prefs

    for r in range(2, ws.max_row + 1):
        nm = ws.cell(r, c_name).value
        if nm is None or str(nm).strip() == "":
            continue
        sd = excel_date(ws.cell(r, c_s).value)
        ed = excel_date(ws.cell(r, c_e).value)
        st = ws.cell(r, c_t).value
        if sd is None or ed is None or st is None:
            continue
        wt = 3
        if c_w:
            try:
                wt = int(ws.cell(r, c_w).value or 3)
            except Exception:
                wt = 3
        prefs.append({
            "consultant_name": str(nm).strip(),
            "start_date": sd,
            "end_date": ed,
            "shift_type": str(st).strip(),
            "weight": max(1, min(5, wt)),
        })
    return prefs


def read_pre_allocations_from_workbook(wb) -> List[Dict]:
    """Read pre-allocated shifts from a sheet named 'PreAllocations' if present.

    Expected columns (header row 1):
      consultant_name (or Name), week_start (or WeekStart), block_type (or Block)

    Returns a list of dicts:
      {"consultant_name": str, "week_start": date, "block_type": str}
    """
    pre: List[Dict] = []
    sheet_name = None
    for sn in ("PreAllocations", "preallocations", "Pre_Allocations"):
        if sn in wb.sheetnames:
            sheet_name = sn
            break
    if sheet_name is None:
        return pre

    ws = wb[sheet_name]
    header = {}
    for c in range(1, ws.max_column + 1):
        v = ws.cell(1, c).value
        if v is not None:
            header[str(v).strip().lower()] = c

    def col(*names):
        for n in names:
            if n in header:
                return header[n]
        return None

    c_name = col("consultant_name", "consultant", "name")
    c_week = col("week_start", "weekstart", "week")
    c_block = col("block_type", "block", "blocktype", "shift")

    if not (c_name and c_week and c_block):
        return pre

    for r in range(2, ws.max_row + 1):
        nm = ws.cell(r, c_name).value
        if nm is None or str(nm).strip() == "":
            continue
        wk = excel_date(ws.cell(r, c_week).value)
        bt = ws.cell(r, c_block).value
        if wk is None or bt is None:
            continue
        bt_s = str(bt).strip()
        if bt_s not in BLOCK_TYPES:
            continue
        pre.append({
            "consultant_name": str(nm).strip(),
            "week_start": wk,
            "block_type": bt_s,
        })
    return pre


def read_inputs(path: str, override_start: Optional[date] = None, override_end: Optional[date] = None):
    wb = load_workbook(path, data_only=False)
    cfg = wb["Config"]

    if override_start is not None or override_end is not None:
        for r in range(1, 80):
            lab = str(cfg[f"A{r}"].value).strip() if cfg[f"A{r}"].value is not None else ""
            if lab == "CycleStartDate" and override_start is not None:
                cfg[f"B{r}"].value = override_start
            if lab == "CycleEndDate" and override_end is not None:
                cfg[f"B{r}"].value = override_end

    def get_cfg(label: str):
        for r in range(1, 80):
            if str(cfg[f"A{r}"].value).strip() == label:
                return excel_date(cfg[f"B{r}"].value)
        raise ValueError(f"Config label not found: {label}")

    start = override_start or get_cfg("CycleStartDate")
    end = override_end or get_cfg("CycleEndDate")
    if start is None or end is None:
        raise ValueError("CycleStartDate/CycleEndDate missing or unparseable in Config sheet.")

    cws = wb["Consultants"]
    consultants: List[Consultant] = []
    for r in range(2, 1000):
        nm = cws[f"A{r}"].value
        if not nm:
            continue
        tags = _parse_unique_tags(cws[f"H{r}"].value)
        sc_rules = _parse_special_circumstances(cws[f"G{r}"].value)  # col G = special_circumstances
        consultants.append(Consultant(
            name=str(nm),
            cardiac=bool(cws[f"B{r}"].value),
            wte=float(cws[f"C{r}"].value or 0.0),
            eligible_a=bool(cws[f"D{r}"].value),
            eligible_d=bool(cws[f"E{r}"].value),
            active=bool(cws[f"F{r}"].value),
            unique=str(cws[f"H{r}"].value or ""),
            unique_tags=tags,
            weekend_wte_cap=_has_tag(tags, "weekendab = 1 wte"),
            no_weekendab=_has_tag(tags, "no weekendab"),
            no_weekend=_has_tag(tags, "no weekend"),
            no_d=_has_tag(tags, "no d"),
            block_rules=tuple(sc_rules),
        ))
    consultants = [c for c in consultants if c.active]
    if not consultants:
        raise ValueError("No active consultants found.")

    lws = wb["Leave"]
    leave_map: Dict[str, Set[date]] = {c.name: set() for c in consultants}
    for r in range(2, 5000):
        nm = lws[f"A{r}"].value
        if not nm:
            continue
        if not bool(lws[f"E{r}"].value):
            continue
        s = excel_date(lws[f"B{r}"].value)
        e = excel_date(lws[f"C{r}"].value)
        if not s or not e:
            continue
        nm = str(nm)
        if nm not in leave_map:
            continue
        for d in daterange(s, e):
            leave_map[nm].add(d)

    bws = wb["BankHolidays"]
    bh: Set[date] = set()
    for r in range(2, 2000):
        d = excel_date(bws[f"A{r}"].value)
        if d:
            bh.add(d)

    prefs = read_preferred_shifts_from_workbook(wb)
    pre_allocs = read_pre_allocations_from_workbook(wb)
    return start, end, consultants, leave_map, bh, prefs, pre_allocs


def _target_allocations(c: Consultant, n_weeks: int = 18) -> Tuple[int, List[str]]:
    """Return (target_count, allowed_block_types) for a consultant.

    The model assigns 5 blocks per week across all consultants.
    Total block-slots per cycle = n_weeks * 5.
    Total WTE across all consultants determines each person's fair share.

    This function returns each consultant's TARGET count and ALLOWED block types.
    The actual target is computed in solve() once total WTE is known, using
    WTE-proportional fairness.  This function returns a *maximum cap* based on
    the 1-week-gap constraint (max = ceil(n_weeks / 2)) and the allowed types.

    Special circumstances rules (col G):
        < 1.0 WTE with rules: rules define WHICH block types, not counts.
            Counts are computed fairly in solve() like everyone else.
        >= 1.0 WTE with rules: rules add EXTRA allowed block types (over 1.0 WTE base).

    The returned target here is used only to set the allowed block type list.
    The actual numeric target is set in solve() proportionally.
    """
    def _apply_eligibility(allowed: List[str]) -> List[str]:
        result = list(allowed)
        if not c.eligible_a:
            for bt in ("AB1", "AB2", "WeekendAB", "WeekendMixed"):
                if bt in result:
                    result.remove(bt)
        if not c.eligible_d or c.no_d:
            for bt in ("DMonThu", "DMonTue", "DWedThu", "WeekendMixed"):
                if bt in result:
                    result.remove(bt)
        if c.no_weekend:
            for bt in ("WeekendAB", "WeekendMixed"):
                if bt in result:
                    result.remove(bt)
        elif c.no_weekendab:
            if "WeekendAB" in result:
                result.remove("WeekendAB")
        return result

    if c.block_rules:
        if c.wte < 1.0:
            # Rules define which block types this consultant is assigned to.
            # Count is determined fairly by WTE in solve().
            allowed_set: set = set()
            for rule in c.block_rules:
                eligible_in_rule = _apply_eligibility(
                    [b for b in rule["blocks"] if b in BLOCK_TYPES]
                )
                allowed_set.update(eligible_in_rule)
            allowed = [b for b in BLOCK_TYPES if b in allowed_set]
            # Return a nominal target of 1 (actual target set by solve())
            return 1, allowed
        else:
            # Rules add extra allowed block types on top of standard set
            extra_allowed: set = set()
            for rule in c.block_rules:
                eligible_in_rule = _apply_eligibility(
                    [b for b in rule["blocks"] if b in BLOCK_TYPES]
                )
                extra_allowed.update(eligible_in_rule)
            base_allowed = _apply_eligibility(list(DEFAULT_BLOCK_TYPES))
            allowed = list(dict.fromkeys(
                [b for b in BLOCK_TYPES if b in set(base_allowed) | extra_allowed]
            ))
            return 1, allowed

    # No rules: determine allowed block types from WTE and eligibility
    if c.wte < 1.0:
        allowed = _apply_eligibility(list(DEFAULT_BLOCK_TYPES))
        # How many distinct block types: round(wte * 5), at least 1
        n_types = max(1, round(c.wte * len(DEFAULT_BLOCK_TYPES)))
        to_drop = len(allowed) - n_types
        for bt in BLOCK_DROP_ORDER:
            if to_drop <= 0:
                break
            if bt in allowed:
                allowed.remove(bt)
                to_drop -= 1
    else:
        allowed = _apply_eligibility(list(DEFAULT_BLOCK_TYPES))

    return 1, allowed  # actual target set by solve()


def solve(
    start: date,
    end: date,
    consultants: List[Consultant],
    leave: Dict[str, Set[date]],
    bank_holidays: Set[date],
    prefs: List[Dict],
    pre_allocations: Optional[List[Dict]] = None,
    hard_no_consecutive_weekends: bool = True,
    hard_week_gap: bool = True,
    relax_cardiac: bool = True,
    time_limit_s: int = 60,
    random_seed: int = 0,
) -> Dict:
    first_monday = start + timedelta(days=(7 - start.weekday()) % 7)
    weeks: List[date] = []
    d = first_monday
    while d <= end:
        weeks.append(d)
        d += timedelta(days=7)
    W = len(weeks)

    names = [c.name for c in consultants]
    N = len(names)
    cardiac = [c.cardiac for c in consultants]

    # Per-consultant allowed block types (from _target_allocations)
    allowed_blocks = []
    for c in consultants:
        _, ab = _target_allocations(c, n_weeks=W)
        allowed_blocks.append(ab)

    # -----------------------------------------------------------------------
    # Allocation model: WTE-based targets with no duplicate block types.
    # -----------------------------------------------------------------------
    MANDATORY_BLOCKS = ["AB1", "AB2", "DMonThu", "WeekendAB", "WeekendMixed"]
    OPTIONAL_BLOCKS  = ["DMonTue", "DWedThu"]
    REF_WEEKS = 18

    # Compute per-consultant target (total number of block allocations):
    #   1.0 WTE → 5 (one of each mandatory type)
    #   <1.0 WTE without SC → round(wte * 5), minimum 1
    #   <1.0 WTE with SC → sum of SC rule counts
    #   >1.0 WTE with SC → 5 base + sum of SC extra counts
    targets = []
    for i, c in enumerate(consultants):
        if c.block_rules and c.wte < 1.0:
            # Target = sum of non-fractional rule counts (scaled)
            t = 0
            for rule in c.block_rules:
                eligible = [b for b in rule["blocks"] if b in allowed_blocks[i]]
                if eligible and not rule["fractional"]:
                    t += max(1, round(rule["count"] * W / REF_WEEKS))
            targets.append(max(1, t))
        elif c.block_rules and c.wte >= 1.0:
            # Base 5 + extras from SC rules
            extras = 0
            for rule in c.block_rules:
                eligible = [b for b in rule["blocks"] if b in allowed_blocks[i]]
                if eligible and not rule["fractional"]:
                    extras += max(1, round(rule["count"] * W / REF_WEEKS))
            targets.append(5 + extras)
        else:
            # Standard: round(wte * 5) for part-time, 5 for full-time
            if c.wte >= 1.0:
                targets.append(5)
            else:
                targets.append(max(1, round(c.wte * 5)))

    model = cp_model.CpModel()
    x = {(w, b, i): model.NewBoolVar(f"x_{w}_{b}_{i}")
         for w in range(W) for b in BLOCK_TYPES for i in range(N)}

    # --- Vacancy variables: one per (week, mandatory_block) ---
    vacancy = {(w, b): model.NewBoolVar(f"vac_{w}_{b}")
               for w in range(W) for b in MANDATORY_BLOCKS}

    # --- Each week: exactly one consultant OR vacancy per MANDATORY block ---
    for w in range(W):
        for b in MANDATORY_BLOCKS:
            model.Add(sum(x[(w, b, i)] for i in range(N)) + vacancy[(w, b)] == 1)
        for b in OPTIONAL_BLOCKS:
            model.Add(sum(x[(w, b, i)] for i in range(N)) <= 1)

    # --- Each consultant: at most one block per week ---
    for w in range(W):
        for i in range(N):
            model.Add(sum(x[(w, b, i)] for b in BLOCK_TYPES) <= 1)

    # --- Block type eligibility per consultant ---
    for w in range(W):
        for i in range(N):
            for b in BLOCK_TYPES:
                if b not in allowed_blocks[i]:
                    model.Add(x[(w, b, i)] == 0)

    # -----------------------------------------------------------------------
    # PER-CONSULTANT ALLOCATION CONSTRAINTS
    # -----------------------------------------------------------------------
    # (A) Total allocation count == target
    # (B) At most 1 of each block type (no duplicates) — EXCEPT where SC
    #     rules for >1 WTE allow extras on specific block groups.
    # (C) SC rule enforcement for group minimums.
    # -----------------------------------------------------------------------
    for i, c in enumerate(consultants):
        # (A) Total allocation count — at most target (vacancy absorbs shortfall)
        total_alloc = sum(x[(w, b, i)] for w in range(W) for b in BLOCK_TYPES)
        model.Add(total_alloc <= targets[i])

        if c.block_rules and c.wte < 1.0:
            # Part-time with SC rules:
            # Each rule group gets exactly/at-least the specified count.
            # At most 1 per individual block type (no duplicates within a group).
            for rule in c.block_rules:
                eligible = [b for b in rule["blocks"] if b in allowed_blocks[i]]
                if not eligible:
                    continue
                scaled = max(1, round(rule["count"] * W / REF_WEEKS))
                group_sum = sum(x[(w, b, i)] for w in range(W) for b in eligible)
                if rule["fractional"]:
                    model.Add(group_sum <= scaled)
                else:
                    model.Add(group_sum >= scaled)
            # No duplicates per block type
            for b in allowed_blocks[i]:
                model.Add(sum(x[(w, b, i)] for w in range(W)) <= 1)

        elif c.block_rules and c.wte >= 1.0:
            # >= 1 WTE with SC rules: base 1 of each mandatory type + extras.
            # Extras can create >1 of a type within the rule's group.
            # Compute max allowed per block type (1 base + extras targeting it)
            extra_count_per_block = {b: 0 for b in BLOCK_TYPES}
            for rule in c.block_rules:
                eligible = [b for b in rule["blocks"] if b in allowed_blocks[i]]
                if not eligible or rule["fractional"]:
                    continue
                scaled = max(1, round(rule["count"] * W / REF_WEEKS))
                for b in eligible:
                    extra_count_per_block[b] += scaled

            for b in allowed_blocks[i]:
                max_b = 1 + extra_count_per_block.get(b, 0)
                model.Add(sum(x[(w, b, i)] for w in range(W)) <= max_b)
                # At least 1 of each base mandatory type
                if b in MANDATORY_BLOCKS:
                    model.Add(sum(x[(w, b, i)] for w in range(W)) >= 1)

        else:
            # Standard (no SC rules): exactly 1 of each allowed type.
            # The total_alloc == target constraint + at-most-1-per-type means
            # the solver must use exactly target distinct types.
            for b in allowed_blocks[i]:
                model.Add(sum(x[(w, b, i)] for w in range(W)) <= 1)

    # --- Pre-allocations: fix specific (week, block, consultant) assignments ---
    pre_alloc_fixed: Set[Tuple[int, str, int]] = set()
    name_to_i = {c.name.strip(): idx for idx, c in enumerate(consultants)}
    name_to_i_lower = {c.name.strip().lower(): idx for idx, c in enumerate(consultants)}
    week_to_idx = {wk: w for w, wk in enumerate(weeks)}

    if pre_allocations:
        for pa in pre_allocations:
            nm = pa["consultant_name"]
            wk = pa["week_start"]
            bt = pa["block_type"]
            i = name_to_i.get(nm) or name_to_i_lower.get(nm.lower())
            if i is None:
                continue
            w = week_to_idx.get(wk)
            if w is None:
                continue
            if bt not in BLOCK_TYPES:
                continue
            model.Add(x[(w, bt, i)] == 1)
            pre_alloc_fixed.add((w, bt, i))

    # --- Leave: cannot assign a block whose days overlap leave ---
    def block_days(week_monday: date, b: str) -> List[date]:
        offsets = {
            "AB1":        (0, 1, 2, 3),       # Mon–Thu
            "AB2":        (1, 2, 3, 4),       # Tue–Fri
            "DMonThu":    (0, 1, 2, 3),       # Mon–Thu (full D week)
            "DMonTue":    (0, 1),             # Mon–Tue (half D)
            "DWedThu":    (2, 3),             # Wed–Thu (half D)
            "WeekendAB":  (4, 5, 6, 7),       # Fri–Mon
            "WeekendMixed": (4, 5, 6),        # Fri–Sun
        }
        return [week_monday + timedelta(days=k) for k in offsets[b]]

    for w, wk in enumerate(weeks):
        for b in BLOCK_TYPES:
            days = block_days(wk, b)
            for i, nm in enumerate(names):
                if any(d in leave.get(nm, set()) for d in days):
                    model.Add(x[(w, b, i)] == 0)

    # --- No consecutive weekends (hard, optional) ---
    if hard_no_consecutive_weekends:
        for i in range(N):
            for w in range(W - 1):
                wknd_this = x[(w, "WeekendAB", i)] + x[(w, "WeekendMixed", i)]
                wknd_next = x[(w + 1, "WeekendAB", i)] + x[(w + 1, "WeekendMixed", i)]
                model.Add(wknd_this + wknd_next <= 1)

    # --- 1-week gap between any two blocks (hard, optional) ---
    if hard_week_gap:
        for i in range(N):
            for w in range(W - 1):
                any_this = sum(x[(w, b, i)] for b in BLOCK_TYPES)
                any_next = sum(x[(w + 1, b, i)] for b in BLOCK_TYPES)
                model.Add(any_this + any_next <= 1)

    # -----------------------------------------------------------------------
    # Cardiac competency constraint (relaxed to soft penalty)
    # -----------------------------------------------------------------------
    # Hard rule: on each weekday, exactly one of the A-slot and D-slot holders
    # must be cardiac-competent.
    # D-slot per day:
    #   Mon (0): DMonThu, DMonTue
    #   Tue (1): DMonThu, DMonTue
    #   Wed (2): DMonThu, DWedThu
    #   Thu (3): DMonThu, DWedThu
    #   Fri (4): WeekendMixed
    # -----------------------------------------------------------------------
    cardiac_penalty_terms = []
    CARDIAC_PENALTY = 10000

    # Map each weekday index to which D-block types cover it
    _D_blocks_for_day = {
        0: ["DMonThu", "DMonTue"],          # Mon
        1: ["DMonThu", "DMonTue"],          # Tue
        2: ["DMonThu", "DWedThu"],          # Wed
        3: ["DMonThu", "DWedThu"],          # Thu
        4: ["WeekendMixed"],                # Fri
    }

    for w in range(W):
        for day in range(5):  # Mon-Fri
            if day in (0, 2):
                A_vars = [x[(w, "AB1", i)] for i in range(N)]
            elif day in (1, 3):
                A_vars = [x[(w, "AB2", i)] for i in range(N)]
            else:  # Fri
                A_vars = [x[(w, "WeekendAB", i)] for i in range(N)]

            d_blocks = _D_blocks_for_day[day]
            D_vars = [x[(w, b, i)] for b in d_blocks for i in range(N)]

            A_cardiac = sum(A_vars[i] for i in range(N) if cardiac[i])
            D_cardiac = sum(
                x[(w, b, i)] for b in d_blocks for i in range(N) if cardiac[i]
            )
            cardiac_sum = A_cardiac + D_cardiac

            if relax_cardiac:
                pen = model.NewIntVar(0, 2, f"cardiac_pen_{w}_{day}")
                model.Add(pen >= 1 - cardiac_sum)
                model.Add(pen >= cardiac_sum - 1)
                cardiac_penalty_terms.append(CARDIAC_PENALTY * pen)
            else:
                model.Add(cardiac_sum == 1)

    # -----------------------------------------------------------------------
    # Objective
    # -----------------------------------------------------------------------
    # Primary: minimise deviation from equal distribution of bank-holiday and
    #          weekend burden (allocation counts are now fixed by hard constraints).
    # Secondary: soft preference satisfaction.
    # Tertiary: soft cardiac penalty (if relax_cardiac=True).
    # -----------------------------------------------------------------------

    # Bank-holiday burden fairness
    bh_count = {}
    for w, wk in enumerate(weeks):
        for b in BLOCK_TYPES:
            bh_count[(w, b)] = sum(1 for d in block_days(wk, b) if d in bank_holidays)

    bh_duty = [model.NewIntVar(0, 500, f"bh_{i}") for i in range(N)]
    for i in range(N):
        model.Add(bh_duty[i] == sum(x[(w, b, i)] * bh_count[(w, b)]
                                    for w in range(W) for b in BLOCK_TYPES))

    bh_all = sum(bh_count[(w, b)] for w in range(W) for b in BLOCK_TYPES)
    # Use WTE as proportional weight for fairness (replaces old targets)
    wte_list = [c.wte for c in consultants]
    sum_wte = sum(wte_list) if sum(wte_list) > 0 else 1.0
    SCALE = 1000
    expected_bh = [int(round(bh_all * (wte_list[i] / sum_wte) * SCALE)) for i in range(N)]
    devBH = [model.NewIntVar(0, 10_000_000, f"devBH_{i}") for i in range(N)]
    for i in range(N):
        model.AddAbsEquality(devBH[i], bh_duty[i] * SCALE - expected_bh[i])

    # Weekend burden fairness
    weekend_duty = [model.NewIntVar(0, 500, f"wknd_{i}") for i in range(N)]
    for i in range(N):
        model.Add(weekend_duty[i] == sum(
            x[(w, "WeekendAB", i)] + x[(w, "WeekendMixed", i)] for w in range(W)))

    weekend_all = 2 * W
    expected_w = [int(round(weekend_all * (wte_list[i] / sum_wte) * SCALE)) for i in range(N)]
    devW = [model.NewIntVar(0, 10_000_000, f"devW_{i}") for i in range(N)]
    for i in range(N):
        model.AddAbsEquality(devW[i], weekend_duty[i] * SCALE - expected_w[i])

    # -----------------------------------------------------------------------
    # Preference satisfaction (soft reward, conflict-aware)
    # -----------------------------------------------------------------------
    # For each preference, compute which (week, block) pairs could satisfy it.
    # When two consultants request the same block type for overlapping weeks,
    # they are in conflict — only one can be satisfied.  The higher-weight
    # request should win, so we scale rewards such that the gap between
    # adjacent strength levels is always larger than the maximum jitter,
    # guaranteeing the higher-strength preference always beats a lower one.
    #
    # Shift-type → block mapping:
    #   A       -> AB1, AB2          (any A-type block that week)
    #   B       -> AB2               (specifically the Tue-Fri A block)
    #   D       -> DMonThu, DMonTue, DWedThu  (any D block)
    #   Weekend -> WeekendAB, WeekendMixed
    # -----------------------------------------------------------------------
    name_to_i = {c.name.strip().lower(): idx for idx, c in enumerate(consultants)}
    rng = random.Random(int(random_seed) if random_seed is not None else 0)

    def _week_overlaps(ps: date, pe: date, wk_start: date) -> bool:
        return not (pe < wk_start or ps > wk_start + timedelta(days=6))

    def _shift_to_blocks(st: str) -> List[str]:
        """Map a user-supplied shift_type string to solver block type names."""
        st_up = st.strip().upper()
        if st_up == "A":
            return ["AB1", "AB2"]
        if st_up == "B":
            return ["AB2"]
        if st_up == "D":
            return ["DMonThu", "DMonTue", "DWedThu"]
        if st_up in ("WEEKEND", "W"):
            return ["WeekendAB", "WeekendMixed"]
        # Try direct block name
        if st in BLOCK_TYPES:
            return [st]
        return []

    # Build a list of parsed preference items
    pref_items: List[Dict] = []
    for p_i, p in enumerate(prefs or []):
        nm = str(p.get("consultant_name", "")).strip().lower()
        if not nm or nm not in name_to_i:
            continue
        i = name_to_i[nm]
        ps, pe = p.get("start_date"), p.get("end_date")
        if ps is None or pe is None:
            continue
        st = str(p.get("shift_type", "")).strip()
        blocks = _shift_to_blocks(st)
        if not blocks:
            continue
        # Only keep blocks the consultant is actually eligible for
        blocks = [b for b in blocks if b in allowed_blocks[i]]
        if not blocks:
            continue
        base_w = max(1, min(5, int(p.get("weight", 3) or 3)))
        # Collect the (week_idx, block) pairs that could satisfy this preference
        match_vars = [
            x[(w, b, i)]
            for w, wk in enumerate(weeks)
            if _week_overlaps(ps, pe, wk)
            for b in blocks
            if b in allowed_blocks[i]
        ]
        if not match_vars:
            continue
        pref_items.append({
            "p_i":       p_i,
            "cons_i":    i,
            "name":      nm,
            "blocks":    blocks,
            "weight":    base_w,
            "ps":        ps,
            "pe":        pe,
            "match_vars": match_vars,
        })

    # -----------------------------------------------------------------------
    # Detect conflicts: two preferences on the same block type in overlapping
    # weeks.  For each conflict group, add a mutual-exclusion constraint so
    # the solver knows only one can be satisfied, and scale rewards so the
    # higher-weight one always wins.
    #
    # Reward scaling:
    #   WEIGHT_STEP  = 1000   (gap between adjacent weight levels)
    #   MAX_JITTER   = 25     (small random jitter for tie-breaking across seeds)
    # Gap (1000) >> jitter (25) so weight always dominates.
    # -----------------------------------------------------------------------
    WEIGHT_STEP = 1000
    MAX_JITTER  = 25

    pref_reward_terms = []

    for item in pref_items:
        p_i = item["p_i"]
        sat = model.NewBoolVar(f"pref_sat_{p_i}")
        match_vars = item["match_vars"]
        # sat == 1  iff  at least one matching (week, block) is assigned to this consultant
        model.Add(sum(match_vars) >= sat)
        for v in match_vars:
            model.Add(sat >= v)

        # Reward scaled by weight: higher weight = higher reward = wins conflicts naturally
        eff_w = item["weight"] * WEIGHT_STEP + rng.randint(0, MAX_JITTER)
        pref_reward_terms.append(eff_w * sat)

    pref_reward = sum(pref_reward_terms) if pref_reward_terms else 0
    cardiac_penalty = sum(cardiac_penalty_terms) if cardiac_penalty_terms else 0

    # Vacancy penalty: heavily penalise each vacancy to use them only as last resort
    VACANCY_PENALTY = 50000
    vacancy_cost = VACANCY_PENALTY * sum(vacancy[(w, b)] for w in range(W) for b in MANDATORY_BLOCKS)

    model.Minimize(vacancy_cost + 3 * sum(devBH) + 2 * sum(devW) + cardiac_penalty - pref_reward)

    solver = cp_model.CpSolver()
    solver.parameters.random_seed = int(random_seed)
    solver.parameters.randomize_search = True
    solver.parameters.max_time_in_seconds = float(time_limit_s)
    solver.parameters.num_search_workers = 8

    status = solver.Solve(model)
    status_name = solver.StatusName(status)
    objective = solver.ObjectiveValue() if status in (cp_model.OPTIMAL, cp_model.FEASIBLE) else None

    sol = {
        "status": status_name,
        "objective": objective,
        "weeks": weeks,
        "assignments": {wk: {} for wk in weeks},
        "pref_score": None,
    }

    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        for w, wk in enumerate(weeks):
            for b in BLOCK_TYPES:
                assigned = False
                for i in range(N):
                    if solver.Value(x[(w, b, i)]) == 1:
                        sol["assignments"][wk][b] = names[i]
                        assigned = True
                        break
                # If no consultant assigned and this is a mandatory block, mark as Vacancy
                if not assigned and b in MANDATORY_BLOCKS:
                    if solver.Value(vacancy[(w, b)]) == 1:
                        sol["assignments"][wk][b] = "Vacancy"

    return sol


def export_to_excel(input_path: str, output_path: str, sol: Dict,
                    override_start: Optional[date] = None, override_end: Optional[date] = None):
    wb = load_workbook(input_path)

    wa = wb["WeekAssignments"]
    rota = wb["Rota"]
    dash = wb["Dashboard"]
    cfg = wb["Config"]

    if override_start is not None or override_end is not None:
        for r in range(1, 80):
            lab = str(cfg[f"A{r}"].value).strip() if cfg[f"A{r}"].value is not None else ""
            if lab == "CycleStartDate" and override_start is not None:
                cfg[f"B{r}"].value = override_start
            if lab == "CycleEndDate" and override_end is not None:
                cfg[f"B{r}"].value = override_end

    cons = wb["Consultants"]
    leave_ws = wb["Leave"]
    bh_ws = wb["BankHolidays"]

    def get_cfg(label: str):
        for r in range(1, 80):
            if str(cfg[f"A{r}"].value).strip() == label:
                return cfg[f"B{r}"].value
        return None

    start = excel_date(get_cfg("CycleStartDate"))
    end = excel_date(get_cfg("CycleEndDate"))
    prev_A_for_start = str(get_cfg("A_Consultant_DayBeforeStart") or "")

    cardiac = {}
    wte = {}
    for r in range(2, 1000):
        nm = cons[f"A{r}"].value
        if not nm:
            continue
        nm = str(nm)
        cardiac[nm] = bool(cons[f"B{r}"].value)
        wte[nm] = float(cons[f"C{r}"].value or 0.0)

    leave_map = {}
    for r in range(2, 5000):
        nm = leave_ws[f"A{r}"].value
        if not nm:
            continue
        if not bool(leave_ws[f"E{r}"].value):
            continue
        s = excel_date(leave_ws[f"B{r}"].value)
        e = excel_date(leave_ws[f"C{r}"].value)
        if not s or not e:
            continue
        nm = str(nm)
        leave_map.setdefault(nm, set())
        d = s
        while d <= e:
            leave_map[nm].add(d)
            d += timedelta(days=1)

    bh_set = set()
    for r in range(2, 2000):
        d = excel_date(bh_ws[f"A{r}"].value)
        if d:
            bh_set.add(d)

    # Clear WeekAssignments
    for r in range(2, wa.max_row + 1):
        for c in range(1, 9):
            wa.cell(r, c).value = None

    weeks = sol["weeks"]
    for r_i, wk in enumerate(weeks, start=2):
        wa.cell(r_i, 1).value = wk
        wa.cell(r_i, 2).value = sol["assignments"][wk].get("AB1", "")
        wa.cell(r_i, 3).value = sol["assignments"][wk].get("AB2", "")
        wa.cell(r_i, 4).value = sol["assignments"][wk].get("DMonThu", "")
        wa.cell(r_i, 5).value = sol["assignments"][wk].get("DMonTue", "")
        wa.cell(r_i, 6).value = sol["assignments"][wk].get("DWedThu", "")
        wa.cell(r_i, 7).value = sol["assignments"][wk].get("WeekendAB", "")
        wa.cell(r_i, 8).value = sol["assignments"][wk].get("WeekendMixed", "")
        wa.cell(r_i, 9).value = sol.get("status", "")
        wa.cell(r_i, 10).value = sol.get("objective", "")

    wk_map = {wk: sol["assignments"][wk] for wk in weeks}

    def week_monday(d: date) -> date:
        return d - timedelta(days=d.weekday())

    # Clear Rota
    for r in range(2, rota.max_row + 1):
        for c in range(1, 7):
            rota.cell(r, c).value = None

    all_days = daterange(start, end)
    prev_A = None
    for row_i, d in enumerate(all_days, start=2):
        dow = d.weekday()
        wk = week_monday(d)
        asg = wk_map.get(wk, {})

        if dow in (0, 2):
            A = asg.get("AB1", "")
        elif dow in (1, 3):
            A = asg.get("AB2", "")
        elif dow == 4:
            A = asg.get("WeekendAB", "")
        elif dow == 5:
            A = asg.get("WeekendMixed", "")
        else:
            A = asg.get("WeekendAB", "")

        B = prev_A_for_start if d == start else (prev_A or "")

        # D-slot: pick the right block type for this day of week.
        # DMonThu covers Mon-Thu; DMonTue covers Mon-Tue; DWedThu covers Wed-Thu.
        # On any given day, at most one of these will be assigned.
        if dow == 0 or dow == 1:    # Mon, Tue
            D = asg.get("DMonThu") or asg.get("DMonTue") or ""
        elif dow == 2:              # Wed
            D = asg.get("DMonThu") or asg.get("DWedThu") or ""
        elif dow == 3:              # Thu
            D = asg.get("DMonThu") or asg.get("DWedThu") or ""
        elif dow == 4:              # Fri
            D = asg.get("WeekendMixed") or ""
        else:
            D = ""

        flags = []
        if not A:
            flags.append("MISSING_A")
        if not B:
            flags.append("MISSING_B")
        if dow <= 4 and not D:
            flags.append("MISSING_D")
        if dow >= 5 and D:
            flags.append("D_SHOULD_BE_BLANK_WEEKEND")
        if A and d in leave_map.get(A, set()):
            flags.append("A_ON_LEAVE")
        if B and d in leave_map.get(B, set()):
            flags.append("B_ON_LEAVE")
        if D and d in leave_map.get(D, set()):
            flags.append("D_ON_LEAVE")
        if dow <= 4:
            a_c = bool(cardiac.get(A, False))
            d_c = bool(cardiac.get(D, False))
            if (a_c + d_c) != 1:
                flags.append("CARDIAC_XOR_BREACH")
        if d in bh_set:
            flags.append("BANK_HOLIDAY")

        rota.cell(row_i, 1).value = d
        rota.cell(row_i, 2).value = d.strftime("%a")
        rota.cell(row_i, 3).value = A
        rota.cell(row_i, 4).value = B
        rota.cell(row_i, 5).value = D
        rota.cell(row_i, 6).value = ",".join(flags)
        prev_A = A

    # Dashboard
    for r in range(2, dash.max_row + 1):
        for c in range(1, 14):
            dash.cell(r, c).value = None

    counts = {nm: {"A": 0, "B": 0, "D": 0, "BH": 0, "wknd": 0, "consec_wknd": 0}
              for nm in cardiac.keys()}
    weekend_by_cons = {nm: [] for nm in cardiac.keys()}
    for wk in weeks:
        for b in ("WeekendAB", "WeekendMixed"):
            nm = wk_map[wk].get(b, "")
            if nm and nm in counts:
                weekend_by_cons.setdefault(nm, []).append(wk)

    for nm, wks in weekend_by_cons.items():
        if nm not in counts:
            continue
        wks = sorted(wks)
        for i in range(len(wks) - 1):
            if (wks[i + 1] - wks[i]).days == 7:
                counts[nm]["consec_wknd"] += 1
        counts[nm]["wknd"] = len(wks)

    for row_i, d in enumerate(all_days, start=2):
        A = rota.cell(row_i, 3).value or ""
        B = rota.cell(row_i, 4).value or ""
        Dv = rota.cell(row_i, 5).value or ""
        is_bh = "BANK_HOLIDAY" in (rota.cell(row_i, 6).value or "")
        if A in counts:
            counts[A]["A"] += 1
        if B in counts:
            counts[B]["B"] += 1
        if Dv in counts and d.weekday() <= 4:
            counts[Dv]["D"] += 1
        if is_bh:
            for nm in (A, B):
                if nm in counts:
                    counts[nm]["BH"] += 1
            if Dv in counts and d.weekday() <= 4:
                counts[Dv]["BH"] += 1

    total_all = sum(v["A"] + v["B"] + v["D"] for v in counts.values())
    total_bh = sum(v["BH"] for v in counts.values())
    sum_wte = sum(wte.values()) if wte else 1.0

    r = 2
    for nm in sorted(counts.keys()):
        A_cnt, B_cnt, D_cnt = counts[nm]["A"], counts[nm]["B"], counts[nm]["D"]
        tot = A_cnt + B_cnt + D_cnt
        exp = total_all * (wte.get(nm, 0.0) / sum_wte)
        bh_cnt = counts[nm]["BH"]
        bh_exp = total_bh * (wte.get(nm, 0.0) / sum_wte)
        dash.cell(r, 1).value = nm
        dash.cell(r, 2).value = wte.get(nm, 0.0)
        dash.cell(r, 3).value = A_cnt
        dash.cell(r, 4).value = B_cnt
        dash.cell(r, 5).value = D_cnt
        dash.cell(r, 6).value = tot
        dash.cell(r, 7).value = round(exp, 2)
        dash.cell(r, 8).value = round(tot - exp, 2)
        dash.cell(r, 9).value = bh_cnt
        dash.cell(r, 10).value = round(bh_exp, 2)
        dash.cell(r, 11).value = round(bh_cnt - bh_exp, 2)
        dash.cell(r, 12).value = counts[nm]["wknd"]
        dash.cell(r, 13).value = counts[nm]["consec_wknd"]
        r += 1

    wb.save(output_path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--time_limit", type=int, default=60)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--period_name", type=str, default="")
    ap.add_argument("--no_hard_week_gap", action="store_true")
    ap.add_argument("--no_hard_no_consec_weekends", action="store_true")
    ap.add_argument("--hard_cardiac", action="store_true",
                    help="Enforce cardiac XOR as a hard constraint (default: soft penalty)")
    args = ap.parse_args()

    override_start = override_end = None
    if args.period_name:
        override_start, override_end = fetch_period_dates_from_supabase(args.period_name)

    try:
        start, end, consultants, leave, bh, prefs, pre_allocs = read_inputs(
            args.input, override_start=override_start, override_end=override_end)
    except Exception as e:
        raise SystemExit(f"Input workbook error: {e}")

    sol = solve(
        start, end, consultants, leave, bh, prefs,
        pre_allocations=pre_allocs,
        hard_no_consecutive_weekends=not args.no_hard_no_consec_weekends,
        hard_week_gap=not args.no_hard_week_gap,
        relax_cardiac=not args.hard_cardiac,
        time_limit_s=args.time_limit,
        random_seed=args.seed,
    )
    print(f"Status: {sol['status']}  Objective: {sol.get('objective')}")
    export_to_excel(args.input, args.output, sol,
                    override_start=override_start, override_end=override_end)
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
