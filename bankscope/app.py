"""BankScope - Banking Business Analytics Dashboard (Flask backend).

Academic demonstration only. Not for production banking or automated lending decisions.
All uploaded data lives in server memory, isolated per browser session, and is discarded
after SESSION_TTL seconds of inactivity. Run with a single process (see README).
"""
import csv
import hmac
import html
import io
import logging
import math
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
import warnings
from collections import Counter
from contextlib import closing
from datetime import datetime
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from flask import Flask, Response, jsonify, redirect, render_template, request, send_file, session
from pandas.api import types as pdt
from sklearn.cluster import KMeans
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (accuracy_score, confusion_matrix, f1_score, precision_score,
                             recall_score, roc_auc_score, roc_curve, silhouette_score)
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

BASE = Path(__file__).resolve().parent
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bankscope")

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("BANKSCOPE_SECRET_KEY") or secrets.token_hex(32),
    MAX_UPLOAD_MB=int(os.environ.get("BANKSCOPE_MAX_UPLOAD_MB", 200)),   # upload size limit
    MAX_ROWS=int(os.environ.get("BANKSCOPE_MAX_ROWS", 500_000)),          # rows kept in memory
    MAX_ML_ROWS=int(os.environ.get("BANKSCOPE_MAX_ML_ROWS", 100_000)),    # rows used to fit models
    SESSION_TTL=int(os.environ.get("BANKSCOPE_SESSION_TTL", 7200)),       # idle seconds
    UPLOAD_DIR=BASE / "uploads",
    MODEL_DIR=BASE / "saved_models",
    DB_PATH=Path(os.environ.get("BANKSCOPE_DB_PATH") or BASE / "bankscope.db"),   # user accounts (SQLite)
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("BANKSCOPE_PRODUCTION") == "1",
)
app.config["MAX_CONTENT_LENGTH"] = app.config["MAX_UPLOAD_MB"] * 1024 * 1024

if os.environ.get("BANKSCOPE_PRODUCTION") == "1":          # behind a proxy such as Render: trust X-Forwarded-* headers
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

STORE, LOCK = {}, threading.Lock()
SEED = 42
ALLOWED_EXT = {".csv", ".xlsx"}


# ----------------------------------------------------------------------------- helpers
class ApiError(Exception):
    def __init__(self, msg, status=400):
        super().__init__(msg)
        self.msg, self.status = msg, status


def clean(o):
    """Make any result JSON-safe (numpy types, NaN/inf -> None, timestamps -> ISO)."""
    if o is None or o is pd.NaT or o is pd.NA:
        return None
    if isinstance(o, dict):
        return {str(k): clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple, set)):
        return [clean(v) for v in o]
    if isinstance(o, np.ndarray):
        return clean(o.tolist())
    if isinstance(o, (bool, np.bool_)):
        return bool(o)
    if isinstance(o, (int, np.integer)):
        return int(o)
    if isinstance(o, (float, np.floating)):
        f = float(o)
        return f if math.isfinite(f) else None
    if isinstance(o, (pd.Timestamp, datetime)):
        return o.isoformat()
    return o if isinstance(o, str) else str(o)


def ok(**data):
    return jsonify({"success": True, "data": clean(data)})


def body():
    d = request.get_json(silent=True)
    if not isinstance(d, dict):
        raise ApiError("Request body must be a JSON object.")
    return d


def as_int(v, name, lo, hi, default):
    if v in (None, ""):
        return default
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise ApiError(f"'{name}' must be a whole number.")
    if not lo <= n <= hi:
        raise ApiError(f"'{name}' must be between {lo} and {hi}.")
    return n


def as_float(v, name, lo, hi, default):
    if v in (None, ""):
        return default
    try:
        n = float(v)
    except (TypeError, ValueError):
        raise ApiError(f"'{name}' must be a number.")
    if not (math.isfinite(n) and lo <= n <= hi):
        raise ApiError(f"'{name}' must be between {lo} and {hi}.")
    return n


def need_col(df, col, label="Column"):
    if not isinstance(col, str) or col not in df.columns:
        raise ApiError(f"{label} '{col}' was not found in the dataset." if col else f"{label} is required.")
    return col


def need_cols(df, cols, label="columns", minimum=1):
    if not isinstance(cols, list) or not all(isinstance(c, str) for c in cols):
        raise ApiError(f"'{label}' must be a list of column names.")
    cols = list(dict.fromkeys(cols))
    bad = [c for c in cols if c not in df.columns]
    if bad:
        raise ApiError(f"Unknown column(s): {', '.join(bad[:5])}.")
    if len(cols) < minimum:
        raise ApiError(f"Select at least {minimum} column(s) for '{label}'.")
    return cols


