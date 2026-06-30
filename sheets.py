import os
import ssl
import time
import calendar
import logging
import threading
from datetime import datetime, timedelta, date

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

log = logging.getLogger(__name__)

_RETRIABLE = (ssl.SSLError, ConnectionError, TimeoutError, OSError)


def _execute(request, max_retries=3):
    """Выполняет запрос к Google API с повтором при транзитных сетевых сбоях
    (SSL-ошибки вида decryption failed/wrong version number, обрывы
    соединения — реальная причина повторявшихся «новый сотрудник не попал
    в табель» багов 30.06.2026: единичный сбой сети тихо терял запись,
    а наружный try/except это просто проглатывал). Любой вызов .execute()
    в этом файле должен идти через эту функцию."""
    last_exc = None
    for attempt in range(max_retries):
        try:
            return request.execute()
        except _RETRIABLE as ex:
            last_exc = ex
            if attempt < max_retries - 1:
                wait = 0.5 * (attempt + 1)
                log.warning(f"Google API сетевой сбой (попытка {attempt+1}/{max_retries}), retry через {wait}с: {ex}")
                time.sleep(wait)
    raise last_exc

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
        for attempt in range(3):
            try:
                creds.refresh(Request())
                break
            except _RETRIABLE as ex:
                if attempt == 2:
                    raise
                log.warning(f"Сбой обновления токена Google (попытка {attempt+1}/3): {ex}")
                time.sleep(0.5 * (attempt + 1))
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
    res = _execute(_svc().spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{sheet}!{range_}"
    ))
    return res.get("values", [])

def _append(sheet, values):
    _execute(_svc().spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{sheet}!A1",
        valueInputOption="USER_ENTERED",
        body={"values": [values]}
    ))

def _write(sheet, range_, values):
    _execute(_svc().spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{sheet}!{range_}",
        valueInputOption="USER_ENTERED",
        body={"values": values}
    ))

def _get_sheet_id(title):
    meta = _execute(_svc().spreadsheets().get(spreadsheetId=SPREADSHEET_ID))
    for s in meta["sheets"]:
        if s["properties"]["title"] == title:
            return s["properties"]["sheetId"]
    return None

def _set_cell_color(sheet_title, row_num, col_index, r, g, b):
    sheet_id = _get_sheet_id(sheet_title)
    if sheet_id is None:
        return
    _execute(_svc().spreadsheets().batchUpdate(
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
    ))


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
    """Пишет текущий объект (или '' если ушёл) в колонку D листа Сотрудники.
    Не должна ронять остальной обработчик (см. инцидент 30.06.2026 — упала и
    оборвала весь geo_arrival, отметка не дошла до сотрудника)."""
    try:
        rows = _read("Сотрудники", "A2:A200")
        for i, row in enumerate(rows):
            if row and str(row[0]).strip() == str(telegram_id):
                _write("Сотрудники", f"D{i + 2}", [[location]])
                return
    except Exception as ex:
        log.warning(f"update_employee_location: сбой для {telegram_id}: {ex}")

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

def _ensure_notifications_sheet():
    meta     = _execute(_svc().spreadsheets().get(spreadsheetId=SPREADSHEET_ID))
    existing = {s["properties"]["title"] for s in meta["sheets"]}
    if "Уведомления" in existing:
        return
    _execute(_svc().spreadsheets().batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={"requests": [{"addSheet": {"properties": {"title": "Уведомления"}}}]}
    ))
    _write("Уведомления", "A1", [["Дата", "Время", "Сотрудник", "Тип", "Запланировано", "Статус", "Текст"]])


def log_notification(name, type_, planned_at, status, text, dt):
    """Лог-реестр напоминаний: что должно было уйти и что реально ушло.
    Не должен ломать основной поток при сбое — только лучшее старание."""
    try:
        _ensure_notifications_sheet()
        _append("Уведомления", [
            dt.strftime("%d.%m.%Y"), dt.strftime("%H:%M"), name, type_, planned_at, status, text,
        ])
    except Exception as ex:
        log.warning(f"log_notification: не удалось залогировать для {name}: {ex}")


