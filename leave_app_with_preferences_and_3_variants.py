import streamlit as st
from datetime import date, datetime, timedelta
from io import BytesIO
import pandas as pd
from openpyxl import load_workbook
from supabase import create_client, Client
import subprocess
import tempfile
from pathlib import Path

# --- Supabase email confirmation / magic link handler ---
query_params = st.query_params

if "access_token" in query_params and "refresh_token" in query_params:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
        session = supabase.auth.set_session(
            query_params["access_token"],
            query_params["refresh_token"]
        )

        if session and session.session:
            st.session_state["sb_session"] = session.session.model_dump()

        # Clean URL and redirect to app root (login/home)
        st.query_params.clear()
        st.success("Email confirmed. You are now signed in.")
        st.rerun()

    except Exception as e:
        st.error("Email confirmation failed. Please try signing in.")

st.set_page_config(page_title="Rota Requests + Solver", layout="wide")
# Persist solver results across Streamlit reruns (for variant comparison UI)
if "draft_results" not in st.session_state:
    st.session_state["draft_results"] = []

# -----------------------------
# Secrets
# -----------------------------
SUPABASE_URL = st.secrets.get("SUPABASE_URL", "")
SUPABASE_ANON_KEY = st.secrets.get("SUPABASE_ANON_KEY", "")
ALLOWED_EMAIL_DOMAIN = st.secrets.get("ALLOWED_EMAIL_DOMAIN", "")  # optional

if not SUPABASE_URL or not SUPABASE_ANON_KEY:
    st.error("Missing SUPABASE_URL or SUPABASE_ANON_KEY in Streamlit secrets.")
    st.stop()

LEAVE_TYPES = ["Annual", "Study", "NOC"]
SHIFT_TYPES = ["A", "B", "D"]
PREF_KINDS = ["Specific date", "Date range", "Week", "Weekend"]

# -----------------------------
# Supabase client helpers
# -----------------------------
def base_client() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_ANON_KEY)

def authed_client(access_token: str) -> Client:
    c = base_client()
    c.postgrest.auth(access_token)  # enforce RLS
    return c

def validate_dates(s: date, e: date) -> str | None:
    if e < s:
        return "Date to cannot be earlier than Date from."
    return None

def overlap(a_start: date, a_end: date, b_start: date, b_end: date) -> bool:
    return a_start <= b_end and b_start <= a_end

def week_bounds(d: date) -> tuple[date, date]:
    # Week = Monday..Sunday based on ISO weekday (Mon=1..Sun=7)
    start = d - timedelta(days=d.isoweekday() - 1)
    end = start + timedelta(days=6)
    return start, end

def weekend_bounds(d: date) -> tuple[date, date]:
    # Weekend = Saturday..Sunday containing date d
    # Find Saturday of that week
    # Convert to Monday-start, then Saturday = start+5
    ws, _ = week_bounds(d)
    sat = ws + timedelta(days=5)
    sun = ws + timedelta(days=6)
    return sat, sun

# ---------------------------------------------------------
# Supabase email confirmation / magic-link success handler
# ---------------------------------------------------------
query_params = st.query_params

if "access_token" in query_params and "refresh_token" in query_params:
    try:
        supabase_tmp = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)

        session = supabase_tmp.auth.set_session(
            query_params["access_token"],
            query_params["refresh_token"]
        )

        if session and session.session:
            st.session_state["sb_session"] = session.session.model_dump()

            # --- Custom confirmation success page ---
            st.markdown("## ✅ Email confirmed successfully")
            st.success(
                "Thank you for confirming your email address.\n\n"
                "You can now:\n"
                "- Request annual or study leave\n"
                "- Submit preferred shifts (weeks, weekends, or shift types)\n"
                "- Track the status of your requests\n\n"
                "Please use the menu on the left to continue."
            )

            st.info("You will be redirected automatically in a moment…")

            # Clean the URL so tokens are not reused
            st.query_params.clear()

            # Short pause so user can read the message
            import time
            time.sleep(2)

            st.rerun()

    except Exception:
        st.error(
            "Your email confirmation link could not be processed.\n\n"
            "Please return to the login page and sign in manually."
        )

