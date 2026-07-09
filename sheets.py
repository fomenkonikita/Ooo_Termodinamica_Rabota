import os
import ssl
import time
import calendar
import logging
import threading
from datetime import datetime, timedelta, date

import httplib2
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build

log = logging.getLogger(__name__)

_RETRIABLE = (ssl.SSLError, ConnectionError, TimeoutError, OSError)

# Время последнего УСПЕШНОГО запроса к Google — читает watchdog в bot.py,
# чтобы понять, что бот завис (см. инцидент 30.06.2026, 45 минут простоя
# без единой попытки самовосстановления).
last_successful_api_call = time.time()

# _service (см. ниже) — один httplib2-коннекшн на весь процесс, а вызывают его
# одновременно несколько потоков (Telegram polling, APScheduler джобы, Flask).
# httplib2.Http() не потокобезопасен при конкурентном использовании одного
# соединения — отсюда `http.client.ResponseNotReady: Request-sent` и в
# худшем случае повреждение памяти на уровне C (`free(): corrupted unsorted
# chunks`, инцидент 02.07.2026, процесс падал каждые ~7 мин). Лок сериализует
# все вызовы к Google API процесса, как уже сделано для дашборда ниже.
_api_lock = threading.Lock()


def _execute(request, max_retries=3):
    """Выполняет запрос к Google API с повтором при транзитных сетевых сбоях
    (SSL-ошибки вида decryption failed/wrong version number, обрывы
    соединения — реальная причина повторявшихся «новый сотрудник не попал
    в табель» багов 30.06.2026: единичный сбой сети тихо терял запись,
    а наружный try/except это просто проглатывал). Любой вызов .execute()
    в этом файле должен идти через эту функцию."""
    global last_successful_api_call
    last_exc = None
    for attempt in range(max_retries):
        try:
            with _api_lock:
                result = request.execute()
            last_successful_api_call = time.time()
            return result
        except _RETRIABLE as ex:
            last_exc = ex
            if attempt < max_retries - 1:
                wait = 0.5 * (attempt + 1)
                log.warning(f"Google API сетевой сбой (попытка {attempt+1}/{max_retries}), retry через {wait}с: {ex}")
                time.sleep(wait)
    raise last_exc

CLIENT_ID      = os.environ.get("GOOGLE_CLIENT_ID", "")
CLIENT_SECRET  = os.environ.get("GOOGLE_CLIENT_SECRET", "")
REFRESH_TOKEN  = os.environ.get("GOOGLE_DRIVE_REFRESH_TOKEN", "")
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "1DZ_XQPAGbSn5aCKVqcBBRQ-X4sAItx23v-qJEw1dYbo")

MONTHS_RU = ["","Январь","Февраль","Март","Апрель","Май","Июнь",
             "Июль","Август","Сентябрь","Октябрь","Ноябрь","Декабрь"]

_service = None

def _svc():
    global _service
    if _service is not None:
        return _service
    with _api_lock:
        if _service is not None:  # другой поток мог успеть создать, пока ждали лок
            return _service
        creds = Credentials(
            token=None,
            refresh_token=REFRESH_TOKEN,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
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
        # httplib2.Http() без timeout может зависнуть НАВСЕГДА, если Google не
        # отвечает (не ошибка — просто тишина) — тогда retry в _execute() не
        # спасает, потому что .execute() сам никогда не возвращается и не
        # бросает исключение. См. инцидент 30.06.2026: бот завис на 45+ минут
        # без единой строчки в логах. 30с — явный таймаут вместо зависания.
        authed_http = AuthorizedHttp(creds, http=httplib2.Http(timeout=30))
        # static_discovery=True — использует discovery-документ из самой библиотеки
        # вместо скачивания+динамического парсинга с сети при каждом старте.
        # Экономит ~160МБ на процесс (найдено 02.07.2026 через tracemalloc при
        # диагностике OOM: googleapiclient/discovery.py давал 127+33МБ без этого).
        _service = build("sheets", "v4", http=authed_http, static_discovery=True)
    return _service


_spreadsheets_res = None
_values_res = None


def _ss():
    """Кэшированный .spreadsheets() — пересоздание на каждый вызов течёт
    памятью: googleapiclient динамически перегенерирует схему/docstring для
    КАЖДОГО нового Resource-объекта (~2.3МБ за вызов, не освобождается —
    подтверждено tracemalloc 02.07.2026: 15 одинаковых чтений без кэша = 34МБ,
    с кэшем = 0.01МБ). Настоящая причина сегодняшних OOM, не конкретная джоба."""
    global _spreadsheets_res
    if _spreadsheets_res is None:
        _spreadsheets_res = _svc().spreadsheets()
    return _spreadsheets_res


def _values():
    """Кэшированный .spreadsheets().values() — см. _ss()."""
    global _values_res
    if _values_res is None:
        _values_res = _ss().values()
    return _values_res


def _col(n):
    """0-indexed column number → letter(s). 0=A, 1=B, 26=AA ..."""
    result = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        result = chr(65 + r) + result
    return result

def _norm_date(s):
    """Нормализует дату из Google Sheets: '1.7.2026' → '01.07.2026'.
    USER_ENTERED заставляет Sheets хранить дату как serial и возвращать
    без ведущих нулей в зависимости от формата столбца."""
    try:
        parts = str(s).strip().split(".")
        if len(parts) == 3:
            return f"{int(parts[0]):02d}.{int(parts[1]):02d}.{parts[2].strip()}"
    except Exception:
        pass
    return str(s).strip()

def _read(sheet, range_):
    res = _execute(_values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{sheet}!{range_}"
    ))
    return res.get("values", [])

def _append(sheet, values):
    _execute(_values().append(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{sheet}!A1",
        valueInputOption="USER_ENTERED",
        body={"values": [values]}
    ))

def _write(sheet, range_, values):
    _execute(_values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{sheet}!{range_}",
        valueInputOption="USER_ENTERED",
        body={"values": values}
    ))

_sheet_id_cache: dict = {}

def _get_sheet_id(title):
    if title not in _sheet_id_cache:
        meta = _execute(_ss().get(spreadsheetId=SPREADSHEET_ID))
        for s in meta["sheets"]:
            _sheet_id_cache[s["properties"]["title"]] = s["properties"]["sheetId"]
    return _sheet_id_cache.get(title)


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

def update_employee_type(telegram_id, new_type):
    """Меняет тип уже зарегистрированного сотрудника (объект/водитель/сервис)
    через команду /тип, без повторной регистрации."""
    rows = _read("Сотрудники", "A2:A200")
    for i, row in enumerate(rows):
        if row and str(row[0]).strip() == str(telegram_id):
            _write("Сотрудники", f"C{i + 2}", [[new_type]])
            return True
    return False

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