def csv_response(df, filename):
    return Response(df.to_csv(index=False), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


# ----------------------------------------------------------------------------- session store
def drop_session(sid):
    STORE.pop(sid, None)
    for f in Path(app.config["MODEL_DIR"]).glob(f"{sid}_*.joblib"):
        f.unlink(missing_ok=True)


def purge():
    now = time.time()
    with LOCK:
        for sid in [s for s, v in STORE.items() if now - v["touched"] > app.config["SESSION_TTL"]]:
            drop_session(sid)


def S(required=True):
    sid = session.get("sid")
    st = STORE.get(sid) if sid else None
    if st is None:
        if required:
            raise ApiError("No dataset loaded. Upload a dataset first.")
        return None
    st["touched"] = time.time()
    return st


def set_work(st, df):
    """Replace the working copy and invalidate everything derived from the old rows."""
    st["work"] = df.reset_index(drop=True)
    st["kinds"] = st["colinfo"] = None
    st["seg"] = None
    st["preds"] = {}
    if st.get("risk", {}).get("source") == "model":
        st["risk"] = {}


def init_dirs():
    for k in ("UPLOAD_DIR", "MODEL_DIR"):
        p = Path(app.config[k])
        p.mkdir(exist_ok=True)
        for f in p.iterdir():
            if f.is_file() and f.name != ".gitkeep":
                f.unlink(missing_ok=True)


# ----------------------------------------------------------------------------- column typing
ID_LAST = {"id", "uuid", "guid", "key", "index", "no", "num", "number", "nbr"}


def tokens(name):
    return [t.lower() for t in re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", str(name))]


def is_id_name(name):
    t = tokens(name)
    return bool(t) and (t[-1] in ID_LAST or t[0] in ("id", "uuid", "guid"))


def parse_dates(s):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return pd.to_datetime(s, errors="coerce", utc=True).dt.tz_localize(None)


def kind_of(name, s):
    """id | numeric | binary | categorical | date | text | empty"""
    nn = s.dropna()
    n = len(nn)
    if n == 0:
        return "empty"
    if pdt.is_datetime64_any_dtype(s):
        return "date"
    if pdt.is_bool_dtype(s):
        return "binary"
    nu = nn.nunique()
    if pdt.is_numeric_dtype(s):
        if is_id_name(name):
            return "id"
        if nu <= 2:
            return "binary"
        if pdt.is_integer_dtype(s) and nu == n and n >= 20 and (nn.is_monotonic_increasing or nn.is_monotonic_decreasing):
            return "id"
        return "numeric"
    if is_id_name(name):
        return "id"
    if nu <= 2:
        return "binary"
    sample = nn.iloc[:200].astype(str)
    if sample.str.contains(r"[-/:]").mean() >= 0.9 and sample.str.len().max() <= 40 \
            and parse_dates(sample).notna().mean() >= 0.9:
        return "date"
    if nu <= 50:
        return "categorical"
    return "id" if nu / n > 0.9 else "text"


def get_kinds(st):
    if not st.get("kinds"):
        df = st["work"]
        st["kinds"] = {c: kind_of(c, df[c]) for c in df.columns}
    return st["kinds"]


def is_num(s):
    return pdt.is_numeric_dtype(s) and not pdt.is_bool_dtype(s)


def num_cols(st, numeric_only=False):
    df, kinds = st["work"], get_kinds(st)
    allowed = ("numeric",) if numeric_only else ("numeric", "binary")
    return [c for c in df.columns if kinds[c] in allowed and is_num(df[c])]


def col_info(st):
    if st.get("colinfo"):
        return st["colinfo"]
    df, kinds = st["work"], get_kinds(st)
    cols, sus_num, sus_date = [], [], []
    for c in df.columns:
        s = df[c]
        nn = s.dropna()
        cols.append({"name": c, "dtype": str(s.dtype), "kind": kinds[c], "is_numeric": bool(is_num(s)),
                     "missing": int(s.isna().sum()), "unique": int(nn.nunique()),
                     "sample": [str(v)[:40] for v in nn.iloc[:3]]})
        if not is_num(s) and not pdt.is_datetime64_any_dtype(s) and len(nn):
            if kinds[c] == "date":
                sus_date.append(c)
            else:
                t = pd.to_numeric(nn.iloc[:500].astype(str).str.replace(r"[,$€£₹%\s]", "", regex=True), errors="coerce")
                if t.notna().mean() >= 0.9:
                    sus_num.append(c)
    st["colinfo"] = (cols, sus_num, sus_date)
    return st["colinfo"]


def find_target(st, words, kinds_ok=("binary", "categorical", "numeric")):
    df, kinds = st["work"], get_kinds(st)
    for c in df.columns:
        if set(tokens(c)) & words and kinds[c] in kinds_ok and 2 <= df[c].nunique() <= 20:
            return c
    return None


CHURN_WORDS = {"churn", "churned", "exited", "exit", "attrition", "attrited", "left"}
CREDIT_WORDS = {"default", "defaulted", "delinquent", "delinquency", "risk", "bad", "creditability"}
POSITIVE_HINTS = ["1", "yes", "true", "y", "churn", "churned", "exited", "attrited customer", "attrited",
                  "default", "defaulted", "bad", "left", "high", "risky", "positive"]


def guess_positive(values):
    low = {str(v).strip().lower(): str(v) for v in values}
    for h in POSITIVE_HINTS:
        if h in low:
            return low[h]
    return None


def tstr(s):
    """Target values as clean strings (1.0 -> '1'); missing values are dropped."""
    s = s.dropna()
    if pdt.is_numeric_dtype(s) and not pdt.is_bool_dtype(s) and len(s) and np.isfinite(s).all() and (s % 1 == 0).all():
        return s.astype("int64").astype(str)
    return s.astype(str).str.strip()


# ----------------------------------------------------------------------------- loading files
def read_header(path, ext, enc):
    try:
        if ext == ".csv":
            with open(path, encoding=enc, newline="") as f:
                return next(csv.reader(f), [])
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True)
        row = next(wb.active.iter_rows(max_row=1, values_only=True), [])
        wb.close()
        return ["" if v is None else str(v) for v in row]
    except Exception:
        return []


def load_table(path, ext):
    max_rows, warns, truncated, enc = app.config["MAX_ROWS"], [], False, None
    if ext == ".csv":
        df = None
        for enc in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                parts, rows = [], 0
                for ch in pd.read_csv(path, encoding=enc, chunksize=min(50_000, max_rows + 1), on_bad_lines="skip"):
                    parts.append(ch)
                    rows += len(ch)
                    if rows > max_rows:
                        truncated = True
                        break
                if not parts:
                    raise ApiError("The file has a header row but no data rows.")
                df = pd.concat(parts, ignore_index=True)
                break
            except UnicodeDecodeError:
                continue
            except pd.errors.EmptyDataError:
                raise ApiError("The file is empty or has no readable columns.")
            except pd.errors.ParserError as e:
                raise ApiError(f"The CSV file is malformed and could not be parsed: {str(e)[:150]}")
        if df is None:
            raise ApiError("Could not decode the file. Save it as UTF-8 CSV and try again.")
        if enc != "utf-8-sig":
            warns.append(f"The file is not UTF-8; it was read as {enc}. Special characters may look different.")
        if not truncated:
            with open(path, "rb") as f:
                lines = sum(b.count(b"\n") for b in iter(lambda: f.read(1 << 20), b""))
            skipped = lines - 1 - len(df)
            if skipped > 0:
                warns.append(f"About {skipped:,} line(s) were not loaded as data rows (malformed, blank or multi-line records).")
        # chunks can infer different dtypes; unify columns that are numeric in every chunk
        for c in df.columns:
            if df[c].dtype == object:
                try:
                    df[c] = pd.to_numeric(df[c])
                except (ValueError, TypeError):
                    pass
    else:
        try:
            df = pd.read_excel(path, engine="openpyxl", nrows=max_rows + 1)
        except Exception:
            raise ApiError("Could not read the Excel file. It may be corrupted, password protected or not a real .xlsx.")
        if len(df) > max_rows:
            truncated = True
        warns.append("Only the first worksheet of the Excel file was read. Excel loads the whole sheet in memory, so prefer CSV for very large data.")
    raw = read_header(path, ext, enc or "utf-8-sig")
    dups = sorted(n for n, k in Counter(raw).items() if k > 1 and n)
    if dups:
        warns.append(f"Duplicate column names were renamed automatically: {', '.join(dups[:5])}.")
    if truncated:
        df = df.iloc[:max_rows]
        warns.append(f"The file has more than {max_rows:,} rows. Only the first {max_rows:,} rows were loaded (limit set by BANKSCOPE_MAX_ROWS).")
    names, seen = [], Counter()
    for i, c in enumerate(df.columns):
        c = str(c).strip() or f"column_{i + 1}"
        seen[c] += 1
        names.append(c if seen[c] == 1 else f"{c}.{seen[c] - 1}")
    df.columns = names
    df = df.drop(columns=[c for c in df.columns if c.startswith("Unnamed:") and df[c].isna().all()])
    df = df.dropna(how="all").reset_index(drop=True)
    if df.empty or df.shape[1] == 0:
        raise ApiError("The dataset is empty after removing blank rows and columns.")
    if df.shape[1] < 2:
        warns.append("The dataset has only one column; most analyses need at least two.")
    return df, warns, truncated


# ----------------------------------------------------------------------------- app plumbing
@app.before_request
def guard():
    purge()
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.path.startswith("/api/"):
        tok, good = request.headers.get("X-CSRF-Token", ""), session.get("csrf", "")
        if not good or not hmac.compare_digest(tok.encode(), good.encode()):
            raise ApiError("Invalid or missing security token. Reload the page and try again.", 403)
    if request.path.startswith("/api/") and request.path not in PUBLIC_API and not session.get("user"):
        raise ApiError("Please sign in to continue.", 401)


@app.after_request
def headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:"
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.errorhandler(Exception)
def on_error(e):
    if isinstance(e, ApiError):
        code, msg = e.status, e.msg
    elif isinstance(e, RequestEntityTooLarge):
        code, msg = 413, f"File too large. The maximum upload size is {app.config['MAX_UPLOAD_MB']} MB."
    elif isinstance(e, HTTPException):
        if not request.path.startswith("/api/"):
            return e
        code, msg = e.code, e.description
    else:
        log.exception("Unhandled error on %s", request.path)
        code, msg = 500, "Unexpected server error. Check the server console for details."
    return jsonify({"success": False, "error": msg}), code


def ensure_session():
    session.setdefault("sid", uuid.uuid4().hex)
    session.setdefault("csrf", secrets.token_hex(16))


# ----------------------------------------------------------------------------- accounts & login
PUBLIC_API = {"/api/auth/login", "/api/auth/register", "/api/auth/me"}
COMMON_PASSWORDS = {"password", "password1", "password123", "12345678", "123456789", "qwertyui", "qwerty123", "iloveyou",
                    "admin123", "letmein1", "welcome1", "abc12345", "11111111", "bankscope"}
USER_RE = re.compile(r"^[a-z0-9_.-]{3,30}$")
FAILS, MAX_FAILS, LOCK_SECONDS = {}, 5, 300
DUMMY_HASH = generate_password_hash("bankscope-dummy-password")


def db():
    Path(app.config["DB_PATH"]).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(app.config["DB_PATH"])
    con.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL, "
                "pw_hash TEXT NOT NULL, created TEXT NOT NULL)")
    return con


def start_session(username):
    """Fresh session after login/register: new workspace id and CSRF token (prevents session fixation)."""
    with LOCK:
        if session.get("sid"):
            drop_session(session["sid"])
    session.clear()
    session["user"] = username
    ensure_session()
    return session["csrf"]


def creds():
    d = body()
    u, p = d.get("username"), d.get("password")
    if not isinstance(u, str) or not isinstance(p, str):
        raise ApiError("Username and password are required.")
    return u.strip().lower(), p


@app.get("/")
def index():
    if not session.get("user"):
        return redirect("/login")
    ensure_session()
    return render_template("index.html", csrf=session["csrf"], user=session["user"])


@app.get("/healthz")
def healthz():
    return "ok", 200, {"Content-Type": "text/plain"}


@app.get("/login")
def login_page():
    if session.get("user"):
        return redirect("/")
    ensure_session()
    return render_template("login.html", csrf=session["csrf"])


@app.get("/api/auth/me")
def auth_me():
    ensure_session()
    return ok(authenticated=bool(session.get("user")), user=session.get("user"), csrf=session["csrf"])


