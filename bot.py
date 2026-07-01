import os
import logging
import threading
from math import radians, cos, sin, asin, sqrt
from datetime import datetime, timedelta

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# Убираем системный прокси
for _k in ("ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
    os.environ.pop(_k, None)

log.info("Importing telebot...")
import telebot
from telebot import types
from apscheduler.schedulers.background import BackgroundScheduler

log.info("Importing sheets...")
try:
    import sheets
    log.info("sheets imported OK")
except Exception as _e:
    log.critical(f"FAILED to import sheets: {_e}", exc_info=True)
    raise

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
if not BOT_TOKEN:
    log.critical("BOT_TOKEN env var not set!")
    raise RuntimeError("BOT_TOKEN is required")
log.info(f"BOT_TOKEN present: {BOT_TOKEN[:8]}...")
TZ_OFFSET = int(os.environ.get("TZ_OFFSET", 5))
ADMIN_IDS = {224397927, 1988448060}  # Никита Фоменко, Александр Лоцманов


class _BotExceptionHandler(telebot.ExceptionHandler):
    """Глобальный перехватчик: ошибка в одном хендлере не должна ронять весь процесс
    (см. инцидент 30.06.2026 — бот падал и перезапускался из-за единичной ошибки
    Telegram API в одном callback-хендлере, теряя на это несколько минут)."""
    def handle(self, exception):
        log.warning(f"Необработанное исключение в хендлере: {exception}")
        return True  # помечаем как обработанное, чтобы polling не падал


bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML", exception_handler=_BotExceptionHandler())

# Состояния: {user_id: {"step": str, "data": dict}}
_state = {}

# Трекер отправленных уведомлений по графику: "Имя_ДД.ММ.ГГГГ_r/l"
_schedule_notified: set = set()


def safe_answer_callback(call_id, text=None):
    try:
        bot.answer_callback_query(call_id, text) if text else bot.answer_callback_query(call_id)
    except Exception as ex:
        log.warning(f"answer_callback_query failed: {ex}")


def safe_clear_markup(chat_id, message_id):
    try:
        bot.edit_message_reply_markup(chat_id, message_id, reply_markup=None)
    except Exception as ex:
        log.warning(f"edit_message_reply_markup failed: {ex}")


def run_background(func, *args, **kwargs):
    """Запускает небоевую часть (месячный лист, локация, GPS-проверка) в фоновом
    потоке, чтобы ответ пользователю не ждал 8-10 последовательных запросов к
    Google Sheets API (см. инцидент 30.06.2026 — отметка «висла» на новых людях)."""
    def _runner():
        try:
            func(*args, **kwargs)
        except Exception as ex:
            log.warning(f"Фоновая задача {func.__name__} упала: {ex}")
    threading.Thread(target=_runner, daemon=True).start()


def now():
    return datetime.utcnow() + timedelta(hours=TZ_OFFSET)


def _parse_hm(time_str):
    """'17:00' → {'hour': 17, 'minute': 0, 'second': 0, 'microsecond': 0}"""
    t = datetime.strptime(time_str, "%H:%M")
    return {"hour": t.hour, "minute": t.minute, "second": 0, "microsecond": 0}


def haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    a = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
    return 2 * R * asin(sqrt(a))


def check_gps_and_alert(uid, emp_name, location, lat, lon, accuracy, dt):
    """Логирует GPS-замер и шлёт админам алерт при подозрении на подмену координат.
    Не блокирует отметку — только сигнал для проверки вручную."""
    reasons = sheets.log_gps_and_check(uid, emp_name, location, lat, lon, accuracy, dt)
    if not reasons:
        return
    text = (f"⚠️ <b>Подозрение на подмену GPS</b>\n"
            f"Сотрудник: {emp_name}\nОбъект: {location}\n"
            f"Время: {dt.strftime('%H:%M')}\n" + "\n".join(f"• {r}" for r in reasons))
    for admin_id in ADMIN_IDS:
        try:
            bot.send_message(admin_id, text)
        except Exception as ex:
            log.warning(f"GPS alert failed: {ex}")


def main_kb(emp_type, is_admin=False):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    if emp_type == "объект":
        kb.add("✅ Пришёл", "🚪 Ушёл")
    else:
        kb.add("🚗 Начал смену", "🏁 Закончил смену")
        kb.add("📍 Отправить точку")
    if is_admin:
        kb.add("📊 Статус")
        kb.add("📋 Уведомления")
    return kb


def location_kb():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add(types.KeyboardButton("📍 Поделиться геолокацией", request_location=True))
    return kb


def locations_kb(locations):
    kb = types.InlineKeyboardMarkup(row_width=1)
    for loc in locations:
        kb.add(types.InlineKeyboardButton(loc, callback_data=f"loc:{loc}"))
    return kb


def type_kb():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("🏗 На объекте", callback_data="type:объект"),
    )
    kb.add(
        types.InlineKeyboardButton("🚗 Водитель", callback_data="type:водитель"),
        types.InlineKeyboardButton("🔧 Сервис",   callback_data="type:сервис"),
    )
    return kb


def type_label(emp_type):
    return {"объект": "На объекте", "водитель": "Водитель", "сервис": "Сервис"}.get(emp_type, emp_type)