def reconcile_employee_locations(dt, snapshot=None):
    """Самопочинка: Сотрудники!D (текущая локация) пишется фоновым потоком
    (run_background) при каждом приходе/уходе — без повтора при сбое. Если
    поток прервался (сетевой сбой, рестарт/OOM процесса до завершения записи —
    см. инцидент 02.07.2026), локация застревает неверной: либо пустая у
    реально пришедшего, либо вчерашняя у реально ушедшего. Сверяет каждого
    активного сотрудника с открытыми записями Журнала за сегодня и правит
    расхождение — пишет, только если значение реально отличается."""
    try:
        emp_rows = _read("Сотрудники", "A2:D200")
    except Exception as ex:
        log.warning(f"reconcile_employee_locations: чтение Сотрудники: {ex}")
        return []

    if snapshot is not None:
        today_str = snapshot["today_str"]
        open_today = [e for e in snapshot["open"] if e["date"] == today_str]
    else:
        open_today = [e for e in get_open_entries_all() if e["date"] == dt.strftime("%d.%m.%Y")]
    expected_by_id = {e["telegram_id"]: e.get("location", "") for e in open_today if e.get("telegram_id")}

    fixed = []
    for i, row in enumerate(emp_rows):
        if not row or not str(row[0]).strip():
            continue
        tg_id = str(row[0]).strip()
        current = row[3].strip() if len(row) > 3 else ""
        expected = expected_by_id.get(tg_id, "")
        if current != expected:
            try:
                _write("Сотрудники", f"D{i + 2}", [[expected]])
                fixed.append({"telegram_id": tg_id, "was": current, "now": expected})
            except Exception as ex:
                log.warning(f"reconcile_employee_locations: запись для {tg_id}: {ex}")
    return fixed


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
    rows = _read("Журнал", "A2:I500")
    for i, row in enumerate(rows):
        # Пропускаем строки где A не похоже на дату (TG ID и другой мусор)
        if str(row[0]).strip().count('.') != 2:
            continue
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
    meta     = _execute(_ss().get(spreadsheetId=SPREADSHEET_ID))
    existing = {s["properties"]["title"] for s in meta["sheets"]}
    if "Уведомления" in existing:
        return
    _execute(_ss().batchUpdate(
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


def get_closed_entries_today(dt, snapshot=None):
    """Закрытые записи за сегодня с их статусом (для сброса застрявшего зелёного)."""
    if snapshot is not None:
        return [{"name": e["name"], "status": e["status"]} for e in snapshot["closed_today"]]
    date_str = dt.strftime("%d.%m.%Y")
    try:
        rows = _read("Журнал", "A2:I500")
    except Exception:
        return []
    return [
        {"name": r[1].strip(), "status": r[7].strip() if len(r) >= 8 else ""}
        for r in rows
        if len(r) >= 6 and _norm_date(r[0]) == date_str and r[1].strip() and r[5].strip()
    ]


def read_today_snapshot(dt):
    """Один read(Журнал) + один read(Сотрудники) → все данные для job-функций.
    Передавать как snapshot= во все функции ниже, чтобы не перечитывать Журнал 5-6 раз за цикл."""
    today_str = dt.strftime("%d.%m.%Y")
    emp_rows = _read("Сотрудники", "A2:E200")
    emp_map  = {row[1].strip(): row[0].strip() for row in emp_rows if len(row) >= 2}
    active_names = [
        row[1].strip() for row in emp_rows
        if len(row) >= 5 and row[4].strip().lower() == "да" and row[1].strip()
    ]
    journal_rows = _read("Журнал", "A2:I500")
    open_entries = []
    closed_today = []
    all_today    = []
    for i, row in enumerate(journal_rows):
        if not row or str(row[0]).strip().count('.') != 2:
            continue
        if len(row) < 2 or not row[1].strip():
            continue
        row_date = _norm_date(row[0])
        name     = row[1].strip()
        is_open  = not (len(row) >= 6 and str(row[5]).strip())
        entry = {
            "row":           i + 2,
            "name":          name,
            "date":          row_date,
            "emp_type":      row[2].strip() if len(row) > 2 else "",
            "location":      row[3].strip() if len(row) > 3 else "",
            "arrival":       row[4].strip() if len(row) > 4 else "",
            "departure":     row[5].strip() if len(row) > 5 else "",
            "hours_str":     row[6].strip() if len(row) > 6 else "",
            "status":        row[7].strip() if len(row) > 7 else "",
            "last_activity": row[8].strip() if len(row) > 8 else "",
            "telegram_id":   emp_map.get(name, ""),
        }
        if is_open:
            open_entries.append(entry)
        if row_date == today_str:
            all_today.append(entry)
            if not is_open:
                closed_today.append(entry)
    return {
        "open":         open_entries,
        "closed_today": closed_today,
        "all_today":    all_today,
        "today_str":    today_str,
        "emp_map":      emp_map,
        "active_names": active_names,
    }



def get_today_notifications(dt):
    """Реестр уведомлений за сегодня — для админ-кнопки."""
    try:
        rows = _read("Уведомления", "A2:G500")
    except Exception:
        return []
    today = dt.strftime("%d.%m.%Y")
    return [r for r in rows if r and _norm_date(r[0]) == today]


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

    # Окна догонки должны совпадать с условиями в job_schedule_check (bot.py),
    # иначе статус на дашборде разойдётся с тем, что бот реально делает.
    # "до начала смены": planned_at = начало-10мин, шлёт пока minutes_late<=130
    # "до конца смены": planned_at = конец-30мин, шлёт в [-30; 45] minutes_late
    catchup_window = {"до начала смены": (None, 130), "до конца смены": (-30, 45)}

    for p in plan:
        key = (p["name"], p["type"])
        minutes_late = (dt - p["planned_at"]).total_seconds() / 60
        lo, hi = catchup_window.get(p["type"], (None, 120))
        if key in sent_map:
            p["actual_time"], p["status"] = sent_map[key]
        elif p["planned_at"] > dt:
            p["actual_time"], p["status"] = None, "запланировано"
        elif (lo is None or minutes_late >= lo) and minutes_late <= hi:
            # В пределах догоняющего окна job_schedule_check — ещё не
            # «пропущено», просто ждём ближайший запуск джобы (≤5 мин)
            p["actual_time"], p["status"] = None, "ожидает отправки"
        else:
            p["actual_time"], p["status"] = None, "ПРОПУЩЕНО"

    plan.sort(key=lambda p: p["planned_at"])
    return plan


def has_closed_entry_today(name, dt):
    """Есть ли у сотрудника уже ЗАВЕРШЁННАЯ (приход+уход) запись сегодня —
    признак того, что повторный приход может быть случайным/тестовым (см.
    инцидент с риском «фантомных» часов при двойной отметке за день)."""
    date_str = dt.strftime("%d.%m.%Y")
    rows = _read("Журнал", "A2:I500")
    for row in rows:
        if len(row) >= 6 and _norm_date(row[0]) == date_str and row[1].strip() == name and str(row[5]).strip():
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
    """Создаёт строку в месячном листе если её нет (для новых сотрудников).
    Часы и цвет — живые формулы/условное форматирование, ничего не пишут сюда."""
    try:
        _ensure_monthly_sheet(f"{MONTHS_RU[dt.month]} {dt.year}", dt.year, dt.month)
        _ensure_employee_row(name, dt)
    except Exception as ex:
        log.warning(f"update_monthly_on_arrival: {name}: {ex}")


def _month_formula_templates(year, month, days_in_month):
    """Шаблоны формул дня/хелпера месячного листа с плейсхолдером {row}.
    Общие для новой строки одного сотрудника (_ensure_employee_row) и для
    предзаполнения всех сотрудников при создании месяца (_ensure_monthly_sheet) —
    раньше предзаполнение писало пустые ячейки без формул вообще (см. фикс
    10.07.2026 в _ensure_monthly_sheet)."""
    # "Прогул" (03.07.2026, сам отметил "Сегодня выходной") — приоритет над
    # часами: даже если что-то есть в Журнале, явная декларация побеждает.
    # Нет отработанных часов → пусто, не 0 (просьба пользователя 10.07.2026).
    day_formulas = [
        f'=IFERROR(IF(COUNTIFS(Прогулы!$A$2:$A$500;DATE({year};{month};{day});'
        f'Прогулы!$B$2:$B$500;$A{{row}})>0;"Прогул";'
        f'IF(SUMIFS(Журнал!$G$2:$G$500;Журнал!$B$2:$B$500;$A{{row}};'
        f'Журнал!$A$2:$A$500;DATE({year};{month};{day}))=0;"";'
        f'SUMIFS(Журнал!$G$2:$G$500;Журнал!$B$2:$B$500;$A{{row}};'
        f'Журнал!$A$2:$A$500;DATE({year};{month};{day}))));"")'
        for day in range(1, days_in_month + 1)
    ]
    helper_formulas = [
        f'=IF(COUNTIFS(Прогулы!$A$2:$A$500;DATE({year};{month};{day});'
        f'Прогулы!$B$2:$B$500;$A{{row}})>0;"прогул";'
        f'IF(AND(DATE({year};{month};{day})=TODAY();'
        f'COUNTIFS(Журнал!$A$2:$A$500;DATE({year};{month};{day});'
        f'Журнал!$B$2:$B$500;$A{{row}};Журнал!$F$2:$F$500;"")>0);"open";'
        f'IF(COUNTIFS(Журнал!$A$2:$A$500;DATE({year};{month};{day});'
        f'Журнал!$B$2:$B$500;$A{{row}};Журнал!$H$2:$H$500;"⚠️ авто")>0;"auto";"")))'
        for day in range(1, days_in_month + 1)
    ]
    return day_formulas, helper_formulas


def _employee_row_values(name, year, month, days_in_month, row_num):
    """Полная строка сотрудника месячного листа: имя + формулы дней + Итого +
    формулы хелпера, готовая для values.update/append."""
    day_formulas, helper_formulas = _month_formula_templates(year, month, days_in_month)
    last_day_col = _col(days_in_month)
    return (
        [name]
        + [f.format(row=row_num) for f in day_formulas]
        + [f"=SUM(B{row_num}:{last_day_col}{row_num})"]
        + [f.format(row=row_num) for f in helper_formulas]
    )


def _ensure_employee_row(name, dt):
    """Создаёт строку сотрудника в месячном листе если её нет — сразу с живыми
    формулами часов (SUMIFS от Журнала) и хелпер-ячейками для условного
    форматирования (миграция 02.07.2026), а не пустыми ячейками."""
    sheet = f"{MONTHS_RU[dt.month]} {dt.year}"
    rows  = _read(sheet, "A2:A100")
    for row in rows:
        if row and row[0].strip() == name:
            return  # уже есть
    days_in_month = calendar.monthrange(dt.year, dt.month)[1]
    year, month = dt.year, dt.month

    # Строка добавилась сразу за последней непустой — индекс уже знаем,
    # повторное чтение не нужно (было узким местом на новых сотрудниках)
    row_num = len(rows) + 2
    helper_start_col = days_in_month + 2  # индекс сразу после Итого (0-based: A=0)

    try:
        sheet_id = _get_sheet_id(sheet)
        if sheet_id is not None:
            _execute(_ss().batchUpdate(spreadsheetId=SPREADSHEET_ID, body={"requests": [{
                "updateSheetProperties": {
                    "properties": {"sheetId": sheet_id,
                                   "gridProperties": {"columnCount": helper_start_col + days_in_month + 5}},
                    "fields": "gridProperties.columnCount",
                }
            }]}))
    except Exception as ex:
        log.warning(f"_ensure_employee_row: расширить сетку листа: {ex}")

    row_values = _employee_row_values(name, year, month, days_in_month, row_num)
    _append(sheet, row_values)
    try:
        if sheet_id is not None:
            _execute(_ss().batchUpdate(spreadsheetId=SPREADSHEET_ID, body={"requests": [
                {
                    "updateDimensionProperties": {
                        "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                                  "startIndex": helper_start_col, "endIndex": helper_start_col + days_in_month},
                        "properties": {"hiddenByUser": True}, "fields": "hiddenByUser",
                    }
                },
                {
                    # [ч]:мм — те же формулы теперь хранят долю суток, не десятичные
                    # часы (иначе 5.6 показалось бы как ~134ч при этом формате)
                    "repeatCell": {
                        "range": {"sheetId": sheet_id,
                                  "startRowIndex": row_num - 1, "endRowIndex": row_num,
                                  "startColumnIndex": 1, "endColumnIndex": days_in_month + 2},
                        "cell": {"userEnteredFormat": {"numberFormat": {"type": "TIME", "pattern": "[h]:mm"}}},
                        "fields": "userEnteredFormat.numberFormat",
                    }
                },
            ]}))
    except Exception as ex:
        log.warning(f"_ensure_employee_row: скрыть хелпер-колонки/формат: {ex}")


def _hours_formula(row_num):
    """Формула для Журнал!G (Отработано): всегда актуальна при ручной правке
    прихода/ухода админом (просьба пользователя 03.07.2026). MAX(0;...) — защита
    от ухода раньше прихода при ошибочной ручной правке."""
    return f'=IF(OR($E{row_num}="";$F{row_num}="");"";MAX(0;$F{row_num}-$E{row_num}))'


def record_departure(name, dt, open_entry):
    arrival_str   = open_entry["arrival"]
    row_num       = open_entry["row"]
    departure_str = dt.strftime("%H:%M")

    # статический расчёт — только для ответа пользователю в чате;
    # в ячейку G пишется ФОРМУЛА (пересчитывается при ручной правке времени)
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

    _write("Журнал", f"F{row_num}:I{row_num}", [[departure_str, _hours_formula(row_num), "✅", ""]])
    return hours_str, hours_decimal


def update_last_activity(name, time_str):
    """Обновляет время последней активности (колонка I) в открытой записи Журнала."""
    rows = _read("Журнал", "A2:I500")
    for i, row in enumerate(rows):
        if len(row) >= 5 and row[1].strip() == name:
            if len(row) < 6 or not str(row[5]).strip():
                row_num = i + 2
                _write("Журнал", f"I{row_num}", [[time_str]])
                return

def reopen_entry(row_num, name, dt):
    """Снимает авто-закрытие: очищает Уход/Статус в Журнале.
    Отработано (G) — формула: при пустом уходе сама показывает пусто,
    при закрытии пересчитается. Часы и цвет дня в месячном листе — живые
    формулы, сами пересчитаются (миграция 02.07.2026)."""
    _write("Журнал", f"F{row_num}:I{row_num}", [["", _hours_formula(row_num), "⏳ продлено до 23:55", ""]])

def _ensure_absence_log_sheet():
    """Лист "Прогулы" — сырой факт "сотрудник сам отметил сегодня выходной/прогул".
    Append-only, как GPS лог. Месячный лист формулой красит день в этот статус,
    KPI/статус дашборда исключают такого сотрудника из "не отметились"/"не на месте"."""
    meta     = _execute(_ss().get(spreadsheetId=SPREADSHEET_ID))
    existing = {s["properties"]["title"] for s in meta["sheets"]}
    if "Прогулы" in existing:
        return
    _execute(_ss().batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={"requests": [{"addSheet": {"properties": {"title": "Прогулы"}}}]}
    ))
    _write("Прогулы", "A1", [["Дата", "Имя", "TG ID", "Время"]])


def record_self_declared_absence(telegram_id, name, dt):
    """Сотрудник сам нажал "Сегодня выходной" — сырой факт в лист Прогулы.
    Месячный лист/дашборд подхватят формулой, код тут больше ничего не красит."""
    _ensure_absence_log_sheet()
    _append("Прогулы", [dt.strftime("%d.%m.%Y"), name, str(telegram_id), dt.strftime("%H:%M")])


def has_declared_absence_today(name, dt):
    """Уже отмечал(а) сегодня выходной/прогул? (защита от повторного нажатия)."""
    today_str = dt.strftime("%d.%m.%Y")
    try:
        rows = _read("Прогулы", "A2:B500")
    except Exception:
        return False
    return any(len(r) >= 2 and _norm_date(r[0]) == today_str and r[1].strip() == name for r in rows)


def get_names_declared_absent_today(dt):
    """Множество имён, отметивших сегодня выходной/прогул — job_schedule_check
    больше не должен их дёргать напоминаниями и включать в "не отметились"."""
    today_str = dt.strftime("%d.%m.%Y")
    try:
        rows = _read("Прогулы", "A2:B500")
    except Exception:
        return set()
    return {r[1].strip() for r in rows if len(r) >= 2 and _norm_date(r[0]) == today_str and r[1].strip()}


def _ensure_gps_log_sheet():
    meta     = _execute(_ss().get(spreadsheetId=SPREADSHEET_ID))
    existing = {s["properties"]["title"] for s in meta["sheets"]}
    if "GPS лог" in existing:
        return
    _execute(_ss().batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={"requests": [{"addSheet": {"properties": {"title": "GPS лог"}}}]}
    ))
    _write("GPS лог", "A1", [["Дата", "Время", "TG ID", "Имя", "Объект", "Lat", "Lon", "Accuracy", "Подозрение"]])


