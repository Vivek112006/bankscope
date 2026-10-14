import io
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app as bankscope  # noqa: E402


def make_df(n=400, seed=1):
    r = np.random.default_rng(seed)
    age = r.integers(18, 80, n)
    bal = np.round(r.gamma(2, 40000, n), 2)
    score = r.integers(350, 850, n)
    geo = r.choice(["France", "Spain", "Germany"], n)
    exited = ((age > 55) | (r.random(n) < 0.08)).astype(int)
    default = ((score < 480) & (r.random(n) < 0.8) | (r.random(n) < 0.05)).astype(int)
    return pd.DataFrame({
        "CustomerId": 15_000_000 + np.arange(n), "Surname": [f"Name{i}" for i in range(n)],
        "CreditScore": score, "Geography": geo, "Gender": r.choice(["Male", "Female"], n), "Age": age,
        "Balance": bal, "NumOfProducts": r.integers(1, 4, n), "HasCrCard": r.integers(0, 2, n),
        "EstimatedSalary": np.round(r.normal(90000, 30000, n), 2), "Exited": exited, "LoanDefault": default,
        "TxnDate": pd.date_range("2022-01-01", periods=n, freq="D").strftime("%Y-%m-%d"),
    })


def csv_bytes(df):
    return df.to_csv(index=False).encode()


_n = iter(range(10_000))


def make_client(user=None):
    c = bankscope.app.test_client()
    tok = c.get("/api/auth/me").get_json()["data"]["csrf"]
    c.environ_base["HTTP_X_CSRF_TOKEN"] = tok
    r = c.post("/api/auth/register", json={"username": user or f"tester{next(_n)}", "password": "correct-horse-1"})
    assert r.status_code == 200, r.get_json()
    c.environ_base["HTTP_X_CSRF_TOKEN"] = r.get_json()["data"]["csrf"]     # token rotates on login
    return c


@pytest.fixture()
def client(tmp_path):
    bankscope.app.config.update(TESTING=True, UPLOAD_DIR=tmp_path / "up", MODEL_DIR=tmp_path / "models", DB_PATH=tmp_path / "users.db")
    (tmp_path / "up").mkdir()
    (tmp_path / "models").mkdir()
    bankscope.FAILS.clear()
    return make_client()


def upload(c, content, name="bank.csv"):
    return c.post("/api/upload", data={"file": (io.BytesIO(content), name)}, content_type="multipart/form-data")


@pytest.fixture()
def loaded(client):
    r = upload(client, csv_bytes(make_df()))
    assert r.status_code == 200, r.get_json()
    return client


def post(c, url, **js):
    return c.post(url, json=js)


# --------------------------------------------------------------------------- upload
def test_valid_csv_upload(client):
    r = upload(client, csv_bytes(make_df()))
    d = r.get_json()
    assert r.status_code == 200 and d["success"]
    assert d["data"]["rows"] == 400 and d["data"]["columns_count"] == 13
    kinds = {c["name"]: c["kind"] for c in d["data"]["columns"]}
    assert kinds["CustomerId"] == "id" and kinds["Surname"] == "id" or kinds["Surname"] == "text"
    assert kinds["Age"] == "numeric" and kinds["Geography"] == "categorical" and kinds["Exited"] == "binary"
    assert kinds["TxnDate"] == "date"
    assert d["data"]["suggested"]["churn"] == "Exited"
    prev = client.get("/api/dataset/preview").get_json()["data"]
    assert len(prev["rows"]) == 10
    assert client.get("/api/dataset/info").get_json()["data"]["rows"] == 400


def test_valid_excel_upload(client):
    buf = io.BytesIO()
    make_df(60).to_excel(buf, index=False, engine="openpyxl")
    r = upload(client, buf.getvalue(), "bank.xlsx")
    assert r.status_code == 200 and r.get_json()["data"]["rows"] == 60


def test_invalid_files_rejected(client):
    assert upload(client, b"hello", "malware.exe").status_code == 400
    assert upload(client, b"not a workbook", "x.xlsx").status_code == 400
    assert upload(client, b"a,b\n\x00\x01\x02,3\n", "x.csv").status_code == 400
    assert client.post("/api/upload", data={}, content_type="multipart/form-data").status_code == 400
    r = upload(client, b"a,b\n1,2\n", "../../evil.csv")        # path traversal attempt: harmless
    assert r.status_code == 200
    assert not list(Path(bankscope.app.config["UPLOAD_DIR"]).iterdir())