def change_type_kb():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("🏗 На объекте", callback_data="changetype:объект"),
    )
    kb.add(
        types.InlineKeyboardButton("🚗 Водитель", callback_data="changetype:водитель"),
        types.InlineKeyboardButton("🔧 Сервис",   callback_data="changetype:сервис"),
    )
    return kb


@bot.message_handler(commands=["тип"])
def cmd_change_type(message):
    uid = message.from_user.id
    emp = sheets.get_employee(str(uid))
    if not emp:
        bot.send_message(message.chat.id, "❌ Вы не зарегистрированы. Напишите /start")
        return
    if sheets.find_open_entry(emp["name"]):
        bot.send_message(message.chat.id, "⚠️ Сначала закройте текущую смену (отметьте «Ушёл»/«Закончил смену»), потом меняйте тип.")
        return
    bot.send_message(message.chat.id,
        f"Сейчас вы: <b>{type_label(emp['type'])}</b>.\nВыберите новый тип:",
        reply_markup=change_type_kb())


@bot.callback_query_handler(func=lambda c: c.data.startswith("changetype:"))
def cb_change_type(call):
    uid = call.from_user.id
    new_type = call.data.split(":", 1)[1]
    emp = sheets.get_employee(str(uid))
    safe_clear_markup(call.message.chat.id, call.message.message_id)
    if not emp:
        safe_answer_callback(call.id, "❌ Не зарегистрированы")
        return
    sheets.update_employee_type(str(uid), new_type)
    bot.send_message(call.message.chat.id,
        f"✅ Тип изменён на <b>{type_label(new_type)}</b>.\nВыберите действие:",
        reply_markup=main_kb(new_type, is_admin=(uid in ADMIN_IDS)))
    safe_answer_callback(call.id)


def still_working_kb(row_num):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("✅ Я ещё на работе", callback_data=f"still_working:{row_num}"))
    return kb


# ── /статус ────────────────────────────────────────────────────────────────────

@bot.message_handler(func=lambda m: m.text == "📊 Статус")
def btn_status(message):
    if message.from_user.id not in ADMIN_IDS:
        return
    _show_status(message.chat.id)


@bot.message_handler(commands=["статус", "status"])
def cmd_status(message):
    if message.from_user.id not in ADMIN_IDS:
        return
    _show_status(message.chat.id)