@app.post("/api/auth/register")
def auth_register():
    u, p = creds()
    if not USER_RE.match(u):
        raise ApiError("Username must be 3-30 characters: letters, numbers, dot, dash or underscore.")
    if not 8 <= len(p) <= 128:
        raise ApiError("Password must be between 8 and 128 characters.")
    if p.lower() == u:
        raise ApiError("Password must not be the same as the username.")
    if p.lower() in COMMON_PASSWORDS or p.isdigit() or len(set(p)) == 1:
        raise ApiError("That password is too easy to guess. Choose something less common.")
    try:
        with closing(db()) as con, con:
            con.execute("INSERT INTO users (username, pw_hash, created) VALUES (?, ?, ?)",
                        (u, generate_password_hash(p), datetime.now().isoformat(timespec="seconds")))
    except sqlite3.IntegrityError:
        raise ApiError("That username is already taken.", 409)
    log.info("New account registered")
    return ok(user=u, csrf=start_session(u))


@app.post("/api/auth/login")
def auth_login():
    u, p = creds()
    key, now = f"{u}|{request.remote_addr}", time.time()
    n, first = FAILS.get(key, (0, now))
    if n >= MAX_FAILS and now - first < LOCK_SECONDS:
        raise ApiError("Too many failed attempts. Please wait a few minutes and try again.", 429)
    if now - first >= LOCK_SECONDS:
        n, first = 0, now
    with closing(db()) as con:
        row = con.execute("SELECT pw_hash FROM users WHERE username = ?", (u,)).fetchone()
    good = check_password_hash(row[0] if row else DUMMY_HASH, p) and row is not None
    if not good:
        FAILS[key] = (n + 1, first)
        raise ApiError("Invalid username or password.", 401)
    FAILS.pop(key, None)
    return ok(user=u, csrf=start_session(u))


@app.post("/api/auth/logout")
def auth_logout():
    with LOCK:
        if session.get("sid"):
            drop_session(session["sid"])
    session.clear()
    return ok(logged_out=True)


@app.get("/api/config")
def config():
    ensure_session()
    return ok(csrf=session["csrf"], max_upload_mb=app.config["MAX_UPLOAD_MB"],
              max_rows=app.config["MAX_ROWS"], max_ml_rows=app.config["MAX_ML_ROWS"])


# ----------------------------------------------------------------------------- upload / info / preview
@app.post("/api/upload")
def upload():
    ensure_session()
    f = request.files.get("file")
    if f is None or not f.filename:
        raise ApiError("No file was uploaded. Choose a CSV or Excel (.xlsx) file.")
    display = secure_filename(f.filename) or "dataset"
    ext = Path(f.filename).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise ApiError("Unsupported file type. Please upload a .csv or .xlsx file.")
    updir = Path(app.config["UPLOAD_DIR"])
    updir.mkdir(exist_ok=True)
    path = updir / f"{uuid.uuid4().hex}{ext}"          # server-generated name: no path traversal
    f.save(path)
    try:
        if path.stat().st_size == 0:
            raise ApiError("The uploaded file is empty.")
        with open(path, "rb") as fh:
            head = fh.read(4096)
        if ext == ".xlsx" and not head.startswith(b"PK"):
            raise ApiError("This file is not a valid .xlsx workbook.")
        if ext == ".csv" and b"\x00" in head:
            raise ApiError("This file looks binary, not a text CSV file.")
        df, warns, truncated = load_table(path, ext)
    finally:
        path.unlink(missing_ok=True)                   # uploaded file never kept on disk
    with LOCK:
        drop_session(session["sid"])
        STORE[session["sid"]] = {"orig": df, "name": display, "touched": time.time(), "results": {},
                                 "models": {}, "preds": {}, "risk": {}, "seg": None, "clean_log": [],
                                 "warnings": warns, "truncated": truncated}
        st = STORE[session["sid"]]
        set_work(st, df.copy())
    log.info("Dataset loaded: %d rows x %d columns", *df.shape)
    return ok(**dataset_info(st))


def dataset_info(st):
    df = st["work"]
    cols, sus_num, sus_date = col_info(st)
    return {"name": st["name"], "rows": len(df), "columns_count": df.shape[1],
            "original_rows": len(st["orig"]), "missing_total": int(df.isna().sum().sum()),
            "missing_pct": float(df.isna().sum().sum() / df.size * 100) if df.size else 0.0,
            "duplicates": int(df.duplicated().sum()),
            "memory_mb": round(float(df.memory_usage(deep=True).sum()) / 1e6, 2),
            "columns": cols, "suspect_numeric": sus_num, "suspect_date": sus_date,
            "suggested": {"churn": find_target(st, CHURN_WORDS), "credit": find_target(st, CREDIT_WORDS)},
            "warnings": st["warnings"], "truncated": st["truncated"], "modified": bool(st["clean_log"])}


@app.get("/api/dataset/info")
def info():
    return ok(**dataset_info(S()))


@app.get("/api/dataset/preview")
def preview():
    st = S()
    n = as_int(request.args.get("rows"), "rows", 1, 100, 10)
    src = st["orig"] if request.args.get("source") == "original" else st["work"]
    return ok(columns=list(src.columns), rows=src.head(n).to_dict("records"))


@app.get("/api/dashboard")
def dashboard():
    st = S()
    df, kinds = st["work"], get_kinds(st)
    na = {"value": None, "note": "Not available for this dataset"}
    cards = {"records": {"value": len(df), "fmt": "int", "note": "Rows in the working dataset"},
             "missing": {"value": int(df.isna().sum().sum()), "fmt": "int",
                         "note": f"{df.isna().sum().sum() / df.size:.1%} of all cells" if df.size else ""},
             "customers": dict(na), "balance": dict(na), "churn": dict(na), "risk": dict(na)}
    for c in df.columns:
        t = set(tokens(c))
        if kinds[c] == "id" and t & {"customer", "client"}:
            cards["customers"] = {"value": int(df[c].nunique()), "fmt": "int", "note": f"Unique values of '{c}'"}
            break
    for c in num_cols(st, numeric_only=True):
        if "balance" in tokens(c):
            cards["balance"] = {"value": float(df[c].mean()), "fmt": "num", "note": f"Mean of '{c}'"}
            break
    tgt = st["results"].get("churn", {}).get("target") or find_target(st, CHURN_WORDS)
    if tgt and tgt in df.columns:
        ty = tstr(df[tgt])
        pos = st["results"].get("churn", {}).get("positive") or guess_positive(ty.unique())
        if pos is not None and len(ty):
            cards["churn"] = {"value": float((ty == pos).mean()), "fmt": "pct", "note": f"Share of '{tgt}' = {pos}"}
    r = st.get("risk")
    if r:
        cards["risk"] = {"value": int(r["count"]), "fmt": "int",
                         "note": "Predicted by the trained credit-risk model" if r["source"] == "model"
                         else "Flagged by transparent rules (not a trained model)"}
    return ok(cards=cards)


# ----------------------------------------------------------------------------- cleaning
def iqr_fences(s):
    q1, q3 = s.quantile(0.25), s.quantile(0.75)
    i = q3 - q1
    return q1 - 1.5 * i, q3 + 1.5 * i