def log_gps_and_check(telegram_id, name, location, lat, lon, accuracy, dt):
    """Логирует GPS-замер при отметке и возвращает список причин подозрения на подмену
    геолокации (пустой список = замер выглядит нормально). Не блокирует отметку —
    только сигнал для админа.

    03.07.2026: флаг "нет данных о точности GPS" убран — Telegram не передаёт
    horizontal_accuracy при отправке кнопкой, срабатывало на 100% отметок (шум).
    Вместо него: побитовый повтор координат N раз ПОДРЯД — живой GPS так себя
    не ведёт (последние знаки гуляют), повтор = кэш Wi-Fi-локации / точка на
    карте / подмена. 1-2 повтора допускаем (Wi-Fi-локация в помещении реально
    залипает), с 3-го подряд — алерт."""
    try:
        _ensure_gps_log_sheet()
        lat_r = round(lat, 6)
        lon_r = round(lon, 6)

        reasons = []
        rows = _read("GPS лог", "A2:H500")
        streak = 0  # сколько ПОСЛЕДНИХ подряд отметок этого сотрудника здесь совпали побитово
        for row in reversed(rows):
            if len(row) < 7:
                continue
            if str(row[2]).strip() != str(telegram_id) or str(row[4]).strip() != location:
                continue
            try:
                # Google возвращает "56,812755" (ru-локаль) — float() ждёт точку.
                # Из-за этого проверка повтора молча не работала до 03.07.2026.
                prev_lat = float(str(row[5]).replace(",", "."))
                prev_lon = float(str(row[6]).replace(",", "."))
            except Exception:
                break
            if round(prev_lat, 6) == lat_r and round(prev_lon, 6) == lon_r:
                streak += 1
            else:
                break
        if streak >= 2:
            reasons.append(f"координаты побитово повторяются {streak + 1}-й раз подряд")

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

