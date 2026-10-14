# BankScope — Banking Business Analytics Dashboard

A college-level Business Analytics project: upload a Kaggle banking dataset (CSV or Excel) and run
**data cleaning, EDA, K-Means customer segmentation, churn prediction, credit-risk analysis,
trend/forecast analysis and an insights report** — all calculated from *your* data.

**Stack:** HTML5 · CSS3 · Vanilla JS · Chart.js (bundled, works offline) · Python · Flask · Pandas · NumPy · Scikit-learn · Joblib · OpenPyXL

> Academic demonstration only. Model outputs are experimental estimates — never use them for real lending or retention decisions.

---

## 1. Install Python (Windows)
1. Download **Python 3.11 or 3.12** from <https://www.python.org/downloads/windows/>.
2. In the installer, **tick "Add python.exe to PATH"**, then click *Install Now*.
3. Open **Windows Terminal** (PowerShell) and check:
   ```powershell
   python --version
   ```
   It should print `Python 3.11.x` or `3.12.x`. (If `python` is not found, try `py --version`.)
4. Install **VS Code** from <https://code.visualstudio.com/> and the *Python* extension (optional but handy).

## 2. Open the project
Unzip `bankscope.zip`, then in Windows Terminal:
```powershell
cd C:\path\to\bankscope
code .          # optional: opens VS Code
```

## 3. Create and activate a virtual environment
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```
Your prompt now starts with `(.venv)`.
If PowerShell says *"running scripts is disabled"*, run this once and activate again:
```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```
(In VS Code: `Ctrl+Shift+P` → *Python: Select Interpreter* → choose `.venv`.)

## 4. Install dependencies
```powershell
python -m pip install --upgrade pip
pip install -r requirements.txt
```

## 5. Start the application
```powershell
python app.py
```
Leave this window open. You will see `Running on http://127.0.0.1:5000`.

## 6. Open the website
Go to **<http://127.0.0.1:5000>** in Chrome, Edge or Firefox. Stop the server with `Ctrl+C`.

## 7. Sign in
The first page is a **login page**. Click **Create account**, choose a username (3–30 letters, numbers, `.`, `-`, `_`) and a password
(at least 8 characters), and you are signed in. Next time use **Sign in**. **Sign out** is in the top-right corner.
* The form checks your input as you type: show/hide password, a password-strength meter, a Caps Lock warning, inline field errors, and **Remember my username** (stores only the username in your browser).
* Very common passwords (e.g. `password`, `12345678`) are rejected.
* Accounts are stored in a local SQLite file, `bankscope.db` (created automatically). Passwords are stored only as salted hashes.
* After 5 wrong passwords for an account, sign-in is paused for 5 minutes.
* Each user's uploaded data is private to their session. Forgot your password? Stop the server and delete `bankscope.db` (this removes all accounts).

## 8. Upload a Kaggle dataset
1. Download a dataset from Kaggle (examples below) and, if it is a `.zip`, extract the `.csv`.
2. Click **Dataset upload**, then drop the file or click to browse.
3. BankScope shows rows, columns, missing values, detected column types (numeric, categorical, date, binary, **id**), and the first 10 rows.

Good datasets to try: *Bank Customer Churn Prediction* (target `Exited`), *Credit Card Customers* (target `Attrition_Flag`),
*Credit Risk / Loan Default* datasets (target such as `loan_status` or `default`), and any bank-transaction CSV with a date column (for Trend analysis).

**Limits (configurable):** 200 MB upload, 500,000 rows kept in memory, 100,000 rows used to fit a model.
Larger files are read in chunks and truncated with a visible warning; models train on a random sample with a visible warning.
To change them (PowerShell, before `python app.py`):
```powershell
$env:BANKSCOPE_MAX_UPLOAD_MB = 500
$env:BANKSCOPE_MAX_ROWS = 1000000
$env:BANKSCOPE_MAX_ML_ROWS = 200000
```