# -----------------------------
# Auth UI
# -----------------------------
st.title("Rota Requests")
st.write(
    "Please sign up/sign in to enter your leave requests and preferred shifts. Requests lockout after a rota period is published."
    "Requests are locked once the rota period is published. Rota admins can draft rota variants using the solver."
)

if "sb_session" not in st.session_state:
    st.session_state["sb_session"] = None

with st.sidebar:
    st.header("Sign in")
    email = st.text_input("Email", value="").strip().lower()
    password = st.text_input("Password", type="password")

    if ALLOWED_EMAIL_DOMAIN and email and not email.endswith("@" + ALLOWED_EMAIL_DOMAIN):
        st.warning(f"Email must end with @{ALLOWED_EMAIL_DOMAIN}.")

    c = base_client()
    col1, col2 = st.columns(2)
    with col1:
        if st.button("Sign in", use_container_width=True):
            if not email or not password:
                st.error("Enter email and password.")
            elif ALLOWED_EMAIL_DOMAIN and not email.endswith("@" + ALLOWED_EMAIL_DOMAIN):
                st.error("Email domain not permitted.")
            else:
                try:
                    res = c.auth.sign_in_with_password({"email": email, "password": password})
                    st.session_state["sb_session"] = res.session.model_dump() if res.session else None
                    st.rerun()
                except Exception:
                    st.error("Sign-in failed. Check credentials and confirmation status.")
    with col2:
        if st.button("Sign up", use_container_width=True):
            if not email or not password:
                st.error("Enter email and password.")
            elif ALLOWED_EMAIL_DOMAIN and not email.endswith("@" + ALLOWED_EMAIL_DOMAIN):
                st.error("Email domain not permitted.")
            else:
                try:
                    _ = c.auth.sign_up({ "email": email,"password": password,
    "options": { "email_redirect_to": "https://rotaicu.streamlit.app"}})
                st.success(
                "Sign-up created successfully.\n\n"
                "Please check your email and click the confirmation link to activate your account."
            )
        except Exception as e:
            st.error("Sign-up failed. Email may already exist or password is too weak.")
            st.exception(e)

    if st.session_state["sb_session"]:
        if st.button("Sign out", use_container_width=True):
            st.session_state["sb_session"] = None
            st.rerun()

sess = st.session_state["sb_session"]
if not sess:
    st.info("Please sign in to continue.")
    st.stop()

access_token = sess["access_token"]
user_id = sess["user"]["id"]
user_email = sess["user"]["email"]
db = authed_client(access_token)

# -----------------------------
# Admin detection
# -----------------------------
def is_rota_admin() -> bool:
    try:
        r = db.table("rota_admins").select("user_id").eq("user_id", user_id).execute()
        is_admin = len(r.data or []) > 0
        return bool(r.data)
    except Exception:
        return False

is_admin = is_rota_admin()

with st.sidebar:
    st.markdown("---")
    st.write(f"Signed in as: {user_email}")
    st.write("Role: Rota admin" if is_admin else "Role: Consultant")

# -----------------------------
# Periods
# -----------------------------
def fetch_periods() -> pd.DataFrame:
    try:
        resp = db.table("rota_periods").select("*").order("start_date").execute()
    except Exception as e:
        st.error("Cannot read rota periods from Supabase (rota_periods).")
        st.info(
            "Fix: run the Supabase migration for rota_periods (create table + RLS policies) "
            "and ensure authenticated has SELECT."
        )
        st.exception(e)
        return pd.DataFrame()

    dfp = pd.DataFrame(resp.data or [])
    if not dfp.empty:
        dfp["start_date"] = pd.to_datetime(dfp["start_date"]).dt.date
        dfp["end_date"] = pd.to_datetime(dfp["end_date"]).dt.date
        dfp["is_published"] = dfp["is_published"].astype(bool)
        dfp["published_at"] = pd.to_datetime(dfp["published_at"], errors="coerce")
    return dfp