def apply_clean(df, ops):
    if not isinstance(ops, dict):
        raise ApiError("'operations' must be an object.")
    df, log_ = df.copy(), []
    drop = need_cols(df, ops.get("drop_columns") or [], "drop_columns", 0)
    if drop:
        if len(drop) >= df.shape[1]:
            raise ApiError("You cannot remove every column.")
        df = df.drop(columns=drop)
        log_.append(f"Removed {len(drop)} column(s): {', '.join(drop[:6])}{'…' if len(drop) > 6 else ''}.")
    for c in need_cols(df, ops.get("to_numeric") or [], "to_numeric", 0):
        before = int(df[c].isna().sum())
        txt = df[c].astype(str).str.replace(r"[^0-9+\-.]", "", regex=True)
        conv = pd.to_numeric(txt.where(df[c].notna()), errors="coerce")
        log_.append(f"Converted '{c}' to numeric ({int(conv.isna().sum()) - before} unreadable value(s) became missing).")
        df[c] = conv.astype("float64")
    for c in need_cols(df, ops.get("to_date") or [], "to_date", 0):
        if is_num(df[c]):
            raise ApiError(f"'{c}' is numeric and cannot be safely converted to a date.")
        before = int(df[c].isna().sum())
        conv = df[c] if pdt.is_datetime64_any_dtype(df[c]) else parse_dates(df[c])
        log_.append(f"Converted '{c}' to date ({int(conv.isna().sum()) - before} unreadable value(s) became missing).")
        df[c] = conv
    if ops.get("drop_duplicates"):
        n = len(df)
        df = df.drop_duplicates()
        log_.append(f"Removed {n - len(df):,} duplicate row(s).")
    kinds = {c: kind_of(c, df[c]) for c in df.columns}
    method = ops.get("outliers", "none")
    if method not in ("none", "cap", "remove"):
        raise ApiError("'outliers' must be none, cap or remove.")
    if method != "none":
        default = [c for c in df.columns if kinds[c] == "numeric" and is_num(df[c])]
        cols = need_cols(df, ops["outlier_columns"], "outlier_columns", 0) if ops.get("outlier_columns") else default
        bad = [c for c in cols if not is_num(df[c])]
        if bad:
            raise ApiError(f"Outlier treatment needs numeric columns: {', '.join(bad[:5])}.")
        if method == "cap":
            for c in cols:
                lo, hi = iqr_fences(df[c].dropna()) if df[c].notna().any() else (None, None)
                if lo is not None:
                    df[c] = df[c].clip(lo, hi)
            log_.append(f"Capped IQR outliers in {len(cols)} column(s) at 1.5×IQR fences.")
        else:
            mask = pd.Series(False, index=df.index)
            for c in cols:
                if df[c].notna().any():
                    lo, hi = iqr_fences(df[c].dropna())
                    mask |= (df[c] < lo) | (df[c] > hi)
            n = len(df)
            df = df[~mask]
            log_.append(f"Removed {n - len(df):,} row(s) containing IQR outliers in {len(cols)} column(s).")
    nf, cf = ops.get("numeric_fill", "none"), ops.get("cat_fill", "none")
    if nf not in ("none", "mean", "median") or cf not in ("none", "mode", "custom"):
        raise ApiError("Invalid fill option.")
    kinds = {c: kind_of(c, df[c]) for c in df.columns}
    filled = 0
    for c in df.columns:
        miss = int(df[c].isna().sum())
        if not miss or miss == len(df) or pdt.is_datetime64_any_dtype(df[c]) or kinds[c] == "id":
            continue
        if is_num(df[c]):
            if kinds[c] == "numeric" and nf != "none":
                df[c] = df[c].fillna(df[c].mean() if nf == "mean" else df[c].median())
            elif kinds[c] == "binary" and nf != "none":
                df[c] = df[c].fillna(df[c].mode().iloc[0])
            else:
                continue
        elif cf == "mode":
            df[c] = df[c].fillna(df[c].mode().iloc[0])
        elif cf == "custom":
            val = str(ops.get("cat_custom") or "Unknown")[:100]
            df[c] = df[c].astype(object).fillna(val)
        else:
            continue
        filled += miss
    if filled:
        log_.append(f"Filled {filled:,} missing value(s) (numeric: {nf}, categorical: {cf}).")
    if df.empty:
        raise ApiError("These settings would remove every row. Choose less aggressive options.")
    return df.reset_index(drop=True), log_ or ["No changes selected."]


@app.post("/api/clean")
def clean_route():
    st = S()
    d = body()
    if d.get("reset"):
        set_work(st, st["orig"].copy())
        st["clean_log"] = []
        return ok(reset=True, **dataset_info(st))
    preview_only = bool(d.get("preview", True))
    before = st["work"]
    new, logs = apply_clean(before, d.get("operations") or {})
    rep = {"rows_before": len(before), "rows_after": len(new), "cols_before": before.shape[1], "cols_after": new.shape[1],
           "missing_before": int(before.isna().sum().sum()), "missing_after": int(new.isna().sum().sum()),
           "dups_before": int(before.duplicated().sum()), "dups_after": int(new.duplicated().sum()),
           "log": logs, "columns": list(new.columns), "preview_rows": new.head(10).to_dict("records"),
           "applied": not preview_only}
    if not preview_only:
        set_work(st, new)
        st["clean_log"] += logs
        rep["info"] = dataset_info(st)
    return ok(**rep)


@app.get("/api/download/cleaned")
def dl_cleaned():
    return csv_response(S()["work"], "bankscope_cleaned.csv")


# ----------------------------------------------------------------------------- EDA
def eda_summary(st):
    df, kinds = st["work"], get_kinds(st)
    cols = num_cols(st)[:60]
    stats = []
    for c in cols:
        s = df[c].replace([np.inf, -np.inf], np.nan).dropna()
        if s.empty:
            continue
        m = s.mode()
        stats.append({"column": c, "count": len(s), "mean": s.mean(), "median": s.median(), "mode": m.iloc[0] if len(m) else None,
                      "min": s.min(), "max": s.max(), "std": s.std(), "q25": s.quantile(.25), "q75": s.quantile(.75),
                      "missing": int(df[c].isna().sum())})
    cats = []
    for c in [c for c in df.columns if kinds[c] in ("categorical", "binary")][:12]:
        vc = df[c].value_counts(dropna=True)
        cats.append({"column": c, "unique": int(len(vc)), "top": [{"value": str(k), "count": int(v), "pct": v / len(df)} for k, v in vc.head(8).items()]})
    miss = df.isna().sum()
    miss = miss[miss > 0].sort_values(ascending=False).head(20)
    outl = []
    for c in num_cols(st, numeric_only=True)[:60]:
        s = df[c].dropna()
        if len(s) < 4:
            continue
        lo, hi = iqr_fences(s)
        n = int(((s < lo) | (s > hi)).sum())
        if n:
            outl.append({"column": c, "count": n, "pct": n / len(s), "lower": lo, "upper": hi})
    outl.sort(key=lambda r: -r["count"])
    corr_cols = [c for c in cols if df[c].nunique() > 1][:15]
    corr = {"columns": [], "matrix": [], "strong": []}
    if len(corr_cols) >= 2:
        cm = df[corr_cols].replace([np.inf, -np.inf], np.nan).corr()
        corr = {"columns": corr_cols, "matrix": cm.round(3).values.tolist(), "strong": []}
        for i, a in enumerate(corr_cols):
            for j in range(i + 1, len(corr_cols)):
                r = cm.iloc[i, j]
                if pd.notna(r) and abs(r) >= 0.5:
                    corr["strong"].append({"a": a, "b": corr_cols[j], "r": round(float(r), 3)})
        corr["strong"].sort(key=lambda r: -abs(r["r"]))
    return {"stats": stats, "categorical": cats, "missing": [{"column": c, "count": int(n), "pct": n / len(df)} for c, n in miss.items()],
            "outliers": outl, "correlation": corr, "rows": len(df)}


@app.get("/api/eda/summary")
def eda_summary_route():
    st = S()
    res = eda_summary(st)
    st["results"]["eda"] = {"stats": res["stats"][:12], "outliers": res["outliers"][:8]}
    return ok(**res)