def get_open_entries_all(snapshot=None):
    """Все незакрытые записи с Telegram ID сотрудника."""
    if snapshot is not None:
        return snapshot["open"]
    emp_rows = _read("Сотрудники", "A2:E200")
    emp_map = {row[1].strip(): row[0].strip() for row in emp_rows if len(row) >= 2}

    rows = _read("Журнал", "A2:I500")
    result = []
    for i, row in enumerate(rows):
        # Пропускаем строки где A не похоже на дату (TG ID и другой мусор)
        if str(row[0]).strip().count('.') != 2:
            continue
        if len(row) >= 5 and row[1].strip():
            if len(row) < 6 or not str(row[5]).strip():
                name = row[1].strip()
                result.append({
                    "row":           i + 2,
                    "name":          name,
                    "date":          _norm_date(row[0]),
                    "emp_type":      row[2].strip() if len(row) > 2 else "",
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

    rows = _read("Журнал", "A2:I500")
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
    """Закрывает запись автоматически, помечает ⚠️ авто в Журнале.
    Часы и красный цвет дня в месячном листе — живые формулы/условное
    форматирование, сами подхватят статус "⚠️ авто" (миграция 02.07.2026)."""
    departure_str = close_dt.strftime("%H:%M")
    try:
        arr = datetime.strptime(arrival_str, "%H:%M")
        dep = datetime.strptime(departure_str, "%H:%M")
        if dep < arr:
            # Уход раньше прихода = ошибка вызывающей логики (не ночная смена — бот
            # закрывает всё в 23:55, через полночь смены не живут). Инцидент 02.07.2026.
            log.warning(f"auto_close_entry: уход {departure_str} раньше прихода {arrival_str} "
                        f"({name}, строка {row_num}) — закрываю временем прихода, 0:00 часов")
            departure_str = arrival_str
    except Exception:
        pass

    # Отработано (G) — формула, пересчитывается при ручной правке времени
    _write("Журнал", f"F{row_num}:I{row_num}", [
        [departure_str, _hours_formula(row_num), "⚠️ авто", ""]
    ])

_known_sheets = set()  # кэш существующих листов — не дёргать метаданные на каждый чих

# Единый цвет "прогул/выходной по декларации" — дневная сетка месячного листа
# И статус в блоке "Сотрудники" дашборда (см. scratchpad add_absence_feature.py)
ABSENCE_COLOR = {"red": 0.72, "green": 0.11, "blue": 0.11}


def _day_color_cf_requests(sheet_id, days_in_month, start_index=0):
    """3 правила условного форматирования на дневную сетку месячного листа:
    прогул (тёмно-красный) > open (зелёный) > auto (красный). Формула
    top-left (B2) ссылается на СВОЮ хелпер-ячейку — Sheets сдвигает ссылку
    относительно для каждой ячейки диапазона (та же строка, колонка +
    days_in_month+1). start_index — куда вставлять (0 для чистого листа;
    >0 если на листе уже есть другие CF-правила, которые нельзя сдвигать)."""
    first_helper_col = _col(days_in_month + 2)  # 0-индекс колонки хелпера дня 1
    rng = {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": 100,
           "startColumnIndex": 1, "endColumnIndex": days_in_month + 1}

    def rule(formula, bg, idx):
        return {"addConditionalFormatRule": {
            "rule": {
                "ranges": [rng],
                "booleanRule": {
                    "condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": formula}]},
                    "format": {"backgroundColor": bg},
                },
            },
            "index": idx,
        }}

    return [
        rule(f'={first_helper_col}2="прогул"', ABSENCE_COLOR, start_index),
        rule(f'={first_helper_col}2="open"', {"red": 0.7, "green": 0.9, "blue": 0.7}, start_index + 1),
        rule(f'={first_helper_col}2="auto"', {"red": 1.0, "green": 0.4, "blue": 0.4}, start_index + 2),
    ]