periods = fetch_periods()

def any_published_overlap(s: date, e: date) -> bool:
    if periods.empty:
        return False
    pubs = periods[periods["is_published"] == True]
    if pubs.empty:
        return False
    return any(overlap(s, e, r["start_date"], r["end_date"]) for _, r in pubs.iterrows())

st.subheader("Published rota periods")
if periods.empty:
    st.info("No rota periods configured yet.")
else:
    st.dataframe(periods[["id","name","start_date","end_date","is_published","published_at"]],
                 use_container_width=True, hide_index=True)
    if not periods[periods["is_published"] == True].empty:
        st.caption("Requests overlapping published periods are locked for consultants.")

# -----------------------------
# Data access
# -----------------------------
def fetch_leave() -> pd.DataFrame:
    r = db.table("leave_requests").select("*").order("start_date").execute()
    df = pd.DataFrame(r.data or [])
    if not df.empty:
        df["start_date"] = pd.to_datetime(df["start_date"]).dt.date
        df["end_date"] = pd.to_datetime(df["end_date"]).dt.date
        df["approved"] = df["approved"].astype(bool)
        df["created_at"] = pd.to_datetime(df["created_at"], errors="coerce")
        df["updated_at"] = pd.to_datetime(df["updated_at"], errors="coerce")
    return df

def fetch_prefs() -> pd.DataFrame:
    r = db.table("preferred_shifts").select("*").order("start_date").execute()
    df = pd.DataFrame(r.data or [])
    if not df.empty:
        df["start_date"] = pd.to_datetime(df["start_date"]).dt.date
        df["end_date"] = pd.to_datetime(df["end_date"]).dt.date
        df["created_at"] = pd.to_datetime(df["created_at"], errors="coerce")
        df["updated_at"] = pd.to_datetime(df["updated_at"], errors="coerce")
    return df

leave_df = fetch_leave()
prefs_df = fetch_prefs()

# -----------------------------
# 1) Leave request (same as before)
# -----------------------------
st.subheader("1) Submit a leave request")
with st.form("leave_add"):
    c1, c2, c3 = st.columns([2, 1, 1])
    with c1:
        leave_name = st.text_input("Consultant name", value="")
        leave_type = st.selectbox("Leave type", options=LEAVE_TYPES)
        leave_notes = st.text_input("Notes (optional)", value="")
    with c2:
        leave_start = st.date_input("Date from", value=date.today(), key="leave_start")
    with c3:
        leave_end = st.date_input("Date to", value=date.today(), key="leave_end")
    leave_submit = st.form_submit_button("Submit leave request")

if leave_submit:
    err = validate_dates(leave_start, leave_end)
    if err:
        st.error(err)
    elif not leave_name.strip():
        st.error("Consultant name is required.")
    elif (not is_admin) and any_published_overlap(leave_start, leave_end):
        st.error("Locked: this overlaps a published rota period.")
    else:
        try:
            db.table("leave_requests").insert({
                "consultant_name": leave_name.strip(),
                "requester_id": user_id,
                "requester_email": user_email,
                "start_date": leave_start.isoformat(),
                "end_date": leave_end.isoformat(),
                "leave_type": leave_type,
                "approved": False,
                "notes": leave_notes.strip()
            }).execute()
            st.success("Leave submitted (pending approval).")
            st.rerun()
        except Exception as e:
            st.error("Insert failed.")
            st.exception(e)

st.subheader("2) Your leave requests")
leave_df = fetch_leave()
if leave_df.empty:
    st.info("No leave requests found.")
else:
    st.dataframe(
        leave_df[["id","consultant_name","start_date","end_date","leave_type","approved","notes","updated_at"]],
        use_container_width=True, hide_index=True
    )

# -----------------------------
# 2) Preferred shifts request
# -----------------------------
st.subheader("3) Submit a preferred-shift request")
st.caption("Examples: request a particular week, a weekend, or a shift type (A/B/D) on specific dates.")

