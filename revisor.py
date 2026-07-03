"""
revisor.py — агент-ревизор бота учёта рабочего времени.

Запуск: python revisor.py
Нужны переменные окружения (те же что у бота):
  GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_DRIVE_REFRESH_TOKEN
  SPREADSHEET_ID (по умолчанию: 1DZ_XQPAGbSn5aCKVqcBBRQ-X4sAItx23v-qJEw1dYbo)
  TZ_OFFSET (по умолчанию: 5)

Выдаёт структурированный список нарушений по 50 критериям.
Критика сгруппирована по блокам: КРИТИЧНО / ОШИБКА / ПРЕДУПРЕЖДЕНИЕ / ИНФО.
"""

import os
import sys
import io
import calendar
from datetime import datetime, timedelta, date

# Windows: форсируем UTF-8 вывод (cp1251 не поддерживает эмодзи)
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# Загружаем .env если есть
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except ImportError:
    pass

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import httplib2
from google_auth_httplib2 import AuthorizedHttp

# ── Подключение к Google Sheets ────────────────────────────────────────────────

CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
REFRESH_TOKEN = os.environ.get("GOOGLE_DRIVE_REFRESH_TOKEN", "")
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "1DZ_XQPAGbSn5aCKVqcBBRQ-X4sAItx23v-qJEw1dYbo")
TZ_OFFSET = int(os.environ.get("TZ_OFFSET", 5))

MONTHS_RU = ["","Январь","Февраль","Март","Апрель","Май","Июнь",
             "Июль","Август","Сентябрь","Октябрь","Ноябрь","Декабрь"]

_DAYS_MAP = {"пн": 0, "вт": 1, "ср": 2, "чт": 3, "пт": 4, "сб": 5, "вс": 6}


def parse_work_days(days_str):
    if not days_str:
        return None
    s = days_str.strip().lower()
    if "-" in s and "," not in s:
        parts = s.split("-", 1)
        a, b = _DAYS_MAP.get(parts[0].strip()), _DAYS_MAP.get(parts[1].strip())
        if a is not None and b is not None:
            return frozenset(range(a, b + 1))
    result = set()
    for tok in s.split(","):
        d = _DAYS_MAP.get(tok.strip())
        if d is not None:
            result.add(d)
    return frozenset(result) if result else None

_svc_instance = None

def _svc():
    global _svc_instance
    if _svc_instance is None:
        creds = Credentials(
            token=None,
            refresh_token=REFRESH_TOKEN,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
        )
        creds.refresh(Request())
        authed_http = AuthorizedHttp(creds, http=httplib2.Http(timeout=30))
        _svc_instance = build("sheets", "v4", http=authed_http, static_discovery=True)
    return _svc_instance


def _read(sheet, range_):
    res = _svc().spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{sheet}!{range_}"
    ).execute()
    return res.get("values", [])


def _get_all_sheet_titles():
    meta = _svc().spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    return {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta["sheets"]}


def _norm_date(s):
    try:
        parts = str(s).strip().split(".")
        if len(parts) == 3:
            return f"{int(parts[0]):02d}.{int(parts[1]):02d}.{parts[2].strip()}"
    except Exception:
        pass
    return str(s).strip()


def _parse_hhmm(s):
    """'8:05' → (8, 5) или None если не парсится."""
    try:
        h, m = str(s).strip().split(":")
        return int(h), int(m)
    except Exception:
        return None


def _hm_to_min(hm):
    if hm is None:
        return None
    return hm[0] * 60 + hm[1]


def _day_cell_worked(c):
    """True если ячейка дня месячного листа означает реальное присутствие.
    После миграции на формулы (02.07.2026) КАЖДАЯ ячейка дня содержит SUMIFS
    и показывает '0:00' даже без смен — непустая строка больше не признак работы."""
    s = str(c).strip()
    if not s:
        return False
    if s in ("0", "0:00", "00:00", "0,00", "0.00"):
        return False
    return True


# ── Форматирование вывода ──────────────────────────────────────────────────────

LEVELS = {"КРИТИЧНО": 0, "ОШИБКА": 1, "ПРЕДУПРЕЖДЕНИЕ": 2, "ИНФО": 3}
_findings = []


def add(level, criterion_num, title, detail):
    _findings.append({
        "level": level,
        "num": criterion_num,
        "title": title,
        "detail": detail,
    })


def print_report():
    now_local = datetime.utcnow() + timedelta(hours=TZ_OFFSET)
    print(f"\n{'='*70}")
    print(f"  РЕВИЗОР-АГЕНТ — {now_local.strftime('%d.%m.%Y %H:%M')} (UTC+{TZ_OFFSET})")
    print(f"  Таблица: {SPREADSHEET_ID}")
    print(f"{'='*70}\n")

    if not _findings:
        print("  ✅ Нарушений не найдено.\n")
        return

    by_level = {lvl: [] for lvl in LEVELS}
    for f in _findings:
        by_level[f["level"]].append(f)

    icons = {"КРИТИЧНО": "🔴", "ОШИБКА": "🟠", "ПРЕДУПРЕЖДЕНИЕ": "🟡", "ИНФО": "🔵"}

    for lvl in LEVELS:
        items = by_level[lvl]
        if not items:
            continue
        print(f"{icons[lvl]}  {lvl} ({len(items)} шт.)")
        print(f"{'─'*70}")
        for f in items:
            print(f"  [{f['num']:02d}] {f['title']}")
            if f["detail"]:
                for line in f["detail"].split("\n"):
                    print(f"       {line}")
        print()

    totals = {lvl: len(by_level[lvl]) for lvl in LEVELS}
    print(f"{'='*70}")
    print(f"  ИТОГО: "
          f"🔴{totals['КРИТИЧНО']} КРИТИЧНО  "
          f"🟠{totals['ОШИБКА']} ОШИБОК  "
          f"🟡{totals['ПРЕДУПРЕЖДЕНИЕ']} ПРЕДУПРЕЖДЕНИЙ  "
          f"🔵{totals['ИНФО']} ИНФО")
    print(f"{'='*70}\n")


# ── Загрузка данных ────────────────────────────────────────────────────────────

def load_all():
    """Читает все листы за один раз — только один проход."""
    print("  Читаю данные из таблицы...")
    data = {}
    now_local = datetime.utcnow() + timedelta(hours=TZ_OFFSET)
    today_str = now_local.strftime("%d.%m.%Y")

    data["now"] = now_local
    data["today_str"] = today_str

    all_sheets = _get_all_sheet_titles()
    data["all_sheets"] = all_sheets
    print(f"  Листов найдено: {list(all_sheets.keys())}")

    # Журнал
    data["journal_header"] = _read("Журнал", "A1:J1")
    data["journal"] = _read("Журнал", "A2:J500")

    # Сотрудники
    try:
        data["employees_header"] = _read("Сотрудники", "A1:H1")
    except Exception:
        data["employees_header"] = []
    data["employees"] = _read("Сотрудники", "A2:H200")

    # Дашборд (редизайн 02.07.2026: контент с колонки B, имена в merged B:C,
    # поэтому сырые строки имеют пустые колонки-прокладки — нормализуем к старой форме)
    try:
        raw_b1 = _read("Дашборд", "B14:G15")    # B=имя, D=объект/тип, F=приход, G=статус
        data["dash_block1"] = [
            [r[0] if len(r) > 0 else "", r[2] if len(r) > 2 else "", r[4] if len(r) > 4 else ""]
            for r in raw_b1 if r
        ]  # → [имя, локация, приход]
        raw_m = _read("Дашборд", "B18:G22")     # B=имя, D=часы, E=дней, F=авто, G=%
        data["dash_monthly"] = [
            [r[0] if len(r) > 0 else "", r[2] if len(r) > 2 else "", r[3] if len(r) > 3 else "",
             r[4] if len(r) > 4 else "", r[5] if len(r) > 5 else ""]
            for r in raw_m if r
        ]  # → [имя, итого_ч, дней, авто, %]
        raw_n = _read("Дашборд", "B26:G28")     # B=имя, D=время, E=тип, F=план, G=статус
        data["dash_notif"] = [
            [r[2] if len(r) > 2 else "", r[0] if len(r) > 0 else "", r[3] if len(r) > 3 else "",
             r[4] if len(r) > 4 else "", r[5] if len(r) > 5 else ""]
            for r in raw_n if r
        ]  # → [время, имя, тип, план, статус] (старая форма для критериев 41-45)
    except Exception as ex:
        print(f"  ⚠️  Дашборд: {ex}")
        data["dash_block1"] = data["dash_monthly"] = data["dash_notif"] = []

    # Уведомления
    try:
        data["notifications"] = _read("Уведомления", "A2:G3000")
    except Exception:
        data["notifications"] = []

    # Месячные листы текущего и прошлого месяца
    monthly_sheets = {}
    for offset_months in [0, -1]:
        dt = now_local
        if offset_months == -1:
            dt = (now_local.replace(day=1) - timedelta(days=1))
        sheet_name = f"{MONTHS_RU[dt.month]} {dt.year}"
        if sheet_name in all_sheets:
            try:
                monthly_sheets[sheet_name] = {
                    "dt": dt,
                    "header": _read(sheet_name, "A1:AH1"),
                    "rows": _read(sheet_name, "A2:AH100"),
                }
            except Exception as ex:
                print(f"  ⚠️  {sheet_name}: {ex}")
    data["monthly_sheets"] = monthly_sheets

    print("  Данные загружены.\n")
    return data


