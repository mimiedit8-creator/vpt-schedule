#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Бот расписания ГБПОУ «Волжский политехнический техникум» (ВПТ), корпус №2, группа РКЛ-3.

Источник: официальное облачное хранилище техникума (Nextcloud, volpt.ru -> «Расписание занятий»).
Работает без внешних зависимостей (только стандартная библиотека Python 3).

Команды:
  python3 vpt_bot.py today            — расписание на сегодня (в stdout)
  python3 vpt_bot.py tomorrow         — расписание на завтра (в stdout)
  python3 vpt_bot.py week             — расписание на ближайшие 7 дней (в stdout)
  python3 vpt_bot.py send tomorrow    — отправить расписание на завтра в Telegram
  python3 vpt_bot.py send today       — отправить расписание на сегодня в Telegram
  python3 vpt_bot.py poll             — режим бота: отвечать на команды /today /tomorrow /week
  python3 vpt_bot.py changes          — проверить, менялся ли файл на завтра после последней отправки

Переменные окружения:
  TG_TOKEN   — токен бота Telegram (от BotFather)
  TG_CHAT_ID — id чата для отправки (узнаётся автоматически при /start)
"""

import base64
import datetime as dt
import hashlib
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

# ──────────────────────────── Настройки ────────────────────────────

SHARE_TOKEN = "bsycQtwSRF93676"  # публичный токен папки «Расписание занятий» на nextcloud.volpt.ru
BUILDING_DIR = "2 КОРПУС РАСПИСАНИЕ"
GROUP = "РКЛ-3"          # группа (ищется строка вида "1-26 РКЛ-3")
BUILDING_NO = "2"
COLLEGE = "ВПТ"

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

# Расписание звонков ВПТ (официальный файл «расписание звонков.pdf», корпус 2, все формы обучения)
BELL_WEEKDAYS = {  # понедельник–пятница
    1: ("08:30", "10:00"),
    2: ("10:10", "11:40"),
    3: ("12:10", "13:40"),
    4: ("13:50", "15:20"),
    5: ("15:30", "17:00"),
    6: ("17:10", "18:40"),
    7: ("18:50", "20:20"),
}
BELL_SATURDAY = {  # суббота (сокращённые пары)
    1: ("08:30", "10:00"),
    2: ("10:10", "11:40"),
    3: ("11:50", "13:20"),
    4: ("13:30", "15:00"),
    5: ("15:10", "16:40"),
    6: ("16:50", "18:20"),
    7: ("18:30", "20:00"),
}

WEEKDAYS_RU = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
MONTHS_RU = ["", "января", "февраля", "марта", "апреля", "мая", "июня",
             "июля", "августа", "сентября", "октября", "ноября", "декабря"]

MSK = dt.timezone(dt.timedelta(hours=3))  # московское время (Volgograd = UTC+3)


def today_msk():
    return dt.datetime.now(MSK).date()

# ──────────────────────── Nextcloud (WebDAV) доступ ────────────────────────

WEBDAV = "https://nextcloud.volpt.ru/public.php/webdav/"


def _auth_headers():
    tok = base64.b64encode(f"{SHARE_TOKEN}:".encode()).decode()
    return {"Authorization": "Basic " + tok}


def http_get(url, timeout=90):
    req = urllib.request.Request(url, headers=_auth_headers())
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def propfind(path=""):
    """Список файлов/папок в публичной папке Nextcloud."""
    url = WEBDAV + urllib.parse.quote(path)
    req = urllib.request.Request(url, method="PROPFIND",
                                 headers={**_auth_headers(), "Depth": "1"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read().decode("utf-8", "replace")
    out = []
    for href in re.findall(r"<d:href>([^<]+)</d:href>", data):
        name = urllib.parse.unquote(href)
        name = name[len("/public.php/webdav/"):]
        if name and name != path.strip("/"):
            out.append(name)
    return out


def list_schedule_files():
    """Файлы папки корпуса: [(дата, имя файла)] — только корень папки, без архива."""
    entries = propfind(BUILDING_DIR + "/")
    result = []
    for name in entries:
        if name.endswith("/"):
            continue
        base = os.path.basename(name)
        if not base.lower().endswith(".xlsx"):
            continue
        d = parse_date_from_name(base)
        if d:
            result.append((d, base))
    result.sort()
    return result


def parse_date_from_name(name):
    """Дата из имени файла вида '17.11.2026 вторник 2 НЕДЕЛЯ.xlsx' (пробелы игнорируются)."""
    squeezed = name.replace(" ", "")
    m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})", squeezed)
    if not m:
        return None
    d, mo, y = (int(x) for x in m.groups())
    if y < 100:
        y += 2000
    try:
        return dt.date(y, mo, d)
    except ValueError:
        return None


def download_day_file(filename):
    url = WEBDAV + urllib.parse.quote(BUILDING_DIR + "/" + filename)
    return http_get(url)


# ──────────────────────────── Разбор XLSX ────────────────────────────

def parse_xlsx(data):
    """Минимальный парсер xlsx -> список строк (список значений-строк)."""
    rows = []
    with zipfile.ZipFile(__import__("io").BytesIO(data)) as z:
        # общие строки
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
            for si in root.findall(f"{ns}si"):
                text = "".join(t.text or "" for t in si.iter(f"{ns}t"))
                shared.append(text)
        # первая таблица
        sheet_name = next(n for n in z.namelist()
                          if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"))
        root = ET.fromstring(z.read(sheet_name))
        ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
        for row in root.iter(f"{ns}row"):
            vals = {}
            for c in row.findall(f"{ns}c"):
                ref = c.get("r", "")
                col = re.match(r"([A-Z]+)", ref)
                col_idx = 0
                if col:
                    for ch in col.group(1):
                        col_idx = col_idx * 26 + (ord(ch) - 64)
                col_idx -= 1
                t = c.get("t")
                v = c.find(f"{ns}v")
                is_node = c.find(f"{ns}is")
                if t == "s" and v is not None:
                    val = shared[int(v.text)]
                elif t == "inlineStr" and is_node is not None:
                    val = "".join(x.text or "" for x in is_node.iter(f"{ns}t"))
                elif v is not None:
                    val = v.text
                else:
                    val = ""
                vals[col_idx] = str(val).strip()
            rows.append([vals.get(i, "") for i in range(max(vals) + 1 if vals else 0)])
    return rows


def cell_str(v):
    return "" if v is None else str(v).strip()


def extract_group_pairs(rows, group):
    """Достаёт блок группы: [(№ пары, дисциплина, преподаватель, аудитория)]."""
    pairs = []
    in_group = False
    header_re = re.compile(r"^\d{1,2}\s*-\s*\d{2}\s+\S")  # "1-26 РКЛ-3"
    group_re = re.compile(rf"(^|\s){re.escape(group)}(\s|$)")
    for row in rows:
        cells = [cell_str(c) for c in (list(row) + ["", "", "", ""])[:4]]
        first = cells[0]
        if header_re.match(first):
            in_group = bool(group_re.search(first))
            continue
        if not in_group:
            continue
        num = first
        if not re.fullmatch(r"\d{1,2}", num):
            continue
        discipline, teacher, room = cells[1], cells[2], cells[3]
        # артефакты: Excel мог сохранить "2.11" как дату (серийный номер)
        if re.fullmatch(r"\d{5}(\.\d+)?", room) and float(room) > 40000:
            serial = float(room)
            d0 = dt.date(1899, 12, 30) + dt.timedelta(days=int(serial))
            room = d0.strftime("%d.%m")
        # "нет" = занятий нет
        if discipline.lower() in ("нет", "-", "—", "") and not teacher and not room:
            continue
        if discipline.lower() == "нет":
            continue
        pairs.append((int(num), discipline, teacher, room))
    return pairs


# ──────────────────────────── Формирование сообщения ────────────────────────────

def bell(pair_no, date):
    table = BELL_SATURDAY if date.weekday() == 5 else BELL_WEEKDAYS
    return table.get(pair_no)


def get_day_schedule(date):
    """Расписание группы на дату. Возвращает (пары|None, имя файла|None, ошибка)."""
    files = list_schedule_files()
    match = [f for d, f in files if d == date]
    if not match:
        return None, None, None
    fname = sorted(match)[-1]  # если несколько — берём последнее
    data = download_day_file(fname)
    rows = parse_xlsx(data)
    pairs = extract_group_pairs(rows, GROUP)
    return pairs, fname, None


def format_day_message(date, pairs, changed=False):
    today = today_msk()
    wd = WEEKDAYS_RU[date.weekday()]
    date_str = f"{date.day} {MONTHS_RU[date.month]}, {wd}"
    lines = []
    if changed:
        lines.append("⚠️ ВНИМАНИЕ: расписание изменилось после предыдущей отправки")
        lines.append("")
    lines += ["📚 РАСПИСАНИЕ НА " + ("ЗАВТРА" if date > today_msk() else "СЕГОДНЯ"),
              "",
              f"📅 Дата: {date_str}",
              f"🏫 {COLLEGE} | Корпус №{BUILDING_NO}",
              f"👨‍🎓 Группа: {GROUP}",
              "",
              "━━━━━━━━━━━━━━"]
    if pairs is None:
        lines += [""]
        if date.weekday() == 6:  # воскресенье
            lines += ["🎉 Завтра воскресенье — занятий нет. Отличных выходных!"]
        else:
            lines += [f"ℹ️ Расписание на {date_str} пока не опубликовано в облаке техникума.",
                      "Сообщение придёт автоматически, как только оно появится."]
        lines += [""]
    elif not pairs:
        lines += ["",
                  "🎉 Занятий нет — отдыхай!",
                  ""]
    else:
        for num, disc, teacher, room in pairs:
            b = bell(num, date)
            time_str = f"{b[0]}–{b[1]}" if b else "время уточнить"
            lines += ["",
                      f"{num} пара",
                      f"🕘 {time_str}",
                      f"📖 {disc if disc else 'дисциплина не указана'}",
                      f"👨‍🏫 {teacher if teacher else 'преподаватель не указан'}",
                      f"📍 {room if room else 'кабинет не указан'}"]
        first = bell(pairs[0][0], date)
        lines += ["", "━━━━━━━━━━━━━━", "",
                  f"⏰ Начало занятий: {first[0] if first else '—'}",
                  f"📚 Всего пар: {len(pairs)}",
                  "",
                  "Хорошего учебного дня!"]
    return "\n".join(lines)


def format_week_message():
    today = today_msk()
    lines = ["🗓 РАСПИСАНИЕ НА НЕДЕЛЮ",
             f"🏫 {COLLEGE} | Корпус №{BUILDING_NO} | 👨‍🎓 {GROUP}",
             "━━━━━━━━━━━━━━"]
    found_any = False
    for i in range(7):
        d = today + dt.timedelta(days=i)
        pairs, fname, _ = get_day_schedule(d)
        wd = WEEKDAYS_RU[d.weekday()]
        marker = " (сегодня)" if i == 0 else (" (завтра)" if i == 1 else "")
        lines.append("")
        lines.append(f"📅 {d.day} {MONTHS_RU[d.month]}, {wd}{marker}")
        if pairs is None:
            lines.append("   ℹ️ расписание пока не опубликовано")
        elif not pairs:
            lines.append("   🎉 занятий нет")
        else:
            found_any = True
            for num, disc, teacher, room in pairs:
                b = bell(num, d)
                t = f"{b[0]}–{b[1]}" if b else "?"
                lines.append(f"   {num}п {t} · {disc} · {room or 'каб. ?'}"
                             + (f" · {teacher}" if teacher else ""))
    if not found_any:
        lines.append("")
        lines.append("ℹ️ На ближайшие дни файлов расписания в облаке техникума нет.")
    return "\n".join(lines)


# ──────────────────────────── Состояние / защита от дублей ────────────────────────────

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


def msg_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# ──────────────────────────── Telegram ────────────────────────────

def tg_api(token, method, payload=None):
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(payload or {}).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"ok": False, "description": e.read().decode("utf-8", "replace")}


def tg_send(token, chat_id, text):
    res = tg_api(token, "sendMessage", {
        "chat_id": chat_id, "text": text,
        "disable_web_page_preview": True,
    })
    return res.get("ok", False), res.get("description", "")


def resolve_chat_id(token):
    """Пытается найти chat_id последнего /start (нужен один раз)."""
    res = tg_api(token, "getUpdates", {"limit": 10})
    if not res.get("ok"):
        return None, res.get("description", "ошибка")
    chat_id = None
    for upd in reversed(res.get("result", [])):
        msg = upd.get("message") or {}
        if msg.get("text", "").startswith("/start"):
            chat_id = msg["chat"]["id"]
            first = msg["chat"].get("first_name", "")
            return chat_id, first
    # любой личный чат
    for upd in reversed(res.get("result", [])):
        msg = upd.get("message") or {}
        if msg.get("chat", {}).get("type") == "private":
            return msg["chat"]["id"], msg["chat"].get("first_name", "")
    return None, None


def poll_commands(token, chat_id, once=False):
    """Отвечает на команды бота. once=True — одна проверка getUpdates (для cron).

    Подтверждает обработанные обновления на сервере Telegram (offset),
    чтобы не отвечать на них повторно — локальное состояние не требуется.
    """
    offset = load_state().get("tg_offset", 0)
    while True:
        res = tg_api(token, "getUpdates", {"timeout": 0 if once else 25,
                                           "offset": offset, "limit": 20})
        if not res.get("ok"):
            print("getUpdates error:", res.get("description"))
            return
        updates = res.get("result", [])
        if not updates:
            if once:
                return
            continue
        max_update_id = 0
        for upd in updates:
            max_update_id = max(max_update_id, upd["update_id"])
            msg = upd.get("message") or upd.get("edited_message") or {}
            text = (msg.get("text") or "").strip()
            chat = msg.get("chat", {}).get("id")
            if not text or not chat:
                continue
            low = text.lower().split("@")[0]
            today = today_msk()
            if low.startswith("/start") or low.startswith("/help"):
                reply = ("Привет! Я бот расписания ВПТ (корпус №2, группа РКЛ-3).\n\n"
                         "/today — расписание на сегодня\n"
                         "/tomorrow — расписание на завтра\n"
                         "/week — расписание на неделю\n"
                         "Ежедневная отправка — в 20:00 по Москве.")
            elif low.startswith("/today"):
                pairs, _, _ = get_day_schedule(today)
                reply = format_day_message(today, pairs)
            elif low.startswith("/tomorrow"):
                pairs, _, _ = get_day_schedule(today + dt.timedelta(days=1))
                reply = format_day_message(today + dt.timedelta(days=1), pairs)
            elif low.startswith("/week"):
                reply = format_week_message()
            elif low.startswith("/chatid"):
                reply = f"Ваш chat_id: {chat}"
            else:
                reply = "Не понял команду. Доступны: /today, /tomorrow, /week"
            ok, err = tg_send(token, chat, reply)
            if not ok:
                print("send error:", err)
        # подтверждаем обработанные обновления на сервере Telegram
        res = tg_api(token, "getUpdates", {"timeout": 0, "offset": max_update_id + 1,
                                           "limit": 1})
        if once:
            return


# ──────────────────────────── Отправка с защитой от дублей ────────────────────────────

def send_daily(token, chat_id, target="tomorrow", force=False):
    today = today_msk()
    date = today if target == "today" else today + dt.timedelta(days=1)
    pairs, fname, err = get_day_schedule(date)
    if pairs is None and err is None and fname is None:
        # файла нет — отправляем уведомление об отсутствии, но не чаще раза в день
        pass
    text = format_day_message(date, pairs)
    h = msg_hash(text)
    st = load_state()
    key = f"sent_{date.isoformat()}_{target}"
    if not force and st.get(key) == h:
        print("Дубль: такое же сообщение уже отправлено, пропускаю.")
        return True
    changed = bool(st.get(key)) and st.get(key) != h
    if changed:
        text = format_day_message(date, pairs, changed=True)
    ok, tg_err = tg_send(token, chat_id, text)
    if ok:
        st[key] = msg_hash(text)
        st["last_send"] = dt.datetime.now().isoformat()
        save_state(st)
        print("Отправлено:\n" + text)
    else:
        print("Ошибка Telegram:", tg_err)
    return ok


# ──────────────────────────── main ────────────────────────────

def main():
    args = sys.argv[1:]
    cmd = args[0] if args else "tomorrow"

    if cmd in ("today", "tomorrow", "сегодня", "завтра"):
        date = today_msk() if cmd in ("today", "сегодня") else today_msk() + dt.timedelta(days=1)
        pairs, fname, err = get_day_schedule(date)
        if err:
            print("Ошибка:", err); sys.exit(1)
        print(format_day_message(date, pairs))
        print(f"\n[файл-источник: {fname or 'не найден'}]")
    elif cmd == "week":
        print(format_week_message())
    elif cmd == "changes":
        date = today_msk() + dt.timedelta(days=1)
        pairs, fname, _ = get_day_schedule(date)
        text = format_day_message(date, pairs)
        h = msg_hash(text)
        st = load_state()
        key = f"sent_{date.isoformat()}_tomorrow"
        if st.get(key) is None:
            print("Ранее на эту дату ничего не отправлялось.")
        elif st.get(key) == h:
            print("Изменений нет.")
        else:
            print("⚠️ Расписание на завтра изменилось с момента последней отправки!")
    elif cmd == "send":
        token = os.environ.get("TG_TOKEN", "")
        chat_id = os.environ.get("TG_CHAT_ID", "")
        target = args[1] if len(args) > 1 else "tomorrow"
        force = "--force" in args
        if not token:
            print("Нужен TG_TOKEN (токен бота из BotFather)"); sys.exit(1)
        if not chat_id:
            found, name = resolve_chat_id(token)
            if found is None:
                print("Не удалось определить chat_id. Отправьте боту /start и повторите,"
                      " или задайте TG_CHAT_ID.")
                sys.exit(1)
            chat_id = str(found)
            print(f"chat_id определён автоматически: {chat_id} ({name})")
        ok = send_daily(token, chat_id, target, force=force)
        sys.exit(0 if ok else 1)
    elif cmd == "poll":
        token = os.environ.get("TG_TOKEN", "")
        if not token:
            print("Нужен TG_TOKEN"); sys.exit(1)
        poll_commands(token, os.environ.get("TG_CHAT_ID", ""), once="--once" in args)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
