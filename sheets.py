import os
import calendar
from datetime import datetime, timedelta, date

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

CLIENT_ID      = os.environ["GOOGLE_CLIENT_ID"]
CLIENT_SECRET  = os.environ["GOOGLE_CLIENT_SECRET"]
REFRESH_TOKEN  = os.environ["GOOGLE_DRIVE_REFRESH_TOKEN"]
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "1DZ_XQPAGbSn5aCKVqcBBRQ-X4sAItx23v-qJEw1dYbo")

MONTHS_RU = ["","Январь","Февраль","Март","Апрель","Май","Июнь",
             "Июль","Август","Сентябрь","Октябрь","Ноябрь","Декабрь"]

_service = None

def _svc():
    global _service
    if _service is None:
        creds = Credentials(
            token=None,
            refresh_token=REFRESH_TOKEN,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        creds.refresh(Request())
        _service = build("sheets", "v4", credentials=creds)
    return _service

def _col(n):
    """0-indexed column number → letter(s). 0=A, 1=B, 26=AA ..."""
    result = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        result = chr(65 + r) + result
    return result

def _read(sheet, range_):
    res = _svc().spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{sheet}!{range_}"
    ).execute()
    return res.get("values", [])

def _append(sheet, values):
    _svc().spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{sheet}!A1",
        valueInputOption="USER_ENTERED",
        body={"values": [values]}
    ).execute()

def _write(sheet, range_, values):
    _svc().spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{sheet}!{range_}",
        valueInputOption="USER_ENTERED",
        body={"values": values}
    ).execute()

def _get_sheet_id(title):
    meta = _svc().spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    for s in meta["sheets"]:
        if s["properties"]["title"] == title:
            return s["properties"]["sheetId"]
    return None

def _set_cell_color(sheet_title, row_num, col_index, r, g, b):
    sheet_id = _get_sheet_id(sheet_title)
    if sheet_id is None:
        return
    _svc().spreadsheets().batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={"requests": [{
            "repeatCell": {
                "range": {
                    "sheetId":          sheet_id,
                    "startRowIndex":    row_num - 1,
                    "endRowIndex":      row_num,
                    "startColumnIndex": col_index,
                    "endColumnIndex":   col_index + 1,
                },
                "cell": {"userEnteredFormat": {
                    "backgroundColor": {"red": r, "green": g, "blue": b}
                }},
                "fields": "userEnteredFormat.backgroundColor",
            }
        }]}
    ).execute()


# ── Публичный API ──────────────────────────────────────────────────────────────

def get_employee(telegram_id):
    # A=Telegram ID, B=Имя, C=Тип, D=Локация(не используется), E=Активен
    rows = _read("Сотрудники", "A2:E200")
    for row in rows:
        if not row or str(row[0]).strip() != str(telegram_id):
            continue
        # Поддержка обоих форматов: 4 колонки (старый) и 5 колонок (новый)
        if len(row) >= 5:
            active = row[4].strip().lower()
        elif len(row) >= 4:
            active = row[3].strip().lower()
        else:
            continue
        if active == "да":
            return {
                "name": row[1].strip(),
                "type": row[2].strip().lower(),
            }
    return None

def register_employee(telegram_id, name, emp_type):
    # A=ID, B=Имя, C=Тип, D=Локация(пусто), E=Активен
    _append("Сотрудники", [telegram_id, name, emp_type, "", "да"])

def update_employee_location(telegram_id, location):
    """Пишет текущий объект (или '' если ушёл) в колонку D листа Сотрудники."""
    rows = _read("Сотрудники", "A2:A200")
    for i, row in enumerate(rows):
        if row and str(row[0]).strip() == str(telegram_id):
            _write("Сотрудники", f"D{i + 2}", [[location]])
            return

_DAYS_MAP = {"пн": 0, "вт": 1, "ср": 2, "чт": 3, "пт": 4, "сб": 5, "вс": 6}

def parse_work_days(days_str):
    """'Пн,Вт,Пт' или 'Пн-Пт' → frozenset weekday номеров (0=Пн … 6=Вс)."""
    if not days_str:
        return None
    s = days_str.strip().lower()
    if "-" in s and "," not in s:
        parts = s.split("-", 1)
        a, b  = _DAYS_MAP.get(parts[0].strip()), _DAYS_MAP.get(parts[1].strip())
        if a is not None and b is not None:
            return frozenset(range(a, b + 1))
    result = set()
    for tok in s.split(","):
        d = _DAYS_MAP.get(tok.strip())
        if d is not None:
            result.add(d)
    return frozenset(result) if result else None