def _show_status(chat_id):
    entries = sheets.get_open_entries_all()
    dt = now()

    if not entries:
        bot.send_message(chat_id,
            f"📊 <b>Статус на {dt.strftime('%d.%m %H:%M')}</b>\n\nСейчас никто не на работе.")
        return

    lines = [f"📊 <b>Статус на {dt.strftime('%d.%m %H:%M')}</b>\n\n✅ <b>На работе:</b>"]
    for e in entries:
        try:
            arr_time = datetime.strptime(e["arrival"], "%H:%M")
            arr_dt   = dt.replace(hour=arr_time.hour, minute=arr_time.minute, second=0, microsecond=0)
            if arr_dt > dt:
                arr_dt -= timedelta(days=1)
            minutes  = int((dt - arr_dt).total_seconds() // 60)
            h, m     = divmod(minutes, 60)
            duration = f"{h}ч {m}мин" if h else f"{m}мин"
        except Exception:
            duration = "—"
        loc_part = f" · {e['location']}" if e.get("location") else ""
        lines.append(f"  • {e['name']} — с {e['arrival']} ({duration}){loc_part}")

    bot.send_message(chat_id, "\n".join(lines))


@bot.message_handler(func=lambda m: m.text == "📋 Уведомления")
def btn_notifications(message):
    if message.from_user.id not in ADMIN_IDS:
        return
    dt = now()
    plan = sheets.get_today_notification_plan(dt)

    lines = [f"📋 <b>Уведомления за {dt.strftime('%d.%m')}</b>\n"]
    if not plan:
        lines.append("На сегодня нет сотрудников с заданным графиком.")
    else:
        icons = {"отправлено": "✅", "запланировано": "⏳", "ПРОПУЩЕНО": "❗"}
        for p in plan:
            icon = icons.get(p["status"], "❌")
            time_str = p["actual_time"] or p["planned_at"].strftime("%H:%M")
            tilde = "" if p["actual_time"] else "~"
            lines.append(f"{icon} {tilde}{time_str} — {p['name']} ({p['type']}) — {p['status']}")

    # Сводка: кто не отметился сегодня вообще
    emps = sheets.get_all_employees_with_schedule()
    today = dt.strftime("%d.%m.%Y")
    not_marked = []
    for emp in emps:
        if not emp["schedule"]:
            continue
        work_days = emp.get("work_days")
        if work_days and dt.weekday() not in work_days:
            continue
        if not sheets.find_open_entry(emp["name"]) and not sheets.has_closed_entry_today(emp["name"], dt):
            not_marked.append(emp["name"])

    if not_marked:
        lines.append("\n⚠️ <b>Не отмечались сегодня вообще:</b>")
        for name in not_marked:
            lines.append(f"  • {name}")

    bot.send_message(message.chat.id, "\n".join(lines))


# ── /start ─────────────────────────────────────────────────────────────────────

@bot.message_handler(commands=["start"])
def cmd_start(message):
    uid = message.from_user.id
    emp = sheets.get_employee(str(uid))
    if emp:
        _state.pop(uid, None)
        bot.send_message(message.chat.id,
            f"Привет, <b>{emp['name']}</b>! 👋",
            reply_markup=main_kb(emp["type"], is_admin=(uid in ADMIN_IDS)))
    else:
        _state[uid] = {"step": "reg_name", "data": {}}
        bot.send_message(message.chat.id,
            "Привет! Вы ещё не зарегистрированы.\n\nКак вас зовут? (Имя и фамилия)")


# ── Регистрация ────────────────────────────────────────────────────────────────

@bot.message_handler(func=lambda m: _state.get(m.from_user.id, {}).get("step") == "reg_name")
def reg_name(message):
    uid = message.from_user.id

    # Защита от дубля: если сотрудник уже активен, не даём процессу регистрации
    # принять текст кнопки меню за имя (см. инцидент 30.06.2026)
    emp = sheets.get_employee(str(uid))
    if emp:
        _state.pop(uid, None)
        bot.send_message(message.chat.id,
            f"Вы уже зарегистрированы как <b>{emp['name']}</b>.",
            reply_markup=main_kb(emp["type"], is_admin=(uid in ADMIN_IDS)))
        return

    name = message.text.strip()
    if len(name) < 2:
        bot.send_message(message.chat.id, "Введите имя (минимум 2 символа):")
        return
    _state[uid]["data"]["name"] = name
    _state[uid]["step"] = "reg_type"
    bot.send_message(message.chat.id,
        f"Отлично, <b>{name}</b>! Вы работаете на объекте или водитель?",
        reply_markup=type_kb())


@bot.callback_query_handler(func=lambda c: c.data.startswith("type:"))
def reg_type(call):
    uid = call.from_user.id
    if _state.get(uid, {}).get("step") != "reg_type":
        safe_answer_callback(call.id)
        return

    emp_type = call.data.split(":", 1)[1]
    name     = _state[uid]["data"]["name"]

    # Повторная защита от дубля прямо перед записью (см. инцидент 30.06.2026)
    existing = sheets.get_employee(str(uid))
    if existing:
        _state.pop(uid, None)
        safe_clear_markup(call.message.chat.id, call.message.message_id)
        bot.send_message(call.message.chat.id,
            f"Вы уже зарегистрированы как <b>{existing['name']}</b>.",
            reply_markup=main_kb(existing["type"], is_admin=(uid in ADMIN_IDS)))
        safe_answer_callback(call.id)
        return

    sheets.register_employee(str(uid), name, emp_type)
    _state.pop(uid, None)

    safe_clear_markup(call.message.chat.id, call.message.message_id)
    bot.send_message(call.message.chat.id,
        f"✅ Зарегистрированы как <b>{name}</b> ({type_label(emp_type)})!\n\nВыберите действие:",
        reply_markup=main_kb(emp_type, is_admin=(uid in ADMIN_IDS)))
    safe_answer_callback(call.id)


# ── Выбор объекта ──────────────────────────────────────────────────────────────

def ask_location(chat_id, uid, action):
    locs = sheets.get_all_locations()
    if not locs:
        bot.send_message(chat_id, "❌ Нет объектов в таблице. Добавьте их в лист «Локации».")
        return
    _state[uid] = {"step": "location_select", "data": {"action": action}}
    bot.send_message(chat_id, "На каком объекте?", reply_markup=locations_kb(locs))


@bot.callback_query_handler(func=lambda c: c.data.startswith("loc:"))
def on_location_selected(call):
    uid   = call.from_user.id
    state = _state.get(uid, {})
    if state.get("step") != "location_select":
        safe_answer_callback(call.id)
        return

    loc_name = call.data.split(":", 1)[1]
    action   = state["data"]["action"]
    _state[uid] = {"step": f"geo_{action}", "data": {"location": loc_name}}

    safe_clear_markup(call.message.chat.id, call.message.message_id)
    bot.send_message(call.message.chat.id,
        f"📍 Объект: <b>{loc_name}</b>\nТеперь поделитесь геолокацией:",
        reply_markup=location_kb())
    safe_answer_callback(call.id)


# ── «Я ещё на работе» ─────────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data.startswith("still_working:"))
def cb_still_working(call):
    uid = call.from_user.id
    emp = sheets.get_employee(str(uid))
    if not emp:
        safe_answer_callback(call.id, "❌ Не зарегистрированы")
        return

    row_num = int(call.data.split(":")[1])
    _state[uid] = {"step": "geo_still_working", "data": {"row_num": row_num}}

    safe_clear_markup(call.message.chat.id, call.message.message_id)
    bot.send_message(call.message.chat.id,
        "📍 Отправьте геолокацию для подтверждения:",
        reply_markup=location_kb())
    safe_answer_callback(call.id)


# ── Кнопки сотрудника на объекте ───────────────────────────────────────────────

@bot.message_handler(func=lambda m: m.text == "✅ Пришёл")
def btn_arrived(message):
    emp = sheets.get_employee(str(message.from_user.id))
    if not emp or emp["type"] != "объект":
        return
    if sheets.find_open_entry(emp["name"]):
        bot.send_message(message.chat.id, "⚠️ Вы уже отметились как пришедший.\nСначала нажмите «Ушёл».")
        return
    # Уже был полный приход+уход сегодня — скорее всего случайное повторное
    # нажатие, переспрашиваем, чтобы не насчитать фантомные часы до 21:00
    if sheets.has_closed_entry_today(emp["name"], now()):
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("✅ Да, начинаю новую смену", callback_data="confirm_second_arrival:yes"),
               types.InlineKeyboardButton("❌ Нет, это случайно", callback_data="confirm_second_arrival:no"))
        bot.send_message(message.chat.id,
            "⚠️ Вы уже отмечали сегодня приход и уход.\nТочно начинаете новую смену?",
            reply_markup=kb)
        return
    ask_location(message.chat.id, message.from_user.id, "arrival")