def _ensure_monthly_sheet(sheet_name, year, month):
    """Создаёт месячный лист с заголовком и серыми выходными если его нет."""
    if sheet_name in _known_sheets:
        return
    meta     = _execute(_ss().get(spreadsheetId=SPREADSHEET_ID))
    existing = {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta["sheets"]}
    _known_sheets.update(existing.keys())
    if sheet_name in existing:
        return

    _execute(_ss().batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={"requests": [{"addSheet": {"properties": {"title": sheet_name}}}]}
    ))
    _known_sheets.add(sheet_name)

    days_in_month = calendar.monthrange(year, month)[1]
    header = ["Сотрудник"] + list(range(1, days_in_month + 1)) + ["Итого"]
    _write(sheet_name, "A1", [header])

    # Форматирование: выходные серые, заголовок жирный, freeze строка+колонка
    meta     = _execute(_ss().get(spreadsheetId=SPREADSHEET_ID))
    sheet_id = next(s["properties"]["sheetId"] for s in meta["sheets"]
                    if s["properties"]["title"] == sheet_name)
    requests = [
        # Закрепить строку 1 и колонку A (Имя)
        {"updateSheetProperties": {
            "properties": {
                "sheetId": sheet_id,
                "gridProperties": {"frozenRowCount": 1, "frozenColumnCount": 1},
            },
            "fields": "gridProperties.frozenRowCount,gridProperties.frozenColumnCount",
        }},
        # Заголовок: синий фон + белый жирный текст
        {"repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 0, "endRowIndex": 1,
                "startColumnIndex": 0, "endColumnIndex": days_in_month + 2,
            },
            "cell": {"userEnteredFormat": {
                "textFormat": {"bold": True,
                               "foregroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0}},
                "backgroundColor": {"red": 0.267, "green": 0.447, "blue": 0.769},
            }},
            "fields": "userEnteredFormat.textFormat,userEnteredFormat.backgroundColor",
        }},
        # Колонка A (имена): 160px
        {"updateDimensionProperties": {
            "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                      "startIndex": 0, "endIndex": 1},
            "properties": {"pixelSize": 160},
            "fields": "pixelSize",
        }},
        # Колонки дней + Итого: 36px каждая
        {"updateDimensionProperties": {
            "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                      "startIndex": 1, "endIndex": days_in_month + 2},
            "properties": {"pixelSize": 36},
            "fields": "pixelSize",
        }},
    ]
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
    # Раскраска дня по хелпер-ячейке (прогул/open/auto) — раньше добавлялась ТОЛЬКО
    # одноразовым scratch-скриптом при миграции 02.07.2026, для новых месяцев
    # (создаются автоматически 1-го числа) не применялась вообще. Теперь в коде.
    requests += _day_color_cf_requests(sheet_id, days_in_month)
    _execute(_ss().batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={"requests": requests}
    ))

    # Предзаполнить всех активных сотрудников ФОРМУЛАМИ (не пустыми ячейками!) —
    # раньше здесь писались просто пустые строки, а _ensure_employee_row не
    # трогает уже существующую по имени строку → на новом месяце SUMIFS/хелперы
    # для предзаполненных сотрудников не появлялись вообще, часы не считались
    # до ручной миграции. Нашёл и починил 10.07.2026 при работе над "0 → пусто".
    try:
        emp_rows = _read("Сотрудники", "A2:E200")
        active_names = [r[1].strip() for r in emp_rows
                        if len(r) >= 5 and r[4].strip().lower() == "да" and r[1].strip()]
        helper_start_col = days_in_month + 2  # индекс сразу после Итого (0-based: A=0)
        if active_names:
            _execute(_ss().batchUpdate(spreadsheetId=SPREADSHEET_ID, body={"requests": [{
                "updateSheetProperties": {
                    "properties": {"sheetId": sheet_id,
                                   "gridProperties": {"columnCount": helper_start_col + days_in_month + 5}},
                    "fields": "gridProperties.columnCount",
                }
            }]}))
        rows_data = [
            _employee_row_values(name, year, month, days_in_month, i + 2)
            for i, name in enumerate(active_names)
        ]
        if rows_data:
            _write(sheet_name, "A2", rows_data)
            _execute(_ss().batchUpdate(spreadsheetId=SPREADSHEET_ID, body={"requests": [
                {
                    "updateDimensionProperties": {
                        "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                                  "startIndex": helper_start_col, "endIndex": helper_start_col + days_in_month},
                        "properties": {"hiddenByUser": True}, "fields": "hiddenByUser",
                    }
                },
                {
                    "repeatCell": {
                        "range": {"sheetId": sheet_id,
                                  "startRowIndex": 1, "endRowIndex": 1 + len(rows_data),
                                  "startColumnIndex": 1, "endColumnIndex": days_in_month + 2},
                        "cell": {"userEnteredFormat": {"numberFormat": {"type": "TIME", "pattern": "[h]:mm"}}},
                        "fields": "userEnteredFormat.numberFormat",
                    }
                },
            ]}))
    except Exception as ex:
        log.warning(f"_ensure_monthly_sheet: предзаполнение сотрудников: {ex}")