# ── 30 КРИТЕРИЕВ ──────────────────────────────────────────────────────────────

def run_checks(d):
    now       = d["now"]
    today_str = d["today_str"]
    journal   = d["journal"]
    employees = d["employees"]

    # Активные сотрудники
    active_names = set()
    active_ids   = {}  # name → tg_id
    employees_by_id = {}  # tg_id → row
    name_to_emp_row = {}
    for row in employees:
        if len(row) < 2:
            continue
        tg_id = str(row[0]).strip()
        name  = row[1].strip() if len(row) > 1 else ""
        active_col = row[4].strip().lower() if len(row) >= 5 else (row[3].strip().lower() if len(row) >= 4 else "")
        if active_col == "да":
            active_names.add(name)
            active_ids[name] = tg_id
        if tg_id:
            employees_by_id[tg_id] = row
        name_to_emp_row[name] = row

    # Журнал: предобработка
    journal_entries = []  # только строки похожие на дату
    journal_garbage = []  # строки-призраки
    for i, row in enumerate(journal):
        if not row:
            continue
        date_val = str(row[0]).strip()
        if date_val.count(".") == 2:
            journal_entries.append({"raw_row": i + 2, "row": row, "date": _norm_date(date_val)})
        else:
            journal_garbage.append({"raw_row": i + 2, "row": row})

    open_entries_today = [e for e in journal_entries
                          if e["date"] == today_str and (len(e["row"]) < 6 or not str(e["row"][5]).strip())]
    open_names_today   = {e["row"][1].strip() for e in open_entries_today if len(e["row"]) > 1}

    # ────────────────────────────────────────────────────────────────────────────
    # 1. Журнал: открытые смены НЕ сегодня (забытые записи)
    # ────────────────────────────────────────────────────────────────────────────
    forgotten = [e for e in journal_entries
                 if e["date"] != today_str and (len(e["row"]) < 6 or not str(e["row"][5]).strip())]
    if forgotten:
        detail_lines = [f"строка {e['raw_row']}: {e['row'][1] if len(e['row']) > 1 else '?'} ({e['date']})"
                        for e in forgotten[:10]]
        add("КРИТИЧНО", 1, "Открытые смены прошлых дней (забытые записи)",
            "\n".join(detail_lines) + (f"\n... и ещё {len(forgotten) - 10}" if len(forgotten) > 10 else ""))

    # ────────────────────────────────────────────────────────────────────────────
    # 2. Журнал: закрытая смена с нулевыми или пустыми часами
    # ────────────────────────────────────────────────────────────────────────────
    zero_hours = []
    for e in journal_entries:
        row = e["row"]
        has_departure = len(row) >= 6 and str(row[5]).strip()
        hours_val = str(row[6]).strip() if len(row) >= 7 else ""
        if has_departure and (not hours_val or hours_val in ("0", "0:00", "0:0")):
            zero_hours.append(e)
    if zero_hours:
        lines = [f"строка {e['raw_row']}: {e['row'][1] if len(e['row']) > 1 else '?'} {e['date']}"
                 f" приход={e['row'][4] if len(e['row']) > 4 else '?'}"
                 f" уход={e['row'][5] if len(e['row']) > 5 else '?'}"
                 for e in zero_hours[:8]]
        add("ОШИБКА", 2, "Закрытые смены с нулевыми/пустыми часами", "\n".join(lines))

    # ────────────────────────────────────────────────────────────────────────────
    # 3. Журнал: дубликаты — 2+ открытых записи одного сотрудника в один день
    # ────────────────────────────────────────────────────────────────────────────
    from collections import defaultdict
    open_by_date_name = defaultdict(list)
    for e in journal_entries:
        if len(e["row"]) < 2:
            continue
        if len(e["row"]) < 6 or not str(e["row"][5]).strip():
            key = (e["date"], e["row"][1].strip())
            open_by_date_name[key].append(e["raw_row"])
    duplicates = {k: v for k, v in open_by_date_name.items() if len(v) > 1}
    if duplicates:
        lines = [f"{name} {dt}: строки {rows}" for (dt, name), rows in list(duplicates.items())[:8]]
        add("КРИТИЧНО", 3, "Дубликаты — несколько открытых записей одного сотрудника в один день",
            "\n".join(lines))

    # ────────────────────────────────────────────────────────────────────────────
    # 4. Журнал: строки-призраки (A не является датой)
    # ────────────────────────────────────────────────────────────────────────────
    if journal_garbage:
        lines = [f"строка {e['raw_row']}: A={str(e['row'][0])[:30]}" for e in journal_garbage[:8]]
        add("ПРЕДУПРЕЖДЕНИЕ", 4, "Журнал: строки с нечитаемой датой в колонке A",
            "\n".join(lines))

    # ────────────────────────────────────────────────────────────────────────────
    # 5. Журнал: приход > уход (хронологическая ошибка) — для закрытых смен
    # ────────────────────────────────────────────────────────────────────────────
    time_errors = []
    for e in journal_entries:
        row = e["row"]
        if len(row) < 6 or not str(row[5]).strip():
            continue
        arr = _hm_to_min(_parse_hhmm(row[4])) if len(row) > 4 else None
        dep = _hm_to_min(_parse_hhmm(row[5])) if len(row) > 5 else None
        if arr is not None and dep is not None and dep < arr and (arr - dep) > 60:
            # > 1 часа разницы — не переход через полночь
            time_errors.append(e)
    if time_errors:
        lines = [f"строка {e['raw_row']}: {e['row'][1] if len(e['row']) > 1 else '?'} "
                 f"приход={e['row'][4] if len(e['row']) > 4 else '?'} "
                 f"уход={e['row'][5] if len(e['row']) > 5 else '?'}"
                 for e in time_errors[:8]]
        add("ОШИБКА", 5, "Журнал: уход раньше прихода (> 1 ч разницы)", "\n".join(lines))

    # ────────────────────────────────────────────────────────────────────────────
    # 6. Журнал: закрытая смена без статуса в H
    # ────────────────────────────────────────────────────────────────────────────
    no_status = []
    valid_statuses = {"✅", "⚠️ авто", "⏳ продлено до 23:55", "⏳ продлено"}
    for e in journal_entries:
        row = e["row"]
        has_departure = len(row) >= 6 and str(row[5]).strip()
        status = str(row[7]).strip() if len(row) >= 8 else ""
        if has_departure and not status:
            no_status.append(e)
    if no_status:
        lines = [f"строка {e['raw_row']}: {e['row'][1] if len(e['row']) > 1 else '?'} {e['date']}"
                 for e in no_status[:8]]
        add("ПРЕДУПРЕЖДЕНИЕ", 6, "Журнал: закрытые смены без статуса в колонке H", "\n".join(lines))

    # ────────────────────────────────────────────────────────────────────────────
    # 7. Журнал: G (часы) не совпадает с расчётом F-E
    # ────────────────────────────────────────────────────────────────────────────
    hours_mismatch = []
    for e in journal_entries:
        row = e["row"]
        if len(row) < 7 or not str(row[5]).strip():
            continue
        arr = _hm_to_min(_parse_hhmm(row[4])) if len(row) > 4 else None
        dep = _hm_to_min(_parse_hhmm(row[5])) if len(row) > 5 else None
        logged = _parse_hhmm(row[6]) if len(row) > 6 and str(row[6]).strip() else None
        if arr is None or dep is None or logged is None:
            continue
        diff = dep - arr
        if diff < 0:
            diff += 24 * 60  # переход через полночь
        logged_min = _hm_to_min(logged)
        if abs(diff - logged_min) > 2:  # допуск 2 минуты
            hours_mismatch.append((e, diff, logged_min))
    if hours_mismatch:
        lines = [f"строка {e['raw_row']}: {e['row'][1] if len(e['row']) > 1 else '?'} "
                 f"расчёт={diff//60}:{diff%60:02d} факт={lm//60}:{lm%60:02d}"
                 for e, diff, lm in hours_mismatch[:8]]
        add("ПРЕДУПРЕЖДЕНИЕ", 7, "Журнал: расчётные часы (F-E) не совпадают с G", "\n".join(lines))

    # ────────────────────────────────────────────────────────────────────────────
    # 8. Журнал: TG ID в J не найден в Сотрудники
    # ────────────────────────────────────────────────────────────────────────────
    ghost_ids = []
    for e in journal_entries:
        row = e["row"]
        tg_id = str(row[9]).strip() if len(row) >= 10 else ""
        if tg_id and tg_id not in employees_by_id:
            ghost_ids.append((e, tg_id))
    if ghost_ids:
        lines = [f"строка {e['raw_row']}: {e['row'][1] if len(e['row']) > 1 else '?'} TG_ID={tid}"
                 for e, tid in ghost_ids[:8]]
        add("ПРЕДУПРЕЖДЕНИЕ", 8, "Журнал: TG ID в колонке J отсутствует в Сотрудники", "\n".join(lines))

    # ────────────────────────────────────────────────────────────────────────────
    # 9. Сотрудники: локация (D) непустая, но нет открытой смены сегодня
    # ────────────────────────────────────────────────────────────────────────────
    location_ghosts = []
    for row in employees:
        if len(row) < 2:
            continue
        name = row[1].strip()
        location = str(row[3]).strip() if len(row) >= 4 else ""
        active = row[4].strip().lower() if len(row) >= 5 else ""
        if active == "да" and location and location not in ("—", "-"):
            if name not in open_names_today:
                location_ghosts.append((name, location))
    if location_ghosts:
        lines = [f"{name}: локация={loc}" for name, loc in location_ghosts[:10]]
        add("ОШИБКА", 9, "Сотрудники: локация непустая, но открытой смены сегодня нет",
            "\n".join(lines))

    # ────────────────────────────────────────────────────────────────────────────
    # 10. Сотрудники: дублирующиеся TG ID
    # ────────────────────────────────────────────────────────────────────────────
    id_count = defaultdict(list)
    for row in employees:
        tg_id = str(row[0]).strip() if row else ""
        if tg_id:
            id_count[tg_id].append(row[1].strip() if len(row) > 1 else "?")
    dup_ids = {k: v for k, v in id_count.items() if len(v) > 1}
    if dup_ids:
        lines = [f"TG_ID {tid}: {names}" for tid, names in list(dup_ids.items())[:8]]
        add("КРИТИЧНО", 10, "Сотрудники: дублирующиеся TG ID", "\n".join(lines))

    # ────────────────────────────────────────────────────────────────────────────
    # 11. Сотрудники: расписание (F) задано, но рабочие дни (H) пустые
    # ────────────────────────────────────────────────────────────────────────────
    incomplete_schedule = []
    for row in employees:
        if len(row) < 5:
            continue
        active = row[4].strip().lower() if len(row) >= 5 else ""
        if active != "да":
            continue
        schedule   = row[5].strip() if len(row) >= 6 else ""
        work_days  = row[7].strip() if len(row) >= 8 else ""
        if schedule and not work_days:
            name = row[1].strip() if len(row) > 1 else "?"
            incomplete_schedule.append((name, schedule))
    if incomplete_schedule:
        lines = [f"{name}: время={sched}, дни=пусто" for name, sched in incomplete_schedule]
        add("ПРЕДУПРЕЖДЕНИЕ", 11,
            "Сотрудники: расписание задано, рабочие дни (H) пустые → уведомления идут каждый день",
            "\n".join(lines))

    # ────────────────────────────────────────────────────────────────────────────
    # 12. Журнал: имена сотрудников которых нет в Сотрудники (призраки)
    # ────────────────────────────────────────────────────────────────────────────
    all_emp_names = {row[1].strip() for row in employees if len(row) > 1 and row[1].strip()}
    journal_names = {e["row"][1].strip() for e in journal_entries if len(e["row"]) > 1}
    ghost_names = journal_names - all_emp_names
    if ghost_names:
        add("ПРЕДУПРЕЖДЕНИЕ", 12,
            "Журнал: имена сотрудников отсутствующих в листе Сотрудники",
            "\n".join(sorted(ghost_names)[:10]))

    # ────────────────────────────────────────────────────────────────────────────
    # 13. Дашборд блок 1: имена не совпадают с открытыми сегодня в Журнале
    # ────────────────────────────────────────────────────────────────────────────
    dash_block1 = d.get("dash_block1", [])
    dash_names = set()
    for row in dash_block1:
        name = str(row[0]).strip() if row else ""
        if name and "никто не на работе" not in name.lower() and name != "—":
            dash_names.add(name)

    extra_in_dash = dash_names - open_names_today
    missing_in_dash = open_names_today - dash_names

    # Макет 02.07.2026: блок "кто на работе" физически вмещает 2 строки (ARRAY_CONSTRAIN).
    # Если открытых смен больше — обрезка ожидаема, пропуски не считаются ошибкой,
    # пока дашборд показывает полные 2 строки и все показанные — валидны.
    DASH_BLOCK1_CAP = 2
    expected_truncation = (len(open_names_today) > DASH_BLOCK1_CAP
                           and len(dash_names) >= DASH_BLOCK1_CAP)

    if extra_in_dash:
        add("КРИТИЧНО", 13,
            "Дашборд блок 1: показывает людей которых нет в открытых сменах сегодня",
            f"Лишние: {sorted(extra_in_dash)}\nОткрытые сегодня по Журналу: {sorted(open_names_today)}")
    if missing_in_dash and not expected_truncation:
        add("КРИТИЧНО", 13,
            "Дашборд блок 1: не показывает людей с открытой сменой сегодня",
            f"Пропущены: {sorted(missing_in_dash)}\nДашборд показывает: {sorted(dash_names)}")

    # ────────────────────────────────────────────────────────────────────────────
    # 14. Дашборд блок 1 пустой ("никто") когда в Журнале есть открытые смены
    # ────────────────────────────────────────────────────────────────────────────
    dash_says_empty = any("никто" in str(r[0]).lower() for r in dash_block1 if r)
    if dash_says_empty and open_names_today:
        add("КРИТИЧНО", 14,
            "Дашборд говорит 'никто не на работе' но в Журнале есть открытые смены сегодня",
            f"Открытые смены: {sorted(open_names_today)}")

    # ────────────────────────────────────────────────────────────────────────────
    # 15. Дашборд блок 1 показывает людей когда открытых смен нет
    # ────────────────────────────────────────────────────────────────────────────
    if dash_names and not open_names_today:
        add("КРИТИЧНО", 15,
            "Дашборд показывает людей на работе, но в Журнале нет открытых смен сегодня",
            f"Дашборд: {sorted(dash_names)}")

    # ────────────────────────────────────────────────────────────────────────────
    # 16. Дашборд месячная сводка: строк меньше числа активных сотрудников
    # ────────────────────────────────────────────────────────────────────────────
    dash_monthly = d.get("dash_monthly", [])
    monthly_names_in_dash = [str(r[0]).strip() for r in dash_monthly if r and str(r[0]).strip()]
    if len(monthly_names_in_dash) < len(active_names):
        missing = active_names - set(monthly_names_in_dash)
        add("ОШИБКА", 16,
            "Дашборд месячная сводка: не все активные сотрудники присутствуют",
            f"Активных: {len(active_names)}, в дашборде: {len(monthly_names_in_dash)}\n"
            f"Отсутствуют: {sorted(missing)}")

    # ────────────────────────────────────────────────────────────────────────────
    # 16b. Дашборд месячная сводка: значения не совпадают с месячным листом
    # ────────────────────────────────────────────────────────────────────────────
    current_sheet_name = f"{MONTHS_RU[now.month]} {now.year}"
    current_month_data = d.get("monthly_sheets", {}).get(current_sheet_name)
    if current_month_data:
        days_in_month = calendar.monthrange(now.year, now.month)[1]
        month_rows = current_month_data["rows"]
        name_to_month_row = {str(r[0]).strip(): r for r in month_rows if r and str(r[0]).strip()}

        dash_value_errors = []
        for dash_row in dash_monthly:
            if not dash_row:
                continue
            name = str(dash_row[0]).strip()
            if not name:
                continue
            dash_total = str(dash_row[1]).strip() if len(dash_row) > 1 else ""
            dash_days  = str(dash_row[2]).strip() if len(dash_row) > 2 else ""

            month_row = name_to_month_row.get(name, [])
            # Итого: индекс days_in_month+1 (после A + 31 дней)
            real_total = str(month_row[days_in_month + 1]).strip() \
                if len(month_row) > days_in_month + 1 else ""
            real_days = sum(1 for c in month_row[1:days_in_month + 1] if _day_cell_worked(c))

            # Сравниваем итого (приводим запятую→точка)
            try:
                dt_num = float(dash_total.replace(",", ".")) if dash_total else 0.0
                rt_num = float(real_total.replace(",", ".")) if real_total else 0.0
                total_mismatch = abs(dt_num - rt_num) > 0.05
            except Exception:
                total_mismatch = dash_total != real_total

            try:
                dd_num = int(dash_days) if dash_days else 0
                days_mismatch = dd_num != real_days
            except Exception:
                days_mismatch = False

            if total_mismatch or days_mismatch:
                parts = []
                if total_mismatch:
                    parts.append(f"итого: дашборд={dash_total or '(пусто)'}, лист={real_total or '(пусто)'}")
                if days_mismatch:
                    parts.append(f"дней: дашборд={dash_days}, лист={real_days}")
                dash_value_errors.append(f"{name}: {'; '.join(parts)}")

        if dash_value_errors:
            add("ОШИБКА", 16,
                "Дашборд месячная сводка: значения расходятся с месячным листом",
                "\n".join(dash_value_errors))

    # ────────────────────────────────────────────────────────────────────────────
    # 17. Дашборд реестр уведомлений пустой, но в листе Уведомления есть записи за сегодня
    # ────────────────────────────────────────────────────────────────────────────
    notifications = d.get("notifications", [])
    today_notifs = [r for r in notifications if r and _norm_date(r[0]) == today_str]
    dash_notif = d.get("dash_notif", [])
    dash_notif_empty = (not dash_notif or
                        all("нет" in str(r[0]).lower() or str(r[0]).strip() in ("—", "")
                            for r in dash_notif if r))
    if dash_notif_empty and today_notifs:
        add("ОШИБКА", 17,
            "Дашборд реестр уведомлений пустой, но в листе Уведомления есть записи за сегодня",
            f"Записей в Уведомления за сегодня: {len(today_notifs)}")

    # ────────────────────────────────────────────────────────────────────────────
    # 18. Месячный лист vs Журнал: часы не совпадают
    # ────────────────────────────────────────────────────────────────────────────
    for sheet_name, mdata in d.get("monthly_sheets", {}).items():
        dt = mdata["dt"]
        month_prefix = f".{dt.month:02d}.{dt.year}"
        rows = mdata["rows"]
        days_in_month = calendar.monthrange(dt.year, dt.month)[1]

        # Журнал → суммы по (имя, день) для этого месяца
        journal_sums = defaultdict(float)
        for e in journal_entries:
            if not e["date"].endswith(month_prefix):
                continue
            row = e["row"]
            if len(row) < 7 or not str(row[5]).strip():
                continue
            try:
                day_num = int(e["date"].split(".")[0])
                h, m = str(row[6]).strip().split(":")
                journal_sums[(row[1].strip(), day_num)] += int(h) + int(m) / 60
            except Exception:
                pass

        mismatches = []
        for sheet_row in rows:
            if not sheet_row:
                continue
            name = str(sheet_row[0]).strip()
            if not name:
                continue
            for day_idx in range(1, days_in_month + 1):
                cell_val = sheet_row[day_idx] if len(sheet_row) > day_idx else ""
                cell_str = str(cell_val).strip()
                if not cell_str:
                    continue
                try:
                    cell_num = float(cell_str.replace(",", "."))
                except Exception:
                    continue
                journal_val = round(journal_sums.get((name, day_idx), 0.0), 2)
                if journal_val == 0.0 and cell_num == 0.0:
                    continue
                if abs(cell_num - journal_val) > 0.05:
                    mismatches.append(
                        f"{name} {day_idx:02d}.{dt.month:02d}: в листе={cell_num}, в Журнале={round(journal_val,2)}"
                    )
        if mismatches:
            add("ОШИБКА", 18,
                f"{sheet_name}: часы в ячейках не совпадают с суммой по Журналу",
                "\n".join(mismatches[:10]) + (f"\n...и ещё {len(mismatches)-10}" if len(mismatches) > 10 else ""))

    # ────────────────────────────────────────────────────────────────────────────
    # 19. Месячный лист: активный сотрудник отсутствует в строках
    # ────────────────────────────────────────────────────────────────────────────
    for sheet_name, mdata in d.get("monthly_sheets", {}).items():
        dt = mdata["dt"]
        # Проверяем только текущий месяц
        if dt.month != now.month or dt.year != now.year:
            continue
        rows = mdata["rows"]
        names_in_sheet = {str(r[0]).strip() for r in rows if r and str(r[0]).strip()}
        missing = active_names - names_in_sheet
        if missing:
            add("ОШИБКА", 19,
                f"{sheet_name}: активные сотрудники отсутствуют в строках табеля",
                "\n".join(sorted(missing)))

    # ────────────────────────────────────────────────────────────────────────────
    # 20. Месячный лист: строки для уволенных / несуществующих сотрудников
    # ────────────────────────────────────────────────────────────────────────────
    for sheet_name, mdata in d.get("monthly_sheets", {}).items():
        rows = mdata["rows"]
        stale = []
        for r in rows:
            if not r:
                continue
            name = str(r[0]).strip()
            if name and name not in all_emp_names:
                stale.append(name)
        if stale:
            add("ИНФО", 20,
                f"{sheet_name}: строки для сотрудников не найденных в Сотрудники",
                "\n".join(stale[:10]))

    # ────────────────────────────────────────────────────────────────────────────
    # 21. Месячный лист: Итого ≠ сумма ячеек дней
    # ────────────────────────────────────────────────────────────────────────────
    for sheet_name, mdata in d.get("monthly_sheets", {}).items():
        dt = mdata["dt"]
        rows = mdata["rows"]
        days_in_month = calendar.monthrange(dt.year, dt.month)[1]
        total_errors = []
        for r in rows:
            if not r:
                continue
            name = str(r[0]).strip()
            if not name:
                continue
            total_cell = str(r[days_in_month + 1]).strip() if len(r) > days_in_month + 1 else ""
            if not total_cell:
                continue
            try:
                total_val = float(total_cell.replace(",", "."))
            except Exception:
                continue
            day_sum = 0.0
            for day_idx in range(1, days_in_month + 1):
                cell = r[day_idx] if len(r) > day_idx else ""
                try:
                    day_sum += float(str(cell).strip().replace(",", "."))
                except Exception:
                    pass
            if abs(total_val - day_sum) > 0.05:
                total_errors.append(f"{name}: Итого={total_val}, сумма={round(day_sum,2)}")
        if total_errors:
            add("ОШИБКА", 21,
                f"{sheet_name}: колонка Итого не совпадает с суммой дней",
                "\n".join(total_errors[:8]))

    # ────────────────────────────────────────────────────────────────────────────
    # 22. Месячный лист: явный 0 для дня, где в Журнале есть часы > 0
    # ────────────────────────────────────────────────────────────────────────────
    for sheet_name, mdata in d.get("monthly_sheets", {}).items():
        dt = mdata["dt"]
        rows = mdata["rows"]
        days_in_month = calendar.monthrange(dt.year, dt.month)[1]
        month_prefix = f".{dt.month:02d}.{dt.year}"
        journal_has_hours = defaultdict(float)
        for e in journal_entries:
            if not e["date"].endswith(month_prefix):
                continue
            row = e["row"]
            if len(row) < 7 or not str(row[5]).strip():
                continue
            try:
                day_num = int(e["date"].split(".")[0])
                h, m = str(row[6]).strip().split(":")
                journal_has_hours[(row[1].strip(), day_num)] += int(h) + int(m) / 60
            except Exception:
                pass
        zero_cells = []
        for r in rows:
            if not r:
                continue
            name = str(r[0]).strip()
            for day_idx in range(1, days_in_month + 1):
                cell = str(r[day_idx]).strip() if len(r) > day_idx else ""
                try:
                    cell_num = float(cell.replace(",", "."))
                except Exception:
                    cell_num = -1
                if cell_num == 0.0 and journal_has_hours.get((name, day_idx), 0) > 0:
                    zero_cells.append(
                        f"{name} {day_idx:02d}.{dt.month:02d}: лист=0, Журнал={round(journal_has_hours[(name,day_idx)],2)}"
                    )
        if zero_cells:
            add("ОШИБКА", 22,
                f"{sheet_name}: ячейки содержат 0, хотя в Журнале есть часы за этот день",
                "\n".join(zero_cells[:8]))

    # ────────────────────────────────────────────────────────────────────────────
    # 23. Месячный лист: число > 0 в ячейке, но в Журнале нет записей за этот день
    # ────────────────────────────────────────────────────────────────────────────
    for sheet_name, mdata in d.get("monthly_sheets", {}).items():
        dt = mdata["dt"]
        rows = mdata["rows"]
        days_in_month = calendar.monthrange(dt.year, dt.month)[1]
        month_prefix = f".{dt.month:02d}.{dt.year}"
        journal_days_with_hours = defaultdict(set)
        for e in journal_entries:
            if not e["date"].endswith(month_prefix):
                continue
            row = e["row"]
            if len(row) < 7 or not str(row[5]).strip():
                continue
            try:
                day_num = int(e["date"].split(".")[0])
                h, m = str(row[6]).strip().split(":")
                if int(h) * 60 + int(m) > 0:
                    journal_days_with_hours[row[1].strip()].add(day_num)
            except Exception:
                pass
        phantom_cells = []
        for r in rows:
            if not r:
                continue
            name = str(r[0]).strip()
            for day_idx in range(1, days_in_month + 1):
                cell = str(r[day_idx]).strip() if len(r) > day_idx else ""
                try:
                    cell_num = float(cell.replace(",", "."))
                except Exception:
                    continue
                if cell_num > 0 and day_idx not in journal_days_with_hours.get(name, set()):
                    if date(dt.year, dt.month, day_idx) > now.date():
                        continue  # будущие дни пропускаем
                    phantom_cells.append(
                        f"{name} {day_idx:02d}.{dt.month:02d}: лист={cell_num}, в Журнале нет"
                    )
        if phantom_cells:
            add("ПРЕДУПРЕЖДЕНИЕ", 23,
                f"{sheet_name}: ячейки с числом > 0 без записей в Журнале за этот день",
                "\n".join(phantom_cells[:8]))

    # ────────────────────────────────────────────────────────────────────────────
    # 24. Разные месячные листы: активный сотрудник есть в одном, нет в другом
    # ────────────────────────────────────────────────────────────────────────────
    monthly_name_sets = {}
    for sheet_name, mdata in d.get("monthly_sheets", {}).items():
        monthly_name_sets[sheet_name] = {
            str(r[0]).strip() for r in mdata["rows"] if r and str(r[0]).strip()
        }
    if len(monthly_name_sets) == 2:
        sheet_names = list(monthly_name_sets.keys())
        diff_a = monthly_name_sets[sheet_names[0]] - monthly_name_sets[sheet_names[1]]
        diff_b = monthly_name_sets[sheet_names[1]] - monthly_name_sets[sheet_names[0]]
        if diff_a:
            add("ИНФО", 24,
                f"В «{sheet_names[0]}» есть строки которых нет в «{sheet_names[1]}»",
                "\n".join(sorted(diff_a)))
        if diff_b:
            add("ИНФО", 24,
                f"В «{sheet_names[1]}» есть строки которых нет в «{sheet_names[0]}»",
                "\n".join(sorted(diff_b)))

    # ────────────────────────────────────────────────────────────────────────────
    # 25. Уведомления: пропущенные плановые (расписание задано, отправки нет, время прошло)
    # ────────────────────────────────────────────────────────────────────────────
    sent_today_set = set()
    for r in today_notifs:
        if len(r) >= 4:
            sent_today_set.add((r[2].strip(), r[3].strip()))

    missed_notifs = []
    for row in employees:
        if len(row) < 6:
            continue
        active = row[4].strip().lower() if len(row) >= 5 else ""
        if active != "да":
            continue
        name     = row[1].strip() if len(row) > 1 else ""
        schedule = row[5].strip() if len(row) >= 6 else ""
        end_time = row[6].strip() if len(row) >= 7 else ""
        work_days_str = row[7].strip() if len(row) >= 8 else ""

        if not schedule:
            continue

        # Рабочие дни
        work_days = parse_work_days(work_days_str) if work_days_str else None
        if work_days and now.weekday() not in work_days:
            continue

        try:
            t = datetime.strptime(schedule, "%H:%M")
            planned = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
            planned_notif_at = planned - timedelta(minutes=10)
            if now > planned_notif_at + timedelta(minutes=130):
                if (name, "до начала смены") not in sent_today_set:
                    missed_notifs.append(f"{name}: до начала смены (план {planned_notif_at.strftime('%H:%M')})")
        except Exception:
            pass

        if end_time:
            try:
                t2 = datetime.strptime(end_time, "%H:%M")
                planned_end = now.replace(hour=t2.hour, minute=t2.minute, second=0, microsecond=0)
                end_notif_at = planned_end - timedelta(minutes=30)
                if now > end_notif_at + timedelta(minutes=45):
                    if (name, "до конца смены") not in sent_today_set:
                        missed_notifs.append(f"{name}: до конца смены (план {end_notif_at.strftime('%H:%M')})")
            except Exception:
                pass

    if missed_notifs:
        add("ОШИБКА", 25, "Уведомления: плановые уведомления не были отправлены сегодня",
            "\n".join(missed_notifs[:10]))

    # ────────────────────────────────────────────────────────────────────────────
    # 26. Уведомления: записи за сегодня без статуса (F пустое)
    # ────────────────────────────────────────────────────────────────────────────
    notif_no_status = [r for r in today_notifs if len(r) < 6 or not str(r[5]).strip()]
    if notif_no_status:
        lines = [f"{r[1] if len(r) > 1 else '?'} {r[2] if len(r) > 2 else '?'}"
                 for r in notif_no_status[:8]]
        add("ПРЕДУПРЕЖДЕНИЕ", 26, "Уведомления: записи за сегодня без статуса в F",
            "\n".join(lines))

    # ────────────────────────────────────────────────────────────────────────────
    # 27. Уведомления: дубликаты — одному сотруднику один тип за один день 2+ раза
    # ────────────────────────────────────────────────────────────────────────────
    notif_count = defaultdict(int)
    for r in today_notifs:
        if len(r) >= 4:
            notif_count[(r[2].strip(), r[3].strip())] += 1
    dup_notifs = {k: v for k, v in notif_count.items() if v > 1}
    if dup_notifs:
        lines = [f"{name} '{ntype}': {cnt} раз" for (name, ntype), cnt in dup_notifs.items()]
        add("ПРЕДУПРЕЖДЕНИЕ", 27, "Уведомления: дубликаты — один тип сотруднику 2+ раза за сегодня",
            "\n".join(lines))

    # ────────────────────────────────────────────────────────────────────────────
    # 28. Журнал: строка 1 не является заголовком (A1 ≠ "Дата")
    # ────────────────────────────────────────────────────────────────────────────
    header = d.get("journal_header", [])
    expected_header = ["Дата", "Имя", "Тип", "Объект", "Приход", "Уход", "Отработано", "Статус"]
    actual_header = header[0] if header else []
    if not actual_header or actual_header[0] != "Дата":
        add("ОШИБКА", 28, "Журнал: строка 1 не является заголовком (A1 ≠ 'Дата')",
            f"Текущее A1: {actual_header[0] if actual_header else 'пусто'}")
    else:
        for idx, col_name in enumerate(expected_header):
            if idx >= len(actual_header) or actual_header[idx] != col_name:
                add("ПРЕДУПРЕЖДЕНИЕ", 28,
                    f"Журнал: колонка {idx+1} в заголовке ожидалась '{col_name}', "
                    f"получено '{actual_header[idx] if idx < len(actual_header) else 'отсутствует'}'",
                    "")
                break

    # ────────────────────────────────────────────────────────────────────────────
    # 29. Месячный лист: заголовок строки 1 не содержит числа 1..31
    # ────────────────────────────────────────────────────────────────────────────
    for sheet_name, mdata in d.get("monthly_sheets", {}).items():
        dt = mdata["dt"]
        days_in_month = calendar.monthrange(dt.year, dt.month)[1]
        hrow = mdata["header"][0] if mdata["header"] else []
        if not hrow:
            add("ОШИБКА", 29, f"{sheet_name}: заголовок строки 1 пустой", "")
            continue
        if hrow[0] != "Сотрудник":
            add("ПРЕДУПРЕЖДЕНИЕ", 29, f"{sheet_name}: A1 ожидался 'Сотрудник', получен '{hrow[0]}'", "")
        expected_last_day = str(days_in_month)
        actual_day_cells = [str(c).strip() for c in hrow[1:days_in_month+1]]
        if actual_day_cells and actual_day_cells[-1] != expected_last_day:
            add("ПРЕДУПРЕЖДЕНИЕ", 29,
                f"{sheet_name}: последний день в заголовке ожидался '{expected_last_day}', "
                f"получен '{actual_day_cells[-1] if actual_day_cells else 'нет'}'", "")

    # ────────────────────────────────────────────────────────────────────────────
    # 30. Сотрудники: колонок меньше 5 (старый формат без поля Активен)
    # ────────────────────────────────────────────────────────────────────────────
    old_format_rows = [row for row in employees if 0 < len(row) < 5]
    if old_format_rows:
        names = [row[1] if len(row) > 1 else row[0] for row in old_format_rows[:5]]
        add("ПРЕДУПРЕЖДЕНИЕ", 30,
            "Сотрудники: строки в старом формате (менее 5 колонок, нет колонки Активен)",
            "\n".join(str(n) for n in names))

    # ════════════════════════════════════════════════════════════════════════════
    # 31-50. ПЕРЕКРЁСТНЫЕ КРИТЕРИИ (источник A == источник B, не просто "есть строка")
    # ════════════════════════════════════════════════════════════════════════════

    open_entry_by_name = {}
    for e in open_entries_today:
        r = e["row"]
        if len(r) > 1 and r[1].strip():
            open_entry_by_name[r[1].strip()] = r

    today_entries = [e for e in journal_entries if e["date"] == today_str]
    cur_month_suffix = f".{now.month:02d}.{now.year}"
    cur_month_entries = [e for e in journal_entries if e["date"].endswith(cur_month_suffix)]

    _type_labels = {"водитель": "Водитель 🚗", "сервис": "Сервис 🔧"}

    # ────────────────────────────────────────────────────────────────────────────
    # 31. Дашборд блок1: время прихода (C) ≠ Журнал!E открытой записи
    # ────────────────────────────────────────────────────────────────────────────
    arrival_mismatches = []
    for r in dash_block1:
        if not r or not str(r[0]).strip():
            continue
        name = str(r[0]).strip()
        jr = open_entry_by_name.get(name)
        if not jr or len(jr) < 5:
            continue
        dash_time = str(r[2]).strip() if len(r) > 2 else ""
        journal_time = str(jr[4]).strip()
        if dash_time != journal_time:
            arrival_mismatches.append(f"{name}: дашборд={dash_time or '(пусто)'}, Журнал!E={journal_time or '(пусто)'}")
    if arrival_mismatches:
        add("ОШИБКА", 31, "Дашборд блок1: время прихода не совпадает с Журнал!E",
            "\n".join(arrival_mismatches))

    # ────────────────────────────────────────────────────────────────────────────
    # 32. Дашборд блок1: локация (B) ≠ Журнал!D (Объект) — только для объектных смен
    # ────────────────────────────────────────────────────────────────────────────
    location_mismatches = []
    for r in dash_block1:
        if not r or not str(r[0]).strip():
            continue
        name = str(r[0]).strip()
        jr = open_entry_by_name.get(name)
        if not jr or len(jr) < 4:
            continue
        journal_object = str(jr[3]).strip() if len(jr) > 3 else ""
        if not journal_object:
            continue  # это водитель/сервис — сверяется в 34, тут не объект
        dash_loc = str(r[1]).strip() if len(r) > 1 else ""
        if dash_loc != journal_object:
            location_mismatches.append(f"{name}: дашборд={dash_loc or '(пусто)'}, Журнал!D={journal_object}")
    if location_mismatches:
        add("ОШИБКА", 32, "Дашборд блок1: локация не совпадает с Журнал!D (Объект)",
            "\n".join(location_mismatches))

    # ────────────────────────────────────────────────────────────────────────────
    # 33. Дублирующиеся имена в блоке 1 дашборда
    # ────────────────────────────────────────────────────────────────────────────
    block1_name_count = defaultdict(int)
    for r in dash_block1:
        if r and str(r[0]).strip():
            block1_name_count[str(r[0]).strip()] += 1
    block1_dups = {n: c for n, c in block1_name_count.items() if c > 1}
    if block1_dups:
        add("КРИТИЧНО", 33, "Дашборд блок1: одно имя встречается несколько раз",
            "\n".join(f"{n}: {c} раз" for n, c in block1_dups.items()))

    # ────────────────────────────────────────────────────────────────────────────
    # 34. Дашборд блок1: тип (водитель/сервис) ≠ Сотрудники!C (тип изменился после отметки)
    # ────────────────────────────────────────────────────────────────────────────
    type_label_mismatches = []
    for r in dash_block1:
        if not r or not str(r[0]).strip():
            continue
        name = str(r[0]).strip()
        jr = open_entry_by_name.get(name)
        if not jr or len(jr) < 4:
            continue
        journal_object = str(jr[3]).strip() if len(jr) > 3 else ""
        if journal_object:
            continue  # объектная смена, локация сверяется в 32
        emp_row = name_to_emp_row.get(name, [])
        cur_type = emp_row[2].strip().lower() if len(emp_row) > 2 else ""
        expected_label = _type_labels.get(cur_type, "—")
        dash_label = str(r[1]).strip() if len(r) > 1 else ""
        if dash_label != expected_label:
            type_label_mismatches.append(
                f"{name}: дашборд={dash_label or '(пусто)'}, ожидалось по Сотрудники!C='{cur_type}' → {expected_label}")
    if type_label_mismatches:
        add("ПРЕДУПРЕЖДЕНИЕ", 34,
            "Дашборд блок1: тип не совпадает с текущим Сотрудники!C (изменился после отметки прихода)",
            "\n".join(type_label_mismatches))

    # ────────────────────────────────────────────────────────────────────────────
    # 35/36. Сводка: Итого / Дней ≠ значениям месячного листа (детализация 16b)
    # ────────────────────────────────────────────────────────────────────────────
    my_sheet_name = f"{MONTHS_RU[now.month]} {now.year}"
    my_month_data = d.get("monthly_sheets", {}).get(my_sheet_name)
    if my_month_data:
        my_days_in_month = calendar.monthrange(now.year, now.month)[1]
        my_name_to_row = {str(r[0]).strip(): r for r in my_month_data["rows"] if r and str(r[0]).strip()}

        total_errors_35, days_errors_36 = [], []
        for dr in dash_monthly:
            if not dr or not str(dr[0]).strip():
                continue
            name = str(dr[0]).strip()
            mrow = my_name_to_row.get(name, [])
            real_total = str(mrow[my_days_in_month + 1]).strip() if len(mrow) > my_days_in_month + 1 else ""
            real_days = sum(1 for c in mrow[1:my_days_in_month + 1] if _day_cell_worked(c))

            dash_total = str(dr[1]).strip() if len(dr) > 1 else ""
            try:
                if abs((float(dash_total.replace(",", ".")) if dash_total else 0.0) -
                       (float(real_total.replace(",", ".")) if real_total else 0.0)) > 0.05:
                    total_errors_35.append(f"{name}: дашборд={dash_total or '(пусто)'}, лист={real_total or '(пусто)'}")
            except Exception:
                if dash_total != real_total:
                    total_errors_35.append(f"{name}: дашборд={dash_total or '(пусто)'}, лист={real_total or '(пусто)'}")

            dash_days = str(dr[2]).strip() if len(dr) > 2 else ""
            try:
                if (int(dash_days) if dash_days else 0) != real_days:
                    days_errors_36.append(f"{name}: дашборд={dash_days or '0'}, факт непустых ячеек={real_days}")
            except Exception:
                pass

        if total_errors_35:
            add("ОШИБКА", 35, "Сводка: Итого не совпадает с Итого месячного листа", "\n".join(total_errors_35))
        if days_errors_36:
            add("ОШИБКА", 36, "Сводка: Дней присутствия не совпадает с числом непустых ячеек в месячном листе",
                "\n".join(days_errors_36))

        # ────────────────────────────────────────────────────────────────────────
        # 37. Сводка: Авто-закрытий ≠ count(⚠️ авто) в Журнале за месяц
        # ────────────────────────────────────────────────────────────────────────
        auto_count_by_name = defaultdict(int)
        for e in cur_month_entries:
            r = e["row"]
            if len(r) >= 8 and str(r[7]).strip() == "⚠️ авто":
                auto_count_by_name[r[1].strip()] += 1
        auto_errors = []
        for dr in dash_monthly:
            if not dr or not str(dr[0]).strip():
                continue
            name = str(dr[0]).strip()
            dash_auto = str(dr[3]).strip() if len(dr) > 3 else ""
            try:
                dash_auto_n = int(dash_auto) if dash_auto else 0
            except Exception:
                continue
            real_auto = auto_count_by_name.get(name, 0)
            if dash_auto_n != real_auto:
                auto_errors.append(f"{name}: дашборд={dash_auto_n}, Журнал={real_auto}")
        if auto_errors:
            add("ОШИБКА", 37, "Сводка: Авто-закрытий не совпадает со счётом '⚠️ авто' в Журнале за месяц",
                "\n".join(auto_errors))

        # ────────────────────────────────────────────────────────────────────────
        # 38. Сводка: % посещаемости ≠ round(дней/рабочих_дней_до_сегодня*100), допуск ±1%
        #     Только для сотрудников с графиком Пн-Пт или пустым полем (см. Эталон)
        # ────────────────────────────────────────────────────────────────────────
        workdays_so_far = sum(1 for dd in range(1, now.day + 1)
                              if date(now.year, now.month, dd).weekday() < 5)
        pct_errors = []
        if workdays_so_far:
            for dr in dash_monthly:
                if not dr or not str(dr[0]).strip():
                    continue
                name = str(dr[0]).strip()
                emp_row = name_to_emp_row.get(name, [])
                work_days_str = emp_row[7].strip() if len(emp_row) > 7 else ""
                wd = parse_work_days(work_days_str) if work_days_str else None
                if wd is not None and wd != frozenset({0, 1, 2, 3, 4}):
                    continue  # нестандартный график — формула не применима (см. Эталон)
                mrow = my_name_to_row.get(name, [])
                real_days = sum(1 for c in mrow[1:my_days_in_month + 1] if _day_cell_worked(c))
                expected_pct = round(real_days / workdays_so_far * 100)
                dash_pct_str = str(dr[4]).strip().replace("%", "") if len(dr) > 4 else ""
                try:
                    dash_pct = int(dash_pct_str) if dash_pct_str else 0
                except Exception:
                    continue
                if abs(dash_pct - expected_pct) > 1:
                    pct_errors.append(f"{name}: дашборд={dash_pct}%, ожидалось={expected_pct}%")
        if pct_errors:
            add("ОШИБКА", 38, "Сводка: % посещаемости не совпадает с формулой (допуск ±1%)",
                "\n".join(pct_errors))

    # ────────────────────────────────────────────────────────────────────────────
    # 39. Сводка: Итого=0/пусто, но в Журнале за месяц есть закрытые смены с часами > 0
    # ────────────────────────────────────────────────────────────────────────────
    month_hours_by_name = defaultdict(float)
    for e in cur_month_entries:
        r = e["row"]
        if len(r) < 7 or not str(r[5]).strip():
            continue
        try:
            h, m = str(r[6]).strip().split(":")
            month_hours_by_name[r[1].strip()] += int(h) + int(m) / 60
        except Exception:
            pass
    zero_total_errors = []
    for dr in dash_monthly:
        if not dr or not str(dr[0]).strip():
            continue
        name = str(dr[0]).strip()
        dash_total = str(dr[1]).strip() if len(dr) > 1 else ""
        if dash_total and dash_total not in ("0", "0.0", "0,0"):
            continue
        real_hours = month_hours_by_name.get(name, 0.0)
        if real_hours > 0.05:
            zero_total_errors.append(f"{name}: дашборд Итого={dash_total or '(пусто)'}, но в Журнале {round(real_hours,2)}ч")
    if zero_total_errors:
        add("ОШИБКА", 39, "Сводка: Итого=0/пусто, хотя в Журнале за месяц есть часы",
            "\n".join(zero_total_errors))

    # ────────────────────────────────────────────────────────────────────────────
    # 40. Сотрудник в сводке, но Активен ≠ "да" в Сотрудники
    # ────────────────────────────────────────────────────────────────────────────
    inactive_in_summary = []
    for dr in dash_monthly:
        if not dr or not str(dr[0]).strip():
            continue
        name = str(dr[0]).strip()
        emp_row = name_to_emp_row.get(name, [])
        active_col = emp_row[4].strip().lower() if len(emp_row) > 4 else ""
        if active_col != "да":
            inactive_in_summary.append(f"{name}: Активен={active_col or '(пусто)'}")
    if inactive_in_summary:
        add("ПРЕДУПРЕЖДЕНИЕ", 40, "Сводка: сотрудник присутствует, но не активен в Сотрудники",
            "\n".join(inactive_in_summary))

    # ────────────────────────────────────────────────────────────────────────────
    # 41-45. Реестр уведомлений дашборда (B26:G28, нормализован при чтении) vs лист Уведомления
    #        Структура реестра: [время_факт_или_план, имя, тип, время_план, статус]
    #        Ключ сопоставления с листом Уведомления: (имя, тип)
    # ────────────────────────────────────────────────────────────────────────────
    PLANNED_STATUSES = {"запланировано", "ожидает отправки", "ПРОПУЩЕНО", "—"}
    # (имя, тип) -> ВСЕ строки Уведомления за сегодня. Раньше хранилась только последняя,
    # и при дубликатах (один тип 2+ раза за день) первые строки реестра ложно
    # не совпадали по времени/статусу с последней записью листа.
    notif_rows_by_key = defaultdict(list)
    for r in today_notifs:
        if len(r) >= 4:
            notif_rows_by_key[(r[2].strip(), r[3].strip())].append(r)

    real_notif_rows = [r for r in dash_notif if r and str(r[1] if len(r) > 1 else "").strip()
                        and str(r[1]).strip() != "—"]

    time_errs, status_errs, name_errs, phantom_errs = [], [], [], []
    sent_like_count = 0
    for r in real_notif_rows:
        name  = str(r[1]).strip() if len(r) > 1 else ""
        ntype = str(r[2]).strip() if len(r) > 2 else ""
        status = str(r[4]).strip() if len(r) > 4 else ""
        is_sent_like = status not in PLANNED_STATUSES
        if not is_sent_like:
            continue
        sent_like_count += 1
        matches = notif_rows_by_key.get((name, ntype), [])
        if not matches:
            phantom_errs.append(f"{name} / {ntype}: статус='{status}', но записи в Уведомления за сегодня нет")
            continue
        # 41: время факт (col0) должно совпадать хотя бы с ОДНОЙ записью Уведомления!B этого ключа
        dash_time = str(r[0]).strip() if len(r) > 0 else ""
        real_times = {str(m[1]).strip() for m in matches if len(m) > 1}
        if dash_time not in real_times:
            time_errs.append(f"{name} / {ntype}: дашборд={dash_time}, Уведомления!B={sorted(real_times)}")
        # 42: статус (col4) должен совпадать хотя бы с одной записью Уведомления!F
        real_statuses = {str(m[5]).strip() for m in matches if len(m) > 5}
        if status not in real_statuses:
            status_errs.append(f"{name} / {ntype}: дашборд={status}, Уведомления!F={sorted(real_statuses)}")
        # 43: имя (col1) ≠ Уведомления!C (посимвольно, включая пробелы)
        real_names = {str(m[2]) for m in matches if len(m) > 2}
        if r[1] not in real_names:
            name_errs.append(f"'{r[1]}' (дашборд) ∉ {sorted(real_names)} (Уведомления!C)")

    if time_errs:
        add("ОШИБКА", 41, "Реестр уведомлений: время факт не совпадает с Уведомления!B", "\n".join(time_errs))
    if status_errs:
        add("ОШИБКА", 42, "Реестр уведомлений: статус не совпадает с Уведомления!F", "\n".join(status_errs))
    if name_errs:
        add("ПРЕДУПРЕЖДЕНИЕ", 43, "Реестр уведомлений: имя не побайтово совпадает с Уведомления!C",
            "\n".join(name_errs))

    # 44: число "фактических" строк реестра ≠ числу записей в Уведомления за сегодня
    # (макет 02.07.2026 вмещает максимум 3 строки — ARRAY_CONSTRAIN, обрезка сверх ожидаема)
    DASH_NOTIF_CAP = 3
    expected_in_dash = min(len(today_notifs), DASH_NOTIF_CAP)
    if sent_like_count != expected_in_dash:
        add("ПРЕДУПРЕЖДЕНИЕ", 44,
            "Реестр уведомлений: число строк с фактическим статусом ≠ числу записей в Уведомления за сегодня",
            f"В реестре (факт): {sent_like_count}, в листе Уведомления за сегодня: {len(today_notifs)}"
            f" (лимит блока: {DASH_NOTIF_CAP})")

    # 45: выдуманные уведомления (статус "отправлено", но записи в Уведомления нет)
    if phantom_errs:
        add("ПРЕДУПРЕЖДЕНИЕ", 45,
            "Реестр уведомлений: строка со статусом отправки, но без соответствия в листе Уведомления",
            "\n".join(phantom_errs))

    # ────────────────────────────────────────────────────────────────────────────
    # 46. Журнал (сегодня): Тип (C) ≠ текущий Сотрудники!C для того же сотрудника
    # ────────────────────────────────────────────────────────────────────────────
    type_drift = []
    for e in today_entries:
        r = e["row"]
        if len(r) < 3:
            continue
        name = r[1].strip()
        journal_type = str(r[2]).strip().lower()
        emp_row = name_to_emp_row.get(name, [])
        cur_type = emp_row[2].strip().lower() if len(emp_row) > 2 else ""
        if cur_type and journal_type and journal_type != cur_type:
            type_drift.append(f"{name}: Журнал!C={journal_type}, Сотрудники!C={cur_type}")
    if type_drift:
        add("ПРЕДУПРЕЖДЕНИЕ", 46, "Журнал: тип записи не совпадает с текущим типом в Сотрудники",
            "\n".join(type_drift))

    # ────────────────────────────────────────────────────────────────────────────
    # 47. Открытая смена сегодня: Журнал!D (Объект) ≠ Сотрудники!D (Локация)
    # ────────────────────────────────────────────────────────────────────────────
    object_drift = []
    for name, r in open_entry_by_name.items():
        journal_object = str(r[3]).strip() if len(r) > 3 else ""
        if not journal_object:
            continue  # водитель/сервис — нет объекта
        emp_row = name_to_emp_row.get(name, [])
        emp_location = str(emp_row[3]).strip() if len(emp_row) > 3 else ""
        if emp_location and journal_object != emp_location:
            object_drift.append(f"{name}: Журнал!D={journal_object}, Сотрудники!D={emp_location}")
    if object_drift:
        add("ПРЕДУПРЕЖДЕНИЕ", 47, "Открытая смена: объект в Журнале не совпадает с локацией в Сотрудники",
            "\n".join(object_drift))

    # ────────────────────────────────────────────────────────────────────────────
    # 48. Активных в Сотрудники vs строк в текущем месячном листе (двустороннее)
    # ────────────────────────────────────────────────────────────────────────────
    if my_month_data:
        names_in_month = {str(r[0]).strip() for r in my_month_data["rows"] if r and str(r[0]).strip()}
        missing_rows = active_names - names_in_month
        ghost_rows = names_in_month - active_names
        if missing_rows:
            add("ОШИБКА", 48, "Активный сотрудник без строки в текущем месячном листе",
                "\n".join(sorted(missing_rows)))
        if ghost_rows:
            add("ПРЕДУПРЕЖДЕНИЕ", 48, "Строка в месячном листе для неактивного сотрудника",
                "\n".join(sorted(ghost_rows)))

    # ────────────────────────────────────────────────────────────────────────────
    # 49. Сотрудник неактивен (Активен ≠ "да"), но в Журнале есть записи за текущий месяц
    # ────────────────────────────────────────────────────────────────────────────
    inactive_with_entries = defaultdict(int)
    for e in cur_month_entries:
        r = e["row"]
        if len(r) < 2:
            continue
        name = r[1].strip()
        emp_row = name_to_emp_row.get(name, [])
        active_col = emp_row[4].strip().lower() if len(emp_row) > 4 else ""
        if emp_row and active_col != "да":
            inactive_with_entries[name] += 1
    if inactive_with_entries:
        add("ПРЕДУПРЕЖДЕНИЕ", 49, "Неактивный сотрудник имеет записи в Журнале за текущий месяц",
            "\n".join(f"{n}: {c} запись(ей)" for n, c in inactive_with_entries.items()))

    # ────────────────────────────────────────────────────────────────────────────
    # 50. Месячный лист: Итого пусто, хотя есть непустые ячейки дней (SUM-формула не работает)
    # ────────────────────────────────────────────────────────────────────────────
    for sheet_name, mdata in d.get("monthly_sheets", {}).items():
        dt = mdata["dt"]
        days_in_month = calendar.monthrange(dt.year, dt.month)[1]
        broken_sum = []
        for r in mdata["rows"]:
            if not r:
                continue
            name = str(r[0]).strip()
            if not name:
                continue
            total_cell = str(r[days_in_month + 1]).strip() if len(r) > days_in_month + 1 else ""
            has_any_day = any(_day_cell_worked(c) for c in r[1:days_in_month + 1])
            if not total_cell and has_any_day:
                broken_sum.append(name)
        if broken_sum:
            add("ОШИБКА", 50, f"{sheet_name}: Итого пусто при наличии заполненных дней (SUM не считает)",
                "\n".join(broken_sum))


# ── MAIN ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Проверяем что credentials заданы
    missing = [k for k in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_DRIVE_REFRESH_TOKEN")
               if not os.environ.get(k)]
    if missing:
        print(f"❌ Нет переменных окружения: {missing}")
        print("   Задай их перед запуском или используй .env файл")
        sys.exit(1)

    try:
        data = load_all()
        run_checks(data)
        print_report()
    except KeyboardInterrupt:
        print("\n  Прервано пользователем.")
    except Exception as ex:
        import traceback
        print(f"\n❌ Критическая ошибка ревизора: {ex}")
        traceback.print_exc()
        sys.exit(1)