@bot.callback_query_handler(func=lambda c: c.data.startswith("confirm_second_arrival:"))
def cb_confirm_second_arrival(call):
    answer = call.data.split(":", 1)[1]
    safe_clear_markup(call.message.chat.id, call.message.message_id)
    safe_answer_callback(call.id)
    if answer == "yes":
        ask_location(call.message.chat.id, call.from_user.id, "arrival")
    else:
        bot.send_message(call.message.chat.id, "Хорошо, отметка не создана.")


@bot.message_handler(func=lambda m: m.text == "🚪 Ушёл")
def btn_left(message):
    emp = sheets.get_employee(str(message.from_user.id))
    if not emp or emp["type"] != "объект":
        return
    open_entry = sheets.find_open_entry(emp["name"])
    if not open_entry:
        bot.send_message(message.chat.id, "⚠️ Нет открытой отметки прихода.")
        return
    _state[message.from_user.id] = {"step": "geo_departure", "data": {"location": open_entry.get("location", "")}}
    bot.send_message(message.chat.id, "📍 Поделитесь геолокацией:", reply_markup=location_kb())


# ── Кнопки водителя ────────────────────────────────────────────────────────────

@bot.message_handler(func=lambda m: m.text == "🚗 Начал смену")
def btn_shift_start(message):
    emp = sheets.get_employee(str(message.from_user.id))
    if not emp or emp["type"] == "объект":
        return
    if sheets.find_open_entry(emp["name"]):
        bot.send_message(message.chat.id, "⚠️ Смена уже начата!")
        return
    # Водитель/сервис не привязаны к одному объекту (несколько локаций за
    # день) — без выбора объекта и без проверки радиуса, просто фиксируем
    # геолокацию начала смены.
    _state[message.from_user.id] = {"step": "geo_arrival", "data": {"location": ""}}
    bot.send_message(message.chat.id, "📍 Поделитесь геолокацией:", reply_markup=location_kb())


@bot.message_handler(func=lambda m: m.text == "🏁 Закончил смену")
def btn_shift_end(message):
    emp = sheets.get_employee(str(message.from_user.id))
    if not emp or emp["type"] == "объект":
        return
    open_entry = sheets.find_open_entry(emp["name"])
    if not open_entry:
        bot.send_message(message.chat.id, "⚠️ Смена не была начата!")
        return
    dt = now()
    worked, hours_decimal = sheets.record_departure(emp["name"], dt, open_entry)
    bot.send_message(message.chat.id,
        f"🏁 Смена завершена!\n🕒 {dt.strftime('%H:%M')}\n⏱ Отработано: <b>{worked}</b>",
        reply_markup=main_kb(emp["type"], is_admin=(message.from_user.id in ADMIN_IDS)))
    run_background(sheets.update_monthly_on_departure, emp["name"], dt, hours_decimal)
    run_background(sheets.update_employee_location, message.from_user.id, "")
    run_background(sheets.update_dashboard, dt)


@bot.message_handler(func=lambda m: m.text == "📍 Отправить точку")
def btn_waypoint(message):
    emp = sheets.get_employee(str(message.from_user.id))
    if not emp or emp["type"] == "объект":
        return
    _state[message.from_user.id] = {"step": "geo_waypoint", "data": {}}
    bot.send_message(message.chat.id, "📍 Поделитесь геолокацией:", reply_markup=location_kb())


# ── Геолокация ─────────────────────────────────────────────────────────────────

