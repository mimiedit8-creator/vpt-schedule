#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Бот-помощник ГБПОУ «Волжский политехнический техникум» (ВПТ), корпус №2, группа РКЛ-3.

Источник расписания: официальное облачное хранилище техникума (Nextcloud, volpt.ru).
Работает без внешних зависимостей (только стандартная библиотека Python 3).

Главное меню (кнопки внизу): 📅 Расписание, 🎵 Музыка, 🤖 ИИ-ассистент, 📦 Посылки,
⭐ Избранное, ⚙️ Настройки, 📊 Статистика, ❓ Помощь.

Команды (CLI):
  python3 vpt_bot.py today            — расписание на сегодня (в stdout)
  python3 vpt_bot.py tomorrow         — расписание на завтра (в stdout)
  python3 vpt_bot.py week             — расписание на ближайшие 7 дней (в stdout)
  python3 vpt_bot.py lastweek         — расписание на прошлую неделю (в stdout)
  python3 vpt_bot.py send tomorrow    — отправить расписание на завтра в Telegram
  python3 vpt_bot.py send today       — отправить расписание на сегодня в Telegram
  python3 vpt_bot.py poll             — режим бота: слушать команды (long polling)
  python3 vpt_bot.py changes          — проверка изменений файла на завтра
  python3 vpt_bot.py track            — проверка всех отслеживаемых посылок
  python3 vpt_bot.py auto             — автоотправка расписания (каждые N часов, без ночи)
  python3 vpt_bot.py cleanup          — очистка устаревших данных в state.json

Переменные окружения:
  TG_TOKEN            — токен бота Telegram (от BotFather)
  TG_CHAT_ID          — id чата для отправки (узнаётся автоматически при /start)
  TG_PASSWORD         — пароль доступа для новых пользователей (необязательно)
  GEMINI_API_KEY      — API-ключ Google Gemini для ИИ-ассистента (aistudio.google.com)