def test_empty_datasets(client):
    assert upload(client, b"", "e.csv").status_code == 400
    assert upload(client, b"a,b,c\n", "e.csv").status_code == 400
    assert upload(client, b"\n\n\n", "e.csv").status_code == 400


def test_duplicate_columns_and_row_limit(client):
    r = upload(client, b"a,a,b\n1,2,3\n4,5,6\n")
    assert r.status_code == 200
    d = r.get_json()["data"]
    assert [c["name"] for c in d["columns"]] == ["a", "a.1", "b"] and any("Duplicate" in w for w in d["warnings"])
    bankscope.app.config["MAX_ROWS"] = 100
    try:
        d = upload(client, csv_bytes(make_df(250))).get_json()["data"]
        assert d["rows"] == 100 and d["truncated"]
    finally:
        bankscope.app.config["MAX_ROWS"] = 500_000


def test_latin1_encoding_and_oversize(client):
    r = upload(client, "name,v\ncafé,1\nnaïve,2\n".encode("latin-1"))
    assert r.status_code == 200
    bankscope.app.config["MAX_CONTENT_LENGTH"] = 50
    try:
        r = upload(client, csv_bytes(make_df(100)))
        assert r.status_code == 413 and not r.get_json()["success"]
    finally:
        bankscope.app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024


def test_csrf_and_no_dataset(client):
    c2 = bankscope.app.test_client()
    assert c2.post("/api/clean", json={}).status_code == 403
    assert client.get("/api/dataset/info").status_code == 400
    assert client.get("/api/eda/summary").get_json()["success"] is False
    assert client.get("/api/nope").status_code == 404


def test_session_isolation(loaded):
    other = make_client()
    assert other.get("/api/dataset/info").status_code == 400


# --------------------------------------------------------------------------- cleaning
def dirty_csv():
    df = make_df(100)
    df.loc[3:12, "Balance"] = np.nan
    df.loc[5:9, "Geography"] = np.nan
    df["Salary"] = df["EstimatedSalary"].map(lambda v: f"${v:,.2f}")
    df.loc[1, "Salary"] = "n/a"
    df.loc[0, "Balance"] = 9_999_999
    df = pd.concat([df, df.iloc[:5]], ignore_index=True)
    return csv_bytes(df)


def test_cleaning_preview_apply_reset(client):
    upload(client, dirty_csv())
    info = client.get("/api/dataset/info").get_json()["data"]
    assert "Salary" in info["suspect_numeric"] and info["duplicates"] == 5
    ops = {"drop_duplicates": True, "numeric_fill": "median", "cat_fill": "custom", "cat_custom": "Unknown",
           "to_numeric": ["Salary"], "outliers": "cap", "drop_columns": ["Surname"]}
    p = post(client, "/api/clean", preview=True, operations=ops).get_json()["data"]
    assert p["rows_after"] == 100 and p["missing_after"] == 0 and p["cols_after"] == info["columns_count"] - 1
    assert client.get("/api/dataset/info").get_json()["data"]["rows"] == 105      # preview changes nothing
    a = post(client, "/api/clean", preview=False, operations=ops).get_json()["data"]
    assert a["applied"] and a["info"]["rows"] == 100 and a["info"]["modified"]
    assert client.get("/api/dataset/preview?source=original").get_json()["data"]["columns"].count("Surname") == 1
    csv_text = client.get("/api/download/cleaned").data.decode()
    assert csv_text.count("\n") == 101 and "Surname" not in csv_text.split("\n")[0]
    r = post(client, "/api/clean", reset=True).get_json()["data"]
    assert r["rows"] == 105 and not r["modified"]


def test_cleaning_outlier_removal_and_validation(loaded):
    d = post(loaded, "/api/clean", preview=True, operations={"outliers": "remove", "outlier_columns": ["Balance"]}).get_json()["data"]
    assert d["rows_after"] < d["rows_before"]
    assert post(loaded, "/api/clean", operations={"drop_columns": ["Nope"]}).status_code == 400
    assert post(loaded, "/api/clean", operations={"numeric_fill": "banana"}).status_code == 400
    assert post(loaded, "/api/clean", operations={"outliers": "cap", "outlier_columns": ["Geography"]}).status_code == 400
    assert post(loaded, "/api/clean", operations={"drop_columns": list(make_df().columns)}).status_code == 400
    assert loaded.post("/api/clean", data="notjson", content_type="text/plain").status_code == 400