with st.form("pref_add"):
    p1, p2, p3 = st.columns([2, 1, 1])
    with p1:
        pref_name = st.text_input("Consultant name (as on rota)", value="", key="pref_name")
        pref_kind = st.selectbox("Preference type", options=PREF_KINDS, key="pref_kind")
        pref_shift = st.selectbox("Shift type", options=SHIFT_TYPES, key="pref_shift")
        pref_weight = st.slider("Strength of preference (1=low, 5=high)", min_value=1, max_value=5, value=3, key="pref_weight")
        pref_notes = st.text_input("Notes (optional)", value="", key="pref_notes")
    with p2:
        ref_date = st.date_input("Reference date", value=date.today(), key="pref_ref_date")
    with p3:
        pref_end_override = st.date_input("End date (used only for Date range)", value=date.today(), key="pref_end_override")

    pref_submit = st.form_submit_button("Submit preferred shift")

if pref_submit:
    # derive start/end based on preference kind
    if pref_kind == "Specific date":
        ps, pe = ref_date, ref_date
    elif pref_kind == "Date range":
        ps, pe = ref_date, pref_end_override
    elif pref_kind == "Week":
        ps, pe = week_bounds(ref_date)
    else:  # Weekend
        ps, pe = weekend_bounds(ref_date)

    err = validate_dates(ps, pe)
    if err:
        st.error(err)
    elif not pref_name.strip():
        st.error("Consultant name is required.")
    elif (not is_admin) and any_published_overlap(ps, pe):
        st.error("Locked: this overlaps a published rota period.")
    else:
        try:
            db.table("preferred_shifts").insert({
                "consultant_name": pref_name.strip(),
                "requester_id": user_id,
                "requester_email": user_email,
                "start_date": ps.isoformat(),
                "end_date": pe.isoformat(),
                "pref_kind": pref_kind,
                "shift_type": pref_shift,
                "weight": int(pref_weight),
                "notes": pref_notes.strip()
            }).execute()
            st.success("Preferred shift submitted.")
            st.rerun()
        except Exception as e:
            st.error("Insert failed.")
            st.exception(e)

st.subheader("4) Your preferred shifts")
prefs_df = fetch_prefs()
if prefs_df.empty:
    st.info("No preferred-shift requests found.")
else:
    st.dataframe(
        prefs_df[["id","consultant_name","start_date","end_date","pref_kind","shift_type","weight","notes","updated_at"]],
        use_container_width=True, hide_index=True
    )

# -----------------------------
# Admin section (approvals + drafting)
# -----------------------------
st.subheader("Admin actions")
if not is_admin:
    st.info("Admin actions are available to rota administrators only.")
    st.stop()

# Approve leave (admin-only)
st.markdown("### Leave approvals")
pending = leave_df[leave_df["approved"] == False].copy() if not leave_df.empty else pd.DataFrame()
if pending.empty:
    st.write("No pending leave requests.")
else:
    st.dataframe(pending[["id","consultant_name","requester_email","start_date","end_date","leave_type","notes","created_at"]],
                 use_container_width=True, hide_index=True)
    approve_id = st.selectbox("Select leave request to approve", options=pending["id"].tolist(), key="leave_appr")
    colA, colB = st.columns([1,1])
    with colA:
        if st.button("Approve leave", use_container_width=True):
            db.table("leave_requests").update({"approved": True}).eq("id", approve_id).execute()
            st.success("Approved.")
            st.rerun()
    with colB:
        if st.button("Reject (delete) leave", use_container_width=True):
            db.table("leave_requests").delete().eq("id", approve_id).execute()
            st.success("Rejected (deleted).")
            st.rerun()

# Drafting
st.markdown("---")
st.markdown("### Draft rota (3 variants)")
st.write(
    """
Workflow:
1) Select the rota period (must be PUBLISHED).
2) Upload the base rota workbook template.
3) The app writes approved leave + preferred shifts into the workbook:
   - `Leave` sheet (approved only, filtered to the selected period)
   - `preferred_shifts` sheet (filtered to the selected period)
4) Runs the solver to generate three candidate rotas for selection.
"""
)

