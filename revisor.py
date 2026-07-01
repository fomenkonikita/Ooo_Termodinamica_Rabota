"""
revisor.py — агент-ревизор бота учёта рабочего времени.

Запуск: python revisor.py
Нужны переменные окружения (те же что у бота):
  GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_DRIVE_REFRESH_TOKEN
  SPREADSHEET_ID (по умолчанию: 1DZ_XQPAGbSn5aCKVqcBBRQ-X4sAItx23v-qJEw1dYbo)
  TZ_OFFSET (по умолчанию: 5)

Выдаёт структурированный список нарушений по 30 критериям.
Критика сгруппирована по блокам: КРИТИЧНО / ОШИБКА / ПРЕДУПРЕЖДЕНИЕ / ИНФО.
"""

import os
import sys
import calendar
from datetime import datetime, timedelta, date

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
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        creds.refresh(Request())
        authed_http = AuthorizedHttp(creds, http=httplib2.Http(timeout=30))
        _svc_instance = build("sheets", "v4", http=authed_http)
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

    # Дашборд
    try:
        data["dash_block1"]   = _read("Дашборд", "A6:C21")
        data["dash_monthly"]  = _read("Дашборд", "A55:E79")
        data["dash_notif"]    = _read("Дашборд", "A82:E120")
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

    if extra_in_dash:
        add("КРИТИЧНО", 13,
            "Дашборд блок 1: показывает людей которых нет в открытых сменах сегодня",
            f"Лишние: {sorted(extra_in_dash)}\nОткрытые сегодня по Журналу: {sorted(open_names_today)}")
    if missing_in_dash:
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