def get_all_employees_with_schedule():
    """Все активные сотрудники с временем начала и рабочими днями."""
    # A=ID, B=Имя, C=Тип, D=Локация, E=Активен, F=Начало работы, G=Конец работы, H=Рабочие дни
    rows = _read("Сотрудники", "A2:H200")
    result = []
    for row in rows:
        if len(row) < 2:
            continue
        active = row[4].strip().lower() if len(row) >= 5 else (row[3].strip().lower() if len(row) >= 4 else "")
        if active != "да":
            continue
        schedule  = row[5].strip() if len(row) >= 6 else ""
        end_time  = row[6].strip() if len(row) >= 7 else ""
        work_days = parse_work_days(row[7].strip() if len(row) >= 8 else "")
        result.append({
            "telegram_id": row[0].strip(),
            "name":        row[1].strip(),
            "type":        row[2].strip().lower() if len(row) >= 3 else "",
            "schedule":    schedule,
            "end_time":    end_time,
            "work_days":   work_days,
        })
    return result


def get_all_locations():
    rows = _read("Локации", "A2:A100")
    return [row[0].strip() for row in rows if row and row[0].strip()]

def get_location(name):
    if not name or name in ("—", "-", ""):
        return None
    rows = _read("Локации", "A2:D200")
    for row in rows:
        if len(row) >= 4 and row[0].strip() == name:
            try:
                return {
                    "lat":    float(str(row[1]).replace(",", ".")),
                    "lon":    float(str(row[2]).replace(",", ".")),
                    "radius": float(str(row[3]).replace(",", ".")),
                }
            except ValueError:
                return None
    return None

def find_open_entry(name):
    # Журнал: A=Дата B=Имя C=Тип D=Объект E=Приход F=Уход G=Отработано H=Статус I=Посл.активность
    rows = _read("Журнал", "A2:I2000")
    for i, row in enumerate(rows):
        if len(row) >= 5 and row[1].strip() == name:
            if len(row) < 6 or not str(row[5]).strip():
                return {
                    "row":           i + 2,
                    "arrival":       row[4],
                    "location":      row[3] if len(row) > 3 else "",
                    "last_activity": row[8] if len(row) > 8 else "",
                }
    return None

def get_entry_by_row(row_num):
    """Читает конкретную строку Журнала."""
    rows = _read("Журнал", f"A{row_num}:I{row_num}")
    if rows and rows[0]:
        row = rows[0]
        return {
            "date":          row[0] if len(row) > 0 else "",
            "name":          row[1] if len(row) > 1 else "",
            "location":      row[3] if len(row) > 3 else "",
            "arrival":       row[4] if len(row) > 4 else "",
            "last_activity": row[8] if len(row) > 8 else "",
        }
    return None

def record_arrival(name, emp_type, location, dt):
    _append("Журнал", [
        dt.strftime("%d.%m.%Y"),
        name,
        emp_type,
        location,
        dt.strftime("%H:%M"),
        "", "", "", "",
    ])
    # Создаём строку в месячном листе сразу при приходе
    _ensure_monthly_sheet(f"{MONTHS_RU[dt.month]} {dt.year}", dt.year, dt.month)
    _ensure_employee_row(name, dt)
    _mark_monthly_present(name, dt)


def _mark_monthly_present(name, dt):
    """Подсвечивает зелёным день в месячном листе — сотрудник сейчас на смене."""
    sheet = f"{MONTHS_RU[dt.month]} {dt.year}"
    rows  = _read(sheet, "A2:A100")
    for i, row in enumerate(rows):
        if row and row[0].strip() == name:
            _set_cell_color(sheet, i + 2, dt.day, 0.7, 0.9, 0.7)  # зелёный
            return


def _reset_monthly_color(name, dt):
    """Возвращает обычный цвет ячейки (серый для выходных, белый для будней)."""
    sheet = f"{MONTHS_RU[dt.month]} {dt.year}"
    rows  = _read(sheet, "A2:A100")
    for i, row in enumerate(rows):
        if row and row[0].strip() == name:
            row_num = i + 2
            if date(dt.year, dt.month, dt.day).weekday() >= 5:
                _set_cell_color(sheet, row_num, dt.day, 0.85, 0.85, 0.85)  # серый выходной
            else:
                _set_cell_color(sheet, row_num, dt.day, 1.0, 1.0, 1.0)  # белый
            return