@bot.message_handler(content_types=["location"])
def handle_location(message):
    uid = message.from_user.id
    emp = sheets.get_employee(str(uid))
    if not emp:
        bot.send_message(message.chat.id, "❌ Вы не зарегистрированы. Напишите /start")
        return

    state    = _state.pop(uid, {})
    step     = state.get("step", "")
    data     = state.get("data", {})
    lat      = message.location.latitude
    lon      = message.location.longitude
    accuracy = message.location.horizontal_accuracy
    dt       = now()

    # Водитель — точка маршрута
    if step == "geo_waypoint":
        sheets.record_waypoint(emp["name"], lat, lon, dt)
        bot.send_message(message.chat.id,
            f"📍 Точка записана\n🕒 {dt.strftime('%H:%M')}\n<code>{lat:.5f}, {lon:.5f}</code>",
            reply_markup=main_kb(emp["type"], is_admin=(uid in ADMIN_IDS)))
        return

    # Подтверждение «Я ещё на работе»
    if step == "geo_still_working":
        row_num = data.get("row_num")
        if emp["type"] == "объект":
            entry    = sheets.get_entry_by_row(row_num)
            loc_name = entry.get("location", "") if entry else ""
            loc      = sheets.get_location(loc_name)
            if loc:
                dist = int(haversine(lat, lon, loc["lat"], loc["lon"]))
                if dist > loc["radius"]:
                    bot.send_message(message.chat.id,
                        f"❌ Вы не на объекте! ({dist}м от {loc_name}). Подтверждение отклонено.",
                        reply_markup=main_kb(emp["type"], is_admin=(uid in ADMIN_IDS)))
                    return
        sheets.reopen_entry(row_num, emp["name"], dt)
        bot.send_message(message.chat.id,
            f"✅ <b>Подтверждено! Вы на работе.</b>\n"
            f"🕒 {dt.strftime('%H:%M')}\n"
            f"⚠️ В 23:55 смена закроется автоматически.",
            reply_markup=main_kb(emp["type"], is_admin=(uid in ADMIN_IDS)))

        def _followup():
            sheets.update_last_activity(emp["name"], dt.strftime("%H:%M"))
            entry = sheets.get_entry_by_row(row_num)
            still_loc = entry.get("location", "") if entry else ""
            sheets.update_employee_location(uid, still_loc)
            check_gps_and_alert(uid, emp["name"], still_loc, lat, lon, accuracy, dt)
            sheets.update_dashboard(dt)
        run_background(_followup)
        return

    loc_name = data.get("location", "")

    if step == "geo_arrival":
        loc = sheets.get_location(loc_name)
        if loc:
            dist = int(haversine(lat, lon, loc["lat"], loc["lon"]))
            if dist > loc["radius"]:
                bot.send_message(message.chat.id,
                    f"❌ Вы не на объекте!\n📏 Расстояние: <b>{dist}м</b> (допустимо: {int(loc['radius'])}м)",
                    reply_markup=main_kb(emp["type"], is_admin=(uid in ADMIN_IDS)))
                return
            dist_msg = f"\n📏 До объекта: {dist}м"
        else:
            dist_msg = ""

        sheets.record_arrival(emp["name"], emp["type"], loc_name, dt, telegram_id=uid)
        icon = "✅" if emp["type"] == "объект" else "🚗"
        loc_line = f"📍 Объект: <b>{loc_name}</b>\n" if loc_name else ""
        bot.send_message(message.chat.id,
            f"{icon} {'Приход' if emp['type'] == 'объект' else 'Смена начата'}!\n{loc_line}🕒 {dt.strftime('%H:%M')}{dist_msg}",
            reply_markup=main_kb(emp["type"], is_admin=(uid in ADMIN_IDS)))
        run_background(sheets.update_monthly_on_arrival, emp["name"], dt)
        run_background(sheets.update_employee_location, uid, loc_name)
        run_background(check_gps_and_alert, uid, emp["name"], loc_name, lat, lon, accuracy, dt)
        run_background(sheets.update_dashboard, dt)

    elif step == "geo_departure":
        loc = sheets.get_location(loc_name)
        if loc:
            dist = int(haversine(lat, lon, loc["lat"], loc["lon"]))
            if dist > loc["radius"]:
                bot.send_message(message.chat.id,
                    f"❌ Вы не на объекте!\n📏 Расстояние: <b>{dist}м</b> (допустимо: {int(loc['radius'])}м)",
                    reply_markup=main_kb(emp["type"], is_admin=(uid in ADMIN_IDS)))
                return
            dist_msg = f"\n📏 До объекта: {dist}м"
        else:
            dist_msg = ""

        open_entry = sheets.find_open_entry(emp["name"])
        if not open_entry:
            bot.send_message(message.chat.id, "⚠️ Нет открытой отметки.", reply_markup=main_kb(emp["type"], is_admin=(uid in ADMIN_IDS)))
            return
        worked, hours_decimal = sheets.record_departure(emp["name"], dt, open_entry)
        bot.send_message(message.chat.id,
            f"🚪 Уход записан!\n🕒 {dt.strftime('%H:%M')}\n⏱ Отработано: <b>{worked}</b>{dist_msg}",
            reply_markup=main_kb(emp["type"], is_admin=(uid in ADMIN_IDS)))
        run_background(sheets.update_monthly_on_departure, emp["name"], dt, hours_decimal)
        run_background(sheets.update_employee_location, uid, "")
        run_background(check_gps_and_alert, uid, emp["name"], loc_name, lat, lon, accuracy, dt)
        run_background(sheets.update_dashboard, dt)

    else:
        bot.send_message(message.chat.id,
            "Используйте кнопки меню.", reply_markup=main_kb(emp["type"], is_admin=(uid in ADMIN_IDS)))


# ── Планировщик ───────────────────────────────────────────────────────────────