@app.post("/api/eda/chart")
def eda_chart():
    st = S()
    d = body()
    df, kinds = st["work"], get_kinds(st)
    ctype = d.get("chart")
    if ctype not in ("bar", "line", "histogram", "scatter"):
        raise ApiError("Chart type must be bar, line, histogram or scatter.")
    x = need_col(df, d.get("x"), "X column")
    y = need_col(df, d.get("y"), "Y column") if d.get("y") else None
    agg = d.get("agg") or "count"
    if agg not in ("count", "mean", "sum", "median"):
        raise ApiError("Aggregation must be count, mean, sum or median.")
    top_n, bins = as_int(d.get("top_n"), "top_n", 3, 50, 15), as_int(d.get("bins"), "bins", 5, 100, 20)
    if y and not is_num(df[y]):
        raise ApiError(f"Y column '{y}' must be numeric.")
    if y is None and agg != "count":
        raise ApiError(f"Choose a numeric Y column to calculate the {agg}.")
    warns = []
    out = {"chart": ctype, "x": x, "y": y, "agg": agg}

    if ctype == "histogram":
        if not is_num(df[x]):
            raise ApiError(f"Histogram needs a numeric column; '{x}' is not numeric.")
        s = df[x].replace([np.inf, -np.inf], np.nan).dropna()
        if s.empty:
            raise ApiError(f"Column '{x}' has no valid values.")
        counts, edges = np.histogram(s, bins=bins)
        out.update(labels=[f"{edges[i]:.4g} – {edges[i + 1]:.4g}" for i in range(len(counts))], values=counts.tolist(),
                   y_label="Records")
    elif ctype == "scatter":
        if y is None or not is_num(df[x]):
            raise ApiError("Scatter plots need two numeric columns (X and Y).")
        pair = df[[x, y]].replace([np.inf, -np.inf], np.nan).dropna()
        if len(pair) < 3:
            raise ApiError("Not enough complete rows to draw a scatter plot.")
        r = pair[x].corr(pair[y]) if pair[x].std() > 0 and pair[y].std() > 0 else None
        if len(pair) > 2000:
            pair = pair.sample(2000, random_state=SEED)
            warns.append("Showing a random sample of 2,000 points for performance.")
        out.update(points=[{"x": a, "y": b} for a, b in zip(pair[x], pair[y])], correlation=r)
    elif ctype == "bar":
        if is_num(df[x]) and df[x].nunique() > 50:
            raise ApiError(f"'{x}' is continuous (many distinct values). Use a histogram instead.")
        sub = df[[x] + ([y] if y else [])].dropna(subset=[x])
        g = sub.groupby(x)[y].agg(agg) if y and agg != "count" else sub.groupby(x).size()
        if len(g) > top_n:
            warns.append(f"'{x}' has {len(g):,} categories; showing the top {top_n}.")
        g = g.sort_values(ascending=False).head(top_n)
        out.update(labels=[str(k) for k in g.index], values=g.values.tolist(),
                   y_label=f"{agg} of {y}" if y and agg != "count" else "Records")
        if len(df) - len(sub):
            warns.append(f"{len(df) - len(sub):,} rows with missing '{x}' were excluded.")
    else:  # line
        use_y = y if (y and agg != "count") else None
        if kinds[x] == "date":
            dt = df[x] if pdt.is_datetime64_any_dtype(df[x]) else parse_dates(df[x])
            ok_ = dt.notna()
            if ok_.sum() < 2:
                raise ApiError(f"Column '{x}' has fewer than 2 valid dates.")
            span = (dt.max() - dt.min()).days
            rule = "D" if span <= 120 else "W" if span <= 900 else "MS"
            s = (df.loc[ok_, use_y] if use_y else pd.Series(1, index=df.index[ok_])).copy()
            s.index = dt[ok_]
            r_ = s.sort_index().resample(rule)
            res = getattr(r_, agg)() if use_y else r_.size()
            res = res.dropna() if use_y else res
            out.update(labels=res.index.strftime("%Y-%m-%d").tolist(), values=res.values.tolist())
        else:
            sub = df[[x] + ([use_y] if use_y else [])].dropna(subset=[x])
            if is_num(df[x]) and sub[x].nunique() > 60:
                key = pd.cut(sub[x], 30)
                lab = key.apply(lambda iv: iv.mid if pd.notna(iv) else np.nan)
            else:
                if sub[x].nunique() > 60:
                    raise ApiError(f"'{x}' has too many categories for a line chart. Pick a date or numeric column.")
                lab = sub[x]
            g = sub.groupby(lab, observed=True)[use_y].agg(agg) if use_y else sub.groupby(lab, observed=True).size()
            g = g.sort_index()
            out.update(labels=[f"{k:.4g}" if isinstance(k, (int, float, np.number)) else str(k) for k in g.index], values=g.values.tolist())
        out["y_label"] = f"{agg} of {y}" if use_y else "Records"
    out["warnings"] = warns
    return ok(**out)


# ----------------------------------------------------------------------------- segmentation
def need_numeric_features(st, feats):
    df, kinds = st["work"], get_kinds(st)
    for f in feats:
        if kinds[f] == "id":
            raise ApiError(f"'{f}' looks like an identifier and cannot be used as a feature.")
        if not is_num(df[f]):
            raise ApiError(f"'{f}' is not numeric. Convert it in Data Cleaning or choose another column.")


@app.post("/api/segment")
def segment():
    st = S()
    d = body()
    df = st["work"]
    feats = need_cols(df, d.get("features"), "features", 2)
    need_numeric_features(st, feats)
    k = as_int(d.get("k"), "k", 2, 10, 4)
    X = df[feats].replace([np.inf, -np.inf], np.nan)
    empty = [f for f in feats if X[f].notna().sum() == 0]
    const = [f for f in feats if X[f].nunique() <= 1]
    if empty or const:
        raise ApiError(f"Features with no values or no variation cannot be clustered: {', '.join((empty + const)[:5])}.")
    if len(df) < max(10, k * 3):
        raise ApiError("Not enough rows for this number of clusters.")
    Z = StandardScaler().fit_transform(SimpleImputer(strategy="median").fit_transform(X))
    rng = np.random.default_rng(SEED)
    fit_idx = rng.choice(len(Z), 50_000, replace=False) if len(Z) > 50_000 else np.arange(len(Z))
    km = KMeans(n_clusters=k, n_init=10, random_state=SEED).fit(Z[fit_idx])
    labels = km.predict(Z) + 1
    sil_idx = rng.choice(len(Z), 5000, replace=False) if len(Z) > 5000 else np.arange(len(Z))
    sil = float(silhouette_score(Z[sil_idx], labels[sil_idx])) if len(set(labels[sil_idx])) > 1 else None
    prof = X.groupby(labels).mean()
    overall, sd = X.mean(), X.std().replace(0, np.nan)
    sizes = pd.Series(labels).value_counts().sort_index()
    desc = []
    for c in prof.index:
        z = ((prof.loc[c] - overall) / sd).dropna()
        hi = [f for f in z[z >= 0.5].sort_values(ascending=False).index][:3]
        lo = [f for f in z[z <= -0.5].sort_values().index][:3]
        parts = ([f"higher {', '.join(hi)}"] if hi else []) + ([f"lower {', '.join(lo)}"] if lo else [])
        desc.append(f"Cluster {c} ({sizes[c]:,} customers, {sizes[c] / len(df):.1%}): " +
                    (("; ".join(parts) + " than the dataset average.") if parts else "close to the dataset average on all selected features."))
    xf = d.get("x_feature") if d.get("x_feature") in feats else feats[0]
    yf = d.get("y_feature") if d.get("y_feature") in feats and d.get("y_feature") != xf else next(f for f in feats if f != xf)
    pts_idx = rng.choice(len(df), 1500, replace=False) if len(df) > 1500 else np.arange(len(df))
    scatter = {str(c): [{"x": a, "y": b} for a, b in zip(X[xf].values[pts_idx][labels[pts_idx] == c], X[yf].values[pts_idx][labels[pts_idx] == c])
                         if not (pd.isna(a) or pd.isna(b))] for c in sizes.index}
    st["seg"] = pd.Series(labels, index=df.index)
    res = {"features": feats, "k": k, "silhouette": sil, "sizes": [{"cluster": int(c), "count": int(n), "pct": n / len(df)} for c, n in sizes.items()],
           "profile": {"columns": feats, "rows": [{"cluster": int(c), "values": prof.loc[c].tolist()} for c in prof.index], "overall": overall.tolist()},
           "descriptions": desc, "scatter": scatter, "x_feature": xf, "y_feature": yf,
           "notes": ["Clusters are statistical groupings. Read the profile table before giving them business names.",
                     "Missing values were filled with the median and features were standardised before clustering."] +
                    (["The model was fitted on a random sample of 50,000 rows; every row was then assigned to a cluster."] if len(Z) > 50_000 else [])}
    st["results"]["segmentation"] = {k_: v for k_, v in res.items() if k_ != "scatter"}
    return ok(**res)


@app.get("/api/download/segments")
def dl_segments():
    st = S()
    if st.get("seg") is None:
        raise ApiError("Run customer segmentation first.")
    return csv_response(st["work"].assign(cluster=st["seg"].values), "bankscope_clusters.csv")


# ----------------------------------------------------------------------------- classification (churn / credit)
LABELS = {"churn": "churn", "credit": "credit-risk"}


@app.get("/api/target/classes")
def target_classes():
    st = S()
    col = need_col(st["work"], request.args.get("column"), "Target column")
    ty = tstr(st["work"][col])
    vc = ty.value_counts()
    return ok(column=col, n_classes=int(len(vc)), missing=int(len(st["work"]) - len(ty)),
              classes=[{"value": str(k), "count": int(v), "pct": v / max(len(ty), 1)} for k, v in vc.head(20).items()],
              suggested=guess_positive(vc.index) if len(vc) == 2 else None, binary=bool(len(vc) == 2))


def prep_X(df, bundle):
    X = df[bundle["feats"]].copy()
    for c in bundle["cat"]:
        s = X[c]
        X[c] = s.astype(object).where(s.isna(), s.astype(str))
    if bundle["num"]:
        X[bundle["num"]] = X[bundle["num"]].apply(pd.to_numeric, errors="coerce").replace([np.inf, -np.inf], np.nan)
    return X


def score_all(st, kind, threshold=0.5):
    df, b = st["work"], st["models"][kind]
    missing = [c for c in b["feats"] if c not in df.columns]
    if missing:
        raise ApiError(f"These model columns no longer exist: {', '.join(missing[:5])}. Retrain the model.")
    prob = np.empty(len(df))
    try:
        for i in range(0, len(df), 50_000):
            prob[i:i + 50_000] = b["pipe"].predict_proba(prep_X(df.iloc[i:i + 50_000], b))[:, 1]
    except (ValueError, TypeError, KeyError):
        raise ApiError("The model could not be applied to the current data (column types changed since training). Retrain the model.")
    st["preds"][kind] = pd.DataFrame({"probability": prob, "prediction": (prob >= threshold).astype(int)}, index=df.index)
    return st["preds"][kind]