def get_today_notifications(dt):
    """Реестр уведомлений за сегодня — для админ-кнопки."""
    try:
        rows = _read("Уведомления", "A2:G3000")
    except Exception:
        return []
    today = dt.strftime("%d.%m.%Y")
    return [r for r in rows if r and r[0] == today]


def get_today_notification_plan(dt):
    """Полный план уведомлений на сегодня — и уже отправленные, и ещё предстоящие,
    и пропущенные (время прошло, а в логе записи нет — сигнал реального сбоя).
    Возвращает список dict, отсортированный по времени: planned_at, name, type,
    status (отправлено / запланировано / ПРОПУЩЕНО / ошибка: ...), actual_time."""
    sent = get_today_notifications(dt)
    sent_map = {}
    for n in sent:
        if len(n) >= 6:
            sent_map[(n[2].strip(), n[3].strip())] = (n[1], n[5])

    emps = get_all_employees_with_schedule()
    plan = []
    for emp in emps:
        if not emp.get("schedule"):
            continue
        work_days = emp.get("work_days")
        if work_days and dt.weekday() not in work_days:
            continue
        name = emp["name"]
        try:
            t = datetime.strptime(emp["schedule"], "%H:%M")
            start_dt = dt.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
            plan.append({"planned_at": start_dt - timedelta(minutes=10), "name": name, "type": "до начала смены"})
        except Exception:
            pass
        end_time_str = emp.get("end_time", "")
        if end_time_str:
            try:
                t = datetime.strptime(end_time_str, "%H:%M")
                end_dt = dt.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
                plan.append({"planned_at": end_dt - timedelta(minutes=30), "name": name, "type": "до конца смены"})
            except Exception:
                pass

    for p in plan:
        key = (p["name"], p["type"])
        if key in sent_map:
            p["actual_time"], p["status"] = sent_map[key]
        elif p["planned_at"] > dt:
            p["actual_time"], p["status"] = None, "запланировано"
        else:
            p["actual_time"], p["status"] = None, "ПРОПУЩЕНО"

    plan.sort(key=lambda p: p["planned_at"])
    return plan


def has_closed_entry_today(name, dt):
    """Есть ли у сотрудника уже ЗАВЕРШЁННАЯ (приход+уход) запись сегодня —
    признак того, что повторный приход может быть случайным/тестовым (см.
    инцидент с риском «фантомных» часов при двойной отметке за день)."""
    date_str = dt.strftime("%d.%m.%Y")
    rows = _read("Журнал", "A2:I2000")
    for row in rows:
        if len(row) >= 6 and row[0] == date_str and row[1].strip() == name and str(row[5]).strip():
            return True
    return False


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

def record_arrival(name, emp_type, location, dt, telegram_id=""):
    """Только критичная запись в Журнал (источник правды) — быстро, 1 запрос к API.
    Месячный лист обновляется отдельно через update_monthly_on_arrival, обычно
    в фоновом потоке, чтобы не задерживать ответ пользователю (см. инцидент
    30.06.2026 — бот «висел» на отметке из-за 8-10 последовательных запросов).
    telegram_id пишется в колонку J — стабильный ключ сотрудника, не меняется
    при переименовании в Сотрудники (см. sync_employee_names)."""
    _append("Журнал", [
        dt.strftime("%d.%m.%Y"),
        name,
        emp_type,
        location,
        dt.strftime("%H:%M"),
        "", "", "", "",
        str(telegram_id),
    ])


def update_monthly_on_arrival(name, dt):
    """Создаёт/подсвечивает строку в месячном листе. Можно (и нужно) вызывать
    асинхронно после record_arrival — не блокирует ответ пользователю."""
    try:
        _ensure_monthly_sheet(f"{MONTHS_RU[dt.month]} {dt.year}", dt.year, dt.month)
        _ensure_employee_row(name, dt)
        _mark_monthly_present(name, dt)
    except Exception as ex:
        log.warning(f"update_monthly_on_arrival: не удалось обновить месячный лист для {name}: {ex}")


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
    # Строка добавилась сразу за последней непустой — индекс уже знаем,
    # повторное чтение не нужно (было узким местом на новых сотрудниках)
    row_num      = len(rows) + 2
    last_day_col = _col(days_in_month)
    total_col    = _col(days_in_month + 1)
    _write(sheet, f"{total_col}{row_num}",
           [[f"=SUM(B{row_num}:{last_day_col}{row_num})"]])


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
    return hours_str, hours_decimal