def job_schedule_check():
    """Каждые 5 минут: график по сотруднику — напоминания до начала/конца + сводка админу."""
    current   = now()
    today     = current.strftime("%d.%m.%Y")
    emps      = sheets.get_all_employees_with_schedule()
    late_list = []
    ok_list   = []

    for emp in emps:
        if not emp["schedule"] or not emp["telegram_id"]:
            continue
        work_days = emp.get("work_days")
        if work_days and current.weekday() not in work_days:
            continue

        name  = emp["name"]
        tg_id = emp["telegram_id"]

        try:
            sched_dt = current.replace(**_parse_hm(emp["schedule"]))
        except Exception:
            continue

        minutes_to_start = (sched_dt - current).total_seconds() / 60
        minutes_after_start = -minutes_to_start

        # За 10 мин до начала — напомнить отметиться. Окно расширено до 2ч
        # «вдогонку»: если бот был недоступен в плановое время (см. инцидент
        # 30.06.2026 — пять пропущенных подряд из-за нестабильности сети),
        # следующий же запуск джобы досылает напоминание с пометкой, а не
        # просто молча помечает «пропущено» навсегда.
        before_key = f"{name}_{today}_bs"
        if -120 <= minutes_to_start < 10 and before_key not in _schedule_notified:
            if not sheets.find_open_entry(name):
                _schedule_notified.add(before_key)
                late = minutes_to_start < 5
                text = "⏰ Через 10 минут начало рабочего дня!\nНе забудь поставить отметку «Пришёл»."
                if late:
                    text = "⏰ Не забудь поставить отметку «Пришёл» (напоминание задержалось из-за технического сбоя)."
                try:
                    bot.send_message(int(tg_id), text)
                    sheets.log_notification(name, "до начала смены", emp["schedule"],
                        "отправлено" if not late else "отправлено с опозданием", text, current)
                except Exception as ex:
                    log.warning(f"Before-start remind failed {tg_id}: {ex}")
                    sheets.log_notification(name, "до начала смены", emp["schedule"], f"ошибка: {ex}", text, current)

        # Через 10 мин после начала — собираем сводку (пришёл/не пришёл)
        after_key = f"{name}_{today}_as"
        if 10 <= minutes_after_start < 15 and after_key not in _schedule_notified:
            _schedule_notified.add(after_key)
            entry = sheets.find_open_entry(name)
            if entry:
                ok_list.append(emp)
            else:
                late_list.append(emp)

        # За 30 мин до конца — напомнить уйти (только если сейчас на работе)
        end_time_str = emp.get("end_time", "")
        if end_time_str:
            try:
                end_dt = current.replace(**_parse_hm(end_time_str))
            except Exception:
                end_dt = None
            if end_dt:
                minutes_to_end = (end_dt - current).total_seconds() / 60
                end_before_key = f"{name}_{today}_be"
                # Окно: от часа до конца смены и максимум 15 мин после неё.
                # Идеал — ровно за 30 мин (см. инцидент 30.06.2026 — частые
                # рестарты бота промахивались мимо узкого 5-минутного окна).
                if -15 <= minutes_to_end <= 60 and end_before_key not in _schedule_notified:
                    if sheets.find_open_entry(name):
                        _schedule_notified.add(end_before_key)
                        late = minutes_to_end < 25
                        text = "🏁 Не забудь поставить отметку «Ушёл»."
                        if late:
                            text = "🏁 Не забудь поставить отметку «Ушёл» (напоминание задержалось из-за технического сбоя)."
                        try:
                            bot.send_message(int(tg_id), text)
                            sheets.log_notification(name, "до конца смены", end_time_str,
                                "отправлено" if not late else "отправлено с опозданием", text, current)
                        except Exception as ex:
                            log.warning(f"Before-end remind failed {tg_id}: {ex}")
                            sheets.log_notification(name, "до конца смены", end_time_str, f"ошибка: {ex}", text, current)

    if late_list or ok_list:
        lines = [f"📋 <b>Статус на {current.strftime('%H:%M')}</b>\n"]
        if ok_list:
            lines.append("✅ <b>Отметились:</b>")
            for emp in ok_list:
                lines.append(f"  • {emp['name']}")
        if late_list:
            lines.append("\n❌ <b>Не отметились (10+ мин опоздание):</b>")
            for emp in late_list:
                lines.append(f"  • {emp['name']} (график: {emp['schedule']})")

        text = "\n".join(lines)
        for admin_id in ADMIN_IDS:
            try:
                bot.send_message(admin_id, text)
            except Exception as ex:
                log.warning(f"Admin schedule report failed: {ex}")


def job_resync_green():
    """Каждые 5 мин: подсвечивает зелёным всех, кто сейчас на смене.
    Подстраховка от тихих сбоев _mark_monthly_present при самом приходе
    (см. инцидент 30.06.2026 — Андрющенко был на смене, но без подсветки)."""
    entries = sheets.get_open_entries_all()
    dt = now()
    for e in entries:
        try:
            sheets._mark_monthly_present(e["name"], dt)
        except Exception as ex:
            log.warning(f"job_resync_green: сбой для {e['name']}: {ex}")


def job_update_dashboard():
    """Каждые 5 мин: закрывает orphaned записи прошлых дней (краш бота =
    job_close_21 не сработал), синхронизирует переименования по TG ID,
    пересчитывает Итого-за-сегодня, обновляет дашборд.
    Каждый шаг изолирован — сбой в одном не отменяет остальные."""
    dt = now()
    try:
        sheets.close_orphaned_entries(dt)
    except Exception as ex:
        log.warning(f"job_update_dashboard: close_orphaned_entries: {ex}")
    try:
        sheets.sync_employee_names(dt)
    except Exception as ex:
        log.warning(f"job_update_dashboard: sync_employee_names: {ex}")
    try:
        sheets.resync_today_totals(dt)
    except Exception as ex:
        log.warning(f"job_update_dashboard: resync_today_totals: {ex}")
    try:
        sheets.update_dashboard(dt)
    except Exception as ex:
        log.warning(f"job_update_dashboard: update_dashboard: {ex}")