def close_orphaned_entries(current_dt, snapshot=None):
    """Закрывает открытые записи:
    1. Прошлых дней — job_close_21 не сработал из-за краша/рестарта бота.
    2. Сегодняшние после 21:00 — бот перезапустился после 21:00 и cron пропустил.
    Пропускает записи со статусом «продлено» — их закроет job_hard_close в 23:55.
    После закрытия чистит локацию в Сотрудниках."""
    today_str = current_dt.strftime("%d.%m.%Y")
    past_21 = current_dt.hour >= 21
    entries = get_open_entries_all(snapshot=snapshot)
    closed = []
    for e in entries:
        is_past_day = e.get("date") != today_str
        is_overdue_today = (e.get("date") == today_str and past_21)
        if not is_past_day and not is_overdue_today:
            continue
        # Продлённые до 23:55 не трогаем — job_hard_close закроет их сам
        if "продлено" in str(e.get("status", "")).lower():
            continue
        try:
            entry_date = datetime.strptime(e["date"], "%d.%m.%Y")
            arr = datetime.strptime(e["arrival"], "%H:%M")
            if is_past_day:
                close_by_hours = entry_date.replace(hour=arr.hour, minute=arr.minute) + timedelta(hours=8)
                close_at_21 = entry_date.replace(hour=21, minute=0, second=0, microsecond=0)
                close_dt = min(close_by_hours, close_at_21)
            else:
                # Сегодня после 21:00 — закрываем как job_close_21.
                # НО: смена, начавшаяся ПОСЛЕ 21:00 — это не "пропущенный job_close_21",
                # а легитимная поздняя смена; её закроет job_hard_close в 23:55.
                # Без этой проверки любой отметившийся после 21:00 закрывался задним
                # числом в 21:00 (уход < прихода, инцидент 02.07.2026).
                if arr.hour >= 21:
                    continue
                close_at_21 = current_dt.replace(hour=21, minute=0, second=0, microsecond=0)
                close_by_hours = current_dt.replace(hour=arr.hour, minute=arr.minute, second=0, microsecond=0) + timedelta(hours=8)
                close_dt = min(close_at_21, close_by_hours)
            auto_close_entry(e["row"], e["name"], e["arrival"], close_dt)
            tg_id = e.get("telegram_id", "")
            if tg_id:
                try:
                    update_employee_location(tg_id, "")
                except Exception:
                    pass
            closed.append({
                "name": e["name"], "date": e["date"], "row": e["row"],
                "telegram_id": tg_id, "arrival": e.get("arrival", ""),
                "close_hhmm": close_dt.strftime("%H:%M"), "is_past_day": is_past_day,
            })
            log.info(f"Закрыта {'прошлого дня' if is_past_day else 'сегодня после 21'} запись: {e['name']} от {e['date']}")
        except Exception as ex:
            log.warning(f"close_orphaned_entries: ошибка для {e['name']} ({e.get('date')}): {ex}")
    return closed