# --------------------------------------------------------------------------- EDA
def test_eda_summary(loaded):
    d = loaded.get("/api/eda/summary").get_json()["data"]
    cols = {s["column"] for s in d["stats"]}
    assert {"Age", "Balance", "CreditScore"} <= cols and "CustomerId" not in cols
    age = next(s for s in d["stats"] if s["column"] == "Age")
    df = make_df()
    assert age["mean"] == pytest.approx(df["Age"].mean()) and age["median"] == pytest.approx(df["Age"].median())
    assert age["std"] == pytest.approx(df["Age"].std())
    assert any(c["column"] == "Geography" for c in d["categorical"])
    m = d["correlation"]["matrix"]
    assert len(m) == len(d["correlation"]["columns"]) and m[0][0] == pytest.approx(1.0)


def test_eda_charts(loaded):
    df = make_df()
    h = post(loaded, "/api/eda/chart", chart="histogram", x="Age", bins=10).get_json()["data"]
    assert sum(h["values"]) == 400 and len(h["labels"]) == 10
    b = post(loaded, "/api/eda/chart", chart="bar", x="Geography").get_json()["data"]
    assert dict(zip(b["labels"], b["values"])) == df["Geography"].value_counts().to_dict()
    b2 = post(loaded, "/api/eda/chart", chart="bar", x="Geography", y="Balance", agg="mean").get_json()["data"]
    assert b2["values"][0] == pytest.approx(df.groupby("Geography")["Balance"].mean().max())
    s = post(loaded, "/api/eda/chart", chart="scatter", x="Age", y="Balance").get_json()["data"]
    assert len(s["points"]) == 400
    l = post(loaded, "/api/eda/chart", chart="line", x="TxnDate", y="Balance", agg="sum").get_json()["data"]
    assert len(l["labels"]) > 1
    l2 = post(loaded, "/api/eda/chart", chart="line", x="Age", y="Balance", agg="mean").get_json()["data"]
    assert len(l2["labels"]) == 30   # 62 distinct ages -> 30 equal-width bins
    for bad in [dict(chart="pie", x="Age"), dict(chart="histogram", x="Geography"), dict(chart="scatter", x="Age"),
                dict(chart="bar", x="Missing"), dict(chart="bar", x="Balance"), dict(chart="bar", x="Geography", agg="mean"),
                dict(chart="histogram", x="Age", bins="abc")]:
        assert post(loaded, "/api/eda/chart", **bad).status_code == 400, bad


# --------------------------------------------------------------------------- segmentation
def test_segmentation(loaded):
    r = post(loaded, "/api/segment", features=["Age", "Balance", "EstimatedSalary"], k=3, x_feature="Age", y_feature="Balance")
    d = r.get_json()["data"]
    assert r.status_code == 200 and len(d["sizes"]) == 3 and sum(s["count"] for s in d["sizes"]) == 400
    assert -1 <= d["silhouette"] <= 1 and len(d["descriptions"]) == 3 and set(d["scatter"]) == {"1", "2", "3"}
    csv_text = loaded.get("/api/download/segments").data.decode()
    assert csv_text.split("\n")[0].endswith("cluster") and csv_text.count("\n") == 401
    for bad in [dict(features=["Age"]), dict(features=["Age", "CustomerId"]), dict(features=["Age", "Geography"]),
                dict(features=["Age", "Balance"], k=1), dict(features=["Age", "Nope"]), dict(features="Age")]:
        assert post(loaded, "/api/segment", **bad).status_code == 400, bad


def test_segment_download_requires_run(loaded):
    assert loaded.get("/api/download/segments").status_code == 400


# --------------------------------------------------------------------------- churn / credit
def classes_ok(c, col, expect):
    d = c.get(f"/api/target/classes?column={col}").get_json()["data"]
    assert d["binary"] and d["suggested"] == expect
    return d