def job_close_21():
    """21:00 — авто-закрытие ВСЕХ открытых смен (макс. 8ч) + кнопка продления."""
    entries = sheets.get_open_entries_all()
    current = now()
    for e in entries:
        tg_id = e.get("telegram_id", "")
        try:
            arr_time = datetime.strptime(e["arrival"], "%H:%M")
            arr_dt   = current.replace(hour=arr_time.hour, minute=arr_time.minute, second=0, microsecond=0)
            if arr_dt > current:
                arr_dt -= timedelta(days=1)

            close_dt = min(current, arr_dt + timedelta(hours=8))
            sheets.auto_close_entry(e["row"], e["name"], e["arrival"], close_dt)

            if tg_id:
                sheets.update_employee_location(tg_id, "")
                bot.send_message(int(tg_id),
                    f"⚠️ <b>Авто-закрытие смены</b>\n"
                    f"🕒 Время закрытия: {close_dt.strftime('%H:%M')} (8 часов с {e['arrival']})\n"
                    f"📊 Отметка поставлена в таблице.\n\n"
                    f"Если вы ещё на работе — нажмите кнопку и подтвердите геолокацией.\n"
                    f"В 23:55 смена закроется в любом случае.",
                    reply_markup=still_working_kb(e["row"]))
        except Exception as ex:
            log.warning(f"21:00 close error for {e.get('name', '?')}: {ex}")

    if entries:
        run_background(sheets.update_dashboard, current)


def job_remind_2350():
    """23:50 — напомнить тем кто продлил смену поставить отметку самому."""
    entries = sheets.get_extended_entries()
    for e in entries:
        tg_id = e.get("telegram_id", "")
        if not tg_id:
            continue
        try:
            bot.send_message(int(tg_id),
                f"⏰ <b>Через 5 минут смена закроется автоматически!</b>\n"
                f"Поставь отметку «Ушёл» с точным временем — иначе закроется в 23:55.")
        except Exception as ex:
            log.warning(f"23:50 remind failed {tg_id}: {ex}")


def job_hard_close():
    """23:55 — жёсткое закрытие всех оставшихся открытых смен."""
    entries = sheets.get_open_entries_all()
    current = now()
    for e in entries:
        try:
            # Используем последнюю активность если есть, иначе текущее время (23:55)
            last_act = e.get("last_activity", "")
            if last_act:
                try:
                    la_time  = datetime.strptime(last_act, "%H:%M")
                    close_dt = current.replace(hour=la_time.hour, minute=la_time.minute, second=0, microsecond=0)
                    if close_dt > current:
                        close_dt -= timedelta(days=1)
                except Exception:
                    close_dt = current
            else:
                close_dt = current

            sheets.auto_close_entry(e["row"], e["name"], e["arrival"], close_dt)

            tg_id = e.get("telegram_id", "")
            if tg_id:
                sheets.update_employee_location(tg_id, "")
                try:
                    emp = sheets.get_employee(tg_id)
                    bot.send_message(int(tg_id),
                        f"🔒 <b>Смена закрыта автоматически в {close_dt.strftime('%H:%M')}</b>\n"
                        f"📊 Ячейка отмечена красным в таблице.\n"
                        f"Если время неверное — обратитесь к руководителю.",
                        reply_markup=main_kb(emp["type"]) if emp else None)
                except Exception:
                    pass
        except Exception as ex:
            log.warning(f"Hard close error for {e.get('name', '?')}: {ex}")

    if entries:
        run_background(sheets.update_dashboard, current)


def job_reconcile():
    """00:10 — сверка вчерашнего дня: Журнал vs лист месяца (пропуски) +
    целостность каждой записи Журнала (Отработано vs реальная разница
    уход-приход, см. инцидент 30.06.2026)."""
    current = now()
    yesterday = current - timedelta(days=1)
    lines = []

    # Закрываем записи из прошлых дней которые остались открытыми (краш бота)
    try:
        orphaned = sheets.close_orphaned_entries(current)
    except Exception as ex:
        log.warning(f"close_orphaned_entries error: {ex}")
        orphaned = []
    if orphaned:
        lines.append("🔒 <b>Закрыты незакрытые записи прошлых дней:</b>")
        for o in orphaned:
            lines.append(f"  • {o['name']} ({o['date']})")

    try:
        fixed_gaps = sheets.reconcile_day(yesterday)
    except Exception as ex:
        log.warning(f"Reconcile error: {ex}")
        fixed_gaps = []
    if fixed_gaps:
        lines.append(f"🔧 <b>Автосверка за {yesterday.strftime('%d.%m.%Y')}</b> — дозаполнены пропуски:")
        for f in fixed_gaps:
            lines.append(f"  • {f['name']} — {f['hours']}ч")

    try:
        fixed_integrity = sheets.verify_journal_integrity()
    except Exception as ex:
        log.warning(f"Integrity check error: {ex}")
        fixed_integrity = []
    if fixed_integrity:
        lines.append("⚠️ <b>Найдены неверные часы в Журнале — исправлено:</b>")
        for f in fixed_integrity:
            lines.append(f"  • {f['name']} ({f['date']}): {f['was']} → {f['now']}")

    if lines:
        text = "\n".join(lines)
        for admin_id in ADMIN_IDS:
            try:
                bot.send_message(admin_id, text)
            except Exception as ex:
                log.warning(f"Reconcile alert failed: {ex}")
        run_background(sheets.update_dashboard, now())


WATCHDOG_TIMEOUT_SEC = 1800  # 30 минут без успешного API вызова = зависший