periods = fetch_periods()
if periods.empty:
    st.error("No rota periods found. Create and publish a period before drafting.")
    st.stop()

periods_sorted = periods.sort_values(["start_date"], ascending=False).copy()
periods_sorted["label"] = periods_sorted.apply(
    lambda r: f"{r['name']} ({r['start_date']} → {r['end_date']}) — {'PUBLISHED' if r['is_published'] else 'unpublished'}",
    axis=1
)
sel_label = st.selectbox("Select rota period", options=periods_sorted["label"].tolist(), key="draft_period")
sel_row = periods_sorted[periods_sorted["label"] == sel_label].iloc[0]

period_start = sel_row["start_date"]
period_end = sel_row["end_date"]
period_published = bool(sel_row["is_published"])

st.caption(f"Selected period window: {period_start} to {period_end}.")

if not period_published:
    st.warning("Drafting is disabled because the selected rota period is not published.")
    st.stop()

template = st.file_uploader("Upload base rota workbook (.xlsx)", type=["xlsx"], key="draft_template")
solver_script = Path("solve_rota.py")

if not solver_script.exists():
    st.warning("`solve_rota.py` not found in the app repository. Add it to your GitHub repo to enable drafting.")
    st.stop()

# Relaxation toggles
relax_week_gap = st.checkbox("Allow fallback: relax 1-week gap constraint", value=True)
relax_no_consec_weekends = st.checkbox("Allow fallback: relax no-consecutive-weekends constraint", value=True)
force_truncate = st.checkbox(
    "Force draft by truncating partial-overlap requests to the period window",
    value=False,
    help="If enabled, requests overlapping the window but extending outside it will be clipped for drafting only."
)

# Variant generation controls
st.caption("Rota variants are produced by running the solver with different random seeds.")
variant_seeds = [11, 22, 33]  # stable defaults

if template is None:
    st.info("Upload the base rota workbook to enable drafting.")