def test_churn_training_and_prediction(loaded):
    classes_ok(loaded, "Exited", "1")
    base = dict(target="Exited", positive_class="1", confirmed=True)
    r = post(loaded, "/api/churn/train", **base)
    d = r.get_json()["data"]
    assert r.status_code == 200
    m = d["metrics"]
    assert all(0 <= m[k] <= 1 for k in ("accuracy", "precision", "recall", "f1", "roc_auc"))
    assert m["roc_auc"] > 0.7 and sum(d["confusion"].values()) == d["n_test"]
    assert "CustomerId" not in d["features"] and "Surname" not in d["features"] and "TxnDate" not in d["features"]
    assert d["importance"] and "Logistic" in d["importance_note"]
    rf = post(loaded, "/api/churn/train", **base, model="rf", balance=True, test_size=0.3).get_json()["data"]
    assert rf["model"] == "rf" and "Random Forest" in rf["importance_note"]
    p = post(loaded, "/api/churn/predict", threshold=0.4).get_json()["data"]
    assert p["rows"] == 400 and len(p["histogram"]) == 10 and len(p["top"]) == 10
    csv_text = loaded.get("/api/download/predictions?kind=churn").data.decode()
    assert "predicted_churn" in csv_text.split("\n")[0] and csv_text.count("\n") == 401
    assert loaded.get("/api/download/model?kind=churn").status_code == 200
    assert post(loaded, "/api/churn/predict", threshold=5).status_code == 400
    cards = loaded.get("/api/dashboard").get_json()["data"]["cards"]
    assert cards["churn"]["value"] == pytest.approx(make_df()["Exited"].mean())


def test_credit_risk_training(loaded):
    r = post(loaded, "/api/credit-risk/train", target="LoanDefault", positive_class="1", confirmed=True, features=["CreditScore", "Age", "Balance", "Geography"])
    d = r.get_json()["data"]
    assert r.status_code == 200 and "Experimental" in d["disclaimer"] and set(d["features"]) == {"CreditScore", "Age", "Balance", "Geography"}
    cards = loaded.get("/api/dashboard").get_json()["data"]["cards"]
    assert cards["risk"]["value"] == d["predicted_positive"]


def test_classifier_errors(loaded):
    ok_ = dict(target="Exited", positive_class="1", confirmed=True)
    assert post(loaded, "/api/churn/train", target="Exited", positive_class="1").status_code == 400          # not confirmed
    assert post(loaded, "/api/churn/train").status_code == 400                                              # missing target
    assert post(loaded, "/api/churn/train", target="Nope", positive_class="1", confirmed=True).status_code == 400
    assert post(loaded, "/api/churn/train", target="Geography", positive_class="France", confirmed=True).status_code == 400  # 3 classes
    assert post(loaded, "/api/churn/train", **{**ok_, "positive_class": "9"}).status_code == 400
    assert post(loaded, "/api/churn/train", **{**ok_, "features": ["CustomerId", "Age"]}).status_code == 400
    assert post(loaded, "/api/churn/train", **{**ok_, "features": ["Exited", "Age"]}).status_code == 400
    assert post(loaded, "/api/churn/train", **{**ok_, "model": "svm"}).status_code == 400
    assert post(loaded, "/api/churn/predict").status_code == 400                                            # untrained
    assert post(loaded, "/api/credit-risk/predict").status_code == 400
    assert loaded.get("/api/download/predictions?kind=churn").status_code == 400
    assert loaded.get("/api/download/predictions?kind=zzz").status_code == 400


def test_single_class_and_tiny_target(client):
    df = make_df(100)
    df["Flag"] = 0
    upload(client, csv_bytes(df))
    r = post(client, "/api/credit-risk/train", target="Flag", positive_class="0", confirmed=True)
    assert r.status_code == 400 and "only one class" in r.get_json()["error"]
    df["Flag"] = 0
    df.loc[:1, "Flag"] = 1
    upload(client, csv_bytes(df))
    assert post(client, "/api/credit-risk/train", target="Flag", positive_class="1", confirmed=True).status_code == 400


def test_credit_descriptive_without_target(loaded):
    r = post(loaded, "/api/credit-risk/descriptive", high_features=["Balance"], low_features=["CreditScore"], percentile=90)
    d = r.get_json()["data"]
    assert r.status_code == 200 and d["min_flags"] == 1 and len(d["indicators"]) == 2 and "not a trained model" in d["disclaimer"]
    assert loaded.get("/api/dashboard").get_json()["data"]["cards"]["risk"]["value"] == d["high_count"]
    assert post(loaded, "/api/credit-risk/descriptive").status_code == 400
    assert post(loaded, "/api/credit-risk/descriptive", high_features=["Age"], low_features=["Age"]).status_code == 400
    assert post(loaded, "/api/credit-risk/descriptive", high_features=["Geography"]).status_code == 400