"""

import base64
import datetime as dt
import hashlib
import io
import json
import logging
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter

# ──────────────────────────── Логирование ────────────────────────────

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("vpt")

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

MSK = dt.timezone(dt.timedelta(hours=3))  # московское время (Europe/Moscow = UTC+3)


def today_msk():
    return dt.datetime.now(MSK).date()


def now_msk():
    return dt.datetime.now(MSK)


# ──────────────────────────── Nextcloud (WebDAV) доступ ────────────────────────────

WEBDAV = "https://nextcloud.volpt.ru/public.php/webdav/"


def _auth_headers():
    tok = base64.b64encode(f"{SHARE_TOKEN}:".encode()).decode()
    return {"Authorization": "Basic " + tok}


def http_get(url, timeout=25, retries=2):
    """GET с тайм-аутом и повторами (короткие тайм-ауты, чтобы бот не зависал)."""
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=_auth_headers())
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:
            last_err = e
            log.warning("Загрузка не удалась (попытка %d/%d): %s", attempt + 1, retries, e)
            if attempt + 1 < retries:
                time.sleep(1)
    raise last_err


def propfind(path=""):
    """Список файлов/папок в публичной папке Nextcloud."""
    url = WEBDAV + urllib.parse.quote(path)
    req = urllib.request.Request(url, method="PROPFIND",
                                 headers={**_auth_headers(), "Depth": "1"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read().decode("utf-8", "replace")
    out = []
    for href in re.findall(r"<d:href>([^<]+)</d:href>", data):
        name = urllib.parse.unquote(href)
        name = name[len("/public.php/webdav/"):]
        if name and name != path.strip("/"):
            out.append(name)
    return out


_FILE_CACHE = {"key": None, "files": None}


def list_schedule_files(include_archive=True):
    """Файлы папки корпуса (+ архив): [(дата, имя_файла, путь_к_файлу)].

    Текущие файлы лежат в корне папки, старые техникум переносит в «АРХИВ».
    """
    key = ("arch" if include_archive else "main",)
    if _FILE_CACHE["key"] == key and _FILE_CACHE["files"] is not None:
        return _FILE_CACHE["files"]
    folders = [BUILDING_DIR + "/"]
    if include_archive:
        folders.append(BUILDING_DIR + "/АРХИВ/")
    result = []
    for folder in folders:
        try:
            entries = propfind(folder)
        except Exception:
            continue
        for name in entries:
            if name.endswith("/"):
                continue
            base = os.path.basename(name)
            if not base.lower().endswith(".xlsx"):
                continue
            d = parse_date_from_name(base)
            if d:
                result.append((d, base, folder))
    result.sort()
    _FILE_CACHE["key"] = key
    _FILE_CACHE["files"] = result
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


def download_day_file(filename, folder=None):
    url = WEBDAV + urllib.parse.quote((folder or BUILDING_DIR + "/") + filename)
    return http_get(url)


# ──────────────────────────── Разбор XLSX ────────────────────────────

def parse_xlsx(data):
    """Минимальный парсер xlsx -> список строк (список значений-строк)."""
    rows = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
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
    match = [f for f in files if f[0] == date]
    if not match:
        return None, None, None
    _, fname, folder = sorted(match)[-1]  # если несколько — берём последнее
    data = download_day_file(fname, folder)
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


def get_day_schedule_safe(date):
    """Как get_day_schedule, но при ошибке сети возвращает (None, None, None)."""
    try:
        return get_day_schedule(date)
    except Exception as e:
        log.error("Ошибка при получении %s: %s", date, e)
        return None, None, None


# ──────────────────────────── Кеш расписания (скорость ответов) ────────────────────────────

CACHE_TTL = 600  # секунд (10 минут) — живёт кеш расписания на день


def get_day_schedule_cached(date):
    """Расписание дня с кешем — основа быстрых ответов на /1 /2 /start.

    Возвращает (pairs, fname, from_cache, stale):
      from_cache=True — данные из кеша (мгновенно, без обращения к облаку);
      stale=True      — облако недоступно, отданы последние сохранённые данные.
    Кеш обновляют и фоновые задачи (watch каждые 30 минут), поэтому
    пользовательские команды почти всегда попадают в свежий кеш.
    """
    st = load_state()
    key = date.isoformat()
    cache = st.get("schedule_cache", {}) or {}
    c = cache.get(key)
    now = time.time()
    if c and now - c.get("fetched_at", 0) < CACHE_TTL and "pairs" in c:
        return c["pairs"], c.get("fname"), True, False
    t0 = time.time()
    try:
        pairs, fname, _ = get_day_schedule(date)
    except Exception as e:
        log.warning("[кеш] источник недоступен (%s) — отдаю сохранённые данные", e)
        if c and "pairs" in c:
            return c["pairs"], c.get("fname"), True, True
        return None, None, False, False
    if pairs is None:
        # файла нет: отличаем «ещё не опубликовано» от «облако недоступно»
        # (при сбое сети список файлов приходит пустым — как без публикации)
        try:
            propfind(BUILDING_DIR + "/")
        except Exception as e:
            log.warning("[кеш] облако недоступно (%s) — отдаю сохранённые данные", e)
            if c and "pairs" in c:
                return c["pairs"], c.get("fname"), True, True
        log.info("[кеш] на %s расписание пока не опубликовано (%.2fс)", date, time.time() - t0)
        return None, None, False, False  # «нет файла» не кешируем
    # храним только ключи за последние 14 дней, чтобы state.json не рос
    fresh = {k: v for k, v in cache.items() if now - v.get("fetched_at", 0) < 14 * 86400}
    fresh[key] = {"fetched_at": now, "pairs": pairs, "fname": fname}
    st["schedule_cache"] = fresh
    save_state(st)
    log.info("[кеш] свежие данные за %s: %.2fс", date, time.time() - t0)
    return pairs, fname, False, False


def format_last_week_message():
    """Расписание на прошлую неделю (понедельник–воскресенье)."""
    today = today_msk()
    monday = today - dt.timedelta(days=today.weekday() + 7)
    lines = ["🗓 РАСПИСАНИЕ НА ПРОШЛУЮ НЕДЕЛЮ",
             f"🏫 {COLLEGE} | Корпус №{BUILDING_NO} | 👨‍🎓 {GROUP}",
             f"📅 {monday.day} {MONTHS_RU[monday.month]} – {monday.day + 6} {MONTHS_RU[monday.month]}",
             "━━━━━━━━━━━━━━"]
    found_any = False
    for i in range(7):
        d = monday + dt.timedelta(days=i)
        pairs, fname, _ = get_day_schedule_safe(d)
        wd = WEEKDAYS_RU[d.weekday()]
        lines.append("")
        lines.append(f"📅 {d.day} {MONTHS_RU[d.month]}, {wd}")
        if pairs is None:
            lines.append("   ℹ️ файл расписания не найден (удалён из облака)")
        elif not pairs:
            lines.append("   🎉 занятий не было")
        else:
            found_any = True
            for num, disc, teacher, room in pairs:
                b = bell(num, d)
                t = f"{b[0]}–{b[1]}" if b else "?"
                lines.append(f"   {num}п {t} · {disc} · {room or 'каб. ?'}"
                             + (f" · {teacher}" if teacher else ""))
    if not found_any:
        lines.append("")
        lines.append("ℹ️ За прошлую неделю файлов расписания в облаке техникума не найдено.")
    return "\n".join(lines)


def format_week_message():
    today = today_msk()
    lines = ["🗓 РАСПИСАНИЕ НА НЕДЕЛЮ",
             f"🏫 {COLLEGE} | Корпус №{BUILDING_NO} | 👨‍🎓 {GROUP}",
             "━━━━━━━━━━━━━━"]
    found_any = False
    for i in range(7):
        d = today + dt.timedelta(days=i)
        pairs, fname, _ = get_day_schedule_safe(d)
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
    tmp = STATE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log.error("Не удалось сохранить state.json: %s", e)


def msg_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# ──────────────────────────── Очистка устаревших данных ────────────────────────────

def clear_temporary_cache():
    """Очищает только безопасный временный кеш.

    Не удаляет music_cache.json, пользовательские настройки, историю,
    избранное, задания, отслеживаемые посылки и актуальный кеш расписания.
    """
    global _FILE_CACHE
    had_file_cache = _FILE_CACHE.get("files") is not None
    _FILE_CACHE = {"key": None, "files": None}

    st = load_state()
    state_changed = cleanup_state(st)
    if state_changed:
        save_state(st)
    return had_file_cache, state_changed


def cleanup_state(st):
    """Аккуратная часовая очистка state.json.

    Удаляет ТОЛЬКО устаревшие/технические данные:
      • кеш расписания старше 14 дней;
      • ключи отправок sent_* старше 45 дней;
      • статусы посылок, которые больше никто не отслеживает;
      • историю музыки и диалога ИИ сверх лимита.
    НЕ трогает: настройки, пароли чатов, избранные треки, задания,
    отслеживаемые посылки, текущий кеш расписания.
    Возвращает True, если что-то изменилось.
    """
    changed = False
    now = time.time()

    # кеш расписания старше 14 дней
    sc = st.get("schedule_cache") or {}
    fresh_sc = {k: v for k, v in sc.items()
                if now - (v or {}).get("fetched_at", 0) < 14 * 86400}
    if len(fresh_sc) != len(sc):
        st["schedule_cache"] = fresh_sc
        changed = True

    # ключи отправок старше 45 дней
    for k in list(st.keys()):
        if k.startswith("sent_"):
            m = re.match(r"sent_(\d{4}-\d{2}-\d{2})_", k)
            if m:
                try:
                    d = dt.date.fromisoformat(m.group(1))
                    if (today_msk() - d).days > 45:
                        del st[k]
                        changed = True
                except ValueError:
                    pass

    # статусы посылок, которые больше не отслеживаются
    tracked = set()
    for nums in (st.get("tracks") or {}).values():
        tracked.update(nums)
    ts = st.get("track_status") or {}
    fresh_ts = {k: v for k, v in ts.items() if k in tracked}
    if len(fresh_ts) != len(ts):
        st["track_status"] = fresh_ts
        changed = True

    # история музыки — не более 60 записей на чат
    for chat, hist in list((st.get("music_history") or {}).items()):
        if isinstance(hist, list) and len(hist) > 60:
            st["music_history"][chat] = hist[-60:]
            changed = True

    # показанные треки — не более 4000
    shown = st.get("music_shown")
    if isinstance(shown, list) and len(shown) > 4000:
        st["music_shown"] = shown[-4000:]
        changed = True

    # диалоги ИИ — не более 2*AI_MAX_HISTORY сообщений
    for chat, cfg in list((st.get("ai") or {}).items()):
        msgs = cfg.get("msgs") or []
        if len(msgs) > 2 * AI_MAX_HISTORY:
            cfg["msgs"] = msgs[-2 * AI_MAX_HISTORY:]
            changed = True

    if changed:
        log.info("[очистка] state.json: удалены устаревшие данные")
    return changed


# ──────────────────────────── Яндекс Доставка: отслеживание ────────────────────────────

TRACK_URL = "https://dostavka.yandex.ru/ndd/tracking/?order_tracking_id={}"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def track_yandex_delivery(number):
    """Отслеживание посылки Яндекс Доставки по трек-номеру.

    Использует публичную страницу отслеживания (данные приходят в готовом виде).
    Возвращает (found, status, events), events — [(дата, статус, адрес)].
    """
    url = TRACK_URL.format(urllib.parse.quote(number))
    last_err = None
    html = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
            with urllib.request.urlopen(req, timeout=20) as r:
                html = r.read().decode("utf-8", "replace")
            break
        except urllib.error.HTTPError as e:
            if e.code == 404:          # сайт отвечает 404 на несуществующий номер
                return False, "", []
            last_err = e
        except Exception as e:
            last_err = e
    if html is None:
        raise last_err
    # карточка статуса есть только у существующих заказов
    # (слово «не найден» в коде страницы есть всегда — на него полагаться нельзя)
    m = re.search(r'<div class="predictor-card[^"]*">(.*?)</div>', html, re.S)
    if not m:
        return False, "", []
    status = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(1))).strip()
    events = []
    for t, d, s in re.findall(
            r'<div class="track-time">([^<]*)</div>\s*<div class="track-desc">([^<]*)</div>\s*'
            r'(?:<div class="track-subtitle">([^<]*)</div>)?', html):
        events.append((t.strip(), d.strip(), (s or "").strip()))
    if not events:
        return False, "", []
    return True, status, events


def format_track_message(number, found, status, events, changed=False):
    lines = []
    if changed:
        lines += ["📦 ДВИЖЕНИЕ ПО ПОСЫЛКЕ", ""]
    lines.append(f"📦 ПОСЫЛКА {number}")
    if not found:
        lines += ["", "❌ Посылка с таким номером не найдена.",
                  "Проверьте номер: формат LO-XXXXXXXXX, без пробелов."]
        return "\n".join(lines)
    if status:
        lines.append(f"📍 {status}")
    lines += ["", "━━━━━━━━━━━━━━", "", "🕐 Последние события (сначала новые):"]
    for t, d, s in list(reversed(events))[:8]:
        line = f"• {t} — {d}" if t else f"• {d}"
        if s:
            line += f" ({s})"
        lines.append(line)
    return "\n".join(lines)


def handle_track_number(token, chat, number, keyboard=True):
    """Показ статуса посылки + сохранение номера для автопроверки."""
    number = number.strip()
    if not re.fullmatch(r"[A-Za-z0-9-]{6,40}", number):
        tg_send(token, chat, "❌ Неверный формат трек-номера (6–40 символов: латиница, "
                             "цифры, дефис). Пример: LO-835645864", keyboard=keyboard)
        return
    try:
        found, status, events = track_yandex_delivery(number)
    except Exception as e:
        log.error("track error: %s", e)
        tg_send(token, chat, "⚠️ Не удалось получить данные с Яндекс Доставки. "
                             "Попробуйте ещё раз через пару минут.", keyboard=keyboard)
        return
    st = load_state()
    tracks = st.setdefault("tracks", {})
    lst = tracks.get(str(chat), [])
    known = number in lst
    if not known:
        lst.append(number)
        tracks[str(chat)] = lst
    st.setdefault("track_status", {})[number] = msg_hash(
        format_track_message(number, found, status, events))
    save_state(st)
    text = ("🔁 Эта посылка уже у меня в памяти — отслеживаю её.\n\n" if known else
            "🆕 Новая посылка — запомнил её.\n\n")
    text += format_track_message(number, found, status, events)
    if not known:
        text += ("\n\n🔔 Слежу за этой посылкой — сообщу, когда она сдвинется "
                 "(проверка каждые 5 часов).")
    tg_send(token, chat, text, keyboard=keyboard)


def track_check_job():
    """Проверка всех отслеживаемых посылок; уведомление при изменении статуса."""
    token = os.environ.get("TG_TOKEN", "")
    st = load_state()
    tracks = st.get("tracks", {})
    statuses = st.get("track_status", {})
    changed_any = False
    for chat, numbers in tracks.items():
        for num in numbers:
            try:
                found, status, events = track_yandex_delivery(num)
            except Exception as e:
                log.error("Ошибка трекинга %s: %s", num, e)
                continue
            text = format_track_message(num, found, status, events)
            h = msg_hash(text)
            prev = statuses.get(num)
            if prev and prev != h:
                tg_send(token, chat, format_track_message(num, found, status, events,
                                                          changed=True))
                changed_any = True
            statuses[num] = h
    st["track_status"] = statuses
    save_state(st)
    log.info("Проверка посылок завершена%s", " — были изменения!" if changed_any else ".")


# ──────────────────────────── Telegram: базовые функции ────────────────────────────

def tg_api(token, method, payload=None):
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(payload or {}).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=35) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"ok": False, "description": e.read().decode("utf-8", "replace")}
    except Exception as e:
        return {"ok": False, "description": str(e)}


def kb(rows):
    """Reply-клавиатура (кнопки внизу экрана)."""
    return {"keyboard": rows, "resize_keyboard": True, "is_persistent": True}


# ── главное меню ──
KB_MAIN = kb([
    [{"text": "📅 Расписание"}, {"text": "🎵 Музыка"}],
    [{"text": "🤖 ИИ-ассистент"}, {"text": "📦 Посылки"}],
    [{"text": "⭐ Избранное"}, {"text": "⚙️ Настройки"}],
    [{"text": "📊 Статистика"}, {"text": "❓ Помощь"}],
])

# ── клавиатура режима ИИ ──
KB_AI = kb([
    [{"text": "🧹 Очистить историю"}, {"text": "🔄 Сменить модель"}],
    [{"text": "⬅️ Главное меню"}],
])

KEYBOARD = KB_MAIN  # совместимость со старым именем


def tg_send(token, chat_id, text, keyboard=False):
    """Отправка сообщения. keyboard: False — без клавиатуры, True — главное меню, dict — своя."""
    payload = {
        "chat_id": chat_id, "text": text,
        "disable_web_page_preview": True,
    }
    if keyboard is True:
        payload["reply_markup"] = KB_MAIN
    elif keyboard:
        payload["reply_markup"] = keyboard
    res = tg_api(token, "sendMessage", payload)
    if not res.get("ok"):
        log.error("send error: %s", res.get("description", ""))
    return res.get("ok", False), res.get("description", "")


def tg_send_chunks(token, chat_id, text, keyboard=False):
    """Отправка длинного текста частями (лимит Telegram — 4096 символов)."""
    ok_all = True
    for i in range(0, max(len(text), 1), 3900):
        ok, err = tg_send(token, chat_id, text[i:i + 3900], keyboard=keyboard if i == 0 else False)
        ok_all = ok_all and ok
    return ok_all


def resolve_chat_id(token):
    """Пытается найти chat_id последнего /start (нужен один раз)."""
    res = tg_api(token, "getUpdates", {"limit": 10})
    if not res.get("ok"):
        return None, res.get("description", "ошибка")
    chat_id = None
    for upd in reversed(res.get("result", [])):
        msg = upd.get("message") or {}
        if "/start" in msg.get("text", "").lower():
            chat_id = msg["chat"]["id"]
            first = msg["chat"].get("first_name", "")
            return chat_id, first
    # любой личный чат
    for upd in reversed(res.get("result", [])):
        msg = upd.get("message") or {}
        if msg.get("chat", {}).get("type") == "private":
            return msg["chat"]["id"], msg["chat"].get("first_name", "")
    return None, None



# ──────────────────────────── Новости: музыка и дизайн ────────────────────────────

NEWS_TIMEOUT = 12
NEWS_LIMIT_PER_TOPIC = 4


def _rss_items(url, limit=NEWS_LIMIT_PER_TOPIC):
    """Получает RSS и возвращает безопасный список коротких новостей."""
    req = urllib.request.Request(
        url, headers={"User-Agent": "VPT-Telegram-Bot/1.0"})
    with urllib.request.urlopen(req, timeout=NEWS_TIMEOUT) as response:
        raw = response.read()
    root = ET.fromstring(raw)
    items = []
    for node in root.findall(".//item")[:limit]:
        title = (node.findtext("title") or "").strip()
        link = (node.findtext("link") or "").strip()
        pub = (node.findtext("pubDate") or "").strip()
        if title:
            items.append({"title": title, "link": link, "date": pub})
    # Atom feeds (если источник вернул Atom вместо RSS)
    if not items:
        ns = {"a": "http://www.w3.org/2005/Atom"}
        for node in root.findall(".//a:entry", ns)[:limit]:
            title = (node.findtext("a:title", default="", namespaces=ns) or "").strip()
            link_node = node.find("a:link", ns)
            link = link_node.get("href", "") if link_node is not None else ""
            pub = (node.findtext("a:updated", default="", namespaces=ns) or "").strip()
            if title:
                items.append({"title": title, "link": link, "date": pub})
    return items


def _google_news_rss(query):
    return ("https://news.google.com/rss/search?q="
            + urllib.parse.quote(query) + "&hl=ru&gl=RU&ceid=RU:ru")


def _playlist_artists(limit=5):
    """Берёт популярных исполнителей из локального кеша плейлиста."""
    cache = load_music_cache() or {}
    counts = Counter()
    for track in cache.get("tracks", []):
        artist = (track.get("a") or "").strip()
        if artist:
            counts[artist] += 1
    return [name for name, _ in counts.most_common(limit)]


def send_news_digest(token, chat):
    """Сводка новостей музыки, артистов из плейлиста и дизайна."""
    tg_send(token, chat, "📰 Собираю свежую сводку новостей…")
    sections = [
        ("🎵 МУЗЫКА", "музыка музыкальная индустрия новые релизы"),
        ("🎨 ДИЗАЙН", "графический дизайн цифровой дизайн типографика"),
    ]
    lines = ["📰 СВОДКА НОВОСТЕЙ", "━━━━━━━━━━━━━━"]
    total = 0
    for heading, query in sections:
        lines += ["", heading]
        try:
            items = _rss_items(_google_news_rss(query))
            if not items:
                lines.append("Пока нет доступных публикаций.")
            for item in items:
                lines.append("• " + item["title"][:220])
                if item["link"]:
                    lines.append(item["link"])
                total += 1
        except Exception as exc:
            log.warning("[news] %s: %s", heading, exc)
            lines.append("Не удалось получить ленту. Попробуй позже.")

    artists = _playlist_artists()
    lines += ["", "🎤 НОВОСТИ ИСПОЛНИТЕЛЕЙ ИЗ ТВОЕГО ПЛЕЙЛИСТА"]
    if artists:
        for artist in artists:
            try:
                items = _rss_items(_google_news_rss(f'"{artist}" музыка'), limit=2)
                if items:
                    lines.append(f"— {artist}")
                    for item in items:
                        lines.append("• " + item["title"][:200])
                        if item["link"]:
                            lines.append(item["link"])
                        total += 1
            except Exception as exc:
                log.warning("[news] артист %s: %s", artist, exc)
    else:
        lines.append("Локальный music_cache.json не найден или в нём нет исполнителей.")

    lines += ["", "━━━━━━━━━━━━━━",
              f"Материалов получено: {total}. Источники: Google News RSS.",
              "Это подборка заголовков и ссылок, а не редакционная оценка."]
    tg_send_chunks(token, chat, "\n".join(lines), keyboard=KB_MAIN)



# ──────────────────────────── ИИ-ассистент (Google Gemini API) ────────────────────────────

# Официальный Gemini REST API. Модель задаётся в AI_MODELS ниже.
AI_URL_TEMPLATE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# Актуальные модели Gemini; доступность и квоты зависят от проекта Google AI Studio.
AI_MODELS = [
    ("✨ Gemini Flash (рекомендуется)", "gemini-3.8-flash"),
    ("⚡ Gemini Flash-Lite", "gemini-3.5-flash-lite"),
]

AI_SYSTEM = (
    "Ты — ИИ-ассистент внутри Telegram-бота студента колледжа. "
    "Отвечай по-русски (если не попросили иначе), дружелюбно, кратко и по делу. "
    "Помогай с учёбой и повседневными задачами: объясняй темы, решай задачи по шагам, "
    "проверяй и улучшай тексты, составляй планы и конспекты, отвечай на вопросы. "
    "Не используй Markdown-разметку (*, **, #) — Telegram покажет её как обычный текст. "
    "Для структуры используй обычные символы: •, 1., —, абзацы."
)
AI_MAX_HISTORY = 16     # сколько сообщений помнит ассистент
AI_MAX_INPUT = 3500     # максимальная длина одного сообщения пользователя
AI_MAX_TOKENS = 1200    # лимит ответа модели
AI_RL_PER_HOUR = 25     # лимит запросов к ИИ на чат в час


def ai_request(messages, model, api_key):
    """Запрос к официальному Google Gemini API. Возвращает текст ответа."""
    system_instruction = AI_SYSTEM
    contents = []
    for message in messages:
        role = message.get("role")
        content = str(message.get("content") or "")
        if role == "system":
            system_instruction = content
            continue
        if role not in ("user", "assistant", "model"):
            continue
        gemini_role = "model" if role in ("assistant", "model") else "user"
        if contents and contents[-1]["role"] == gemini_role:
            contents[-1]["parts"].append({"text": content})
        else:
            contents.append({"role": gemini_role, "parts": [{"text": content}]})

    if not contents:
        raise RuntimeError("пустой запрос к Gemini")

    body = {
        "systemInstruction": {"parts": [{"text": system_instruction}]},
        "contents": contents,
        "generationConfig": {"maxOutputTokens": AI_MAX_TOKENS},
    }
    url = AI_URL_TEMPLATE.format(model=urllib.parse.quote(model, safe="-"))
    req = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "x-goog-api-key": api_key,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as response:
            data = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:1200]
        try:
            detail_json = json.loads(detail)
            detail = (detail_json.get("error") or {}).get("message") or detail
        except Exception:
            pass
        raise RuntimeError(f"Gemini API HTTP {exc.code}: {detail}") from exc

    candidates = data.get("candidates") or []
    if not candidates:
        feedback = data.get("promptFeedback") or {}
        reason = feedback.get("blockReason") or "модель не вернула ответ"
        raise RuntimeError(f"Gemini: {reason}")

    parts = ((candidates[0].get("content") or {}).get("parts") or [])
    answer = "".join(part.get("text", "") for part in parts if isinstance(part, dict))
    answer = answer.strip()
    if not answer:
        finish = candidates[0].get("finishReason", "пустой ответ")
        raise RuntimeError(f"Gemini вернул пустой ответ ({finish})")
    return answer


def ai_reply(token, chat, user_text):
    """Диалог с Gemini: контекст хранится в state.json (переживает перезапуск)."""
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    cs = str(chat)
    if not api_key:
        tg_send_chunks(token, chat,
                       "🔑 ИИ-ассистент ещё не настроен.\n\n"
                       "Как включить Google Gemini:\n"
                       "1. Открой aistudio.google.com и создай API key.\n"
                       "2. В GitHub: Settings → Secrets and variables → Actions → "
                       "New repository secret.\n"
                       "3. Имя секрета: GEMINI_API_KEY; значение — твой ключ.\n"
                       "4. Убедись, что workflow передаёт секрет в переменную окружения "
                       "GEMINI_API_KEY, затем перезапусти бота.\n\n"
                       "Не публикуй ключ в коде или сообщениях.",
                       keyboard=KB_AI)
        return
    if len(user_text) > AI_MAX_INPUT:
        tg_send(token, chat, f"✂️ Сообщение слишком длинное ({len(user_text)} символов). "
                             f"Максимум {AI_MAX_INPUT} — пришли текст меньшими частями.",
                keyboard=KB_AI)
        return

    st = load_state()
    cfg = st.setdefault("ai", {}).setdefault(cs, {"msgs": [], "model": AI_MODELS[0][1]})

    # лимит запросов в час
    hour = int(time.time() // 3600)
    rl = st.setdefault("ai_rl", {}).setdefault(cs, {})
    if rl.get("h") == hour and rl.get("n", 0) >= AI_RL_PER_HOUR:
        tg_send(token, chat, "⏳ Лимит ИИ на этот час исчерпан "
                             f"({AI_RL_PER_HOUR} сообщений). Он обновится в начале следующего часа.",
                keyboard=KB_AI)
        return
    if rl.get("h") == hour:
        rl["n"] = rl.get("n", 0) + 1
    else:
        rl["h"] = hour
        rl["n"] = 1

    msgs = ([{"role": "system", "content": AI_SYSTEM}]
            + cfg.get("msgs", [])[-AI_MAX_HISTORY:]
            + [{"role": "user", "content": user_text}])

    model = cfg.get("model") or AI_MODELS[0][1]
    order = [model] + [m for _, m in AI_MODELS if m != model]
    answer, used, errors = None, None, []
    for m in order:
        try:
            answer = ai_request(msgs, m, api_key)
            used = m
            break
        except Exception as e:
            errors.append(f"{m}: {e}")
            log.warning("Модель Gemini %s недоступна: %s", m, e)
    st.setdefault("stats", {})["ai"] = st["stats"].get("ai", 0) + 1

    if answer is None:
        save_state(st)
        # Подробности остаются в серверном логе; пользователю не раскрываем ключи.
        tg_send(token, chat, "⚠️ Gemini сейчас не ответил. Возможные причины: "
                             "неверный API-ключ, недоступная модель или лимит запросов. "
                             "Попробуй позже или проверь журнал запуска бота.",
                keyboard=KB_AI)
        return

    if used != model:
        cfg["model"] = used
    cfg.setdefault("msgs", []).append({"role": "user", "content": user_text})
    cfg["msgs"].append({"role": "assistant", "content": answer})
    del cfg["msgs"][:-2 * AI_MAX_HISTORY]
    save_state(st)
    tg_send_chunks(token, chat, answer or "…", keyboard=KB_AI)


def ai_clear(token, chat):
    st = load_state()
    cfg = (st.get("ai") or {}).get(str(chat)) or {}
    n = len(cfg.get("msgs", []))
    cfg["msgs"] = []
    st.setdefault("ai", {})[str(chat)] = cfg
    save_state(st)
    tg_send(token, chat, f"🧹 История диалога очищена ({n} сообщений). Начинай заново!",
            keyboard=KB_AI)


def ai_model_picker(token, chat, edit=None):
    st = load_state()
    cur = ((st.get("ai") or {}).get(str(chat)) or {}).get("model") or AI_MODELS[0][1]
    rows = []
    for i, (name, mid) in enumerate(AI_MODELS):
        mark = " ✅" if mid == cur else ""
        rows.append([{"text": (name + mark)[:60], "callback_data": f"ai:m:{i}"}])
    rows.append([{"text": "⬅️ Настройки", "callback_data": "set:menu"}])
    text = ("🤖 ВЫБОР МОДЕЛИ ИИ\n━━━━━━━━━━━━━━\n"
            "Используется Google Gemini API. Доступность моделей и лимиты зависят "
            "от проекта Google AI Studio.\n"
            "Если выбранная модель недоступна, бот попробует следующую.")
    if edit:
        tg_api(token, "editMessageText", {"chat_id": chat, "message_id": edit, "text": text,
                                          "reply_markup": {"inline_keyboard": rows}})
    else:
        tg_api(token, "sendMessage", {"chat_id": chat, "text": text,
                                      "reply_markup": {"inline_keyboard": rows}})


def enter_ai_mode(token, chat):
    st = load_state()
    cs = str(chat)
    st.setdefault("ai_mode", {})[cs] = True
    st.setdefault("pending_track", {}).pop(cs, None)
    st.setdefault("pending_music", {}).pop(cs, None)
    save_state(st)
    tg_send_chunks(token, chat,
                   "🤖 ИИ-АССИСТЕНТ АКТИВЕН\n━━━━━━━━━━━━━━\n"
                   "Пиши мне что угодно:\n"
                   "• вопросы по учёбе и жизни\n"
                   "• задания — объясню, решу, проверю\n"
                   "• тексты — исправлю, сокращу, перескажу\n"
                   "• планы, конспекты, идеи\n\n"
                   "Я помню контекст нашего диалога. Кнопки внизу:\n"
                   "🧹 Очистить историю — начать диалог заново\n"
                   "🔄 Сменить модель — выбрать другую ИИ\n"
                   "⬅️ Главное меню — вернуться к боту",
                   keyboard=KB_AI)


def exit_ai_mode(token, chat):
    st = load_state()
    st.setdefault("ai_mode", {}).pop(str(chat), None)
    save_state(st)
    tg_send(token, chat, "⬅️ Вышел из ИИ-ассистента. Главное меню:", keyboard=KB_MAIN)


# ──────────────────────────── Музыка: плейлист «Мне нравится» Яндекс Музыки ────────────────────────────

# Публичная ссылка на плейлист (видна всем, кто её знает).
YA_PLAYLIST_URL = "https://music.yandex.ru/playlists/lk.3847e4c5-10ba-478d-996d-5bf9542417f4"
MUSIC_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "music_cache.json")
MUSIC_BATCH = 20               # размер случайной подборки


def load_music_cache():
    try:
        with open(MUSIC_CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def music_cache_info():
    """Короткая справка о кеше плейлиста для кнопки «Обновить»."""
    cache = load_music_cache() or {}
    fetched = cache.get("fetched_at", 0)
    when = (dt.datetime.fromtimestamp(fetched, MSK).strftime("%d.%m.%Y %H:%M")
            if fetched else "неизвестно")
    return (f"ℹ️ Список треков сохранён в кеше репозитория "
            f"({cache.get('count', 0)} треков, собран {when}).\n"
            "Яндекс Музыка не отдаёт API зарубежным серверам GitHub "
            "(ошибка 451 — лицензионное ограничение), поэтому автосинхронизация "
            "выключена: чтобы подтянуть свежие лайки, попроси ассистента "
            "пересобрать кеш.")


def _music_label(t):
    return f"{t['a']} — {t['t']}" if t["a"] else t["t"]


def _music_url(t):
    return f"https://music.yandex.ru/album/{t['ab']}/track/{t['id']}"


def pick_music(st, tracks, mode="random", n=MUSIC_BATCH):
    """Подборка треков: random — случайные, next — по порядку плейлиста.

    Уже показанные треки исключаются, пока не останется меньше n —
    тогда список «прослушанных» сбрасывается.
    """
    shown = set(st.get("music_shown", []))
    pool = [t for t in tracks if t["k"] not in shown]
    if len(pool) < n:
        shown = set()
        pool = list(tracks)
    if mode == "next":
        sel = pool[:n]
    else:
        sel = random.sample(pool, n) if len(pool) > n else list(pool)
    shown |= {t["k"] for t in sel}
    st["music_shown"] = sorted(shown)[:4000]
    return sel


def music_track_map():
    """Словарь k -> трек из кеша плейлиста."""
    cache = load_music_cache() or {}
    return {t["k"]: t for t in cache.get("tracks", [])}


def search_tracks(query, limit=25):
    """Поиск треков по названию/исполнителю/альбому (без учёта регистра)."""
    q = query.lower().strip()
    if not q:
        return []
    cache = load_music_cache() or {}
    out = []
    for t in cache.get("tracks", []):
        if (q in t.get("t", "").lower() or q in t.get("a", "").lower()
                or q in t.get("al", "").lower()):
            out.append(t)
            if len(out) >= limit:
                break
    return out


def music_list_message(title, tracks, footer=None):
    lines = [title, "━━━━━━━━━━━━━━"]
    for i, t in enumerate(tracks, 1):
        lines.append(f"{i}. {_music_label(t)}")
    if footer:
        lines += ["", footer]
    return "\n".join(lines)


def music_list_kb(tracks, extra_rows):
    """Кнопки-ссылки на треки + служебные кнопки."""
    rows = []
    for i, t in enumerate(tracks, 1):
        label = f"▶️ {i}. {_music_label(t)}"
        if len(label) > 64:
            label = label[:63] + "…"
        rows.append([{"text": label, "url": _music_url(t)}])
    rows += extra_rows
    return {"inline_keyboard": rows}


MUSIC_BACK_ROW = [{"text": "⬅️ Меню музыки", "callback_data": "music:menu"}]


def record_music_shown(st, chat, sel):
    """Запоминаем последнюю подборку (для «повторить» и добавления в избранное)
    и историю прослушанного."""
    cs = str(chat)
    st.setdefault("music_last", {})[cs] = [t["k"] for t in sel][:25]
    hist = st.setdefault("music_history", {}).setdefault(cs, [])
    now = time.time()
    for t in sel:
        hist.append({"k": t["k"], "at": now})
    del hist[:-60]
    st.setdefault("stats", {})["music"] = st["stats"].get("music", 0) + 1


def _music_stats_bump():
    st = load_state()
    st.setdefault("stats", {})["music"] = st["stats"].get("music", 0) + 1
    save_state(st)


def send_music_menu(token, chat, edit=None):
    text = ("🎧 МУЗЫКА\n━━━━━━━━━━━━━━\n"
            "Твой плейлист «Мне нравится» (Яндекс Музыка).\n"
            "Что показать?")
    rows = [
        [{"text": "🎲 Случайные 20", "callback_data": "music:new"},
         {"text": "⏭ Подряд", "callback_data": "music:next"}],
        [{"text": "🔍 Найти трек", "callback_data": "music:search"},
         {"text": "⭐ Избранное", "callback_data": "music:favs"}],
        [{"text": "🕘 Недавно", "callback_data": "music:history"},
         {"text": "🎻 По исполнителю", "callback_data": "music:artists"}],
        [{"text": "🎧 Открыть плейлист в Яндекс Музыке", "url": YA_PLAYLIST_URL}],
    ]
    kb_ = {"inline_keyboard": rows}
    if edit:
        tg_api(token, "editMessageText", {"chat_id": chat, "message_id": edit, "text": text,
                                          "reply_markup": kb_, "disable_web_page_preview": True})
    else:
        tg_api(token, "sendMessage", {"chat_id": chat, "text": text, "reply_markup": kb_,
                                      "disable_web_page_preview": True})


def send_music_selection(token, chat, mode="random", edit=None, note=None):
    """Отправляет (или редактирует) сообщение с подборкой треков."""
    cache = load_music_cache()
    if not cache or not cache.get("tracks"):
        text = ("⚠️ Кеш плейлиста Яндекс Музыки не найден.\n"
                "Попроси ассистента пересобрать music_cache.json.")
        if edit:
            tg_api(token, "editMessageText", {"chat_id": chat, "message_id": edit, "text": text})
        else:
            tg_send(token, chat, text, keyboard=True)
        return
    st = load_state()
    sel = pick_music(st, cache["tracks"], mode)
    record_music_shown(st, chat, sel)
    save_state(st)
    text = format_music_message(sel, cache, note=note)
    extra = [
        [{"text": "🎲 Новые 20", "callback_data": "music:new"},
         {"text": "⏭ Следующая", "callback_data": "music:next"}],
        [{"text": "⏮ Повторить подборку", "callback_data": "music:prev"},
         {"text": "⭐ В избранное", "callback_data": "music:favadd"}],
        [{"text": "ℹ️ Обновить плейлист", "callback_data": "music:refresh"},
         {"text": "⬅️ Меню музыки", "callback_data": "music:menu"}],
    ]
    kb_ = music_list_kb(sel, extra)
    if edit:
        tg_api(token, "editMessageText", {"chat_id": chat, "message_id": edit, "text": text,
                                          "reply_markup": kb_, "disable_web_page_preview": True})
    else:
        tg_api(token, "sendMessage", {"chat_id": chat, "text": text, "reply_markup": kb_,
                                      "disable_web_page_preview": True})


def resend_music_last(token, chat, edit=None):
    """Повторно показать последнюю подборку (те же треки)."""
    st = load_state()
    keys = (st.get("music_last") or {}).get(str(chat)) or []
    tmap = music_track_map()
    tracks = [tmap[k] for k in keys if k in tmap]
    if not tracks:
        send_music_selection(token, chat, mode="random", edit=edit)
        return
    _music_stats_bump()
    extra = [
        [{"text": "🎲 Новые 20", "callback_data": "music:new"},
         {"text": "⏭ Следующая", "callback_data": "music:next"}],
        [{"text": "⭐ В избранное", "callback_data": "music:favadd"},
         {"text": "⬅️ Меню музыки", "callback_data": "music:menu"}],
    ]
    kb_ = music_list_kb(tracks, extra)
    text = music_list_message(f"⏮ ПОВТОР ПОДБОРКИ — {len(tracks)} треков", tracks,
                              "▶️ Тапни по кнопке трека ниже — откроется в Яндекс Музыке")
    if edit:
        tg_api(token, "editMessageText", {"chat_id": chat, "message_id": edit, "text": text,
                                          "reply_markup": kb_, "disable_web_page_preview": True})
    else:
        tg_api(token, "sendMessage", {"chat_id": chat, "text": text, "reply_markup": kb_,
                                      "disable_web_page_preview": True})


def format_music_message(sel, cache, note=None):
    fetched = cache.get("fetched_at", 0)
    when = (dt.datetime.fromtimestamp(fetched, MSK).strftime("%d.%m в %H:%M")
            if fetched else "?")
    lines = ["🎧 МОЯ МУЗЫКА",
             f"🎲 Случайная подборка — {len(sel)} треков",
             "━━━━━━━━━━━━━━"]
    for i, t in enumerate(sel, 1):
        lines.append(f"{i}. {_music_label(t)}")
    lines += ["━━━━━━━━━━━━━━", "",
              "▶️ Тапни по кнопке трека ниже — откроется в Яндекс Музыке"]
    if note:
        lines.append(note)
    lines.append(f"В плейлисте: {cache.get('count', len(cache.get('tracks', [])))} "
                 f"треков · обновлено {when}")
    return "\n".join(lines)


def handle_music_search(token, chat, query):
    """Результаты поиска треков по тексту пользователя."""
    results = search_tracks(query)
    if not results:
        tg_send(token, chat, f"🔍 По запросу «{query[:60]}» ничего не найдено.\n"
                             "Попробуй другое слово — ищу по названию, исполнителю и альбому.",
                keyboard=True)
        send_music_menu(token, chat)
        return
    st = load_state()
    record_music_shown(st, chat, results)
    save_state(st)
    text = music_list_message(f"🔍 ПОИСК: «{query[:40]}» — найдено {len(results)}",
                              results,
                              "▶️ Тапни по кнопке трека — откроется в Яндекс Музыке")
    extra = [
        [{"text": "⭐ В избранное", "callback_data": "music:favadd"},
         {"text": "🔍 Новый поиск", "callback_data": "music:search"}],
        [MUSIC_BACK_ROW[0]],
    ]
    tg_api(token, "sendMessage", {"chat_id": chat, "text": text,
                                  "reply_markup": music_list_kb(results, extra),
                                  "disable_web_page_preview": True})


def show_favorites(token, chat, edit=None):
    st = load_state()
    keys = (st.get("music_favs") or {}).get(str(chat)) or []
    tmap = music_track_map()
    tracks = [tmap[k] for k in keys if k in tmap]
    if not tracks:
        text = ("⭐ ИЗБРАННОЕ\n━━━━━━━━━━━━━━\n"
                "Пока пусто. Как добавить:\n"
                "1. Открой 🎵 Музыка → 🎲 Случайные 20 или 🔍 Поиск\n"
                "2. Нажми «⭐ В избранное» под списком\n"
                "3. Напиши номера треков, например: 3 7 12")
        rows = [[{"text": "🎲 Случайные 20", "callback_data": "music:new"}],
                [MUSIC_BACK_ROW[0]]]
    else:
        text = music_list_message(f"⭐ ИЗБРАННОЕ — {len(tracks)} треков", tracks,
                                  "▶️ Тапни по кнопке трека — откроется в Яндекс Музыке")
        rows = [
            [{"text": "➕ Добавить ещё", "callback_data": "music:favadd"},
             {"text": "🗑 Убрать", "callback_data": "music:favdel"}],
            [{"text": "🧹 Очистить избранное", "callback_data": "music:favclear"}],
            [MUSIC_BACK_ROW[0]],
        ]
    kb_ = {"inline_keyboard": rows + ([[{"text": f"▶️ {i}. {_music_label(t)}",
                                         "url": _music_url(t)}]
                                       for i, t in enumerate(tracks, 1)] if tracks else [])}
    if edit:
        tg_api(token, "editMessageText", {"chat_id": chat, "message_id": edit, "text": text,
                                          "reply_markup": kb_, "disable_web_page_preview": True})
    else:
        tg_api(token, "sendMessage", {"chat_id": chat, "text": text, "reply_markup": kb_,
                                      "disable_web_page_preview": True})


def show_history(token, chat, edit=None):
    st = load_state()
    hist = list(reversed((st.get("music_history") or {}).get(str(chat)) or []))[:20]
    tmap = music_track_map()
    seen, tracks = set(), []
    for h in hist:
        t = tmap.get(h.get("k"))
        if t and t["k"] not in seen:
            seen.add(t["k"])
            tracks.append(t)
    if not tracks:
        text = ("🕘 НЕДАВНОЕ\n━━━━━━━━━━━━━━\n"
                "История пуста — она заполняется сама, когда ты открываешь подборки "
                "и результаты поиска.")
        rows = [[MUSIC_BACK_ROW[0]]]
    else:
        text = music_list_message(f"🕘 НЕДАВНОЕ — последние {len(tracks)} треков", tracks,
                                  "▶️ Тапни по кнопке трека — откроется в Яндекс Музыке")
        rows = [[{"text": f"▶️ {i}. {_music_label(t)}", "url": _music_url(t)}]
                for i, t in enumerate(tracks, 1)] + [[MUSIC_BACK_ROW[0]]]
    kb_ = {"inline_keyboard": rows}
    if edit:
        tg_api(token, "editMessageText", {"chat_id": chat, "message_id": edit, "text": text,
                                          "reply_markup": kb_, "disable_web_page_preview": True})
    else:
        tg_api(token, "sendMessage", {"chat_id": chat, "text": text, "reply_markup": kb_,
                                      "disable_web_page_preview": True})


def send_artists_menu(token, chat, edit=None):
    cache = load_music_cache() or {}
    cnt = Counter(t["a"] for t in cache.get("tracks", []) if t.get("a"))
    top = [a for a, _ in cnt.most_common(24)]
    if not top:
        send_music_menu(token, chat, edit=edit)
        return
    st = load_state()
    mapping = {}
    rows = []
    for i in range(0, len(top), 2):
        row = []
        for a in top[i:i + 2]:
            h = hashlib.md5(a.encode()).hexdigest()[:8]
            mapping[h] = a
            label = f"🎻 {a}"
            row.append({"text": label[:60], "callback_data": f"music:ar:{h}"})
        rows.append(row)
    rows.append([MUSIC_BACK_ROW[0]])
    st["music_artmenu"] = {str(chat): mapping}
    save_state(st)
    text = ("🎻 ИСПОЛНИТЕЛИ\n━━━━━━━━━━━━━━\n"
            "Топ исполнителей твоего плейлиста (по числу треков).\n"
            f"Всего исполнителей: {len(cnt)}")
    kb_ = {"inline_keyboard": rows}
    if edit:
        tg_api(token, "editMessageText", {"chat_id": chat, "message_id": edit, "text": text,
                                          "reply_markup": kb_})
    else:
        tg_api(token, "sendMessage", {"chat_id": chat, "text": text, "reply_markup": kb_})


def show_artist_tracks(token, chat, artist):
    cache = load_music_cache() or {}
    tracks = [t for t in cache.get("tracks", []) if t.get("a") == artist][:30]
    if not tracks:
        send_artists_menu(token, chat)
        return
    st = load_state()
    record_music_shown(st, chat, tracks)
    save_state(st)
    text = music_list_message(f"🎻 {artist} — {len(tracks)} треков", tracks,
                              "▶️ Тапни по кнопке трека — откроется в Яндекс Музыке")
    extra = [
        [{"text": "⭐ В избранное", "callback_data": "music:favadd"}],
        [{"text": "🎻 Другие исполнители", "callback_data": "music:artists"},
         {"text": "⬅️ Меню музыки", "callback_data": "music:menu"}],
    ]
    tg_api(token, "sendMessage", {"chat_id": chat, "text": text,
                                  "reply_markup": music_list_kb(tracks, extra),
                                  "disable_web_page_preview": True})


def set_pending_music(token, chat, mode, prompt):
    st = load_state()
    st.setdefault("pending_music", {})[str(chat)] = {"mode": mode, "ts": time.time()}
    st.setdefault("pending_track", {}).pop(str(chat), None)
    save_state(st)
    tg_send(token, chat, prompt, keyboard=True)


def music_apply_numbers(token, chat, text, add):
    """Добавление/удаление треков в избранное по номерам из последнего списка."""
    nums = [int(x) for x in re.findall(r"\d{1,2}", text)]
    st = load_state()
    cs = str(chat)
    keys = (st.get("music_last") or {}).get(cs) or []
    tmap = music_track_map()
    favs = st.setdefault("music_favs", {}).setdefault(cs, [])
    applied = []
    for n in nums:
        if 1 <= n <= len(keys):
            k = keys[n - 1]
            if k not in tmap:
                continue
            if add:
                if k not in favs:
                    favs.append(k)
                    applied.append(tmap[k])
            else:
                if k in favs:
                    favs.remove(k)
                    applied.append(tmap[k])
    if len(favs) > 300:
        del favs[:-300]
    save_state(st)
    if applied:
        what = "добавил" + ("а" if False else "") if add else "убрал"
        names = "\n".join(f"⭐ {t['a']} — {t['t']}" for t in applied[:10])
        more = f"\n…и ещё {len(applied) - 10}" if len(applied) > 10 else ""
        verb = "Добавил в избранное" if add else "Убрал из избранного"
        tg_send(token, chat, f"✅ {verb}:\n{names}{more}\n\nВсего в избранном: {len(favs)}",
                keyboard=True)
    else:
        tg_send(token, chat, "ℹ️ Не нашёл таких номеров в последнем списке. "
                             "Сначала открой подборку или поиск, затем нажми "
                             "«⭐ В избранное» и напиши номера.", keyboard=True)
    show_favorites(token, chat)


# ──────────────────────────── Автоотправка расписания ────────────────────────────

AUTO_DEFAULT = {"enabled": False, "interval": 4,
                "quiet_start": "03:00", "quiet_end": "08:00"}


def parse_hhmm(s, fallback):
    try:
        h, m = s.split(":")
        return int(h) % 24, int(m) % 60
    except Exception:
        return fallback


def in_quiet(t, cfg):
    """True, если время t (МСК) внутри периода тишины."""
    sh, sm = parse_hhmm(cfg.get("quiet_start", "03:00"), (3, 0))
    eh, em = parse_hhmm(cfg.get("quiet_end", "08:00"), (8, 0))
    m = t.hour * 60 + t.minute
    s, e = sh * 60 + sm, eh * 60 + em
    if s < e:
        return s <= m < e
    if s > e:  # период через полночь
        return m >= s or m < e
    return False


def auto_slots(cfg, now):
    """Все слоты отправки вокруг текущего момента (от вчерашнего quiet_end вперёд)."""
    interval = max(1, int(cfg.get("interval", 4)))
    eh, em = parse_hhmm(cfg.get("quiet_end", "08:00"), (8, 0))
    base = now.replace(hour=eh, minute=em, second=0, microsecond=0)
    if base > now:
        base -= dt.timedelta(days=1)
    slots = []
    for i in range(0, 48 // interval + 2):
        s = base + dt.timedelta(hours=i * interval)
        if not in_quiet(s, cfg):
            slots.append(s)
    return slots


def auto_due(cfg, now):
    """Последний слот, который уже наступил (None — если сейчас тишина или ещё не время)."""
    if not cfg.get("enabled") or in_quiet(now, cfg):
        return None
    past = [s for s in auto_slots(cfg, now) if s <= now]
    return max(past) if past else None


def auto_next(cfg, now):
    """Ближайший будущий слот отправки."""
    future = [s for s in auto_slots(cfg, now) if s > now]
    return min(future) if future else None


def auto_cfg_of(st, chat):
    cfg = dict(AUTO_DEFAULT)
    cfg.update((st.get("auto") or {}).get(str(chat)) or {})
    return cfg


def send_auto_menu(token, chat, edit=None):
    st = load_state()
    cs = str(chat)
    cfg = auto_cfg_of(st, cs)
    st.setdefault("auto", {})[cs] = cfg
    save_state(st)
    now = now_msk()
    status = "✅ включена" if cfg.get("enabled") else "⏹ выключена"
    last = cfg.get("last_slot", "")
    nxt = auto_next(cfg, now)
    lines = ["⏰ АВТООТПРАВКА РАСПИСАНИЯ", "━━━━━━━━━━━━━━",
             f"Статус: {status}",
             f"🔁 Периодичность: каждые {cfg.get('interval', 4)} часа",
             f"🌙 Тишина: {cfg.get('quiet_start', '03:00')}–{cfg.get('quiet_end', '08:00')} (МСК)",
             f"📨 Последняя отправка: {last[:16].replace('T', ' ') or '—'}",
             f"⏭ Следующая: {nxt.strftime('%d.%m %H:%M') if nxt else '—'}",
             "",
             "Расписание присылается автоматически, даже если оно",
             "не менялось. Настройки сохраняются при перезапуске бота."]
    rows = [
        [{"text": "✅ Включить", "callback_data": "auto:on"},
         {"text": "⏹ Выключить", "callback_data": "auto:off"}],
        [{"text": "🕐 2 ч", "callback_data": "auto:i2"},
         {"text": "🕐 4 ч", "callback_data": "auto:i4"},
         {"text": "🕐 6 ч", "callback_data": "auto:i6"}],
        [{"text": "⬅️ Настройки", "callback_data": "set:menu"}],
    ]
    kb_ = {"inline_keyboard": rows}
    if edit:
        tg_api(token, "editMessageText", {"chat_id": chat, "message_id": edit,
                                          "text": "\n".join(lines), "reply_markup": kb_})
    else:
        tg_api(token, "sendMessage", {"chat_id": chat, "text": "\n".join(lines),
                                      "reply_markup": kb_})


def auto_job():
    """Запускается ежечасно из GitHub Actions: отправляет расписание по расписанию."""
    token = os.environ.get("TG_TOKEN", "")
    if not token:
        log.error("auto: нужен TG_TOKEN")
        return
    now = now_msk()
    # расписание на сегодня получаем до загрузки state (чтобы не затереть кеш)
    today = now.date()
    pairs, _, _, stale = get_day_schedule_cached(today)
    text = format_day_message(today, pairs)
    if stale:
        text = "⚠️ Облако техникума временно недоступно — последние сохранённые данные.\n\n" + text

    st = load_state()
    cleanup_state(st)
    sent = 0
    for chat, cfg in list((st.get("auto") or {}).items()):
        if not cfg.get("enabled"):
            continue
        due = auto_due(cfg, now)
        last = cfg.get("last_slot", "")
        if not due or (last and due.isoformat() <= last):
            continue
        ok, err = tg_send(token, chat, text)
        if ok:
            cfg["last_slot"] = due.isoformat()
            st.setdefault("stats", {})["auto"] = st["stats"].get("auto", 0) + 1
            sent += 1
            log.info("[авто] отправлено расписание в чат %s (слот %s)", chat, due)
        else:
            log.error("[авто] ошибка отправки в чат %s: %s", chat, err)
    if sent:
        save_state(st)
    log.info("[авто] проверка завершена, отправлено: %d", sent)


# ──────────────────────────── Меню: расписание, настройки, статистика ────────────────────────────

def send_schedule_menu(token, chat):
    text = ("📅 МОЁ РАСПИСАНИЕ\n━━━━━━━━━━━━━━\n"
            f"🏫 {COLLEGE} | Корпус №{BUILDING_NO} | 👨‍🎓 {GROUP}\n"
            "Что показать?")
    rows = [
        [{"text": "📅 Сегодня", "callback_data": "sch:today"},
         {"text": "📚 Завтра", "callback_data": "sch:tomorrow"}],
        [{"text": "🗓 Неделя", "callback_data": "sch:week"},
         {"text": "↩️ Прошлая неделя", "callback_data": "sch:lastweek"}],
        [{"text": "⏰ Автоотправка", "callback_data": "sch:auto"}],
    ]
    tg_api(token, "sendMessage", {"chat_id": chat, "text": text,
                                  "reply_markup": {"inline_keyboard": rows}})


def send_settings_menu(token, chat, edit=None):
    st = load_state()
    cs = str(chat)
    auto = auto_cfg_of(st, cs)
    ai_on = bool(os.environ.get("GEMINI_API_KEY"))
    lines = ["⚙️ НАСТРОЙКИ", "━━━━━━━━━━━━━━",
             f"⏰ Автоотправка расписания: "
             f"{'вкл' if auto.get('enabled') else 'выкл'}, каждые {auto.get('interval', 4)} ч",
             f"🤖 ИИ-ассистент: {'настроен ✅' if ai_on else 'нужен ключ GEMINI_API_KEY'}",
             "🔒 Пароль: " + ("включён" if os.environ.get("TG_PASSWORD") else "выключен")]
    rows = [
        [{"text": "⏰ Автоотправка расписания", "callback_data": "sch:auto"}],
        [{"text": "🤖 ИИ-модель", "callback_data": "ai:model"}],
        [{"text": "🧹 Очистить временный кеш", "callback_data": "cache:clear"}],
        [{"text": "ℹ️ О боте", "callback_data": "set:about"}],
    ]
    kb_ = {"inline_keyboard": rows}
    if edit:
        tg_api(token, "editMessageText", {"chat_id": chat, "message_id": edit,
                                          "text": "\n".join(lines), "reply_markup": kb_})
    else:
        tg_api(token, "sendMessage", {"chat_id": chat, "text": "\n".join(lines),
                                      "reply_markup": kb_})


def send_about(token, chat, edit=None):
    text = ("ℹ️ О БОТЕ\n━━━━━━━━━━━━━━\n"
            "Бот-помощник студента ВПТ (корпус №2, группа РКЛ-3).\n\n"
            "📚 Расписание берёт из официального облака техникума "
            "(nextcloud.volpt.ru) — всегда актуально.\n"
            "🎧 Музыка — твой плейлист «Мне нравится» Яндекс Музыки "
            "(кеш треков в репозитории).\n"
            "📦 Посылки — отслеживание Яндекс Доставки, проверка каждые 5 часов.\n"
            "🤖 ИИ — Google Gemini API, контекст диалога сохраняется.\n"
            "⏰ Автоотправка расписания — каждые N часов, ночью тишина.\n\n"
            "Работает на GitHub Actions (бесплатно), состояние хранится в state.json.\n"
            "Версия: 2.0")
    if edit:
        tg_api(token, "editMessageText", {"chat_id": chat, "message_id": edit, "text": text})
    else:
        tg_send(token, chat, text, keyboard=True)


def send_stats(token, chat):
    st = load_state()
    s = st.get("stats", {})
    cache = load_music_cache() or {}
    cs = str(chat)
    favs = len((st.get("music_favs") or {}).get(cs) or [])
    n_tracks = sum(len(v) for v in (st.get("tracks") or {}).values())
    hw = len(st.get("homework", []))
    sc = st.get("schedule_cache", {}) or {}
    text = ("📊 СТАТИСТИКА\n━━━━━━━━━━━━━━\n"
            f"💬 Команд обработано: {s.get('cmd', 0)}\n"
            f"🤖 Запросов к ИИ: {s.get('ai', 0)}\n"
            f"⏰ Автоотправок расписания: {s.get('auto', 0)}\n"
            f"🎧 Музыкальных подборок: {s.get('music', 0)}\n"
            f"📦 Отслеживаемых посылок: {n_tracks}\n"
            "\n"
            f"🎵 Треков в плейлисте: {cache.get('count', 0)}\n"
            f"⭐ В избранном: {favs}\n"
            f"📝 Записанных заданий: {hw}\n"
            f"🗓 Кеш расписания: {len(sc)} дн.\n"
            f"👥 Разрешённых чатов: {len(st.get('allowed_chats', []) or [])}")
    tg_send(token, chat, text, keyboard=True)


def send_help(token, chat):
    reply = ("❓ ПОМОЩЬ\n━━━━━━━━━━━━━━\n"
             "Главное меню — кнопки внизу экрана:\n"
             "📅 Расписание — сегодня/завтра/неделя/автоотправка\n"
             "🎵 Музыка — подборки, поиск, избранное, исполнители\n"
             "🤖 ИИ-ассистент — диалог с ИИ, помнит контекст\n"
             "📦 Посылки — отслеживание Яндекс Доставки\n"
             "⭐ Избранное — любимые треки\n"
             "⚙️ Настройки — автоотправка, модель ИИ\n"
             "📊 Статистика — счётчики бота\n\n"
             "Команды:\n"
             "/1 /today — расписание на сегодня\n"
             "/2 /tomorrow — расписание на завтра\n"
             "/3 /посылка — отследить посылку\n"
             "/4 /lastweek — прошлая неделя\n"
             "/5 /задания — все задания\n"
             "/задание <предмет> <текст> — записать задание\n"
             "/задание удалить <номер> — удалить задание\n"
             "/week — неделя\n"
             "/77 /музыка — меню музыки\n"
             "/ai — включить ИИ-ассистента\n"
             "/stats — статистика\n\n"
             "Расписание на завтра приходит в 20:00 (или сразу после публикации).\n"
             "Посылки проверяются каждые 5 часов, при движении — уведомление.\n"
             "Автоотправка расписания настраивается: 📅 Расписание → ⏰ Автоотправка.")
    tg_send(token, chat, reply, keyboard=True)


# ──────────────────────────── Домашние задания (/задание, /5) ────────────────────────────

HOMEWORK_MAX = 100
COMMON_SUBJECTS = [
    "история", "математика", "информатика", "русский язык", "литература",
    "физика", "химия", "биология", "обществознание", "английский язык",
    "физическая культура", "физра", "обж", "право", "экономика",
    "введение в специальность", "индивидуальный проект",
]


def text_after_command(text, cmds):
    """Текст после команды (команда может стоять в любом месте, как у кнопок)."""
    low = text.lower()
    best = None
    for c in cmds:
        i = low.find(c)
        if i >= 0 and (best is None or i < best[0]):
            best = (i, c)
    if not best:
        return text.strip()
    i, c = best
    return text[i + len(c):].strip(" :;-,—").strip()


def known_subjects():
    """Известные предметы: из расписания группы (кеш) + типичный список."""
    subs = {s.lower() for s in COMMON_SUBJECTS}
    st = load_state()
    for c in (st.get("schedule_cache", {}) or {}).values():
        for p in c.get("pairs") or []:
            if p and p[1]:
                subs.add(p[1].lower())
    return subs


def split_subject(rest):
    """Отделяет предмет от текста задания: «история выучить §5» → («история», «выучить §5»)."""
    toks = rest.split()
    subs = known_subjects()
    for n in (2, 1):  # проверяем и два слова («русский язык»), и одно
        if len(toks) >= n:
            cand = " ".join(toks[:n]).lower().rstrip(":;,.—-")
            if cand in subs:
                return " ".join(toks[:n]).rstrip(":;,.—-"), " ".join(toks[n:]).strip()
    return None, rest


def save_homework(text, photo_id=None):
    st = load_state()
    hw = st.setdefault("homework", [])
    subject, body = split_subject(text)
    if not body and not photo_id:
        return None, "пусто"
    item = {"n": (hw[-1]["n"] + 1) if hw else 1,
            "date": today_msk().isoformat(),
            "subject": (subject or "Общее").capitalize() if subject != "Общее" else "Общее",
            "text": body, "photo": photo_id}
    hw.append(item)
    if len(hw) > HOMEWORK_MAX:
        del hw[:len(hw) - HOMEWORK_MAX]
    save_state(st)
    return item, None


def format_homework_message():
    st = load_state()
    hw = st.get("homework", [])
    if not hw:
        return ("📝 Заданий пока нет.\n\n"
                "Добавить: /задание <предмет> <текст>\n"
                "Например: /задание история выучить §5\n"
                "Можно прислать фото с подписью /задание <предмет>\n"
                "Список: /5")
    lines = ["📝 ЗАДАНИЯ (всего: {})".format(len(hw)), "━━━━━━━━━━━━━━"]
    by_subj = {}
    for it in hw:
        by_subj.setdefault(it["subject"], []).append(it)
    for subj, items in by_subj.items():
        lines += ["", f"📚 {subj}:"]
        for it in items:
            d = f"{it['date'][8:10]}.{it['date'][5:7]}"
            t = it["text"] or "📷 фото"
            mark = " (+📷 фото)" if it.get("photo") and it.get("text") else ""
            lines.append(f"  {it['n']}. {t}{mark} — {d}")
    lines += ["", "Удалить: /задание удалить <номер>"]
    return "\n".join(lines)


def send_homework_list(token, chat, keyboard=True):
    tg_send(token, chat, format_homework_message(), keyboard=keyboard)
    # прикреплённые фото отправляем отдельно (по file_id, без скачивания)
    st = load_state()
    for it in st.get("homework", []):
        if it.get("photo"):
            cap = f"📝 #{it['n']} {it['subject']}"
            if it.get("text"):
                cap += f": {it['text']}"
            tg_api(token, "sendPhoto", {"chat_id": chat, "photo": it["photo"],
                                        "caption": cap[:900]})


# ──────────────────────────── Разбор команд ────────────────────────────

TRACK_RE_FMT = re.compile(r"[A-Za-z0-9-]{6,40}")

# Слова → команды. Порядок важен: более частные случаи раньше.
COMMAND_WORDS = {
    "посылк": "/3", "посыл": "/3", "пакет": "/3", "трек-номер": "/3",
    "сегодня": "/1", "завтра": "/2",
    "прошл": "/4",
    "неделю": "/week", "неделя": "/week",
    "задания": "/5", "задание": "/5",
    "музык": "menu:music", "плейлист": "menu:music", "песн": "menu:music",
    "старт": "/start", "привет": "/start", "начать": "/start",
    "помощь": "/help", "help": "/help",
    # меню-разделы (проверяются после команд выше)
    "расписан": "menu:sched",
    "ассистент": "menu:ai",
    "избранн": "menu:favs",
    "настройк": "menu:settings",
    "статистик": "menu:stats",
    "меню": "/start",
}


def parse_command(text):
    """Команда из сообщения.

    Кнопки Telegram присылают текст целиком — «📦 Посылки»,
    поэтому ищем /команду в любом месте текста. Понимаем и простые
    слова («посылка», «сегодня») без слэша.
    """
    low = text.lower()
    m = re.search(r"/[a-zа-яё0-9]+", low)
    if m:
        return m.group(0)
    for word, cmd in COMMAND_WORDS.items():
        if word in low:
            return cmd
    return low.split("@")[0]


def extract_track_arg(text):
    """Трек-номер из сообщения вида «/3 LO-835645864» (или нажатой кнопки)."""
    for tok in text.split():
        if TRACK_RE_FMT.fullmatch(tok) and len(tok) >= 6:
            return tok
    return ""


# ──────────────────────────── Обработка сообщений ────────────────────────────

def handle_message(token, chat_id, msg):
    text = (msg.get("text") or msg.get("caption") or "").strip()
    chat = msg.get("chat", {}).get("id")
    if not text or not chat:
        return
    low = parse_command(text)
    today = today_msk()
    tomorrow = today + dt.timedelta(days=1)
    st = load_state()
    cs = str(chat)
    password = os.environ.get("TG_PASSWORD", "")

    # ── проверка пароля ──
    if password and cs != str(chat_id or "") and cs not in st.get("allowed_chats", []):
        if text == password:
            st.setdefault("allowed_chats", []).append(cs)
            save_state(st)
            tg_send(token, chat, "✅ Пароль принят! Теперь можешь пользоваться ботом.",
                    keyboard=True)
        else:
            tg_send(token, chat, "🔒 Это приватный бот. Введите пароль для доступа.")
        return

    # ── новые быстрые команды /6–/9 ──
    # Проверяем их до режима ИИ, чтобы команды работали даже внутри диалога.
    if low == "/6":
        enter_ai_mode(token, chat)
        return
    if low == "/7":
        had_file_cache, state_changed = clear_temporary_cache()
        tg_send(token, chat,
                "🧹 Кеш очищен.\n"
                + ("• Кеш списка файлов расписания сброшен.\n" if had_file_cache else
                   "• Кеш списка файлов уже был пуст.\n")
                + ("• Устаревшие технические записи удалены.\n" if state_changed else
                   "• Устаревших технических записей нет.\n")
                + "Музыкальная библиотека, избранное, настройки и история ИИ сохранены.",
                keyboard=KB_MAIN)
        return
    if low == "/8":
        admin_chat = str(os.environ.get("ADMIN_CHAT_ID") or os.environ.get("TG_CHAT_ID") or "")
        if not admin_chat or str(chat) != admin_chat:
            tg_send(token, chat, "🔒 Перезагрузка доступна только администратору бота.")
            return
        tg_send(token, chat, "🔄 Перезапускаю процесс бота…")
        time.sleep(1)
        os.execv(sys.executable, [sys.executable] + sys.argv)
        return
    if low == "/9":
        send_news_digest(token, chat)
        return

    # ── режим ИИ-ассистента ──
    if st.get("ai_mode", {}).get(cs):
        l = text.lower()
        if l.startswith("/start") or l.startswith("/help") or "главное меню" in l \
                or l.startswith("меню") or "⬅️" in text:
            exit_ai_mode(token, chat)
            return
        if ("очист" in l and "истор" in l) or l.startswith("🧹"):
            ai_clear(token, chat)
            return
        if "модел" in l or l.startswith("🔄"):
            ai_model_picker(token, chat)
            return
        ai_reply(token, chat, text)
        return

    # ── ожидание трек-номера после /3 ──
    if st.get("pending_track", {}).get(cs) and not low.startswith("/"):
        st.setdefault("pending_track", {}).pop(cs, None)
        save_state(st)
        handle_track_number(token, chat, extract_track_arg(text) or text)
        return

    # ── ожидание ввода для музыки (поиск / номера избранного) ──
    pm = (st.get("pending_music") or {}).get(cs)
    if pm and not low.startswith("/"):
        st.setdefault("pending_music", {}).pop(cs, None)
        save_state(st)
        mode = pm.get("mode")
        if mode == "search":
            handle_music_search(token, chat, text)
        elif mode == "favadd":
            music_apply_numbers(token, chat, text, add=True)
        elif mode == "favdel":
            music_apply_numbers(token, chat, text, add=False)
        return

    # счётчик команд
    st.setdefault("stats", {})["cmd"] = st["stats"].get("cmd", 0) + 1
    save_state(st)

    # ── меню-разделы ──
    if low.startswith("menu:sched"):
        send_schedule_menu(token, chat)
        return
    if low.startswith("menu:music"):
        send_music_menu(token, chat)
        return
    if low.startswith("menu:ai"):
        enter_ai_mode(token, chat)
        return
    if low.startswith("menu:favs"):
        show_favorites(token, chat)
        return
    if low.startswith("menu:settings"):
        send_settings_menu(token, chat)
        return
    if low.startswith("menu:stats"):
        send_stats(token, chat)
        return

    # ── команды ──
    t_cmd = time.time()
    if low.startswith("/start"):
        pairs, _, _, stale = get_day_schedule_cached(today)
        reply = ("" if not stale else
                 "⚠️ Облако техникума временно недоступно — показываю последние "
                 "сохранённые данные.\n\n")
        reply += ("Привет! Я бот-помощник ВПТ (корпус №2, группа РКЛ-3).\n"
                  "Главное меню — кнопки внизу: 📅 Расписание, 🎵 Музыка, "
                  "🤖 ИИ-ассистент, 📦 Посылки, ⭐ Избранное, ⚙️ Настройки, 📊 Статистика.\n"
                  "Команды: /6 — ИИ-ассистент, /7 — очистить временный кеш, "
                  "/8 — перезапустить бота (администратор), /9 — новости.\n"
                  "Домашку записывай: /задание история выучить §5 "
                  "(или пришли фото с подписью /задание).\n"
                  "Вечером слежу за публикацией расписания на завтра.\n"
                  "За посылками Яндекс Доставки — проверка каждые 5 часов.\n\n"
                  "─── Твоё расписание на сегодня ───\n\n"
                  + format_day_message(today, pairs))
        tg_send_chunks(token, chat, reply, keyboard=True)
        return
    if low.startswith("/help"):
        send_help(token, chat)
        return
    if low.startswith("/stats"):
        send_stats(token, chat)
        return
    if low.startswith("/settings"):
        send_settings_menu(token, chat)
        return
    if low.startswith("/ai"):
        enter_ai_mode(token, chat)
        return
    if low.startswith("/3") or low.startswith("/посылка"):
        arg = extract_track_arg(text)
        if arg:
            handle_track_number(token, chat, arg)
        else:
            saved = st.get("tracks", {}).get(cs, [])
            if saved:
                # память: предлагаем отследить сохранённые посылки или новую
                rows = [[{"text": f"📦 {num}", "callback_data": f"track:{num}"}]
                        for num in saved[-6:]]
                rows.append([{"text": "🆕 Новая посылка", "callback_data": "track:new"}])
                tg_api(token, "sendMessage", {
                    "chat_id": chat,
                    "text": "📦 Какие посылки отслеживаем?",
                    "reply_markup": {"inline_keyboard": rows}})
            else:
                st.setdefault("pending_track", {})[cs] = True
                save_state(st)
                tg_send(token, chat, "📦 Отправь мне трек-номер посылки Яндекс Доставки "
                                     "(например, LO-835645864)", keyboard=True)
        return
    if low.startswith("/77") or low.startswith("/музык") or low.startswith("/music"):
        send_music_menu(token, chat)
        return
    if low.startswith("/5") or low.startswith("/задан"):
        # сохранить можно только через /задание; /5 и слова — показать список
        wants_save = text.lower().startswith("/задание") or "/задание " in text.lower()
        rest = text_after_command(text, ["/задание", "/задания", "/5"]) if wants_save else ""
        lowr = rest.lower()
        if not rest and not msg.get("photo"):
            send_homework_list(token, chat, keyboard=True)
        elif lowr.startswith("очист"):
            st = load_state()
            st["homework"] = []
            save_state(st)
            tg_send(token, chat, "🗑 Все задания удалены.", keyboard=True)
        elif lowr.startswith(("удали", "удалить")):
            m = re.search(r"\d+", rest)
            st = load_state()
            hw = st.get("homework", [])
            if m:
                n = int(m.group())
                before = len(hw)
                st["homework"] = [x for x in hw if x["n"] != n]
                save_state(st)
                if len(st["homework"]) < before:
                    tg_send(token, chat, f"✅ Задание #{n} удалено.", keyboard=True)
                else:
                    tg_send(token, chat, f"Задание #{n} не найдено. Список: /5",
                            keyboard=True)
            else:
                tg_send(token, chat, "Формат: /задание удалить <номер>", keyboard=True)
        else:
            photo_id = msg.get("photo", [])[-1]["file_id"] if msg.get("photo") else None
            item, err = save_homework(rest, photo_id)
            if err:
                tg_send(token, chat, "Пустое задание. Пример: /задание история выучить §5",
                        keyboard=True)
            else:
                what = item["text"] or "📷 фото"
                tg_send(token, chat,
                        f"✅ Задание сохранено: #{item['n']} {item['subject']}\n"
                        f"{what}\n\nВсе задания: /5", keyboard=True)
        return
    if low.startswith("/1") or low.startswith("/today"):
        pairs, _, _, stale = get_day_schedule_cached(today)
        reply = format_day_message(today, pairs)
        if stale:
            reply = ("⚠️ Облако техникума временно недоступно — последние "
                     "сохранённые данные.\n\n" + reply)
    elif low.startswith("/2") or low.startswith("/tomorrow"):
        pairs, _, _, stale = get_day_schedule_cached(tomorrow)
        reply = format_day_message(tomorrow, pairs)
        if stale:
            reply = ("⚠️ Облако техникума временно недоступно — последние "
                     "сохранённые данные.\n\n" + reply)
    elif low.startswith("/4") or low.startswith("/lastweek"):
        reply = format_last_week_message()
    elif low.startswith("/week"):
        reply = format_week_message()
    elif low.startswith("/chatid"):
        reply = f"Ваш chat_id: {chat}"
    else:
        reply = ("Не понял команду 🙂\n"
                 "Используй кнопки меню внизу или /help — покажу всё, что умею.")
    log.info("[скорость] команда %r подготовлена за %.2fс", low[:12], time.time() - t_cmd)
    tg_send(token, chat, reply, keyboard=True)


# ──────────────────────────── Обработка нажатий на кнопки ────────────────────────────

def handle_callback_query(token, cq):
    """Обработка нажатий на inline-кнопки (меню, музыка, посылки, ИИ, автоотправка)."""
    data = cq.get("data", "")
    cq_msg = cq.get("message") or {}
    chat = cq_msg.get("chat", {}).get("id")
    mid = cq_msg.get("message_id")

    def answer(text=""):
        tg_api(token, "answerCallbackQuery", {"callback_query_id": cq["id"], "text": text})

    if not chat:
        answer()
        return

    try:
        # ── музыка ──
        if data == "music:new":
            answer("🎲 Собираю новую подборку…")
            send_music_selection(token, chat, mode="random", edit=mid)
        elif data == "music:next":
            answer("🎵 Следующая подборка…")
            send_music_selection(token, chat, mode="next", edit=mid)
        elif data == "music:prev":
            answer("⏮ Повторяю подборку…")
            resend_music_last(token, chat, edit=mid)
        elif data == "music:refresh":
            answer("ℹ️ Показываю информацию о плейлисте")
            send_music_selection(token, chat, mode="random", edit=mid,
                                 note=music_cache_info())
        elif data == "music:menu":
            answer()
            send_music_menu(token, chat, edit=mid)
        elif data == "music:search":
            answer("🔍 Жду запрос")
            set_pending_music(token, chat, "search",
                              "🔍 Напиши название трека, исполнителя или альбом — "
                              "найду в твоём плейлисте.")
        elif data == "music:favs":
            answer("⭐ Избранное")
            show_favorites(token, chat, edit=mid)
        elif data == "music:favadd":
            answer("⭐ Жду номера треков")
            set_pending_music(token, chat, "favadd",
                              "⭐ Напиши номера треков через пробел (например: 3 7 12) "
                              "из последнего списка — добавлю их в избранное.")
        elif data == "music:favdel":
            answer("🗑 Жду номера треков")
            set_pending_music(token, chat, "favdel",
                              "🗑 Напиши номера треков через пробел — уберу их "
                              "из избранного.")
        elif data == "music:favclear":
            answer("🧹 Очищаю избранное")
            st = load_state()
            (st.get("music_favs") or {}).pop(str(chat), None)
            save_state(st)
            show_favorites(token, chat, edit=mid)
        elif data == "music:history":
            answer("🕘 Недавно прослушанное")
            show_history(token, chat, edit=mid)
        elif data == "music:artists":
            answer("🎻 Исполнители")
            send_artists_menu(token, chat, edit=mid)
        elif data.startswith("music:ar:"):
            answer("🎶 Треки исполнителя")
            h = data[len("music:ar:"):]
            st = load_state()
            artist = ((st.get("music_artmenu") or {}).get(str(chat)) or {}).get(h)
            if artist:
                show_artist_tracks(token, chat, artist)
            else:
                send_artists_menu(token, chat, edit=mid)

        # ── расписание ──
        elif data == "sch:today":
            answer("📅 Сегодня")
            pairs, _, _, stale = get_day_schedule_cached(today_msk())
            reply = format_day_message(today_msk(), pairs)
            if stale:
                reply = ("⚠️ Облако техникума временно недоступно — последние "
                         "сохранённые данные.\n\n" + reply)
            tg_send(token, chat, reply, keyboard=True)
        elif data == "sch:tomorrow":
            answer("📚 Завтра")
            tomorrow = today_msk() + dt.timedelta(days=1)
            pairs, _, _, stale = get_day_schedule_cached(tomorrow)
            reply = format_day_message(tomorrow, pairs)
            if stale:
                reply = ("⚠️ Облако техникума временно недоступно — последние "
                         "сохранённые данные.\n\n" + reply)
            tg_send(token, chat, reply, keyboard=True)
        elif data == "sch:week":
            answer("🗓 Неделя")
            tg_send(token, chat, format_week_message(), keyboard=True)
        elif data == "sch:lastweek":
            answer("↩️ Прошлая неделя")
            tg_send(token, chat, format_last_week_message(), keyboard=True)
        elif data == "sch:auto":
            answer("⏰ Автоотправка")
            send_auto_menu(token, chat, edit=mid)

        # ── автоотправка ──
        elif data in ("auto:on", "auto:off", "auto:i2", "auto:i4", "auto:i6"):
            st = load_state()
            cs = str(chat)
            cfg = auto_cfg_of(st, cs)
            if data == "auto:on":
                cfg["enabled"] = True
                # слот, который уже наступил, считаем выполненным — не спамим сразу
                due = auto_due(cfg, now_msk())
                if due:
                    cfg["last_slot"] = due.isoformat()
                answer("✅ Автоотправка включена")
            elif data == "auto:off":
                cfg["enabled"] = False
                answer("⏹ Автоотправка выключена")
            else:
                cfg["interval"] = int(data[7:])
                answer(f"🔁 Интервал: каждые {cfg['interval']} ч")
            st.setdefault("auto", {})[cs] = cfg
            save_state(st)
            send_auto_menu(token, chat, edit=mid)

        # ── настройки / ИИ ──
        elif data == "set:menu":
            answer()
            send_settings_menu(token, chat, edit=mid)
        elif data == "set:about":
            answer()
            send_about(token, chat, edit=mid)
        elif data == "cache:clear":
            had_file_cache, state_changed = clear_temporary_cache()
            details = []
            details.append("кеш списка файлов Nextcloud сброшен" if had_file_cache
                           else "кеш списка файлов Nextcloud уже пуст")
            details.append("устаревшие технические записи удалены" if state_changed
                           else "устаревших технических записей нет")
            answer("🧹 Временный кеш очищен")
            tg_send(token, chat,
                    "🧹 ОЧИСТКА КЕША\n━━━━━━━━━━━━━━\n"
                    + "\n".join("• " + x for x in details)
                    + "\n\nСохранены: музыкальная библиотека, кеш актуального "
                      "расписания, настройки, история ИИ, избранное, задания "
                      "и отслеживаемые посылки.",
                    keyboard=KB_MAIN)
            send_settings_menu(token, chat)
        elif data == "ai:model":
            answer("🤖 Выбор модели")
            ai_model_picker(token, chat, edit=mid)
        elif data.startswith("ai:m:"):
            idx = int(data[5:])
            name, mid_model = AI_MODELS[idx]
            st = load_state()
            cfg = st.setdefault("ai", {}).setdefault(str(chat),
                                                     {"msgs": [], "model": AI_MODELS[0][1]})
            cfg["model"] = mid_model
            save_state(st)
            answer(f"✅ Модель: {name}")
            ai_model_picker(token, chat, edit=mid)
        elif data == "ai:clear":
            answer("🧹 История очищена")
            st = load_state()
            cfg = (st.get("ai") or {}).get(str(chat)) or {}
            cfg["msgs"] = []
            st.setdefault("ai", {})[str(chat)] = cfg
            save_state(st)

        # ── посылки ──
        elif data == "track:new":
            answer()
            st = load_state()
            st.setdefault("pending_track", {})[str(chat)] = True
            save_state(st)
            tg_send(token, chat, "📦 Отправь трек-номер новой посылки Яндекс Доставки "
                                 "(например, LO-835645864)", keyboard=True)
        elif data.startswith("track:"):
            answer("📦 Проверяю посылку…")
            handle_track_number(token, chat, data[6:])
        else:
            answer()
    except Exception as e:
        log.exception("Ошибка обработки кнопки %s: %s", data, e)
        answer("⚠️ Ошибка, попробуй ещё раз")


# ──────────────────────────── Long polling ────────────────────────────

def poll_commands(token, chat_id, once=False, max_seconds=None):
    """Отвечает на команды бота. once=True — одна проверка getUpdates (для cron).

    Подтверждает каждое обработанное обновление сразу (offset),
    чтобы не отвечать на него повторно. max_seconds ограничивает время работы
    (long polling 25с — ответы приходят практически мгновенно;
    для GitHub Actions слушаем ~40 мин/час).
    """
    offset = load_state().get("tg_offset", 0)
    deadline = time.time() + max_seconds if max_seconds else None
    last_cleanup = time.time()
    log.info("[poll] запущен (offset=%s)", offset)
    while True:
        if deadline and time.time() >= deadline:
            log.info("[poll] время работы исчерпано (%sс), выхожу", max_seconds)
            return
        # часовая очистка устаревших данных
        if time.time() - last_cleanup > 3600:
            try:
                st = load_state()
                if cleanup_state(st):
                    save_state(st)
            except Exception:
                log.exception("[очистка] ошибка")
            last_cleanup = time.time()
        try:
            res = tg_api(token, "getUpdates", {"timeout": 0 if once else 25,
                                               "offset": offset, "limit": 20})
        except Exception as e:
            log.error("getUpdates error: %s", e)
            res = {"ok": False, "description": str(e)}
        if not res.get("ok"):
            log.warning("getUpdates: %s", res.get("description"))
            if once:
                return
            time.sleep(5)
            continue
        updates = res.get("result", [])
        if not updates:
            if once:
                return
            continue
        for upd in updates:
            offset = max(offset, upd["update_id"] + 1)
            try:
                cq = upd.get("callback_query")
                if cq:
                    handle_callback_query(token, cq)
                else:
                    msg = upd.get("message") or upd.get("edited_message") or {}
                    handle_message(token, chat_id, msg)
            except Exception:
                log.exception("Ошибка обработки обновления %s", upd.get("update_id"))
            # сразу подтверждаем обработку — защита от дублей при падении
            tg_api(token, "getUpdates", {"timeout": 0, "offset": offset, "limit": 1})
        if once:
            return


# ──────────────────────────── Отправка с защитой от дублей ────────────────────────────

def send_daily(token, chat_id, target="tomorrow", force=False, only_published=False):
    today = today_msk()
    date = today if target == "today" else today + dt.timedelta(days=1)
    pairs, fname, err = get_day_schedule(date)
    if only_published and pairs is None:
        log.info("Файл на %s ещё не опубликован — ожидание (режим watch).", date.isoformat())
        return True
    text = format_day_message(date, pairs)
    h = msg_hash(text)
    st = load_state()
    key = f"sent_{date.isoformat()}_{target}"
    if not force and st.get(key) == h:
        log.info("Дубль: такое же сообщение уже отправлено, пропускаю.")
        return True
    changed = bool(st.get(key)) and st.get(key) != h
    if changed:
        text = format_day_message(date, pairs, changed=True)
    ok, tg_err = tg_send(token, chat_id, text)
    if ok:
        st[key] = msg_hash(text)
        st["last_send"] = dt.datetime.now().isoformat()
        save_state(st)
        log.info("Отправлено расписание на %s", date.isoformat())
    else:
        log.error("Ошибка Telegram: %s", tg_err)
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
    elif cmd == "lastweek":
        print(format_last_week_message())
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
        target = next((a for a in args[1:] if not a.startswith("--")), "tomorrow")
        force = "--force" in args
        only_published = "--only-published" in args
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
        ok = send_daily(token, chat_id, target, force=force, only_published=only_published)
        sys.exit(0 if ok else 1)
    elif cmd == "track":
        track_check_job()
    elif cmd == "auto":
        auto_job()
    elif cmd == "cleanup":
        st = load_state()
        if cleanup_state(st):
            save_state(st)
            print("Очистка выполнена, устаревшие данные удалены.")
        else:
            print("Устаревших данных не найдено.")
    elif cmd == "music":
        cache = load_music_cache()
        if not cache or not cache.get("tracks"):
            print("кеш музыки не найден (music_cache.json)"); sys.exit(1)
        st = {}
        sel = pick_music(st, cache["tracks"], "random")
        for i, t in enumerate(sel, 1):
            print(f"{i}. {_music_label(t)}")
        print(f"показано {len(sel)} треков (пул без повторов: "
              f"{len(cache['tracks']) - len(st['music_shown'])})")
    elif cmd == "musictest":
        token = os.environ.get("TG_TOKEN", "")
        chat_id = os.environ.get("TG_CHAT_ID", "")
        if not token or not chat_id:
            print("Нужны TG_TOKEN и TG_CHAT_ID"); sys.exit(1)
        send_music_selection(token, chat_id)
        print("Тестовая подборка музыки отправлена.")
    elif cmd == "poll":
        token = os.environ.get("TG_TOKEN", "")
        if not token:
            print("Нужен TG_TOKEN"); sys.exit(1)
        max_s = None
        if "--max-seconds" in args:
            try:
                max_s = int(args[args.index("--max-seconds") + 1])
            except (IndexError, ValueError):
                print("Формат: --max-seconds 2400"); sys.exit(1)
        poll_commands(token, os.environ.get("TG_CHAT_ID", ""), once="--once" in args,
                      max_seconds=max_s)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