def update_monthly_on_departure(name, dt, hours_decimal):
    """Пишет часы и сбрасывает цвет в месячном листе. Вызывать после record_departure,
    обычно асинхронно — не блокирует ответ пользователю."""
    try:
        _write_monthly(name, dt, hours_decimal)
        _reset_monthly_color(name, dt)
    except Exception as ex:
        log.warning(f"update_monthly_on_departure: не удалось обновить месячный лист для {name}: {ex}")

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

def _ensure_gps_log_sheet():
    meta     = _execute(_svc().spreadsheets().get(spreadsheetId=SPREADSHEET_ID))
    existing = {s["properties"]["title"] for s in meta["sheets"]}
    if "GPS лог" in existing:
        return
    _execute(_svc().spreadsheets().batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={"requests": [{"addSheet": {"properties": {"title": "GPS лог"}}}]}
    ))
    _write("GPS лог", "A1", [["Дата", "Время", "TG ID", "Имя", "Объект", "Lat", "Lon", "Accuracy", "Подозрение"]])


def log_gps_and_check(telegram_id, name, location, lat, lon, accuracy, dt, check_accuracy=True):
    """Логирует GPS-замер при отметке и возвращает список причин подозрения на подмену
    геолокации (пустой список = замер выглядит нормально). Не блокирует отметку —
    только сигнал для админа. check_accuracy=False — для платформ, которые вообще не
    отдают точность (например MAX), чтобы не алертить на каждую отметку."""
    try:
        _ensure_gps_log_sheet()
        lat_r = round(lat, 6)
        lon_r = round(lon, 6)

        reasons = []
        if check_accuracy and (not accuracy or accuracy <= 0):
            reasons.append("нет данных о точности GPS (horizontal_accuracy)")

        rows = _read("GPS лог", "A2:H5000")
        for row in rows:
            if len(row) < 7:
                continue
            if row[2].strip() != str(telegram_id) or row[4].strip() != location:
                continue
            try:
                prev_lat, prev_lon = float(row[5]), float(row[6])
            except Exception:
                continue
            if round(prev_lat, 6) == lat_r and round(prev_lon, 6) == lon_r:
                reasons.append(f"координаты побитово совпадают с визитом {row[0]} {row[1]}")
                break

        _append("GPS лог", [
            dt.strftime("%d.%m.%Y"), dt.strftime("%H:%M"), str(telegram_id), name, location,
            lat_r, lon_r, accuracy if accuracy else "", "; ".join(reasons),
        ])
        return reasons
    except Exception as ex:
        log.warning(f"log_gps_and_check: сбой проверки GPS для {name}: {ex}")
        return []


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
    """См. _write_monthly — та же логика: пишем СУММУ всех закрытых смен
    этого дня, не только последнюю (авто-закрытую)."""
    sheet    = f"{MONTHS_RU[dt.month]} {dt.year}"
    day      = dt.day
    date_str = dt.strftime("%d.%m.%Y")

    day_total_decimal = hours_decimal
    try:
        journal_rows = _read("Журнал", "A2:I3000")
        total_min = 0
        for r in journal_rows:
            if len(r) >= 7 and r[0].strip() == date_str and r[1].strip() == name and r[6].strip():
                h, m = r[6].strip().split(":")
                total_min += int(h) * 60 + int(m)
        if total_min > 0:
            day_total_decimal = round(total_min / 60, 2)
    except Exception as ex:
        log.warning(f"_mark_monthly_auto: не удалось пересчитать сумму за день для {name}: {ex}")

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
    _write(sheet, f"{col}{row_num}", [[day_total_decimal]])
    _set_cell_color(sheet, row_num, day, 1.0, 0.4, 0.4)  # красный

