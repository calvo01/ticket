from __future__ import annotations

import csv
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, date
from typing import Optional, List, Dict, Any, Tuple

from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

APP_TITLE = "YesChef — Alertas de Etiquetas (MVP)"
DB_PATH = os.environ.get("APP_DB_PATH", "app.db")

def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON;")
    return con

def init_db() -> None:
    con = _conn()
    cur = con.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS customers (
        kitchen_id INTEGER PRIMARY KEY,
        name TEXT,
        cnpj TEXT,
        is_active INTEGER NOT NULL DEFAULT 1,
        alert_days INTEGER NOT NULL DEFAULT 7,
        avg_window_days INTEGER NOT NULL DEFAULT 90,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS ledger_entries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kitchen_id INTEGER NOT NULL,
        entry_date TEXT NOT NULL,            -- YYYY-MM-DD
        quantity INTEGER NOT NULL,            -- pode ser negativo
        entry_type TEXT NOT NULL,             -- INITIAL | PURCHASE | ADJUST
        note TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        FOREIGN KEY (kitchen_id) REFERENCES customers(kitchen_id)
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS usage_daily (
        kitchen_id INTEGER NOT NULL,
        day TEXT NOT NULL,                    -- YYYY-MM-DD
        labels_used INTEGER NOT NULL,
        imported_at TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (kitchen_id, day),
        FOREIGN KEY (kitchen_id) REFERENCES customers(kitchen_id)
    );
    """)
    con.commit()
    con.close()

def parse_yyyy_mm_dd(s: str) -> str:
    """
    Aceita:
      - YYYY-MM-DD  (ex: 2026-02-26)
      - DD-MM-YYYY  (ex: 26-02-2026)
      - DD/MM/YYYY  (ex: 26/02/2026)
    Retorna sempre YYYY-MM-DD.
    """
    raw = (s or "").strip()
    raw2 = raw.replace("/", "-")
    for fmt in ("%Y-%m-%d", "%d-%m-%Y"):
        try:
            d = datetime.strptime(raw2, fmt).date()
            return d.isoformat()
        except Exception:
            pass
    raise ValueError(f"Data inválida: {raw}. Use 2026-02-26 ou 26-02-2026.")

def upsert_usage_from_csv(con: sqlite3.Connection, fileobj) -> Tuple[int,int,int]:
    """
    Espera colunas mínimas:
      - kitchen_id
      - day (YYYY-MM-DD)
      - labels_used

    Colunas opcionais (se vierem no CSV, ajudam no cadastro automático):
      - kitchen_name (ou name)
      - cnpj
      - is_active

    Comportamento (MVP v3):
      - Se a kitchen não existir em `customers`, ela é criada automaticamente.
      - Sempre faz UPSERT em usage_daily.
    Retorna (upsert_usage, skipped_rows, new_customers_created)
    """
    reader = csv.DictReader((line.decode("utf-8-sig") for line in fileobj))
    required = {"kitchen_id","day","labels_used"}
    headers = set([h.strip() for h in (reader.fieldnames or [])])

    if not required.issubset(headers):
        raise ValueError(f"CSV precisa ter colunas: {', '.join(sorted(required))}")

    def _get(row: Dict[str, Any], *keys: str) -> Optional[str]:
        for k in keys:
            if k in row and row[k] is not None and str(row[k]).strip() != "":
                return str(row[k]).strip()
        return None

    n_upsert = 0
    n_skip = 0
    n_new = 0

    for row in reader:
        try:
            kitchen_id = int(str(row["kitchen_id"]).strip())
            day = parse_yyyy_mm_dd(str(row["day"]))
            labels_used = int(float(str(row["labels_used"]).strip()))
        except Exception:
            n_skip += 1
            continue

        exists = con.execute("SELECT 1 FROM customers WHERE kitchen_id = ?", (kitchen_id,)).fetchone()
        if not exists:
            name = _get(row, "kitchen_name", "name", "company_name")
            cnpj = _get(row, "cnpj")
            is_active_raw = _get(row, "is_active")
            is_active = 1
            if is_active_raw is not None:
                try:
                    is_active = 1 if int(float(is_active_raw)) == 1 else 0
                except Exception:
                    is_active = 1

            con.execute("""
                INSERT INTO customers(kitchen_id, name, cnpj, is_active, alert_days, avg_window_days)
                VALUES(?, ?, ?, ?, 7, 90);
            """, (kitchen_id, name, cnpj, is_active))
            n_new += 1

        con.execute("""
            INSERT INTO usage_daily(kitchen_id, day, labels_used)
            VALUES(?, ?, ?)
            ON CONFLICT(kitchen_id, day) DO UPDATE SET
                labels_used = excluded.labels_used,
                imported_at = datetime('now');
        """, (kitchen_id, day, labels_used))
        n_upsert += 1

    con.commit()
    return n_upsert, n_skip, n_new

def last_initial_date(con: sqlite3.Connection, kitchen_id: int) -> Optional[str]:
    row = con.execute("""
        SELECT entry_date
        FROM ledger_entries
        WHERE kitchen_id = ? AND entry_type = 'INITIAL'
        ORDER BY entry_date DESC, id DESC
        LIMIT 1;
    """, (kitchen_id,)).fetchone()
    return row["entry_date"] if row else None

def compute_alerts(con: sqlite3.Connection) -> List[Dict[str, Any]]:
    customers = con.execute("""
        SELECT kitchen_id, COALESCE(name, '') AS name, COALESCE(cnpj, '') AS cnpj,
               is_active, alert_days, avg_window_days
        FROM customers
        WHERE is_active = 1
        ORDER BY kitchen_id ASC;
    """).fetchall()

    today = date.today().isoformat()
    out: List[Dict[str, Any]] = []

    for c in customers:
        kitchen_id = int(c["kitchen_id"])
        init_date = last_initial_date(con, kitchen_id)

        # regra do Felipe: se não tiver INITIAL, saldo é 0 e já entra em alerta
        has_initial = init_date is not None
        if not has_initial:
            saldo = 0
            avg_daily = 0.0
            reorder_point = 0
            status = "ALERTA"
            out.append({
                "kitchen_id": kitchen_id,
                "name": c["name"],
                "cnpj": c["cnpj"],
                "saldo": saldo,
                "avg_daily": avg_daily,
                "reorder_point": reorder_point,
                "days_left": None,
                "status": status,
                "reason": "Sem INITIAL (estoque inicial)."
            })
            continue

        # soma lançamentos desde o INITIAL (inclusive)
        ledger_total = con.execute("""
            SELECT COALESCE(SUM(quantity), 0) AS total
            FROM ledger_entries
            WHERE kitchen_id = ? AND entry_date >= ?;
        """, (kitchen_id, init_date)).fetchone()["total"]

        # soma uso desde o INITIAL
        used_total = con.execute("""
            SELECT COALESCE(SUM(labels_used), 0) AS total
            FROM usage_daily
            WHERE kitchen_id = ? AND day >= ?;
        """, (kitchen_id, init_date)).fetchone()["total"]

        saldo = int(ledger_total) - int(used_total)

        # média móvel: últimos avg_window_days, mas sem olhar antes do INITIAL
        window_days = max(1, int(c["avg_window_days"]))
        alert_days = max(1, int(c["alert_days"]))

        # início da janela: max(initial_date, today - window_days)
        window_start = (date.today().toordinal() - window_days)
        window_start_date = date.fromordinal(window_start).isoformat()
        effective_start = max(window_start_date, init_date)

        used_window = con.execute("""
            SELECT COALESCE(SUM(labels_used), 0) AS total
            FROM usage_daily
            WHERE kitchen_id = ? AND day >= ? AND day <= ?;
        """, (kitchen_id, effective_start, today)).fetchone()["total"]

        # média simples por dia (como definido no MVP)
        avg_daily = float(used_window) / float(window_days)

        reorder_point = int(round(avg_daily * alert_days))

        if saldo <= 0:
            status = "CRÍTICO"
            reason = "Saldo <= 0."
        elif avg_daily > 0 and saldo <= reorder_point:
            status = "ALERTA"
            reason = f"Saldo <= ponto de reposição ({alert_days} dias)."
        else:
            status = "OK"
            reason = ""

        days_left = None
        if avg_daily > 0:
            days_left = int(saldo // avg_daily) if saldo > 0 else 0

        out.append({
            "kitchen_id": kitchen_id,
            "name": c["name"],
            "cnpj": c["cnpj"],
            "saldo": saldo,
            "avg_daily": avg_daily,
            "reorder_point": reorder_point,
            "days_left": days_left,
            "status": status,
            "reason": reason
        })

    # ordena: CRÍTICO, ALERTA, sem INITIAL (já é ALERTA), OK
    prio = {"CRÍTICO": 0, "ALERTA": 1, "OK": 2}
    out.sort(key=lambda x: (prio.get(x["status"], 9), x["saldo"]))
    return out

app = FastAPI(title=APP_TITLE)
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.on_event("startup")
def _startup():
    init_db()

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    con = _conn()
    alerts = compute_alerts(con)
    customers = con.execute("""
        SELECT kitchen_id, COALESCE(name,'') AS name, COALESCE(cnpj,'') AS cnpj,
               alert_days, avg_window_days, is_active
        FROM customers
        ORDER BY kitchen_id ASC;
    """).fetchall()
    con.close()
    ok = request.query_params.get("ok")
    err = request.query_params.get("err")
    upserted = request.query_params.get("upserted")
    skipped = request.query_params.get("skipped")
    new_customers = request.query_params.get("new")
    return templates.TemplateResponse("index.html", {
        "request": request,
        "title": APP_TITLE,
        "alerts": alerts,
        "customers": customers,
        "ok": ok,
        "err": err,
        "upserted": upserted,
        "skipped": skipped,
        "new_customers": new_customers
    })

@app.post("/customers")
def add_customer(
    kitchen_id: int = Form(...),
    name: str = Form(""),
    cnpj: str = Form(""),
    alert_days: int = Form(7),
    avg_window_days: int = Form(90),
    is_active: int = Form(1),
):
    con = _conn()
    con.execute("""
        INSERT INTO customers(kitchen_id, name, cnpj, is_active, alert_days, avg_window_days)
        VALUES(?, ?, ?, ?, ?, ?)
        ON CONFLICT(kitchen_id) DO UPDATE SET
            name = excluded.name,
            cnpj = excluded.cnpj,
            is_active = excluded.is_active,
            alert_days = excluded.alert_days,
            avg_window_days = excluded.avg_window_days;
    """, (kitchen_id, name.strip() or None, cnpj.strip() or None, int(is_active), int(alert_days), int(avg_window_days)))
    con.commit()
    con.close()
    return RedirectResponse(url="/", status_code=303)

@app.post("/ledger")
def add_ledger(
    kitchen_id: int = Form(...),
    entry_date: str = Form(...),  # YYYY-MM-DD
    quantity: int = Form(...),
    entry_type: str = Form(...),  # INITIAL | PURCHASE | ADJUST
    note: str = Form(""),
):
    entry_type = entry_type.strip().upper()
    if entry_type not in {"INITIAL","PURCHASE","ADJUST"}:
        return RedirectResponse(url="/?err=tipo_invalido", status_code=303)

    try:
        entry_date = parse_yyyy_mm_dd(entry_date)
    except ValueError:
        return RedirectResponse(url='/?err=data_invalida', status_code=303)

    con = _conn()
    # exige customer existir
    exists = con.execute("SELECT 1 FROM customers WHERE kitchen_id = ?", (kitchen_id,)).fetchone()
    if not exists:
        con.close()
        return RedirectResponse(url="/?err=cliente_nao_existe", status_code=303)

    con.execute("""
        INSERT INTO ledger_entries(kitchen_id, entry_date, quantity, entry_type, note)
        VALUES(?, ?, ?, ?, ?);
    """, (kitchen_id, entry_date, int(quantity), entry_type, note.strip() or None))
    con.commit()
    con.close()
    return RedirectResponse(url="/", status_code=303)

@app.post("/upload")
async def upload_csv(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".csv"):
        return RedirectResponse(url="/?err=arquivo_invalido", status_code=303)

    con = _conn()
    data = await file.read()
    try:
        upserted, skipped, new_customers = upsert_usage_from_csv(con, data.splitlines(True))
    except Exception:
        con.close()
        return RedirectResponse(url="/?err=csv_invalido", status_code=303)
    con.close()
    return RedirectResponse(url=f"/?ok=importado&upserted={upserted}&skipped={skipped}&new={new_customers}", status_code=303)