def test_unseen_category_and_missing_values(client):
    df = make_df(300)
    df.loc[10:30, "Balance"] = np.nan
    df.loc[40:50, "Geography"] = np.nan
    upload(client, csv_bytes(df))
    assert post(client, "/api/churn/train", target="Exited", positive_class="1", confirmed=True).status_code == 200
    post(client, "/api/clean", preview=False, operations={"cat_fill": "custom", "cat_custom": "Atlantis"})
    assert post(client, "/api/churn/predict").status_code == 200          # unseen category handled


# --------------------------------------------------------------------------- forecast
def test_forecast(loaded):
    r = post(loaded, "/api/forecast", date_column="TxnDate", metric="Balance", agg="sum", freq="M", window=3, horizon=4)
    d = r.get_json()["data"]
    assert r.status_code == 200 and len(d["forecast"]) == 4 and len(d["actual"]) == d["periods"] == len(d["labels"])
    assert d["forecast"][0] == pytest.approx(np.mean(d["actual"][-3:]))
    assert d["forecast_labels"][0] > d["labels"][-1]
    cnt = post(loaded, "/api/forecast", date_column="TxnDate", agg="count", freq="W").get_json()["data"]
    assert sum(cnt["actual"]) == 400
    for bad in [dict(date_column="Age", metric="Balance"), dict(date_column="Geography", metric="Balance"),
                dict(date_column="TxnDate"), dict(date_column="TxnDate", metric="Geography"), dict(date_column="TxnDate", metric="Balance", freq="Y"),
                dict(date_column="TxnDate", metric="Balance", window=99), dict(metric="Balance")]:
        assert post(loaded, "/api/forecast", **bad).status_code == 400, bad


def test_forecast_insufficient_history(client):
    upload(client, b"d,v\n2024-01-01,1\n2024-01-02,2\n2024-01-03,3\n2024-01-04,4\n")
    r = post(client, "/api/forecast", date_column="d", metric="v", freq="M")
    assert r.status_code == 400 and "Insufficient" in r.get_json()["error"]


# --------------------------------------------------------------------------- report
def test_report_and_insights(loaded):
    post(loaded, "/api/segment", features=["Age", "Balance"], k=3)
    post(loaded, "/api/churn/train", target="Exited", positive_class="1", confirmed=True)
    post(loaded, "/api/forecast", date_column="TxnDate", metric="Balance", freq="M")
    loaded.get("/api/eda/summary")
    ins = loaded.get("/api/insights").get_json()["data"]["insights"]
    cats = {i["category"] for i in ins}
    assert {"Data quality", "Categories", "Segments", "Churn", "Models", "Trend"} <= cats
    h = loaded.get("/api/report?format=html")
    assert h.status_code == 200 and b"BankScope Analytics Report" in h.data and b"Logistic" not in b""
    c = loaded.get("/api/report?format=csv")
    assert c.status_code == 200 and b"section,item,value" in c.data
    assert loaded.get("/api/report?format=pdf").status_code == 400


# --------------------------------------------------------------------------- frontend contract
def test_frontend_routes_exist(client):
    js = (Path(bankscope.__file__).parent / "static/js/app.js").read_text(encoding="utf-8")
    used = set(re.findall(r"""['"`](/api/[a-z0-9/\-]+)""", js))
    used = {u for u in used if not u.endswith("/")}          # prefix checks such as startsWith('/api/auth/')
    assert len(used) >= 15
    rules = {r.rule for r in bankscope.app.url_map.iter_rules()}
    assert used <= rules, used - rules


def test_index_page(client):
    r = client.get("/")
    assert r.status_code == 200 and b'name="csrf"' in r.data and b"BankScope" in r.data and b"Sign out" in r.data
    assert client.get("/static/js/app.js").status_code == 200
    assert client.get("/uploads/x").status_code == 404 and client.get("/saved_models/x").status_code == 404