def importance(pipe, model_name):
    names = [re.sub(r"^(num|cat)__", "", n) for n in pipe.named_steps["pre"].get_feature_names_out()]
    m = pipe.named_steps["model"]
    if model_name == "logreg":
        vals, signed = np.abs(m.coef_[0]), m.coef_[0]
        note = "Logistic Regression: absolute standardised coefficient (sign shows direction toward the positive class)."
    else:
        vals, signed = m.feature_importances_, m.feature_importances_
        note = "Random Forest: impurity-based importance."
    order = np.argsort(vals)[::-1][:15]
    return [{"feature": names[i], "importance": float(vals[i]), "signed": float(signed[i])} for i in order], note


def train_classifier(kind):
    st = S()
    d = body()
    df, kinds = st["work"], get_kinds(st)
    label = LABELS[kind]
    target = need_col(df, d.get("target"), "Target column")
    if not d.get("confirmed"):
        raise ApiError(f"Please confirm which class represents the {label} outcome before training.")
    ty = tstr(df[target])
    classes = sorted(ty.unique())
    if len(classes) < 2:
        raise ApiError(f"Target '{target}' has only one class. A model needs both outcomes to learn from.")
    if len(classes) > 2:
        raise ApiError(f"Target '{target}' has {len(classes)} classes ({', '.join(classes[:5])}…). Only binary targets are supported.")
    positive = str(d.get("positive_class", ""))
    if positive not in classes:
        raise ApiError(f"Positive class must be one of: {', '.join(classes)}.")
    model_name = d.get("model", "logreg")
    if model_name not in ("logreg", "rf"):
        raise ApiError("Model must be 'logreg' or 'rf'.")
    test_size = as_float(d.get("test_size"), "test_size", 0.1, 0.5, 0.2)
    ok_kinds = ("numeric", "binary", "categorical")
    feats = d.get("features")
    if feats is None:
        feats = [c for c in df.columns if c != target and kinds[c] in ok_kinds]
    feats = need_cols(df, feats, "features", 1)
    if target in feats:
        raise ApiError("The target column cannot also be an input feature (data leakage).")
    bad = [f for f in feats if kinds[f] not in ok_kinds]
    if bad:
        raise ApiError(f"These columns are identifiers, free text or dates and cannot be used as features: {', '.join(bad[:5])}.")
    warns = []
    drop = [f for f in feats if df[f].notna().sum() == 0 or df[f].nunique() <= 1]
    if drop:
        warns.append(f"Ignored empty or constant feature(s): {', '.join(drop[:5])}.")
        feats = [f for f in feats if f not in drop]
    if not feats:
        raise ApiError("No usable features remain. Select columns that vary across customers.")
    y_all = (ty == positive).astype(int)
    if y_all.sum() < 5 or (len(y_all) - y_all.sum()) < 5 or len(y_all) < 30:
        raise ApiError("Each class needs at least 5 rows (and at least 30 rows overall) to train and test a model.")
    num = [f for f in feats if is_num(df[f])]
    bundle = {"feats": feats, "num": num, "cat": [f for f in feats if f not in num], "target": target, "positive": positive,
              "classes": classes, "model_name": model_name}
    idx = y_all.index
    sampled = len(idx) > app.config["MAX_ML_ROWS"]
    if sampled:
        idx = np.sort(np.random.default_rng(SEED).choice(idx.values, app.config["MAX_ML_ROWS"], replace=False))
        warns.append(f"Training used a random sample of {app.config['MAX_ML_ROWS']:,} of {len(y_all):,} labelled rows (memory protection).")
    X, y = prep_X(df.loc[idx], bundle), y_all.loc[idx].values
    share = min(y.mean(), 1 - y.mean())
    if share < 0.2:
        warns.append(f"Imbalanced target: the minority class is only {share:.1%} of rows. Accuracy can be misleading; focus on recall, precision and ROC-AUC, or tick 'balance classes'.")
    for f in num:
        v = X[f]
        if v.nunique() > 1 and abs(np.corrcoef(v.fillna(v.median()), y)[0, 1]) > 0.98:
            warns.append(f"'{f}' is almost perfectly correlated with the target. This may be data leakage; consider removing it.")
    Xtr, Xte, ytr, yte = train_test_split(X, y, test_size=test_size, stratify=y, random_state=SEED)
    pre = ColumnTransformer([
        ("num", Pipeline([("imp", SimpleImputer(strategy="median")), ("sc", StandardScaler())]), bundle["num"]),
        ("cat", Pipeline([("imp", SimpleImputer(strategy="most_frequent")),
                          ("oh", OneHotEncoder(handle_unknown="infrequent_if_exist", max_categories=20, sparse_output=False))]), bundle["cat"]),
    ])
    cw = "balanced" if d.get("balance") else None
    model = (LogisticRegression(max_iter=1000, class_weight=cw, random_state=SEED) if model_name == "logreg" else
             RandomForestClassifier(n_estimators=150, max_depth=12, min_samples_leaf=2, class_weight=cw, random_state=SEED, n_jobs=-1))
    pipe = Pipeline([("pre", pre), ("model", model)])
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        pipe.fit(Xtr, ytr)                                  # preprocessing is fitted on training rows only
    prob = pipe.predict_proba(Xte)[:, 1]
    pred = (prob >= 0.5).astype(int)
    tn, fp, fn, tp = confusion_matrix(yte, pred, labels=[0, 1]).ravel()
    auc = float(roc_auc_score(yte, prob)) if len(np.unique(yte)) == 2 else None
    fpr, tpr, _ = roc_curve(yte, prob)
    keep = np.unique(np.linspace(0, len(fpr) - 1, min(60, len(fpr))).astype(int))
    imp, imp_note = importance(pipe, model_name)
    metrics = {"accuracy": accuracy_score(yte, pred), "precision": precision_score(yte, pred, zero_division=0),
               "recall": recall_score(yte, pred, zero_division=0), "f1": f1_score(yte, pred, zero_division=0), "roc_auc": auc,
               "baseline_accuracy": max(yte.mean(), 1 - yte.mean())}
    bundle["pipe"] = pipe
    st["models"][kind] = bundle
    try:
        joblib.dump({k: v for k, v in bundle.items()}, Path(app.config["MODEL_DIR"]) / f"{session['sid']}_{kind}.joblib", compress=3)
    except OSError:
        log.warning("Could not save model file")
        warns.append("The model could not be saved to disk, but it is available for this session.")
    pr = score_all(st, kind)
    if kind == "credit":
        st["risk"] = {"count": int(pr["prediction"].sum()), "source": "model"}
    res = {"kind": kind, "target": target, "positive": positive, "classes": classes, "model": model_name, "features": feats,
           "metrics": metrics, "confusion": {"tn": tn, "fp": fp, "fn": fn, "tp": tp}, "roc": {"fpr": fpr[keep], "tpr": tpr[keep]},
           "importance": imp, "importance_note": imp_note, "n_train": len(ytr), "n_test": len(yte), "sampled": sampled,
           "warnings": warns, "predicted_positive": int(pr["prediction"].sum()), "total_rows": len(df)}
    if kind == "credit":
        res["disclaimer"] = "Experimental estimate for academic use only. Not a financial or lending decision."
    st["results"][kind] = {k: v for k, v in res.items() if k not in ("roc",)}
    return ok(**res)


def predict_classifier(kind):
    st = S()
    d = body()
    if kind not in st["models"]:
        raise ApiError(f"Train a {LABELS[kind]} model first.")
    thr = as_float(d.get("threshold"), "threshold", 0.01, 0.99, 0.5)
    pr = score_all(st, kind, thr)
    hist, _ = np.histogram(pr["probability"], bins=10, range=(0, 1))
    top = pr.sort_values("probability", ascending=False).head(10)
    res = {"threshold": thr, "rows": len(pr), "predicted_positive": int(pr["prediction"].sum()),
           "rate": float(pr["prediction"].mean()), "histogram": hist.tolist(),
           "top": [{"row": int(i) + 1, "probability": float(p)} for i, p in top["probability"].items()],
           "positive": st["models"][kind]["positive"], "target": st["models"][kind]["target"]}
    if kind == "credit":
        st["risk"] = {"count": res["predicted_positive"], "source": "model"}
        res["disclaimer"] = "Experimental estimate for academic use only. Not a financial or lending decision."
    st["results"].setdefault(kind, {})["prediction"] = {k: res[k] for k in ("threshold", "predicted_positive", "rate")}
    return ok(**res)


@app.post("/api/churn/train")
def churn_train():
    return train_classifier("churn")