## 9. Run each analysis module
| Module | What to do |
|---|---|
| **Dashboard** | Cards (records, customers, average balance, missing values, churn rate, high-risk count) are calculated from your data. A card shows *Not available for this dataset* if the needed column/model does not exist. |
| **Data cleaning** | Tick options → **Preview changes** (nothing is applied) → **Apply**. Remove duplicates, fill missing values (mean/median/mode/custom), fix text-that-looks-numeric or date columns, treat IQR outliers (cap/remove), drop columns. **Reset** restores the original upload. **Download cleaned CSV**. |
| **Exploratory analysis** | Descriptive stats (mean, median, mode, min, max, std), missing/outlier summaries, correlation heatmap, categorical counts, and a chart builder (bar, line, histogram, scatter). |
| **Customer segmentation** | Pick ≥2 numeric features, choose *k*, **Run**. See cluster sizes, rule-based descriptions, a scatter plot and an averages table. Download cluster assignments. |
| **Churn prediction** | Choose the target column → check the suggested "churned" value → tick the confirmation box → choose features/model → **Train**. See accuracy, precision, recall, F1, ROC-AUC, confusion matrix, ROC curve and feature importance. **Predict** scores every row; download predictions CSV or the model file. |
| **Credit risk** | Same workflow, for a real default/risk label. If you have **no** label, use *Descriptive risk flags* (transparent percentile rules, clearly *not* a trained model). |
| **Trend analysis** | Choose a date column and metric; group by day/week/month; see history plus a moving-average baseline forecast (dashed). Needs ≥6 periods. |
| **Reports & insights** | Rule-based insights from real calculations; download an HTML or CSV report. |

## 10. Run the automated tests
```powershell
python -m pytest tests -v
```
All tests should pass (login/register/logout, lockout, protected pages, upload CSV/Excel, invalid/empty files, cleaning, EDA, charts, segmentation, churn, credit risk,
forecast, error handling, CSRF, session isolation, and a check that every API route used by `app.js` exists).

## 11. Troubleshooting
| Problem | Fix |
|---|---|
| `python` not recognised | Re-run the installer and tick *Add to PATH*, or use `py -3.12 -m venv .venv`. |
| `Activate.ps1 cannot be loaded` | `Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned` |
| `No module named flask` (or pandas…) | The venv is not active. Run `.\.venv\Scripts\Activate.ps1`, then `pip install -r requirements.txt`. |
| Pip build errors on very new Python (3.13+) | Install Python 3.11 or 3.12 and recreate `.venv`. |
| Port 5000 already in use | `$env:PORT = 5050; python app.py` and open `http://127.0.0.1:5050`. |
| "No dataset loaded" after a restart or 2 hours idle | Data lives only in server memory and is discarded on restart/idle. Upload again. |
| "Invalid or missing security token" / sent back to the login page | The server was restarted or the session ended. Reload and sign in again. |
| "File too large" | Raise `BANKSCOPE_MAX_UPLOAD_MB` or upload a smaller/sampled CSV. |
| Weird characters (é → Ã©) | The file was not UTF-8; BankScope warns and reads it as Windows-1252/Latin-1. Re-save as UTF-8 for best results. |
| "Only binary targets are supported" | Choose a column with exactly two values (e.g. 0/1, Yes/No). |
| "Each class needs at least 5 rows" / "only one class" | The target is too rare/constant to learn from. |
| Numeric column shown as *categorical/text* | It contains symbols like `$`/`,`. Use *Data cleaning → Fix data types*. |
| Date column not offered in Trend | Convert it in *Data cleaning → Fix data types → text columns that look like dates*. |
| Excel file is slow | Excel loads the whole sheet; export to CSV for large data. |

## 12. Deploy on Render (public website)
The project already contains `render.yaml`, a `/healthz` health check and a production server (gunicorn).

1. **Put the project on GitHub.** Create a free account at <https://github.com>, make a new *private* repository, then in the project folder:
   ```powershell
   git init
   git add .
   git commit -m "BankScope"
   git branch -M main
   git remote add origin https://github.com/YOUR-NAME/bankscope.git
   git push -u origin main
   ```
   (`.gitignore` already keeps `.venv`, uploads and the user database out of the repo.)