# --------------------------------------------------------------------------- login / accounts
def test_pages_require_login(tmp_path):
    bankscope.app.config.update(TESTING=True, DB_PATH=tmp_path / "u.db")
    c = bankscope.app.test_client()
    r = c.get("/")
    assert r.status_code == 302 and r.headers["Location"].endswith("/login")
    page = c.get("/login")
    assert page.status_code == 200 and b"Create account" in page.data and b"BankScope" in page.data
    for url in ("/api/dataset/info", "/api/config", "/api/dashboard", "/api/report"):
        r = c.get(url)
        assert r.status_code == 401 and r.get_json()["success"] is False, url
    assert c.get("/static/js/login.js").status_code == 200


def test_register_validation_and_duplicates(client, tmp_path):
    c = bankscope.app.test_client()
    c.environ_base["HTTP_X_CSRF_TOKEN"] = c.get("/api/auth/me").get_json()["data"]["csrf"]
    bad = [{"username": "ab", "password": "longenough1"}, {"username": "has space", "password": "longenough1"},
           {"username": "gooduser", "password": "short"}, {"username": "gooduser12", "password": "gooduser12"},
           {"username": 5, "password": "longenough1"}, {"username": "gooduser"}, {}]
    for b in bad:
        assert c.post("/api/auth/register", json=b).status_code == 400, b
    for weak in ("password", "12345678", "aaaaaaaaaa", "qwerty123"):
        r = c.post("/api/auth/register", json={"username": "weakling", "password": weak})
        assert r.status_code == 400 and "easy to guess" in r.get_json()["error"], weak
    assert c.post("/api/auth/register", json={"username": "Alice_1", "password": "longenough1"}).status_code == 200
    c2 = bankscope.app.test_client()
    c2.environ_base["HTTP_X_CSRF_TOKEN"] = c2.get("/api/auth/me").get_json()["data"]["csrf"]
    r = c2.post("/api/auth/register", json={"username": "alice_1", "password": "longenough1"})   # case-insensitive
    assert r.status_code == 409
    import sqlite3
    row = sqlite3.connect(bankscope.app.config["DB_PATH"]).execute("SELECT pw_hash FROM users WHERE username='alice_1'").fetchone()
    assert row and "longenough1" not in row[0] and row[0].startswith(("scrypt:", "pbkdf2:"))      # hashed, never plain


def test_login_logout_flow(client):
    me = client.get("/api/auth/me").get_json()["data"]
    assert me["authenticated"] and me["user"].startswith("tester")
    upload(client, csv_bytes(make_df(50)))
    assert client.get("/api/dataset/info").status_code == 200
    assert client.get("/", follow_redirects=False).status_code == 200
    assert client.get("/login").status_code == 302
    r = client.post("/api/auth/logout", json={})
    assert r.status_code == 200
    assert client.get("/api/dataset/info").status_code == 401
    assert client.get("/", follow_redirects=False).status_code == 302
    # log back in: the previous workspace must be gone
    user = me["user"]
    client.environ_base["HTTP_X_CSRF_TOKEN"] = client.get("/api/auth/me").get_json()["data"]["csrf"]
    r = client.post("/api/auth/login", json={"username": user.upper(), "password": "correct-horse-1"})
    assert r.status_code == 200
    client.environ_base["HTTP_X_CSRF_TOKEN"] = r.get_json()["data"]["csrf"]
    assert client.get("/api/dataset/info").status_code == 400


def test_wrong_password_and_lockout(client):
    c = bankscope.app.test_client()
    c.environ_base["HTTP_X_CSRF_TOKEN"] = c.get("/api/auth/me").get_json()["data"]["csrf"]
    user = client.get("/api/auth/me").get_json()["data"]["user"]
    for _ in range(5):
        r = c.post("/api/auth/login", json={"username": user, "password": "wrong-password"})
        assert r.status_code == 401 and r.get_json()["error"] == "Invalid username or password."
    assert c.post("/api/auth/login", json={"username": "nobody-here", "password": "x"}).status_code == 401
    r = c.post("/api/auth/login", json={"username": user, "password": "correct-horse-1"})
    assert r.status_code == 429                                         # locked even with the right password
    assert c.get("/api/dataset/info").status_code == 401


def test_auth_requires_csrf(tmp_path):
    bankscope.app.config.update(TESTING=True, DB_PATH=tmp_path / "u.db")
    c = bankscope.app.test_client()
    c.get("/login")
    assert c.post("/api/auth/register", json={"username": "someone", "password": "longenough1"}).status_code == 403
    assert c.post("/api/auth/login", json={"username": "someone", "password": "longenough1"}).status_code == 403