@app.post("/api/churn/predict")
def churn_predict():
    return predict_classifier("churn")


@app.post("/api/credit-risk/train")
def credit_train():
    return train_classifier("credit")


@app.post("/api/credit-risk/predict")
def credit_predict():
    return predict_classifier("credit")


@app.post("/api/credit-risk/descriptive")
def credit_descriptive():
    """Rule-based descriptive risk flags for datasets WITHOUT a default label. Not a trained model."""
    st = S()
    d = body()
    df = st["work"]
    high = need_cols(df, d.get("high_features") or [], "high_features", 0)
    low = need_cols(df, d.get("low_features") or [], "low_features", 0)
    if set(high) & set(low):
        raise ApiError("A column cannot be both 'higher is riskier' and 'lower is riskier'.")
    if not high and not low:
        raise ApiError("Select at least one risk indicator column.")
    need_numeric_features(st, high + low)
    pct = as_int(d.get("percentile"), "percentile", 50, 99, 90)
    score, rows = pd.Series(0, index=df.index), []
    for f, direction in [(f, "high") for f in high] + [(f, "low") for f in low]:
        s = df[f].replace([np.inf, -np.inf], np.nan)
        if s.notna().sum() == 0:
            raise ApiError(f"Column '{f}' has no values.")
        thr = s.quantile(pct / 100) if direction == "high" else s.quantile(1 - pct / 100)
        flag = (s > thr) if direction == "high" else (s < thr)
        score += flag.fillna(False).astype(int)
        rows.append({"feature": f, "direction": direction, "threshold": thr, "flagged": int(flag.sum())})
    n = len(rows)
    need = max(1, math.ceil(n / 2))
    flagged = int((score >= need).sum())
    dist = score.value_counts().sort_index()
    res = {"indicators": rows, "percentile": pct, "min_flags": need, "high_count": flagged, "high_pct": flagged / len(df),
           "distribution": [{"flags": int(k), "count": int(v)} for k, v in dist.items()],
           "disclaimer": "Transparent rule-based flags, not a trained model and not a credit decision. A real default label is needed for prediction."}
    if st.get("risk", {}).get("source") != "model":
        st["risk"] = {"count": flagged, "source": "rules"}
    st["results"]["credit_rules"] = res
    return ok(**res)


@app.get("/api/download/predictions")
def dl_predictions():
    st = S()
    kind = request.args.get("kind")
    if kind not in LABELS:
        raise ApiError("kind must be 'churn' or 'credit'.")
    if kind not in st["preds"]:
        raise ApiError(f"Train a {LABELS[kind]} model first.")
    p = st["preds"][kind].rename(columns={"prediction": f"predicted_{kind}", "probability": f"{kind}_probability"})
    return csv_response(pd.concat([st["work"], p.round(4)], axis=1), f"bankscope_{kind}_predictions.csv")


@app.get("/api/download/model")
def dl_model():
    st = S()
    kind = request.args.get("kind")
    if kind not in LABELS or kind not in st["models"]:
        raise ApiError("Train a model first.")
    path = Path(app.config["MODEL_DIR"]) / f"{session['sid']}_{kind}.joblib"
    if not path.exists():
        raise ApiError("The saved model file is not available.", 404)
    return send_file(path, as_attachment=True, download_name=f"bankscope_{kind}_model.joblib")


# ----------------------------------------------------------------------------- forecasting
@app.post("/api/forecast")
def forecast():
    st = S()
    d = body()
    df, kinds = st["work"], get_kinds(st)
    dc = need_col(df, d.get("date_column"), "Date column")
    if is_num(df[dc]) or pdt.is_bool_dtype(df[dc]):
        raise ApiError(f"'{dc}' is numeric, not a date/timestamp column.")
    agg = d.get("agg") or "sum"
    if agg not in ("sum", "mean", "count"):
        raise ApiError("Aggregation must be sum, mean or count.")
    metric = d.get("metric") or None
    if metric:
        need_col(df, metric, "Metric")
        need_numeric_features(st, [metric])
    elif agg != "count":
        raise ApiError("Select a numeric metric, or choose 'count' to analyse the number of records.")
    freq = d.get("freq") or "M"
    if freq not in ("D", "W", "M"):
        raise ApiError("Frequency must be D, W or M.")
    rule = {"D": "D", "W": "W", "M": "MS"}[freq]
    window, horizon = as_int(d.get("window"), "window", 2, 12, 3), as_int(d.get("horizon"), "horizon", 1, 24, 6)
    dates = df[dc]
    if pdt.is_datetime64_any_dtype(dates):
        dates = dates.dt.tz_localize(None) if dates.dt.tz is not None else dates
    else:
        dates = parse_dates(dates)
    valid = dates.notna()
    if valid.mean() < 0.8:
        raise ApiError(f"Only {valid.mean():.0%} of '{dc}' could be read as dates. Choose a valid date column or clean it first.")
    s = (df.loc[valid, metric] if metric else pd.Series(1.0, index=df.index[valid])).astype(float)
    s.index = dates[valid]
    r_ = s.sort_index().resample(rule)
    series = r_.sum() if agg == "sum" else r_.mean() if agg == "mean" else r_.count()
    gaps = int(series.isna().sum())
    if gaps:
        series = series.interpolate(limit_direction="both")
    n = len(series)
    if n < 6:
        raise ApiError(f"Insufficient history: only {n} period(s) after grouping. At least 6 are needed; try a finer frequency.")
    if n > 2000:
        raise ApiError(f"{n:,} periods is too many to plot. Group by week or month instead.")
    if window >= n:
        raise ApiError("Moving-average window must be smaller than the number of periods.")
    vals = series.values.astype(float)
    hist, fc = list(vals), []
    for _ in range(horizon):
        fc.append(float(np.mean(hist[-window:])))
        hist.append(fc[-1])
    ma = series.rolling(window).mean()
    ma_pred = np.array([vals[t - window:t].mean() for t in range(window, n)])
    mae = float(np.mean(np.abs(vals[window:] - ma_pred)))
    naive = float(np.mean(np.abs(vals[window:] - vals[window - 1:-1])))
    fidx = pd.date_range(series.index[-1], periods=horizon + 1, freq=rule)[1:]
    fmt = "%Y-%m" if freq == "M" else "%Y-%m-%d"
    slope = float(np.polyfit(np.arange(n), vals, 1)[0])
    rel = slope * (n - 1) / abs(vals.mean()) if vals.mean() else 0
    direction = "increasing" if rel > 0.1 else "decreasing" if rel < -0.1 else "broadly flat"
    wm = {"D": "daily", "W": "weekly", "M": "monthly"}[freq]
    warns = ["The moving-average forecast is a simple baseline: it ignores seasonality and external factors and flattens over time."]
    if n < 12:
        warns.append(f"Only {n} periods of history are available; treat the forecast with extra caution.")
    if gaps:
        warns.append(f"{gaps} empty period(s) were filled by interpolation.")
    warns.append("The first and last periods may be incomplete, which can distort the trend.")
    label = f"{agg} of {metric}" if metric else "number of records"
    res = {"date_column": dc, "metric": metric, "agg": agg, "freq": freq, "window": window, "horizon": horizon, "periods": n,
           "labels": series.index.strftime(fmt).tolist(), "actual": vals, "moving_average": ma.values,
           "forecast_labels": fidx.strftime(fmt).tolist(), "forecast": fc, "metric_label": label,
           "stats": {"first": vals[0], "last": vals[-1], "mean": vals.mean(), "min": vals.min(), "max": vals.max(),
                     "change_pct": (vals[-1] - vals[0]) / abs(vals[0]) if vals[0] else None,
                     "last_change_pct": (vals[-1] - vals[-2]) / abs(vals[-2]) if vals[-2] else None, "trend": direction,
                     "mae_moving_average": mae, "mae_naive": naive},
           "summary": f"{wm.capitalize()} {label} shows a {direction} linear trend over {n} periods.", "warnings": warns}
    st["results"]["forecast"] = {k: v for k, v in res.items() if k not in ("actual", "moving_average", "labels")}
    return ok(**res)


# ----------------------------------------------------------------------------- insights & report
def group_rates(key, flag, min_n):
    g = pd.DataFrame({"k": key, "f": flag}).dropna().groupby("k", observed=True)["f"].agg(["mean", "size"])
    return g[g["size"] >= min_n]