def run_watchdog():
    """Сторож: если 30 минут подряд НИ ОДИН запрос к Google API не прошёл
    успешно, бот сам себя завершает — Render поднимает новый процесс.
    Таймаут 30 мин (не 10) чтобы rolling deploy успел пройти health check."""
    import time as _time
    _time.sleep(120)  # дать время на старт и первый API вызов
    while True:
        _time.sleep(60)
        stale_for = _time.time() - sheets.last_successful_api_call
        if stale_for > WATCHDOG_TIMEOUT_SEC:
            log.error(f"WATCHDOG: нет успешных запросов к Google {int(stale_for)}с — перезапуск")
            os._exit(1)


def run_health_server():
    from flask import Flask
    import time as _time
    app = Flask(__name__)

    @app.route("/")
    def health():
        # Возвращаем 200 пока процесс жив — Render не должен убивать бота
        # из-за временных проблем с Google API. /status — для диагностики.
        return "OK", 200

    @app.route("/status")
    def api_status():
        stale_for = _time.time() - sheets.last_successful_api_call
        if stale_for > WATCHDOG_TIMEOUT_SEC:
            return f"STALE: no Google API call in {int(stale_for)}s", 503
        return f"OK: last API call {int(stale_for)}s ago", 200

    @app.route("/_restart")
    def force_restart():
        """Быстрый перезапуск процесса — Render поднимает новый за ~3 сек."""
        import threading as _threading
        def _do_exit():
            _time.sleep(0.5)
            os._exit(0)
        _threading.Thread(target=_do_exit, daemon=True).start()
        return "Restarting...", 200

    @app.route("/privacy")
    def privacy():
        return """<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"><title>Политика конфиденциальности</title></head>
<body style="font-family: sans-serif; max-width: 700px; margin: 40px auto; line-height: 1.6;">
<h2>Политика конфиденциальности</h2>
<p>Бот учёта рабочего времени ООО «Термодинамика» собирает и обрабатывает:</p>
<ul>
<li>Telegram ID и имя пользователя</li>
<li>Геолокацию (широта/долгота) в момент отметки прихода/ухода</li>
<li>Время и дату отметок</li>
</ul>
<p>Данные используются исключительно для учёта рабочего времени и контроля присутствия
сотрудников на объектах ООО «Термодинамика». Хранятся в защищённой Google Таблице
с ограниченным доступом, только для уполномоченных сотрудников компании.
Третьим лицам не передаются.</p>
<p>По вопросам обращайтесь к администратору бота.</p>
</body></html>""", 200

    port = int(os.environ.get("PORT", 8080))
    log.info(f"Starting Flask on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False, threaded=True)


def job_keepalive():
    """Пингует себя чтобы Render не усыплял сервис через 15 мин бездействия."""
    import requests as _req
    url = os.environ.get("RENDER_EXTERNAL_URL", "https://attendance-bot-sdjc.onrender.com")
    try:
        _req.get(url + "/", timeout=10)
    except Exception:
        pass


@bot.message_handler(commands=["дашборд", "dashboard"])
def cmd_dashboard(message):
    if message.from_user.id not in ADMIN_IDS:
        return
    msg = bot.reply_to(message, "🔄 Обновляю дашборд...")
    try:
        job_update_dashboard()
        bot.edit_message_text("✅ Дашборд обновлён", message.chat.id, msg.message_id)
    except Exception as ex:
        bot.edit_message_text(f"⚠️ Ошибка: {ex}", message.chat.id, msg.message_id)


@bot.message_handler(commands=["restart"])
def cmd_restart(message):
    if message.from_user.id not in ADMIN_IDS:
        return
    bot.reply_to(message, "♻️ Перезапускаю бота...")
    import threading as _t
    import time as _time2
    def _do():
        _time2.sleep(1)
        os._exit(0)
    _t.Thread(target=_do, daemon=True).start()


if __name__ == "__main__":
    import threading
    threading.Thread(target=run_health_server, daemon=True).start()  # Flask как daemon — оригинальная архитектура
    threading.Thread(target=run_watchdog, daemon=True).start()

    scheduler = BackgroundScheduler()
    scheduler.add_job(job_schedule_check,   "interval", minutes=5)   # каждые 5 мин: до/после начала, до конца
    scheduler.add_job(job_resync_green,     "interval", minutes=5)   # каждые 5 мин: подсветка зелёным тех, кто на смене
    scheduler.add_job(job_update_dashboard, "interval", minutes=5)   # каждые 5 мин: все код-зависимые блоки Дашборда
    scheduler.add_job(job_close_21,         "cron",     hour=21, minute=0)   # 21:00 авто-закрытие всех + кнопка продления
    scheduler.add_job(job_remind_2350,      "cron",     hour=23, minute=50)  # 23:50 напоминание продлившим
    scheduler.add_job(job_hard_close,       "cron",     hour=23, minute=55)  # 23:55 жёсткое закрытие
    scheduler.add_job(job_reconcile,        "cron",     hour=0,  minute=10)  # 00:10 сверка прошедшего дня
    scheduler.add_job(job_keepalive,        "interval", minutes=10)          # каждые 10 мин: не даём Render усыплять
    scheduler.start()

    log.info("Attendance bot started (scheduler active)")
    bot.infinity_polling(timeout=30, long_polling_timeout=20)  # polling на main thread — оригинальная архитектура