def ensure_dashboard_employee_rows():
    """Самопочинка блоков "Сотрудники" и "Месячная сводка" на Дашборде: при появлении
    нового активного сотрудника вставляет строку ВНУТРЬ блока и перегенерирует
    построчные формулы (имя/объект/пришёл/статус и часы/дни/авто/% явки).

    Дашборд — живые формулы (KNOWLEDGE.md), но СТРУКТУРУ (по строке на сотрудника)
    формула изменить не может — это единственная структурная правка за кодом.
    Блоки ищутся по тексту заголовков в колонке B (не по номерам строк — они
    плывут при каждом расширении). Вставка ВНУТРЬ блока (перед последней строкой)
    растягивает условное форматирование и merges автоматически; formulas
    перегенерируются на ВСЕ строки блока (идемпотентно). Ничего не делает,
    если все активные уже в блоках."""
    emp_rows = _read("Сотрудники", "A2:E200")
    active = [str(r[1]).strip() for r in emp_rows
              if len(r) >= 5 and str(r[1]).strip() and str(r[4]).strip().lower() == "да"]
    if not active:
        return None

    col_b = _read("Дашборд", "B1:B100")
    hdr_emp = hdr_month = hdr_notif = None
    for i, row in enumerate(col_b):
        text = str(row[0]).strip() if row else ""
        if text.startswith("🟢"):
            hdr_emp = i + 1
        elif text.startswith("📊"):
            hdr_month = i + 1
        elif text.startswith("🔔"):
            hdr_notif = i + 1
    if not (hdr_emp and hdr_month and hdr_notif):
        log.warning("ensure_dashboard_employee_rows: заголовки блоков не найдены, пропуск")
        return None

    # данные блока: заголовок +2 (после шапки колонок) .. следующий заголовок -2 (1 строка-разделитель)
    b1_start, b1_end = hdr_emp + 2, hdr_month - 2
    m_start, m_end = hdr_month + 2, hdr_notif - 2
    cap1, cap_m = b1_end - b1_start + 1, m_end - m_start + 1
    n = len(active)

    b1_names = [str(r[0]).strip() if r else "" for r in
                _read("Дашборд", f"B{b1_start}:B{b1_end}")] if cap1 > 0 else []
    b1_names += [""] * (cap1 - len(b1_names))
    if n <= cap1 and b1_names[:n] == active and not any(b1_names[n:]):
        return None  # всё уже на месте

    dash_id = _get_sheet_id("Дашборд")
    requests = []
    # вставки ВНУТРЬ блоков, снизу вверх (сводка ниже блока сотрудников)
    add_m = max(0, n - cap_m)
    add_1 = max(0, n - cap1)
    if add_m:
        requests.append({"insertDimension": {
            "range": {"sheetId": dash_id, "dimension": "ROWS",
                      "startIndex": m_end - 1, "endIndex": m_end - 1 + add_m},
            "inheritFromBefore": True}})
    if add_1:
        requests.append({"insertDimension": {
            "range": {"sheetId": dash_id, "dimension": "ROWS",
                      "startIndex": b1_end - 1, "endIndex": b1_end - 1 + add_1},
            "inheritFromBefore": True}})
    if requests:
        _execute(_ss().batchUpdate(spreadsheetId=SPREADSHEET_ID, body={"requests": requests}))

    # позиции после вставок
    b1_end += add_1
    m_start += add_1
    m_end += add_1 + add_m
    cap1 += add_1
    cap_m += add_m

    # merge B:C на каждой строке данных (unmerge + MERGE_ROWS — идемпотентно)
    merge_reqs = []
    for start, end in ((b1_start, b1_end), (m_start, m_end)):
        merge_reqs.append({"unmergeCells": {"range": {
            "sheetId": dash_id, "startRowIndex": start - 1, "endRowIndex": end,
            "startColumnIndex": 1, "endColumnIndex": 3}}})
        merge_reqs.append({"mergeCells": {"range": {
            "sheetId": dash_id, "startRowIndex": start - 1, "endRowIndex": end,
            "startColumnIndex": 1, "endColumnIndex": 3}, "mergeType": "MERGE_ROWS"}})
    _execute(_ss().batchUpdate(spreadsheetId=SPREADSHEET_ID, body={"requests": merge_reqs}))

    # перегенерация формул на все строки обоих блоков
    tz_offset = int(os.environ.get("TZ_OFFSET", 5))
    now_local = datetime.utcnow() + timedelta(hours=tz_offset)
    year, month = now_local.year, now_local.month
    days_in_month = calendar.monthrange(year, month)[1]
    ms = f"{MONTHS_RU[month]} {year}"
    total_col = _col(days_in_month + 1)
    last_day = _col(days_in_month)
    next_y, next_m = (year, month + 1) if month < 12 else (year + 1, 1)

    V = {}
    for i in range(cap1):
        r = b1_start + i
        if i < n:
            nm = active[i].replace('"', '""')
            crit = (f"'Журнал'!$A$2:$A$500=TODAY();'Журнал'!$B$2:$B$500=\"{nm}\";"
                    f"'Журнал'!$F$2:$F$500=\"\"")
            V[f"B{r}"] = active[i]
            V[f"D{r}"] = ("=IFERROR(INDEX(FILTER("
                          "IF('Журнал'!$D$2:$D$500<>\"\";'Журнал'!$D$2:$D$500;'Журнал'!$C$2:$C$500);"
                          f"{crit});1);\"\")")
            V[f"F{r}"] = f"=IFERROR(INDEX(FILTER(TEXT('Журнал'!$E$2:$E$500;\"HH:mm\");{crit});1);\"\")"
            V[f"G{r}"] = (f"=IF(COUNTIFS('Журнал'!$A$2:$A$500;TODAY();'Журнал'!$B$2:$B$500;\"{nm}\";"
                          f"'Журнал'!$F$2:$F$500;\"\";'Журнал'!$H$2:$H$500;\"⏳ продлено до 23:55\")>0;"
                          f"\"Продлил смену\";"
                          f"IF(COUNTIFS('Журнал'!$A$2:$A$500;TODAY();'Журнал'!$B$2:$B$500;\"{nm}\";"
                          f"'Журнал'!$F$2:$F$500;\"\")>0;\"На месте\";\"Не на месте\"))")
        else:
            for c in "BDFG":
                V[f"{c}{r}"] = ""
    for i in range(cap_m):
        r = m_start + i
        if i < n:
            nm = active[i].replace('"', '""')
            V[f"B{r}"] = active[i]
            V[f"D{r}"] = f"=IFERROR(VLOOKUP(\"{nm}\";'{ms}'!$A$2:${total_col}$50;{days_in_month + 2};0);0)"
            V[f"E{r}"] = (f"=SUMPRODUCT(('{ms}'!$A$2:$A$50=\"{nm}\")*(N('{ms}'!$B$2:${last_day}$50)>0))"
                          f"+IF(AND("
                          f"COUNTIFS('Журнал'!$A$2:$A$500;TODAY();'Журнал'!$B$2:$B$500;\"{nm}\";'Журнал'!$F$2:$F$500;\"\")>0;"
                          f"SUMIFS('Журнал'!$G$2:$G$500;'Журнал'!$B$2:$B$500;\"{nm}\";'Журнал'!$A$2:$A$500;TODAY())=0"
                          f");1;0)")
            V[f"F{r}"] = (f"=COUNTIFS('Журнал'!$B$2:$B$500;\"{nm}\";"
                          f"'Журнал'!$A$2:$A$500;\">=\"&DATE({year};{month};1);"
                          f"'Журнал'!$A$2:$A$500;\"<\"&DATE({next_y};{next_m};1);"
                          f"'Журнал'!$H$2:$H$500;\"⚠️ авто\")")
            # Знаменатель НЕ считает сегодня, если сотрудник ещё вообще не отмечался
            # сегодня (просьба пользователя 10.07.2026: "я был во все рабочие дни",
            # хотя сегодняшний день ещё не начался для него) — как только появится
            # любая запись в Журнале за сегодня, день сразу входит в знаменатель.
            V[f"G{r}"] = (
                f"=IFERROR(E{r}/(NETWORKDAYS(DATE({year};{month};1);TODAY())"
                f"-IF(AND(NETWORKDAYS(TODAY();TODAY())=1;"
                f"COUNTIFS('Журнал'!$A$2:$A$500;TODAY();'Журнал'!$B$2:$B$500;\"{nm}\")=0);1;0));0)"
            )
        else:
            for c in "BDEFG":
                V[f"{c}{r}"] = ""

    data = [{"range": f"Дашборд!{a1}", "values": [[v]]} for a1, v in V.items()]
    _execute(_values().batchUpdate(spreadsheetId=SPREADSHEET_ID, body={
        "valueInputOption": "USER_ENTERED", "data": data}))
    log.info(f"ensure_dashboard_employee_rows: {n} сотрудников, вставлено строк: "
             f"блок1={add_1}, сводка={add_m}, перезаписано ячеек={len(data)}")
    return {"employees": n, "added_block1": add_1, "added_month": add_m}