_known_sheets = set()  # кэш существующих листов — не дёргать метаданные на каждый чих

def _ensure_monthly_sheet(sheet_name, year, month):
    """Создаёт месячный лист с заголовком и серыми выходными если его нет."""
    if sheet_name in _known_sheets:
        return
    meta     = _execute(_svc().spreadsheets().get(spreadsheetId=SPREADSHEET_ID))
    existing = {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta["sheets"]}
    _known_sheets.update(existing.keys())
    if sheet_name in existing:
        return

    _execute(_svc().spreadsheets().batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={"requests": [{"addSheet": {"properties": {"title": sheet_name}}}]}
    ))
    _known_sheets.add(sheet_name)

    days_in_month = calendar.monthrange(year, month)[1]
    header = ["Имя"] + list(range(1, days_in_month + 1)) + ["Итого"]
    _write(sheet_name, "A1", [header])

    # Красим субботы и воскресенья серым
    meta     = _execute(_svc().spreadsheets().get(spreadsheetId=SPREADSHEET_ID))
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
        _execute(_svc().spreadsheets().batchUpdate(
            spreadsheetId=SPREADSHEET_ID,
            body={"requests": requests}
        ))

def verify_journal_integrity():
    """Сверяет КАЖДУЮ закрытую запись Журнала: совпадает ли «Отработано» (G)
    с реальной разницей уход-приход. Чинит расхождения сама и возвращает
    список исправленного — неважно, откуда взялась ошибка (баг, ручная
    правка), эта проверка ловит её саму по себе (см. инцидент 30.06.2026 —
    запись с 0:00 вместо 2:00, причину которой не удалось установить
    из-за пересозданной git-истории)."""
    fixed = []
    try:
        rows = _read("Журнал", "A2:J3000")
        for i, row in enumerate(rows):
            if len(row) < 7 or not row[4].strip() or not row[5].strip():
                continue
            arrival_str, departure_str, stored = row[4].strip(), row[5].strip(), row[6].strip()
            try:
                arr = datetime.strptime(arrival_str, "%H:%M")
                dep = datetime.strptime(departure_str, "%H:%M")
                if dep < arr:
                    dep += timedelta(days=1)
                total_min = int((dep - arr).total_seconds() // 60)
                real_str  = f"{total_min // 60}:{total_min % 60:02d}"
            except Exception:
                continue
            if stored != real_str:
                row_num = i + 2
                _write("Журнал", f"G{row_num}", [[real_str]])
                fixed.append({
                    "name": row[1].strip(), "date": row[0].strip(),
                    "was": stored, "now": real_str,
                })
    except Exception as ex:
        log.warning(f"verify_journal_integrity: сбой: {ex}")
    return fixed


def reconcile_day(dt):
    """Сверяет Журнал и месячный лист за указанный день, дозаполняет пропуски.
    Возвращает список исправленных записей [{name, date, hours}]."""
    date_str = dt.strftime("%d.%m.%Y")
    rows = _read("Журнал", "A2:I2000")
    totals = {}
    for row in rows:
        if len(row) < 7 or row[0].strip() != date_str:
            continue
        name      = row[1].strip()
        hours_str = str(row[6]).strip()
        try:
            h, m = hours_str.split(":")
            hours_decimal = round(int(h) + int(m) / 60, 2)
        except Exception:
            continue
        if hours_decimal <= 0:
            continue
        totals[name] = totals.get(name, 0) + hours_decimal

    fixed = []
    if not totals:
        return fixed

    sheet = f"{MONTHS_RU[dt.month]} {dt.year}"
    _ensure_monthly_sheet(sheet, dt.year, dt.month)
    sheet_rows  = _read(sheet, "A2:AH100")
    name_to_row = {row[0].strip(): i + 2 for i, row in enumerate(sheet_rows) if row}

    for name, total in totals.items():
        row_num = name_to_row.get(name)
        if row_num is None:
            _ensure_employee_row(name, dt)
            sheet_rows  = _read(sheet, "A2:AH100")
            name_to_row = {row[0].strip(): i + 2 for i, row in enumerate(sheet_rows) if row}
            row_num = name_to_row.get(name)
        if row_num is None:
            continue
        row         = sheet_rows[row_num - 2] if row_num - 2 < len(sheet_rows) else []
        current_val = row[dt.day] if len(row) > dt.day else ""
        if not str(current_val).strip():
            col = _col(dt.day)
            _write(sheet, f"{col}{row_num}", [[total]])
            fixed.append({"name": name, "date": date_str, "hours": total})
    return fixed


def _write_monthly(name, dt, hours_decimal):
    """Пишет ИТОГ часов за день — не просто последнюю смену, а сумму ВСЕХ
    закрытых смен этого сотрудника в этот день из Журнала. Раньше писалась
    только последняя смена, перезаписывая предыдущие — если за день было
    несколько заходов/выходов (обед, повторный приход), реальные часы
    занижались (см. инцидент 30.06.2026: 3 смены за день, в табеле
    оставалась только вторая, 0:15, вместо суммы ~3,75ч)."""
    sheet     = f"{MONTHS_RU[dt.month]} {dt.year}"
    day       = dt.day
    date_str  = dt.strftime("%d.%m.%Y")

    day_total_decimal = hours_decimal
    try:
        journal_rows = _read("Журнал", "A2:I3000")
        total_min = 0
        for r in journal_rows:
            if len(r) >= 7 and r[0].strip() == date_str and r[1].strip() == name and r[6].strip():
                h, m = r[6].strip().split(":")
                total_min += int(h) * 60 + int(m)
        if total_min > 0:
            day_total_decimal = round(total_min / 60, 2)
    except Exception as ex:
        log.warning(f"_write_monthly: не удалось пересчитать сумму за день для {name}, пишу только последнюю смену: {ex}")

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
        _write(sheet, f"{col}{row_num}", [[day_total_decimal]])


_dashboard_lock = threading.Lock()


def sync_employee_names(dt):
    """Самопочинка: если сотрудника переименовали в листе Сотрудники (имя там
    не привязано жёстко ни к чему), его прошлые записи в Журнале и в листе
    текущего месяца остаются под старым именем — статистика «теряется»
    (см. инцидент 30.06.2026, Роман → Роман Рященко). TG ID в Сотрудники
    никогда не меняется, поэтому он — надёжный ключ: сверяем по нему и
    переименовываем старые записи автоматически, без ручного вмешательства."""
    try:
        emp_rows = _read("Сотрудники", "A2:E200")
        id_to_name = {row[0].strip(): row[1].strip() for row in emp_rows if len(row) >= 2 and row[0].strip()}

        journal_rows = _read("Журнал", "A2:J3000")
        renames = {}
        for i, row in enumerate(journal_rows):
            if len(row) < 10 or not str(row[9]).strip():
                continue
            tg_id    = str(row[9]).strip()
            old_name = row[1].strip()
            new_name = id_to_name.get(tg_id)
            if new_name and new_name != old_name:
                _write("Журнал", f"B{i+2}", [[new_name]])
                renames[old_name] = new_name

        if not renames:
            return

        sheet = f"{MONTHS_RU[dt.month]} {dt.year}"
        month_rows = _read(sheet, "A2:A100")
        month_names = {r[0].strip() for r in month_rows if r}
        for old_name, new_name in renames.items():
            if new_name in month_names:
                continue  # строка с новым именем уже есть — не сливаем, чтобы не потерять данные
            for i, row in enumerate(month_rows):
                if row and row[0].strip() == old_name:
                    _write(sheet, f"A{i+2}", [[new_name]])
                    log.info(f"sync_employee_names: «{old_name}» -> «{new_name}» в {sheet}")
                    break
    except Exception as ex:
        log.warning(f"sync_employee_names: сбой: {ex}")


_dashboard_dirty      = False
_dashboard_dirty_lock = threading.Lock()


def update_dashboard(dt):
    """Заполняет все код-зависимые блоки листа «Дашборд». Публичная точка
    входа — гарантирует, что событие, пришедшее ПОКА идёт пересборка, не
    потеряется молча (см. инцидент 30.06.2026: уход+приход почти подряд —
    второй вызов раньше просто пропускался, дашборд застревал на старых
    данных до следующего таймера). Если пересборка уже идёт — помечаем
    «нужно ещё раз» и текущий запуск перед выходом перечитает данные заново."""
    global _dashboard_dirty
    if not _dashboard_lock.acquire(blocking=False):
        with _dashboard_dirty_lock:
            _dashboard_dirty = True
        log.info("update_dashboard: пересборка уже идёт, запросили повтор после неё")
        return
    try:
        while True:
            with _dashboard_dirty_lock:
                _dashboard_dirty = False
            _rebuild_dashboard(dt)
            with _dashboard_dirty_lock:
                if not _dashboard_dirty:
                    break
                log.info("update_dashboard: за время пересборки пришло новое событие, повторяем")
    finally:
        _dashboard_lock.release()


def _rebuild_dashboard(dt):
    try:
        # Блок 1: кто сейчас на работе
        entries = get_open_entries_all()
        live_rows = [[e["name"], e.get("location", ""), e["arrival"]] for e in entries] \
            if entries else [["Сейчас никто не на работе", "", ""]]
        _write("Дашборд", "A6", live_rows)

        # Блок 4: GPS-аномалии (последние 20, новые сверху)
        try:
            gps_rows = _read("GPS лог", "A2:I5000")
        except Exception:
            gps_rows = []
        anomalies = [r for r in gps_rows if len(r) >= 9 and str(r[8]).strip()]
        anomalies = list(reversed(anomalies))[:20]
        anomaly_rows = [[r[0], r[1], r[3], r[4], r[8]] for r in anomalies] \
            if anomalies else [["Аномалий не найдено", "", "", "", ""]]
        _write("Дашборд", "A31", anomaly_rows)

        emp_rows = _read("Сотрудники", "A2:E200")
        employees = [r[1].strip() for r in emp_rows if len(r) >= 5 and r[4].strip().lower() == "да"]

        sheet = f"{MONTHS_RU[dt.month]} {dt.year}"
        days_in_month = calendar.monthrange(dt.year, dt.month)[1]
        month_rows = _read(sheet, "A2:AH100")
        name_to_row = {r[0].strip(): r for r in month_rows if r}

        journal_rows = _read("Журнал", "A2:I3000")
        month_prefix = f".{dt.month:02d}.{dt.year}"

        workdays_so_far = sum(
            1 for d in range(1, dt.day + 1)
            if date(dt.year, dt.month, d).weekday() < 5
        )

        summary_rows = []
        for name in employees:
            row = name_to_row.get(name, [])
            day_cells = row[1:1 + days_in_month] if len(row) > 1 else []
            total = row[1 + days_in_month] if len(row) > 1 + days_in_month else ""
            days_present = sum(1 for c in day_cells if str(c).strip())
            auto_closed = sum(
                1 for r in journal_rows
                if len(r) >= 8 and r[1].strip() == name and r[0].endswith(month_prefix) and r[7] == "⚠️ авто"
            )
            pct = round(days_present / workdays_so_far * 100) if workdays_so_far else 0
            summary_rows.append([name, total, days_present, auto_closed, f"{pct}%"])

        if summary_rows:
            _write("Дашборд", "A55", summary_rows)

        # Реестр уведомлений за сегодня — план (включая ещё не наступившие) + факт
        plan = get_today_notification_plan(dt)
        notif_rows = [
            [
                p["actual_time"] or p["planned_at"].strftime("%H:%M"),
                p["name"], p["type"],
                p["planned_at"].strftime("%H:%M"),
                p["status"],
            ]
            for p in plan
        ] if plan else [["—", "—", "—", "—", "на сегодня нет сотрудников с графиком"]]
        _write("Дашборд", "A82", notif_rows)
    except Exception as ex:
        log.warning(f"update_dashboard: сбой обновления дашборда: {ex}")