def _ensure_employee_row(name, dt):
    """Создаёт строку сотрудника в месячном листе если её нет."""
    sheet = f"{MONTHS_RU[dt.month]} {dt.year}"
    rows  = _read(sheet, "A2:A100")
    for row in rows:
        if row and row[0].strip() == name:
            return  # уже есть
    days_in_month = calendar.monthrange(dt.year, dt.month)[1]
    _append(sheet, [name] + [""] * days_in_month + [""])
    # Пишем формулу Итого
    rows = _read(sheet, "A2:A100")
    for i, row in enumerate(rows):
        if row and row[0].strip() == name:
            row_num      = i + 2
            last_day_col = _col(days_in_month)
            total_col    = _col(days_in_month + 1)
            _write(sheet, f"{total_col}{row_num}",
                   [[f"=SUM(B{row_num}:{last_day_col}{row_num})"]])
            return

def record_departure(name, dt, open_entry):
    arrival_str   = open_entry["arrival"]
    row_num       = open_entry["row"]
    departure_str = dt.strftime("%H:%M")

    try:
        arr = datetime.strptime(arrival_str, "%H:%M")
        dep = datetime.strptime(departure_str, "%H:%M")
        if dep < arr:
            dep += timedelta(days=1)
        total_min     = int((dep - arr).total_seconds() // 60)
        hours_str     = f"{total_min // 60}:{total_min % 60:02d}"
        hours_decimal = round(total_min / 60, 2)
    except Exception:
        hours_str, hours_decimal = "0:00", 0.0

    _write("Журнал", f"F{row_num}:I{row_num}", [[departure_str, hours_str, "✅", ""]])
    _write_monthly(name, dt, hours_decimal)
    _reset_monthly_color(name, dt)
    return hours_str

def update_last_activity(name, time_str):
    """Обновляет время последней активности (колонка I) в открытой записи Журнала."""
    rows = _read("Журнал", "A2:I2000")
    for i, row in enumerate(rows):
        if len(row) >= 5 and row[1].strip() == name:
            if len(row) < 6 or not str(row[5]).strip():
                row_num = i + 2
                _write("Журнал", f"I{row_num}", [[time_str]])
                return

def reopen_entry(row_num, name, dt):
    """Снимает авто-закрытие: очищает Уход/Отработано/Статус и ячейку в месячном листе."""
    _write("Журнал", f"F{row_num}:I{row_num}", [["", "", "⏳ продлено до 23:55", ""]])
    # Берём дату прихода из журнала чтобы очистить правильную ячейку в месячном листе
    entry = get_entry_by_row(row_num)
    if entry and entry.get("date"):
        try:
            entry_dt = datetime.strptime(entry["date"], "%d.%m.%Y")
        except Exception:
            entry_dt = dt
    else:
        entry_dt = dt
    _clear_monthly_auto(name, entry_dt)
    _mark_monthly_present(name, entry_dt)

def _clear_monthly_auto(name, dt):
    """Очищает 'авто' и сбрасывает цвет ячейки в месячном листе."""
    sheet = f"{MONTHS_RU[dt.month]} {dt.year}"
    day   = dt.day

    rows = _read(sheet, "A2:A100")
    row_num = None
    for i, row in enumerate(rows):
        if row and row[0].strip() == name:
            row_num = i + 2
            break
    if not row_num:
        return

    col = _col(day)
    _write(sheet, f"{col}{row_num}", [[""]])
    _set_cell_color(sheet, row_num, day, 1.0, 1.0, 1.0)  # белый

def record_waypoint(name, lat, lon, dt):
    _append("Точки водителей", [
        dt.strftime("%d.%m.%Y"),
        name,
        dt.strftime("%H:%M"),
        round(lat, 6),
        round(lon, 6),
    ])
    update_last_activity(name, dt.strftime("%H:%M"))

def get_open_entries_all():
    """Все незакрытые записи с Telegram ID сотрудника."""
    emp_rows = _read("Сотрудники", "A2:E200")
    emp_map = {row[1].strip(): row[0].strip() for row in emp_rows if len(row) >= 2}

    rows = _read("Журнал", "A2:I2000")
    result = []
    for i, row in enumerate(rows):
        if len(row) >= 5 and row[1].strip():
            if len(row) < 6 or not str(row[5]).strip():
                name = row[1].strip()
                result.append({
                    "row":           i + 2,
                    "name":          name,
                    "date":          row[0],
                    "arrival":       row[4],
                    "location":      row[3] if len(row) > 3 else "",
                    "telegram_id":   emp_map.get(name, ""),
                    "last_activity": row[8] if len(row) > 8 else "",
                })
    return result

def get_extended_entries():
    """Открытые записи со статусом '⏳ продлено' (подтвердили что ещё на работе)."""
    emp_rows = _read("Сотрудники", "A2:E200")
    emp_map  = {row[1].strip(): row[0].strip() for row in emp_rows if len(row) >= 2}

    rows = _read("Журнал", "A2:I2000")
    result = []
    for i, row in enumerate(rows):
        if len(row) >= 8 and row[1].strip() and "продлено" in str(row[7]):
            if len(row) < 6 or not str(row[5]).strip():
                name = row[1].strip()
                result.append({
                    "row":         i + 2,
                    "name":        name,
                    "telegram_id": emp_map.get(name, ""),
                })
    return result


def auto_close_entry(row_num, name, arrival_str, close_dt):
    """Закрывает запись автоматически, помечает ⚠️ и красит ячейку в месячном листе."""
    departure_str = close_dt.strftime("%H:%M")
    try:
        arr = datetime.strptime(arrival_str, "%H:%M")
        dep = datetime.strptime(departure_str, "%H:%M")
        if dep < arr:
            dep += timedelta(days=1)
        total_min     = int((dep - arr).total_seconds() // 60)
        hours_str     = f"{total_min // 60}:{total_min % 60:02d}"
        hours_decimal = round(total_min / 60, 2)
    except Exception:
        hours_str, hours_decimal = "0:00", 0.0

    _write("Журнал", f"F{row_num}:I{row_num}", [
        [departure_str, hours_str, "⚠️ авто", ""]
    ])
    _mark_monthly_auto(name, close_dt, hours_decimal)

def _mark_monthly_auto(name, dt, hours_decimal):
    sheet = f"{MONTHS_RU[dt.month]} {dt.year}"
    day   = dt.day

    _ensure_monthly_sheet(sheet, dt.year, dt.month)
    rows = _read(sheet, "A2:A100")
    row_num = None
    for i, row in enumerate(rows):
        if row and row[0].strip() == name:
            row_num = i + 2
            break
    if row_num is None:
        _ensure_employee_row(name, dt)
        rows = _read(sheet, "A2:A100")
        for i, row in enumerate(rows):
            if row and row[0].strip() == name:
                row_num = i + 2
                break
    if not row_num:
        return

    col = _col(day)
    _write(sheet, f"{col}{row_num}", [[hours_decimal]])
    _set_cell_color(sheet, row_num, day, 1.0, 0.4, 0.4)  # красный

def _ensure_monthly_sheet(sheet_name, year, month):
    """Создаёт месячный лист с заголовком и серыми выходными если его нет."""
    meta     = _svc().spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    existing = {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta["sheets"]}
    if sheet_name in existing:
        return

    _svc().spreadsheets().batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={"requests": [{"addSheet": {"properties": {"title": sheet_name}}}]}
    ).execute()

    days_in_month = calendar.monthrange(year, month)[1]
    header = ["Имя"] + list(range(1, days_in_month + 1)) + ["Итого"]
    _write(sheet_name, "A1", [header])

    # Красим субботы и воскресенья серым
    meta     = _svc().spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    sheet_id = next(s["properties"]["sheetId"] for s in meta["sheets"]
                    if s["properties"]["title"] == sheet_name)
    requests = []
    for day in range(1, days_in_month + 1):
        if date(year, month, day).weekday() >= 5:  # Сб=5, Вс=6
            requests.append({
                "repeatCell": {
                    "range": {
                        "sheetId":          sheet_id,
                        "startRowIndex":    0,
                        "endRowIndex":      1000,
                        "startColumnIndex": day,
                        "endColumnIndex":   day + 1,
                    },
                    "cell": {"userEnteredFormat": {
                        "backgroundColor": {"red": 0.85, "green": 0.85, "blue": 0.85}
                    }},
                    "fields": "userEnteredFormat.backgroundColor",
                }
            })
    if requests:
        _svc().spreadsheets().batchUpdate(
            spreadsheetId=SPREADSHEET_ID,
            body={"requests": requests}
        ).execute()

def _write_monthly(name, dt, hours_decimal):
    sheet = f"{MONTHS_RU[dt.month]} {dt.year}"
    day   = dt.day

    _ensure_monthly_sheet(sheet, dt.year, dt.month)
    rows = _read(sheet, "A2:A100")
    row_num = None
    for i, row in enumerate(rows):
        if row and row[0].strip() == name:
            row_num = i + 2
            break

    if row_num is None:
        # Строки нет — создаём (на случай если водитель или авто-закрытие без record_arrival)
        _ensure_employee_row(name, dt)
        rows = _read(sheet, "A2:A100")
        for i, row in enumerate(rows):
            if row and row[0].strip() == name:
                row_num = i + 2
                break

    if row_num:
        col = _col(day)
        _write(sheet, f"{col}{row_num}", [[hours_decimal]])