_header_styled_months: set = set()    # кэш: не перекрашивать заголовок дважды


def _apply_monthly_header_style(sheet_name, year, month):
    """Приводит заголовок месячного листа к единому стилю: 'Сотрудник', синий фон,
    белый жирный текст. Нужна для листов созданных старым кодом (до 01.07.2026)."""
    if sheet_name in _header_styled_months:
        return
    try:
        days_in_month = calendar.monthrange(year, month)[1]
        vals = _read(sheet_name, "A1:A1")
        if vals and vals[0] and vals[0][0] == "Сотрудник":
            # Заголовок уже правильный — только красим
            pass
        else:
            header = ["Сотрудник"] + list(range(1, days_in_month + 1)) + ["Итого"]
            _write(sheet_name, "A1", [header])

        sheet_id = _get_sheet_id(sheet_name)
        if sheet_id is None:
            return
        _execute(_ss().batchUpdate(
            spreadsheetId=SPREADSHEET_ID,
            body={"requests": [
                # Синий заголовок
                {"repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": 0, "endRowIndex": 1,
                        "startColumnIndex": 0, "endColumnIndex": days_in_month + 2,
                    },
                    "cell": {"userEnteredFormat": {
                        "textFormat": {"bold": True,
                                       "foregroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0}},
                        "backgroundColor": {"red": 0.267, "green": 0.447, "blue": 0.769},
                    }},
                    "fields": "userEnteredFormat.textFormat,userEnteredFormat.backgroundColor",
                }},
                # Колонка A (имена): 160px
                {"updateDimensionProperties": {
                    "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                              "startIndex": 0, "endIndex": 1},
                    "properties": {"pixelSize": 160},
                    "fields": "pixelSize",
                }},
                # Колонки дней + Итого: 36px каждая
                {"updateDimensionProperties": {
                    "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                              "startIndex": 1, "endIndex": days_in_month + 2},
                    "properties": {"pixelSize": 36},
                    "fields": "pixelSize",
                }},
                # Freeze: первая строка + первая колонка (Июнь создан до этого кода)
                {"updateSheetProperties": {
                    "properties": {
                        "sheetId": sheet_id,
                        "gridProperties": {"frozenRowCount": 1, "frozenColumnCount": 1},
                    },
                    "fields": "gridProperties.frozenRowCount,gridProperties.frozenColumnCount",
                }},
                # Итого колонка: жирный + голубой фон
                {"repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": 1, "endRowIndex": 100,
                        "startColumnIndex": days_in_month + 1,
                        "endColumnIndex": days_in_month + 2,
                    },
                    "cell": {"userEnteredFormat": {
                        "textFormat": {"bold": True},
                        "backgroundColor": {"red": 0.85, "green": 0.9, "blue": 1.0},
                    }},
                    "fields": "userEnteredFormat.textFormat.bold,userEnteredFormat.backgroundColor",
                }},
            ]}
        ))
        _header_styled_months.add(sheet_name)
        log.info(f"_apply_monthly_header_style: стиль применён для {sheet_name}")
    except Exception as ex:
        log.warning(f"_apply_monthly_header_style: {sheet_name}: {ex}")


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

        journal_rows = _read("Журнал", "A2:J500")
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


def setup_spreadsheet():
    """Однократная настройка таблицы при старте бота:
    — заголовки строки 1 в Журнале
    — скрытие колонок I и J (служебные: Активность, TG ID)
    — удаление row groups с листа Дашборд (кнопки «+» появлялись от старого кода)
    """
    try:
        current = _read("Журнал", "A1:J1")
        if not current or not current[0] or current[0][0] != "Дата":
            _write("Журнал", "A1:J1", [["Дата", "Имя", "Тип", "Объект", "Приход",
                                         "Уход", "Отработано", "Статус", "Активность", "TG ID"]])
            log.info("setup_spreadsheet: заголовки Журнала записаны")

        journal_id   = _get_sheet_id("Журнал")
        dashboard_id = _get_sheet_id("Дашборд")
        requests = []

        if journal_id is not None:
            requests.append({"updateDimensionProperties": {
                "range": {"sheetId": journal_id, "dimension": "COLUMNS",
                          "startIndex": 8, "endIndex": 10},
                "properties": {"hiddenByUser": True},
                "fields": "hiddenByUser",
            }})
            # G (Отработано) хранит долю суток (Уход-Приход) — без явного
            # формата Sheets показывает десятичное число (6,5), а не длительность
            # (6:30), просьба пользователя 10.07.2026. Idempotent — безопасно
            # применять на каждом старте, чинит и старые строки тоже.
            requests.append({"repeatCell": {
                "range": {"sheetId": journal_id,
                          "startRowIndex": 1, "endRowIndex": 5000,
                          "startColumnIndex": 6, "endColumnIndex": 7},
                "cell": {"userEnteredFormat": {"numberFormat": {"type": "TIME", "pattern": "[h]:mm"}}},
                "fields": "userEnteredFormat.numberFormat",
            }})

        if dashboard_id is not None:
            requests.append({"deleteDimensionGroup": {
                "range": {"sheetId": dashboard_id, "dimension": "ROWS",
                          "startIndex": 0, "endIndex": 1000},
            }})

        if requests:
            _execute(_ss().batchUpdate(
                spreadsheetId=SPREADSHEET_ID,
                body={"requests": requests},
            ))
            log.info("setup_spreadsheet: batchUpdate выполнен")

        # Применить единый стиль к текущему и прошлому месяцу
        # (Июнь создан до добавления freeze/Итого-стиля, нужно перекрасить)
        tz_offset = int(os.environ.get("TZ_OFFSET", 5))
        now_local = datetime.utcnow() + timedelta(hours=tz_offset)
        for _dt in [now_local, now_local.replace(day=1) - timedelta(days=1)]:
            _header_styled_months.discard(f"{MONTHS_RU[_dt.month]} {_dt.year}")
            _apply_monthly_header_style(f"{MONTHS_RU[_dt.month]} {_dt.year}", _dt.year, _dt.month)
    except Exception as ex:
        log.warning(f"setup_spreadsheet: {ex}")