def build_insights(st):
    df, kinds = st["work"], get_kinds(st)
    out = []
    add = lambda cat, txt: out.append({"category": cat, "text": txt})
    miss = df.isna().sum()
    miss = miss[miss > 0].sort_values(ascending=False)
    if miss.empty:
        add("Data quality", "The working dataset has no missing values.")
    else:
        add("Data quality", "Columns with the most missing values: " +
            ", ".join(f"{c} ({n:,}, {n / len(df):.1%})" for c, n in miss.head(3).items()) + ".")
    dup = int(df.duplicated().sum())
    if dup:
        add("Data quality", f"{dup:,} duplicate rows were found ({dup / len(df):.1%} of records).")
    res = eda_summary(st)
    if res["outliers"]:
        o = res["outliers"][0]
        add("Data quality", f"'{o['column']}' has the most IQR outliers: {o['count']:,} values ({o['pct']:.1%}) fall outside {o['lower']:.4g} to {o['upper']:.4g}.")
    for c in [c for c in df.columns if kinds[c] == "categorical"][:3]:
        vc = df[c].value_counts()
        add("Categories", f"In '{c}', the largest category is '{vc.index[0]}' with {vc.iloc[0]:,} records ({vc.iloc[0] / len(df):.1%}).")
    for p in res["correlation"]["strong"][:3]:
        strength = "strong" if abs(p["r"]) >= 0.7 else "moderate"
        add("Relationships", f"'{p['a']}' and '{p['b']}' have a {strength} {'positive' if p['r'] > 0 else 'negative'} correlation (r = {p['r']:.2f}). Correlation does not prove that one causes the other.")
    seg = st["results"].get("segmentation")
    if seg:
        for t in seg["descriptions"]:
            add("Segments", t)
    tgt = st["results"].get("churn", {}).get("target") or find_target(st, CHURN_WORDS)
    if tgt and tgt in df.columns:
        ty = tstr(df[tgt])
        pos = st["results"].get("churn", {}).get("positive") or guess_positive(ty.unique())
        if pos is not None and ty.nunique() == 2:
            flag = (ty == pos).astype(float)
            add("Churn", f"Overall, {flag.mean():.1%} of customers have '{tgt}' = {pos}.")
            cands = []
            for c in [c for c in df.columns if c != tgt and kinds[c] in ("categorical", "binary", "numeric")][:15]:
                key = df.loc[flag.index, c]
                if kinds[c] == "numeric":
                    try:
                        key = pd.qcut(key, 4, duplicates="drop")
                    except ValueError:
                        continue
                gr = group_rates(key, flag, 30)
                if len(gr) >= 2:
                    cands.append((gr["mean"].max() - gr["mean"].min(), c, gr))
            for _, c, gr in sorted(cands, key=lambda t: -t[0])[:3]:
                hi, lo = gr["mean"].idxmax(), gr["mean"].idxmin()
                add("Churn", f"By '{c}', the rate is highest for {hi} ({gr.loc[hi, 'mean']:.1%}, n={int(gr.loc[hi, 'size']):,}) and lowest for {lo} ({gr.loc[lo, 'mean']:.1%}, n={int(gr.loc[lo, 'size']):,}). This is a descriptive difference, not proof of cause.")
    for kind in ("churn", "credit"):
        r = st["results"].get(kind)
        if r and "metrics" in r:
            m = r["metrics"]
            add("Models", f"The {LABELS[kind]} model ({'Logistic Regression' if r['model'] == 'logreg' else 'Random Forest'}) scored recall {m['recall']:.2f}, precision {m['precision']:.2f} and F1 {m['f1']:.2f} on held-out test data. These are experimental estimates.")
    rr = st["results"].get("credit_rules")
    if rr:
        add("Risk", f"{rr['high_count']:,} customers ({rr['high_pct']:.1%}) meet at least {rr['min_flags']} of the selected rule-based risk flags. This is descriptive, not a prediction.")
    fc = st["results"].get("forecast")
    if fc:
        s = fc["stats"]
        ch = f" ({s['change_pct']:+.1%} from first to last period)" if s.get("change_pct") is not None else ""
        add("Trend", fc["summary"] + ch + " " + "A moving-average baseline forecast was produced; it is not a prediction guarantee.")
    return out


@app.get("/api/insights")
def insights_route():
    return ok(insights=build_insights(S()))


@app.get("/api/report")
def report():
    st = S()
    fmt = request.args.get("format", "html")
    if fmt not in ("html", "csv"):
        raise ApiError("format must be html or csv.")
    df, ins, R = st["work"], build_insights(st), st["results"]
    info_ = dataset_info(st)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    facts = [("Dataset", st["name"]), ("Generated", stamp), ("Rows (working)", f"{info_['rows']:,}"), ("Rows (original)", f"{info_['original_rows']:,}"),
             ("Columns", info_["columns_count"]), ("Missing values", f"{info_['missing_total']:,} ({info_['missing_pct']:.2f}%)"),
             ("Duplicate rows", f"{info_['duplicates']:,}")]
    metrics = []
    for kind in ("churn", "credit"):
        r = R.get(kind)
        if r and "metrics" in r:
            m = r["metrics"]
            metrics.append((f"{LABELS[kind].title()} model", f"{r['model']} | target={r['target']} (positive={r['positive']}) | accuracy {m['accuracy']:.3f}, precision {m['precision']:.3f}, recall {m['recall']:.3f}, F1 {m['f1']:.3f}, ROC-AUC {'n/a' if m['roc_auc'] is None else format(m['roc_auc'], '.3f')}"))
    seg = R.get("segmentation")
    if seg:
        metrics.append(("Segmentation", f"K-Means k={seg['k']} on {', '.join(seg['features'])}; silhouette {'n/a' if seg['silhouette'] is None else format(seg['silhouette'], '.3f')}"))
    fc = R.get("forecast")
    if fc:
        metrics.append(("Forecast", f"{fc['metric_label']} by {fc['freq']}, moving average window {fc['window']}, horizon {fc['horizon']}"))
    stats = R.get("eda", {}).get("stats", [])
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["section", "item", "value"])
        for k, v in facts:
            w.writerow(["Dataset", k, v])
        for t in st["clean_log"]:
            w.writerow(["Cleaning", "step", t])
        for k, v in metrics:
            w.writerow(["Analyses", k, v])
        for s in stats:
            w.writerow(["Statistics", s["column"], f"mean={s['mean']:.4g}; median={s['median']:.4g}; std={s['std']:.4g}; min={s['min']:.4g}; max={s['max']:.4g}"])
        for i in ins:
            w.writerow(["Insight", i["category"], i["text"]])
        w.writerow(["Note", "disclaimer", "Academic demonstration. Model outputs are experimental estimates, not financial decisions."])
        return Response("\ufeff" + buf.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=bankscope_report.csv"})
    e = html.escape
    rows = lambda items: "".join(f"<tr><th>{e(str(k))}</th><td>{e(str(v))}</td></tr>" for k, v in items)
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>BankScope Report</title><style>
body{{font:15px/1.6 'Segoe UI',system-ui,sans-serif;color:#0b1b3a;max-width:900px;margin:40px auto;padding:0 20px}}
h1{{color:#1d4ed8}}h2{{border-bottom:2px solid #dbeafe;padding-bottom:6px;margin-top:32px}}
table{{border-collapse:collapse;width:100%}}th,td{{text-align:left;padding:8px 10px;border-bottom:1px solid #e5e7eb;vertical-align:top}}th{{width:28%;color:#475569}}
li{{margin:6px 0}}small{{color:#64748b}}</style></head><body>
<h1>BankScope Analytics Report</h1><small>Generated {e(stamp)}. Academic demonstration only.</small>
<h2>Dataset</h2><table>{rows(facts)}</table>
<h2>Data cleaning applied</h2><ul>{''.join(f'<li>{e(t)}</li>' for t in st['clean_log']) or '<li>No cleaning applied (original data).</li>'}</ul>
<h2>Analyses and model evaluation</h2>{'<table>' + rows(metrics) + '</table>' if metrics else '<p>No analyses have been run yet.</p>'}
<h2>Descriptive statistics</h2>{'<table><tr><th>Column</th><td><b>mean · median · std · min · max</b></td></tr>' + ''.join(f"<tr><th>{e(s['column'])}</th><td>{s['mean']:.4g} · {s['median']:.4g} · {s['std']:.4g} · {s['min']:.4g} · {s['max']:.4g}</td></tr>" for s in stats) + '</table>' if stats else '<p>Open Exploratory Analysis to include statistics.</p>'}
<h2>Business insights</h2><ul>{''.join(f"<li><b>{e(i['category'])}:</b> {e(i['text'])}</li>" for i in ins)}</ul>
<h2>Important notes</h2><ul><li>Insights are calculated from the uploaded data only; correlation does not imply causation.</li>
<li>Model outputs are experimental estimates and must not be used for real lending or retention decisions.</li></ul></body></html>"""
    return Response(page, mimetype="text/html", headers={"Content-Disposition": "attachment; filename=bankscope_report.html"})


init_dirs()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5000)), debug=False, threaded=True)