else:
    if st.button("Draft 3 rota versions now"):
        # --- Prepare filtered leave (approved) ---
        leave_all = fetch_leave()
        leave_ok = leave_all[leave_all["approved"] == True].copy() if not leave_all.empty else pd.DataFrame()

        # Keep only those overlapping window; error if partial overlap unless truncate enabled
        if not leave_ok.empty:
            overlaps_period = leave_ok.apply(lambda r: overlap(r["start_date"], r["end_date"], period_start, period_end), axis=1)
            leave_overlap = leave_ok[overlaps_period].copy()

            contained = leave_overlap.apply(lambda r: (r["start_date"] >= period_start) and (r["end_date"] <= period_end), axis=1)
            leave_partial = leave_overlap[~contained].copy()

            if not leave_partial.empty and not force_truncate:
                st.error("Approved leave partially overlaps the selected period. Enable truncation or correct dates.")
                st.dataframe(leave_partial[["consultant_name","requester_email","start_date","end_date","leave_type"]],
                             use_container_width=True, hide_index=True)
                st.stop()

            if not leave_partial.empty and force_truncate:
                leave_overlap.loc[leave_partial.index, "start_date"] = leave_partial["start_date"].apply(lambda d: max(d, period_start))
                leave_overlap.loc[leave_partial.index, "end_date"] = leave_partial["end_date"].apply(lambda d: min(d, period_end))
                contained = leave_overlap.apply(lambda r: (r["start_date"] >= period_start) and (r["end_date"] <= period_end), axis=1)

            leave_final = leave_overlap[contained].copy()
        else:
            leave_final = leave_ok

        # --- Prepare filtered preferences ---
        pref_all = fetch_prefs()
        if not pref_all.empty:
            overlaps_period = pref_all.apply(lambda r: overlap(r["start_date"], r["end_date"], period_start, period_end), axis=1)
            pref_overlap = pref_all[overlaps_period].copy()

            contained = pref_overlap.apply(lambda r: (r["start_date"] >= period_start) and (r["end_date"] <= period_end), axis=1)
            pref_partial = pref_overlap[~contained].copy()

            if not pref_partial.empty and not force_truncate:
                st.error("Preferred-shift requests partially overlap the selected period. Enable truncation or correct dates.")
                st.dataframe(pref_partial[["consultant_name","requester_email","start_date","end_date","pref_kind","shift_type","weight"]],
                             use_container_width=True, hide_index=True)
                st.stop()

            if not pref_partial.empty and force_truncate:
                pref_overlap.loc[pref_partial.index, "start_date"] = pref_partial["start_date"].apply(lambda d: max(d, period_start))
                pref_overlap.loc[pref_partial.index, "end_date"] = pref_partial["end_date"].apply(lambda d: min(d, period_end))
                contained = pref_overlap.apply(lambda r: (r["start_date"] >= period_start) and (r["end_date"] <= period_end), axis=1)

            pref_final = pref_overlap[contained].copy()
        else:
            pref_final = pref_all

        # --- Build solver input workbook ---
        wb = load_workbook(BytesIO(template.getvalue()))
        if "Leave" not in wb.sheetnames:
            st.error("Workbook must contain a sheet named 'Leave'.")
            st.stop()

        # Ensure preferred_shifts sheet exists
        if "preferred_shifts" not in wb.sheetnames:
            wb.create_sheet("preferred_shifts")

        # Write Leave sheet (same structure as existing template)
        lws = wb["Leave"]
        for r in range(2, 5000):
            if lws[f"A{r}"].value in (None, ""):
                break
            for col in ("A","B","C","D","E"):
                lws[f"{col}{r}"].value = None

        r = 2
        if not leave_final.empty:
            leave_final = leave_final.sort_values(["start_date","consultant_name"], na_position="last")
            for _, rec in leave_final.iterrows():
                lws[f"A{r}"].value = rec["consultant_name"]
                lws[f"B{r}"].value = rec["start_date"]
                lws[f"C{r}"].value = rec["end_date"]
                lws[f"D{r}"].value = rec["leave_type"]
                lws[f"E{r}"].value = True
                r += 1

        # Write preferred_shifts sheet (header + rows)
        pws = wb["preferred_shifts"]
        # Clear sheet completely then rewrite header for clarity
        pws.delete_rows(1, pws.max_row if pws.max_row else 1)
        pws.append(["Name", "StartDate", "EndDate", "PrefKind", "ShiftType", "Weight", "Notes"])

        if not pref_final.empty:
            pref_final = pref_final.sort_values(["start_date","consultant_name"], na_position="last")
            for _, rec in pref_final.iterrows():
                pws.append([
                    rec["consultant_name"],
                    rec["start_date"],
                    rec["end_date"],
                    rec.get("pref_kind"),
                    rec.get("shift_type"),
                    int(rec.get("weight", 3)),
                    rec.get("notes")
                ])

        # --- Run solver: produce 3 variants ---
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            solver_input = td_path / "Rota_Master_WITH_Leave.xlsx"
            wb.save(solver_input)

            attempts_base = []
            attempts_base.append(("Strict", ["python", "solve_rota.py", "--input", str(solver_input), "--output", "OUT.xlsx"]))
            if relax_week_gap:
                attempts_base.append(("Relax week-gap", ["python", "solve_rota.py", "--input", str(solver_input), "--output", "OUT.xlsx", "--no_hard_week_gap"]))
            if relax_no_consec_weekends:
                attempts_base.append(("Relax no-consec-weekends", ["python", "solve_rota.py", "--input", str(solver_input), "--output", "OUT.xlsx", "--no_hard_no_consec_weekends"]))
            if relax_week_gap and relax_no_consec_weekends:
                attempts_base.append(("Relax BOTH", ["python", "solve_rota.py", "--input", str(solver_input), "--output", "OUT.xlsx",
                                                    "--no_hard_week_gap", "--no_hard_no_consec_weekends"]))

            results = []  # list of (variant_name, bytes, label_used)
            for i, seed in enumerate(variant_seeds, start=1):
                variant_name = f"Variant {i} (seed {seed})"
                out_file = td_path / f"Rota_Solved_V{i}.xlsx"

                succeeded = False
                used_label = None
                last_logs = ""

                for label, cmd in attempts_base:
                    # Require a small patch in solve_rota.py: accept --seed and apply it to OR-Tools solver parameters.
                    cmd2 = cmd.copy()
                    # Replace output placeholder
                    cmd2[cmd2.index("OUT.xlsx")] = str(out_file)
                    cmd2 += ["--seed", str(seed)]

                    st.write(f"Running {variant_name}: **{label}**")
                    proc = subprocess.run(cmd2, capture_output=True, text=True)

                    if proc.stdout and proc.stdout.strip():
                        st.code(proc.stdout[-1500:])
                    if proc.stderr and proc.stderr.strip():
                        st.code(proc.stderr[-1500:])

                    if proc.returncode == 0 and out_file.exists() and out_file.stat().st_size > 0:
                        succeeded = True
                        used_label = label
                        break
                    else:
                        last_logs = (proc.stderr or proc.stdout or "")[-3000:]

                if not succeeded:
                    st.warning(f"{variant_name} failed under all attempts. Logs (last):")
                    if last_logs:
                        st.code(last_logs)
                    continue

                results.append((variant_name, out_file.read_bytes(), used_label))

            if not results:
                st.error("No variants could be produced. Check solver logs above. Ensure `solve_rota.py` supports `--seed` and reads `preferred_shifts`.")
                st.stop()

            st.session_state["draft_results"] = results
            st.success(f"Produced {len(results)} rota variant(s). Download below.")
            for variant_name, data_bytes, used_label in results:
                st.download_button(
                    f"Download {variant_name} — {used_label}",
                    data=data_bytes,
                    file_name=f"Rota_Solved_{variant_name.replace(' ', '_').replace('(', '').replace(')', '')}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )

results = st.session_state.get("draft_results", [])

if len(results) >= 2:
    st.markdown("---")
    st.subheader("Visual differences between rota variants")

    # Choose baseline and comparator
    variant_names = [r[0] for r in results]
    baseline_name = st.selectbox("Baseline variant", variant_names, index=0, key="diff_base")
    compare_name = st.selectbox("Compare to", variant_names, index=1, key="diff_comp")

    base_bytes = next(b for (n, b, _) in results if n == baseline_name)
    comp_bytes = next(b for (n, b, _) in results if n == compare_name)

    sheets = common_sheets(base_bytes, comp_bytes)
    default_sheet = "Rota" if "Rota" in sheets else sheets[0]
    sheet = st.selectbox("Sheet to compare", sheets, index=sheets.index(default_sheet), key="diff_sheet")

    diffs = diff_sheet(base_bytes, comp_bytes, sheet_name=sheet, max_changes=5000)
    s = diff_summary(diffs)

    c1, c2, c3 = st.columns(3)
    c1.metric("Changed cells", s["changed_cells"])
    c2.metric("Rows affected", s["changed_rows"])
    c3.metric("Columns affected", s["changed_cols"])

    if diffs.empty:
        st.success("No differences detected on the selected sheet.")
    else:
        with st.expander("Show where differences occur (row/column hotspots)", expanded=True):
            colA, colB = st.columns(2)
            with colA:
                st.caption("Top changed rows")
                st.dataframe(top_changed_rows(diffs, 30), use_container_width=True, hide_index=True)
            with colB:
                st.caption("Top changed columns")
                st.dataframe(top_changed_cols(diffs, 30), use_container_width=True, hide_index=True)

        with st.expander("Cell-level differences (sample)", expanded=False):
            st.dataframe(diffs.head(500), use_container_width=True, hide_index=True)

        st.caption("Note: This is a generic cell-level diff. If you want an 'assignment-level' diff (e.g., "
                   "which consultant changed on which day/shift), confirm the exact output layout of your solved "
                   "workbook and we can parse it into structured comparisons.")