2. **Create the service.** Sign up at <https://render.com> with GitHub, then **New +** → **Blueprint** → pick your repo → **Apply**.
   Render reads `render.yaml` and sets everything up (Python 3.12, `pip install -r requirements.txt`, gunicorn, health check, a random secret key, HTTPS).
   *Manual alternative:* **New + → Web Service**, build command `pip install -r requirements.txt`, start command
   `gunicorn app:app --workers 1 --threads 4 --timeout 120 --bind 0.0.0.0:$PORT`, health check path `/healthz`, and the environment variables
   `PYTHON_VERSION=3.12.3`, `BANKSCOPE_PRODUCTION=1`, `BANKSCOPE_SECRET_KEY=<any long random text>`.
3. **Wait for the build** (a few minutes). Open the `https://bankscope-xxxx.onrender.com` link Render shows, create an account and upload a dataset.

**Important limits of the free plan**
| Limit | What it means for BankScope |
|---|---|
| Sleeps after ~15 min idle; first request takes ~30-60 s | Uploaded data and logins held in memory are lost when it sleeps or restarts. |
| Filesystem is wiped on restart/redeploy | **User accounts (`bankscope.db`) are lost too.** Re-register after a restart. Fine for a demo. |
| 512 MB RAM | `render.yaml` lowers the limits to 25 MB uploads, 100,000 rows, 30,000 model rows. Raise them only on a bigger plan. |
| Keep `--workers 1` | Datasets live in one process's memory; two workers would "lose" your upload. |

To keep accounts permanently you need a **paid plan with a persistent disk**: uncomment the `disk:` block and the `BANKSCOPE_DB_PATH` lines in `render.yaml`
(accounts then live on `/var/data`). Uploaded datasets are still memory-only by design.
Never commit real banking data to GitHub; use only public Kaggle datasets on a public demo site.

## Project structure
```
bankscope/
  app.py              Flask app: all APIs, validation, analytics, ML
  requirements.txt
  README.md
  bankscope.db        user accounts (created on first run)
  render.yaml         Render deployment blueprint
  templates/index.html, login.html
  static/css/style.css
  static/js/app.js    UI logic (fetch -> Flask JSON APIs)
  static/js/login.js  login / register page logic
  static/js/chart.umd.js   Chart.js 4.4.3 (bundled, MIT licence)
  uploads/            temporary upload area (files deleted right after parsing)
  saved_models/       temporary trained models (deleted on idle/restart)
  tests/test_app.py
```

## API summary
All responses: `{"success": true, "data": {...}}` or `{"success": false, "error": "message"}` with a proper HTTP status.
State-changing (`POST`) requests need the `X-CSRF-Token` header (the front end does this automatically).

`POST /api/auth/register|login|logout` · `GET /api/auth/me` (public, the rest need sign-in) · `POST /api/upload` · `GET /api/dataset/info` · `GET /api/dataset/preview` · `GET /api/dashboard` · `POST /api/clean` ·
`GET /api/eda/summary` · `POST /api/eda/chart` · `POST /api/segment` · `GET /api/target/classes` ·
`POST /api/churn/train` · `POST /api/churn/predict` · `POST /api/credit-risk/train` · `POST /api/credit-risk/predict` ·
`POST /api/credit-risk/descriptive` · `POST /api/forecast` · `GET /api/insights` · `GET /api/report?format=html|csv` ·
`GET /api/download/cleaned|segments|predictions|model`

## Design & security notes
* Uploads are saved under a **random server-side filename** (never the client's), parsed, then **deleted immediately**. Datasets are never written to browser storage.
* Each browser session gets an isolated in-memory workspace; the session id is in a signed cookie. Idle workspaces (2 h) and their model files are purged.
* Original data is always preserved; cleaning edits a separate working copy.
* Churn/credit models: train/test split, imputation/scaling/one-hot encoding **fitted on training data only** (scikit-learn `Pipeline`), unseen categories handled, identifier/text/date columns excluded, class-imbalance and possible-leakage warnings, fixed random seed (42).
* No tracebacks reach the browser; logs contain counts only, never data values.
* **Production would additionally need:** HTTPS with `BANKSCOPE_PRODUCTION=1` (secure cookies), a fixed `BANKSCOPE_SECRET_KEY`, stronger authentication (email verification, password reset, MFA, shared rate limiting — the built-in login is a simple demo), a production server (e.g. `pip install waitress` then `waitress-serve --threads=4 app:app` — keep a **single process**, because workspaces live in process memory), a persistent store if saved analyses are required, and a security/model-risk review. This project intentionally uses none of these.
