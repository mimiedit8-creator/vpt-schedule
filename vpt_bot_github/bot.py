#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Бот-помощник ГБПОУ «Волжский политехнический техникум» (ВПТ), корпус №2, группа РКЛ-3.

Источник расписания: официальное облачное хранилище техникума (Nextcloud, volpt.ru).
Работает без внешних зависимостей (только стандартная библиотека Python 3).
"""

import base64
import datetime as dt
import email.utils
import hashlib
import io
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor

# ──────────────────────────── Логирование ────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("vpt")

# ──────────────────────────── Настройки ────────────────────────────

SHARE_TOKEN = "bsycQtwSRF93676"
BUILDING_DIR = "2 КОРПУС РАСПИСАНИЕ"
GROUP = "РКЛ-3"
BUILDING_NO = "2"
COLLEGE = "ВПТ"

DATA_DIR = os.environ.get("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
os.makedirs(DATA_DIR, exist_ok=True)
STATE_FILE = os.environ.get("STATE_FILE", os.path.join(DATA_DIR, "state.json"))

BELL_WEEKDAYS = {
    1: ("08:30", "10:00"),
    2: ("10:10", "11:40"),
    3: ("12:10", "13:40"),
    4: ("13:50", "15:20"),
    5: ("15:30", "17:00"),
    6: ("17:10", "18:40"),
    7: ("18:50", "20:20"),
}
BELL_SATURDAY = {
    1: ("08:30", "10:00"),
    2: ("10:10", "11:40"),
    3: ("11:50", "13:20"),
    4: ("13:30", "15:00"),
    5: ("15:10", "16:40"),
    6: ("16:50", "18:20"),
    7: ("18:30", "20:00"),
}

WEEKDAYS_RU = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
MONTHS_RU = [
    "", "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]

MSK = dt.timezone(dt.timedelta(hours=3))

_STATE_LOCK = threading.RLock()
_FILE_CACHE = {"key": None, "ts": 0.0, "files": None}
_FILE_CACHE_TTL = 120.0
_DAY_MEM_CACHE = {}  # date -> (ts, pairs, fname)
_DAY_MEM_TTL = 300.0


def today_msk():
    return dt.datetime.now(MSK).date()


def now_msk():
    return dt.datetime.now(MSK)


# ──────────────────────────── Nextcloud (WebDAV) доступ ────────────────────────────

WEBDAV = "https://nextcloud.volpt.ru/public.php/webdav/"


def _auth_headers():
    tok = base64.b64encode(f"{SHARE_TOKEN}:".encode()).decode()
    return {"Authorization": "Basic " + tok}


def http_get(url, timeout=12, retries=2):
    """Быстрый GET с короткими тайм-аутами и ретраями."""
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
                time.sleep(0.4)
    raise last_err


def propfind(path="", timeout=12):
    """Список файлов/папок в публичной папке Nextcloud."""
    url = WEBDAV + urllib.parse.quote(path)
    req = urllib.request.Request(
        url,
        method="PROPFIND",
        headers={**_auth_headers(), "Depth": "1"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read().decode("utf-8", "replace")
    out = []
    for href in re.findall(r"<d:href>([^<]+)</d:href>", data):
        name = urllib.parse.unquote(href)
        if name.startswith("/public.php/webdav/"):
            name = name[len("/public.php/webdav/"):]
        if name and name != path.strip("/"):
            out.append(name)
    return out


def list_schedule_files(include_archive=True):
    """Файлы папки корпуса (+ архив): [(дата, имя_файла, путь_к_файлу)]."""
    key = ("arch" if include_archive else "main",)
    now = time.time()
    if (
        _FILE_CACHE["key"] == key
        and _FILE_CACHE["files"] is not None
        and now - _FILE_CACHE["ts"] < _FILE_CACHE_TTL
    ):
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
    _FILE_CACHE["ts"] = now
    _FILE_CACHE["files"] = result
    return result


def parse_date_from_name(name):
    """Дата из имени файла вида '17.11.2026 вторник 2 НЕДЕЛЯ.xlsx'."""
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
    """Быстрый парсер xlsx -> список строк."""
    rows = []
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root.findall(f"{ns}si"):
                shared.append("".join(t.text or "" for t in si.iter(f"{ns}t")))
        sheet_name = next(
            n for n in z.namelist()
            if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")
        )
        root = ET.fromstring(z.read(sheet_name))
        for row in root.iter(f"{ns}row"):
            vals = {}
            for c in row.findall(f"{ns}c"):
                ref = c.get("r", "")
                m = re.match(r"([A-Z]+)", ref)
                col_idx = 0
                if m:
                    for ch in m.group(1):
                        col_idx = col_idx * 26 + (ord(ch) - 64)
                col_idx -= 1
                t = c.get("t")
                v = c.find(f"{ns}v")
                is_node = c.find(f"{ns}is")
                if t == "s" and v is not None and v.text is not None:
                    idx = int(v.text)
                    val = shared[idx] if 0 <= idx < len(shared) else ""
                elif t == "inlineStr" and is_node is not None:
                    val = "".join(x.text or "" for x in is_node.iter(f"{ns}t"))
                elif v is not None and v.text is not None:
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
    header_re = re.compile(r"^\d{1,2}\s*-\s*\d{2}\s+\S")
    group_re = re.compile(rf"(^|\s){re.escape(group)}(\s|$)")
    for row in rows:
        cells = [cell_str(c) for c in (list(row) + ["", "", "", ""])[:4]]
        first = cells[0]
        if header_re.match(first):
            in_group = bool(group_re.search(first))
            continue
        if not in_group:
            continue
        if not re.fullmatch(r"\d{1,2}", first):
            continue
        discipline, teacher, room = cells[1], cells[2], cells[3]
        if re.fullmatch(r"\d{5}(\.\d+)?", room) and float(room) > 40000:
            serial = float(room)
            d0 = dt.date(1899, 12, 30) + dt.timedelta(days=int(serial))
            room = d0.strftime("%d.%m")
        if discipline.lower() in ("нет", "-", "—", ""):
            continue
        pairs.append((int(first), discipline, teacher, room))
    return pairs


# ──────────────────────────── Формирование сообщения ────────────────────────────

def bell(pair_no, date):
    table = BELL_SATURDAY if date.weekday() == 5 else BELL_WEEKDAYS
    return table.get(pair_no)


def get_day_schedule(date):
    """Расписание группы на дату. Возвращает (пары|None, имя файла|None, ошибка)."""
    now = time.time()
    mem = _DAY_MEM_CACHE.get(date)
    if mem and now - mem[0] < _DAY_MEM_TTL:
        return mem[1], mem[2], None

    files = list_schedule_files()
    match = [f for f in files if f[0] == date]
    if not match:
        return None, None, None
    _, fname, folder = sorted(match)[-1]
    data = download_day_file(fname, folder)
    rows = parse_xlsx(data)
    pairs = extract_group_pairs(rows, GROUP)
    _DAY_MEM_CACHE[date] = (now, pairs, fname)
    return pairs, fname, None


def format_day_message(date, pairs, changed=False):
    today = today_msk()
    wd = WEEKDAYS_RU[date.weekday()]
    date_str = f"{date.day} {MONTHS_RU[date.month]}, {wd}"
    lines = []
    if changed:
        lines.extend(["⚠️ ВНИМАНИЕ: расписание изменилось после предыдущей отправки", ""])
    lines.extend([
        "📚 РАСПИСАНИЕ НА " + ("ЗАВТРА" if date > today else "СЕГОДНЯ"),
        "",
        f"📅 Дата: {date_str}",
        f"🏫 {COLLEGE} | Корпус №{BUILDING_NO}",
        f"👨‍🎓 Группа: {GROUP}",
        "",
        "━━━━━━━━━━━━━━",
    ])
    if pairs is None:
        lines.append("")
        if date.weekday() == 6:
            lines.append("🎉 Воскресенье — занятий нет. Отличных выходных!")
        else:
            lines.extend([
                f"ℹ️ Расписание на {date_str} пока не опубликовано в облаке техникума.",
                "Сообщение придёт автоматически, как только оно появится.",
            ])
        lines.append("")
    elif not pairs:
        lines.extend(["", "🎉 Занятий нет — отдыхай!", ""])
    else:
        for num, disc, teacher, room in pairs:
            b = bell(num, date)
            time_str = f"{b[0]}–{b[1]}" if b else "время уточнить"
            lines.extend([
                "",
                f"{num} пара",
                f"🕘 {time_str}",
                f"📖 {disc or 'дисциплина не указана'}",
                f"👨‍🏫 {teacher or 'преподаватель не указан'}",
                f"📍 {room or 'кабинет не указан'}",
            ])
        first = bell(pairs[0][0], date)
        lines.extend([
            "",
            "━━━━━━━━━━━━━━",
            "",
            f"⏰ Начало занятий: {first[0] if first else '—'}",
            f"📚 Всего пар: {len(pairs)}",
            "",
            "Хорошего учебного дня!",
        ])
    return "\n".join(lines)


def get_day_schedule_safe(date):
    try:
        return get_day_schedule(date)
    except Exception as e:
        log.error("Ошибка при получении %s: %s", date, e)
        return None, None, None


# ──────────────────────────── Кеш расписания (скорость ответов) ────────────────────────────

CACHE_TTL = 600


def get_day_schedule_cached(date):
    """Расписание дня с быстрым кешем в памяти и на диске."""
    now = time.time()
    mem = _DAY_MEM_CACHE.get(date)
    if mem and now - mem[0] < CACHE_TTL:
        return mem[1], mem[2], True, False

    key = date.isoformat()
    with _STATE_LOCK:
        st = load_state()
        cache = st.get("schedule_cache") or {}
        c = cache.get(key)
        if c and now - c.get("fetched_at", 0) < CACHE_TTL and "pairs" in c:
            pairs = [tuple(x) for x in c["pairs"]]
            _DAY_MEM_CACHE[date] = (now, pairs, c.get("fname"))
            return pairs, c.get("fname"), True, False

    t0 = time.time()
    try:
        pairs, fname, _ = get_day_schedule(date)
    except Exception as e:
        log.warning("[кеш] источник недоступен (%s) — отдаю сохранённые данные", e)
        if c and "pairs" in c:
            pairs = [tuple(x) for x in c["pairs"]]
            return pairs, c.get("fname"), True, True
        return None, None, False, False

    if pairs is None:
        if c and "pairs" in c and now - c.get("fetched_at", 0) < 3600:
            pairs = [tuple(x) for x in c["pairs"]]
            return pairs, c.get("fname"), True, True
        log.info("[кеш] на %s расписание пока не опубликовано (%.2fс)", date, time.time() - t0)
        return None, None, False, False

    with _STATE_LOCK:
        st = load_state()
        cache = st.get("schedule_cache") or {}
        fresh = {k: v for k, v in cache.items() if now - (v or {}).get("fetched_at", 0) < 14 * 86400}
        fresh[key] = {"fetched_at": now, "pairs": pairs, "fname": fname}
        st["schedule_cache"] = fresh
        save_state(st)

    _DAY_MEM_CACHE[date] = (now, pairs, fname)
    log.info("[кеш] свежие данные за %s: %.2fс", date, time.time() - t0)
    return pairs, fname, False, False


def _fetch_days_parallel(dates):
    """Загружает несколько дней параллельно для ускорения недельных команд."""
    results = {}
    with ThreadPoolExecutor(max_workers=min(len(dates), 4) or 1) as pool:
        fmap = {pool.submit(get_day_schedule_cached, d): d for d in dates}
        for fut, d in fmap.items():
            try:
                pairs, fname, _, _ = fut.result()
                results[d] = (pairs, fname)
            except Exception as e:
                log.error("Ошибка при получении %s: %s", d, e)
                results[d] = (None, None)
    return results


def format_last_week_message():
    today = today_msk()
    monday = today - dt.timedelta(days=today.weekday() + 7)
    days = [monday + dt.timedelta(days=i) for i in range(7)]
    fetched = _fetch_days_parallel(days)

    lines = [
        "🗓 РАСПИСАНИЕ НА ПРОШЛУЮ НЕДЕЛЮ",
        f"🏫 {COLLEGE} | Корпус №{BUILDING_NO} | 👨‍🎓 {GROUP}",
        f"📅 {monday.day} {MONTHS_RU[monday.month]} – {(monday + dt.timedelta(days=6)).day} {MONTHS_RU[(monday + dt.timedelta(days=6)).month]}",
        "━━━━━━━━━━━━━━",
    ]
    found_any = False
    for d in days:
        pairs, _ = fetched.get(d, (None, None))
        wd = WEEKDAYS_RU[d.weekday()]
        lines.extend(["", f"📅 {d.day} {MONTHS_RU[d.month]}, {wd}"])
        if pairs is None:
            lines.append("   ℹ️ файл расписания не найден (удалён из облака)")
        elif not pairs:
            lines.append("   🎉 занятий не было")
        else:
            found_any = True
            for num, disc, teacher, room in pairs:
                b = bell(num, d)
                t = f"{b[0]}–{b[1]}" if b else "?"
                lines.append(
                    f"   {num}п {t} · {disc} · {room or 'каб. ?'}"
                    + (f" · {teacher}" if teacher else "")
                )
    if not found_any:
        lines.extend(["", "ℹ️ За прошлую неделю файлов расписания в облаке техникума не найдено."])
    return "\n".join(lines)


def format_week_message():
    today = today_msk()
    days = [today + dt.timedelta(days=i) for i in range(7)]
    fetched = _fetch_days_parallel(days)

    lines = [
        "🗓 РАСПИСАНИЕ НА НЕДЕЛЮ",
        f"🏫 {COLLEGE} | Корпус №{BUILDING_NO} | 👨‍🎓 {GROUP}",
        "━━━━━━━━━━━━━━",
    ]
    found_any = False
    for i, d in enumerate(days):
        pairs, _ = fetched.get(d, (None, None))
        wd = WEEKDAYS_RU[d.weekday()]
        marker = " (сегодня)" if i == 0 else (" (завтра)" if i == 1 else "")
        lines.extend(["", f"📅 {d.day} {MONTHS_RU[d.month]}, {wd}{marker}"])
        if pairs is None:
            lines.append("   ℹ️ расписание пока не опубликовано")
        elif not pairs:
            lines.append("   🎉 занятий нет")
        else:
            found_any = True
            for num, disc, teacher, room in pairs:
                b = bell(num, d)
                t = f"{b[0]}–{b[1]}" if b else "?"
                lines.append(
                    f"   {num}п {t} · {disc} · {room or 'каб. ?'}"
                    + (f" · {teacher}" if teacher else "")
                )
    if not found_any:
        lines.extend(["", "ℹ️ На ближайшие дни файлов расписания в облаке техникума нет."])
    return "\n".join(lines)


# ──────────────────────────── Состояние / защита от дублей ────────────────────────────

def load_state():
    with _STATE_LOCK:
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}


def save_state(st):
    with _STATE_LOCK:
        tmp = f"{STATE_FILE}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(st, f, ensure_ascii=False, indent=1)
            os.replace(tmp, STATE_FILE)
        except Exception as e:
            log.error("Не удалось сохранить state.json: %s", e)
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass


def msg_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# ──────────────────────────── Очистка устаревших данных ────────────────────────────

def clear_temporary_cache():
    global _NEWS_CACHE
    with _NEWS_LOCK:
        news_cache_count = len(_NEWS_CACHE)
        _NEWS_CACHE.clear()
    _DAY_MEM_CACHE.clear()
    _FILE_CACHE["files"] = None
    with _STATE_LOCK:
        st = load_state()
        state_changed = cleanup_state(st)
        if state_changed:
            save_state(st)
    return news_cache_count > 0, state_changed


def cleanup_state(st):
    changed = False
    now = time.time()

    sc = st.get("schedule_cache") or {}
    fresh_sc = {k: v for k, v in sc.items() if now - (v or {}).get("fetched_at", 0) < 14 * 86400}
    if len(fresh_sc) != len(sc):
        st["schedule_cache"] = fresh_sc
        changed = True

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

    tracked = set()
    for nums in (st.get("tracks") or {}).values():
        tracked.update(nums)
    ts = st.get("track_status") or {}
    fresh_ts = {k: v for k, v in ts.items() if k in tracked}
    if len(fresh_ts) != len(ts):
        st["track_status"] = fresh_ts
        changed = True

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
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def track_yandex_delivery(number):
    url = TRACK_URL.format(urllib.parse.quote(number))
    last_err = None
    html = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
            with urllib.request.urlopen(req, timeout=12) as r:
                html = r.read().decode("utf-8", "replace")
            break
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return False, "", []
            last_err = e
        except Exception as e:
            last_err = e
        if attempt < 2:
            time.sleep(0.5)
    if html is None:
        raise last_err
    m = re.search(r'<div class="predictor-card[^"]*">(.*?)</div>', html, re.S)
    if not m:
        return False, "", []
    status = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(1))).strip()
    events = []
    for t, d, s in re.findall(
        r'<div class="track-time">([^<]*)</div>\s*<div class="track-desc">([^<]*)</div>\s*'
        r'(?:<div class="track-subtitle">([^<]*)</div>)?',
        html,
    ):
        events.append((t.strip(), d.strip(), (s or "").strip()))
    if not events:
        return False, "", []
    return True, status, events


def format_track_message(number, found, status, events, changed=False):
    lines = []
    if changed:
        lines.extend(["📦 ДВИЖЕНИЕ ПО ПОСЫЛКЕ", ""])
    lines.append(f"📦 ПОСЫЛКА {number}")
    if not found:
        lines.extend([
            "",
            "❌ Посылка с таким номером не найдена.",
            "Проверьте номер: формат LO-XXXXXXXXX, без пробелов.",
        ])
        return "\n".join(lines)
    if status:
        lines.append(f"📍 {status}")
    lines.extend(["", "━━━━━━━━━━━━━━", "", "🕐 Последние события (сначала новые):"])
    for t, d, s in list(reversed(events))[:8]:
        line = f"• {t} — {d}" if t else f"• {d}"
        if s:
            line += f" ({s})"
        lines.append(line)
    return "\n".join(lines)


def handle_track_number(token, chat, number, keyboard=True):
    number = number.strip()
    if not re.fullmatch(r"[A-Za-z0-9-]{6,40}", number):
        tg_send(
            token,
            chat,
            "❌ Неверный формат трек-номера (6–40 символов: латиница, "
            "цифры, дефис). Пример: LO-835645864",
            keyboard=keyboard,
        )
        return
    try:
        found, status, events = track_yandex_delivery(number)
    except Exception as e:
        log.error("track error: %s", e)
        tg_send(
            token,
            chat,
            "⚠️ Не удалось получить данные с Яндекс Доставки. "
            "Попробуйте ещё раз через пару минут.",
            keyboard=keyboard,
        )
        return

    with _STATE_LOCK:
        st = load_state()
        tracks = st.setdefault("tracks", {})
        lst = tracks.get(str(chat), [])
        known = number in lst
        if not known:
            lst.append(number)
            tracks[str(chat)] = lst
        st.setdefault("track_status", {})[number] = msg_hash(
            format_track_message(number, found, status, events)
        )
        save_state(st)

    text = (
        "🔁 Эта посылка уже у меня в памяти — отслеживаю её.\n\n"
        if known
        else "🆕 Новая посылка — запомнил её.\n\n"
    )
    text += format_track_message(number, found, status, events)
    if not known:
        text += (
            "\n\n🔔 Слежу за этой посылкой — сообщу, когда она сдвинется "
            "(проверка каждые 5 часов)."
        )
    tg_send(token, chat, text, keyboard=keyboard)


def track_check_job():
    token = os.environ.get("TG_TOKEN", "")
    if not token:
        return
    st = load_state()
    tracks = st.get("tracks", {})
    statuses = dict(st.get("track_status", {}))
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
                tg_send(token, chat, format_track_message(num, found, status, events, changed=True))
                changed_any = True
            statuses[num] = h
    with _STATE_LOCK:
        st = load_state()
        st["track_status"] = statuses
        save_state(st)
    log.info("Проверка посылок завершена%s", " — были изменения!" if changed_any else ".")


# ──────────────────────────── Telegram: базовые функции ────────────────────────────

def tg_api(token, method, payload=None):
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        api_timeout = 26 if method == "getUpdates" and (payload or {}).get("timeout", 0) else 8
        with urllib.request.urlopen(req, timeout=api_timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"ok": False, "description": e.read().decode("utf-8", "replace")}
    except Exception as e:
        return {"ok": False, "description": str(e)}


def kb(rows):
    return {"keyboard": rows, "resize_keyboard": True, "is_persistent": True}


KB_MAIN = kb([
    [{"text": "📅 Расписание"}, {"text": "📦 Посылки"}],
    [{"text": "🤖 ИИ-ассистент"}, {"text": "📊 Статистика"}],
    [{"text": "⚙️ Настройки"}, {"text": "📰 Новости"}],
    [{"text": "🧹 Очистка кеша"}, {"text": "❓ Помощь"}],
])

KB_AI = kb([
    [{"text": "🧹 Очистить историю"}, {"text": "🔄 Сменить модель"}],
    [{"text": "❌ Отмена"}],
])

KEYBOARD = KB_MAIN


def tg_send(token, chat_id, text, keyboard=False):
    payload = {
        "chat_id": chat_id,
        "text": text,
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
    ok_all = True
    for i in range(0, max(len(text), 1), 3900):
        ok, _ = tg_send(token, chat_id, text[i:i + 3900], keyboard=keyboard if i == 0 else False)
        ok_all = ok_all and ok
    return ok_all


def resolve_chat_id(token):
    res = tg_api(token, "getUpdates", {"limit": 10})
    if not res.get("ok"):
        return None, res.get("description", "ошибка")
    for upd in reversed(res.get("result", [])):
        msg = upd.get("message") or {}
        if "/start" in msg.get("text", "").lower():
            return msg["chat"]["id"], msg["chat"].get("first_name", "")
    for upd in reversed(res.get("result", [])):
        msg = upd.get("message") or {}
        if msg.get("chat", {}).get("type") == "private":
            return msg["chat"]["id"], msg["chat"].get("first_name", "")
    return None, None


# ──────────────────────────── Новости мира и технологий ────────────────────────────

NEWS_TIMEOUT = 6
NEWS_LIMIT_PER_TOPIC = 4
NEWS_CACHE_TTL = 15 * 60
_NEWS_CACHE = {}
_NEWS_LOCK = threading.Lock()
_NEWS_ACTIVE_CHATS = set()


def _rss_items(url, limit=NEWS_LIMIT_PER_TOPIC):
    now = time.time()
    with _NEWS_LOCK:
        cached = _NEWS_CACHE.get(url)
        if cached and now - cached[0] < NEWS_CACHE_TTL:
            return list(cached[1][:limit])

    req = urllib.request.Request(url, headers={"User-Agent": "VPT-Telegram-Bot/1.0 (RSS reader)"})
    with urllib.request.urlopen(req, timeout=NEWS_TIMEOUT) as response:
        raw = response.read(1_500_000)
    root = ET.fromstring(raw)
    items = []
    for node in root.findall(".//item")[:limit]:
        title = (node.findtext("title") or "").strip()
        link = (node.findtext("link") or "").strip()
        pub = (node.findtext("pubDate") or "").strip()
        if title:
            items.append({"title": title, "link": link, "date": pub})
    if not items:
        ns = {"a": "http://www.w3.org/2005/Atom"}
        for node in root.findall(".//a:entry", ns)[:limit]:
            title = (node.findtext("a:title", default="", namespaces=ns) or "").strip()
            link_node = node.find("a:link", ns)
            link = link_node.get("href", "") if link_node is not None else ""
            pub = (
                node.findtext("a:published", default="", namespaces=ns)
                or node.findtext("a:updated", default="", namespaces=ns)
                or ""
            ).strip()
            if title:
                items.append({"title": title, "link": link, "date": pub})

    with _NEWS_LOCK:
        _NEWS_CACHE[url] = (now, items)
    return items[:limit]


def _google_news_rss(query):
    return (
        "https://news.google.com/rss/search?q="
        + urllib.parse.quote(query)
        + "&hl=ru&gl=RU&ceid=RU:ru"
    )


def _news_date_label(value):
    if not value:
        return ""
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(MSK).strftime("%d.%m.%Y %H:%M")
    except Exception:
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            return parsed.astimezone(MSK).strftime("%d.%m.%Y %H:%M")
        except Exception:
            return value[:32]


def _collect_news_digest():
    sections = [
        ("🌍 НОВОСТИ МИРА", "важные новости мира сегодня", 4),
        ("💻 ТЕХНОЛОГИИ И ИНТЕРНЕТ", "технологии интернет цифровые сервисы новости", 4),
        ("🇷🇺 НОВОСТИ РОССИИ", "главные новости России сегодня", 4),
    ]
    lines = ["📰 СВЕЖИЕ НОВОСТИ", "━━━━━━━━━━━━━━"]
    total = 0
    seen = set()
    errors = 0

    def append_items(items):
        nonlocal total
        for item in items:
            title = re.sub(r"\s+", " ", item.get("title", "")).strip()
            key = title.casefold()
            if not title or key in seen:
                continue
            seen.add(key)
            date_label = _news_date_label(item.get("date", ""))
            lines.append("• " + title[:220])
            if date_label:
                lines.append("🕒 " + date_label)
            if item.get("link", "").startswith(("https://", "http://")):
                lines.append(item["link"][:800])
            total += 1

    fetched = {}
    with ThreadPoolExecutor(max_workers=len(sections)) as pool:
        jobs = {
            pool.submit(_rss_items, _google_news_rss(query), limit): heading
            for heading, query, limit in sections
        }
        for heading, future in ((h, f) for f, h in jobs.items()):
            try:
                fetched[heading] = future.result()
            except Exception as exc:
                errors += 1
                log.warning("[news] %s: %s", heading, exc)
                fetched[heading] = None

    for heading, _query, _limit in sections:
        lines.extend(["", heading])
        items = fetched.get(heading)
        if items is None:
            lines.append("Временно не удалось получить эту ленту.")
        else:
            append_items(items)

    lines.extend([
        "",
        "━━━━━━━━━━━━━━",
        f"Новостей найдено: {total}.",
        "Источники: публичные RSS-ленты Google News.",
        "Заголовки и ссылки ведут к публикациям первоисточников.",
    ])
    if total == 0 and errors:
        lines.extend(["", "Сейчас RSS-источники недоступны. Попробуй немного позже."])
    return "\n".join(lines)


def _news_worker(token, chat):
    try:
        digest = _collect_news_digest()
        tg_send_chunks(token, chat, digest, keyboard=KB_MAIN)
    except Exception:
        log.exception("[news] ошибка фоновой сборки")
        tg_send(token, chat, "⚠️ Не удалось собрать новости. Попробуй позже.", keyboard=KB_MAIN)
    finally:
        with _NEWS_LOCK:
            _NEWS_ACTIVE_CHATS.discard(str(chat))


def send_news_digest(token, chat):
    chat_key = str(chat)
    with _NEWS_LOCK:
        if chat_key in _NEWS_ACTIVE_CHATS:
            tg_send(token, chat, "📰 Новости уже собираются. Подожди немного.", keyboard=KB_MAIN)
            return
        _NEWS_ACTIVE_CHATS.add(chat_key)
    threading.Thread(
        target=_news_worker,
        args=(token, chat),
        daemon=True,
        name="vpt-news",
    ).start()


# ──────────────────────────── ИИ-ассистент (Google Gemini API) ────────────────────────────

AI_URL_TEMPLATE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

AI_MODELS = [
    ("✨ Gemini 2.5 Flash (рекомендуется)", "gemini-2.5-flash"),
    ("⚡ Gemini 2.5 Flash-Lite", "gemini-2.5-flash-lite"),
    ("🔮 Gemini 3.8 Flash", "gemini-3.8-flash"),
]

AI_SYSTEM = (
    "Ты — ИИ-ассистент внутри Telegram-бота студента колледжа. "
    "Отвечай по-русски (если не попросили иначе), дружелюбно, кратко и по делу. "
    "Помогай с учёбой и повседневными задачами: объясняй темы, решай задачи по шагам, "
    "проверяй и улучшай тексты, составляй планы и конспекты, отвечай на вопросы. "
    "Не используй Markdown-разметку (*, **, #) — Telegram покажет её как обычный текст. "
    "Для структуры используй обычные символы: •, 1., —, абзацы."
)
AI_MAX_HISTORY = 16
AI_MAX_INPUT = 3500
AI_MAX_TOKENS = 1200
AI_RL_PER_HOUR = 25


def ai_request(messages, model, api_key):
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
        with urllib.request.urlopen(req, timeout=35) as response:
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
    answer = "".join(part.get("text", "") for part in parts if isinstance(part, dict)).strip()
    if not answer:
        finish = candidates[0].get("finishReason", "пустой ответ")
        raise RuntimeError(f"Gemini вернул пустой ответ ({finish})")
    return answer


def ai_reply(token, chat, user_text):
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    cs = str(chat)
    if not api_key:
        tg_send_chunks(
            token,
            chat,
            "🔑 ИИ-ассистент ещё не настроен.\n\n"
            "Как включить Google Gemini:\n"
            "1. Открой aistudio.google.com и создай API key.\n"
            "2. В GitHub: Settings → Secrets and variables → Actions → "
            "New repository secret.\n"
            "3. Имя секрета: GEMINI_API_KEY; значение — твой ключ.\n"
            "4. Убедись, что workflow передаёт секрет в переменную окружения "
            "GEMINI_API_KEY, затем перезапусти бота.",
            keyboard=KB_AI,
        )
        return
    if len(user_text) > AI_MAX_INPUT:
        tg_send(
            token,
            chat,
            f"✂️ Сообщение слишком длинное ({len(user_text)} символов). "
            f"Максимум {AI_MAX_INPUT} — пришли текст меньшими частями.",
            keyboard=KB_AI,
        )
        return

    hour = int(time.time() // 3600)
    with _STATE_LOCK:
        st = load_state()
        cfg = st.setdefault("ai", {}).setdefault(cs, {"msgs": [], "model": AI_MODELS[0][1]})
        rl = st.setdefault("ai_rl", {}).setdefault(cs, {})
        if rl.get("h") == hour and rl.get("n", 0) >= AI_RL_PER_HOUR:
            tg_send(
                token,
                chat,
                f"⏳ Лимит ИИ на этот час исчерпан ({AI_RL_PER_HOUR} сообщений).",
                keyboard=KB_AI,
            )
            return
        rl["n"] = (rl.get("n", 0) + 1) if rl.get("h") == hour else 1
        rl["h"] = hour
        history_slice = list(cfg.get("msgs", [])[-AI_MAX_HISTORY:])
        model = cfg.get("model") or AI_MODELS[0][1]
        save_state(st)

    tg_api(token, "sendChatAction", {"chat_id": chat, "action": "typing"})
    msgs = (
        [{"role": "system", "content": AI_SYSTEM}]
        + history_slice
        + [{"role": "user", "content": user_text}]
    )
    order = [model] + [m for _, m in AI_MODELS if m != model]
    answer, used = None, None
    for m in order:
        try:
            answer = ai_request(msgs, m, api_key)
            used = m
            break
        except Exception as e:
            log.warning("Модель Gemini %s недоступна: %s", m, e)

    with _STATE_LOCK:
        st = load_state()
        st.setdefault("stats", {})["ai"] = st["stats"].get("ai", 0) + 1
        if answer is not None:
            cfg = st.setdefault("ai", {}).setdefault(cs, {"msgs": [], "model": AI_MODELS[0][1]})
            if used != model:
                cfg["model"] = used
            cfg.setdefault("msgs", []).append({"role": "user", "content": user_text})
            cfg["msgs"].append({"role": "assistant", "content": answer})
            del cfg["msgs"][:-2 * AI_MAX_HISTORY]
        save_state(st)

    if answer is None:
        tg_send(
            token,
            chat,
            "⚠️ Gemini сейчас не ответил. Проверь API-ключ, доступность модели или лимит.",
            keyboard=KB_AI,
        )
        return
    tg_send_chunks(token, chat, answer, keyboard=KB_AI)


def ai_clear(token, chat):
    with _STATE_LOCK:
        st = load_state()
        cfg = (st.get("ai") or {}).get(str(chat)) or {}
        n = len(cfg.get("msgs", []))
        cfg["msgs"] = []
        st.setdefault("ai", {})[str(chat)] = cfg
        save_state(st)
    tg_send(token, chat, f"🧹 История диалога очищена ({n} сообщений). Начинай заново!", keyboard=KB_AI)


def ai_model_picker(token, chat, edit=None):
    st = load_state()
    cur = ((st.get("ai") or {}).get(str(chat)) or {}).get("model") or AI_MODELS[0][1]
    rows = []
    for i, (name, mid) in enumerate(AI_MODELS):
        mark = " ✅" if mid == cur else ""
        rows.append([{"text": (name + mark)[:60], "callback_data": f"ai:m:{i}"}])
    rows.append([{"text": "⬅️ Настройки", "callback_data": "set:menu"}])
    text = (
        "🤖 ВЫБОР МОДЕЛИ ИИ\n━━━━━━━━━━━━━━\n"
        "Используется Google Gemini API. Если выбранная модель недоступна, "
        "бот автоматически попробует следующую."
    )
    payload = {"chat_id": chat, "text": text, "reply_markup": {"inline_keyboard": rows}}
    if edit:
        payload["message_id"] = edit
        tg_api(token, "editMessageText", payload)
    else:
        tg_api(token, "sendMessage", payload)


def enter_ai_mode(token, chat):
    cs = str(chat)
    with _STATE_LOCK:
        st = load_state()
        st.setdefault("ai_mode", {})[cs] = True
        st.setdefault("pending_track", {}).pop(cs, None)
        save_state(st)
    tg_send_chunks(
        token,
        chat,
        "🤖 ИИ-АССИСТЕНТ АКТИВЕН\n━━━━━━━━━━━━━━\n"
        "Пиши мне что угодно: вопросы по учёбе, задачи, тексты, планы.\n"
        "Я помню контекст нашего диалога.\n\n"
        "Кнопки внизу:\n"
        "🧹 Очистить историю — начать диалог заново\n"
        "🔄 Сменить модель — выбрать модель Gemini\n"
        "❌ Отмена — выйти в главное меню",
        keyboard=KB_AI,
    )


def exit_ai_mode(token, chat):
    with _STATE_LOCK:
        st = load_state()
        st.setdefault("ai_mode", {}).pop(str(chat), None)
        save_state(st)
    tg_send(token, chat, "⬅️ Вышел из ИИ-ассистента. Главное меню:", keyboard=KB_MAIN)


# ──────────────────────────── Автоотправка расписания ────────────────────────────

AUTO_DEFAULT = {"enabled": False, "interval": 4, "quiet_start": "03:00", "quiet_end": "08:00"}


def parse_hhmm(s, fallback):
    try:
        h, m = s.split(":")
        return int(h) % 24, int(m) % 60
    except Exception:
        return fallback


def in_quiet(t, cfg):
    sh, sm = parse_hhmm(cfg.get("quiet_start", "03:00"), (3, 0))
    eh, em = parse_hhmm(cfg.get("quiet_end", "08:00"), (8, 0))
    m = t.hour * 60 + t.minute
    s, e = sh * 60 + sm, eh * 60 + em
    if s < e:
        return s <= m < e
    if s > e:
        return m >= s or m < e
    return False


def auto_slots(cfg, now):
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
    if not cfg.get("enabled") or in_quiet(now, cfg):
        return None
    past = [s for s in auto_slots(cfg, now) if s <= now]
    return max(past) if past else None


def auto_next(cfg, now):
    future = [s for s in auto_slots(cfg, now) if s > now]
    return min(future) if future else None


def auto_cfg_of(st, chat):
    cfg = dict(AUTO_DEFAULT)
    cfg.update((st.get("auto") or {}).get(str(chat)) or {})
    return cfg


def send_auto_menu(token, chat, edit=None):
    cs = str(chat)
    with _STATE_LOCK:
        st = load_state()
        cfg = auto_cfg_of(st, cs)
        st.setdefault("auto", {})[cs] = cfg
        save_state(st)
    now = now_msk()
    status = "✅ включена" if cfg.get("enabled") else "⏹ выключена"
    last = cfg.get("last_slot", "")
    nxt = auto_next(cfg, now)
    lines = [
        "⏰ АВТООТПРАВКА РАСПИСАНИЯ",
        "━━━━━━━━━━━━━━",
        f"Статус: {status}",
        f"🔁 Периодичность: каждые {cfg.get('interval', 4)} часа",
        f"🌙 Тишина: {cfg.get('quiet_start', '03:00')}–{cfg.get('quiet_end', '08:00')} (МСК)",
        f"📨 Последняя отправка: {last[:16].replace('T', ' ') or '—'}",
        f"⏭ Следующая: {nxt.strftime('%d.%m %H:%M') if nxt else '—'}",
    ]
    rows = [
        [
            {"text": "✅ Включить", "callback_data": "auto:on"},
            {"text": "⏹ Выключить", "callback_data": "auto:off"},
        ],
        [
            {"text": "🕐 2 ч", "callback_data": "auto:i2"},
            {"text": "🕐 4 ч", "callback_data": "auto:i4"},
            {"text": "🕐 6 ч", "callback_data": "auto:i6"},
        ],
        [{"text": "⬅️ Настройки", "callback_data": "set:menu"}],
    ]
    kb_ = {"inline_keyboard": rows}
    payload = {"chat_id": chat, "text": "\n".join(lines), "reply_markup": kb_}
    if edit:
        payload["message_id"] = edit
        tg_api(token, "editMessageText", payload)
    else:
        tg_api(token, "sendMessage", payload)


def auto_job():
    token = os.environ.get("TG_TOKEN", "")
    if not token:
        log.error("auto: нужен TG_TOKEN")
        return
    now = now_msk()
    today = now.date()
    pairs, _, _, stale = get_day_schedule_cached(today)
    text = format_day_message(today, pairs)
    if stale:
        text = "⚠️ Облако техникума временно недоступно — последние сохранённые данные.\n\n" + text

    with _STATE_LOCK:
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
    text = (
        "📅 МОЁ РАСПИСАНИЕ\n━━━━━━━━━━━━━━\n"
        f"🏫 {COLLEGE} | Корпус №{BUILDING_NO} | 👨‍🎓 {GROUP}\n"
        "Что показать?"
    )
    rows = [
        [
            {"text": "📅 Сегодня", "callback_data": "sch:today"},
            {"text": "📚 Завтра", "callback_data": "sch:tomorrow"},
        ],
        [
            {"text": "🗓 Неделя", "callback_data": "sch:week"},
            {"text": "↩️ Прошлая неделя", "callback_data": "sch:lastweek"},
        ],
        [{"text": "⏰ Автоотправка", "callback_data": "sch:auto"}],
    ]
    tg_api(token, "sendMessage", {"chat_id": chat, "text": text, "reply_markup": {"inline_keyboard": rows}})


def send_settings_menu(token, chat, edit=None):
    st = load_state()
    auto = auto_cfg_of(st, str(chat))
    ai_on = bool(os.environ.get("GEMINI_API_KEY"))
    lines = [
        "⚙️ НАСТРОЙКИ",
        "━━━━━━━━━━━━━━",
        f"⏰ Автоотправка расписания: {'вкл' if auto.get('enabled') else 'выкл'}, каждые {auto.get('interval', 4)} ч",
        f"🤖 ИИ-ассистент: {'настроен ✅' if ai_on else 'нужен ключ GEMINI_API_KEY'}",
        "🔒 Пароль: " + ("включён" if os.environ.get("TG_PASSWORD") else "выключен"),
    ]
    rows = [
        [{"text": "⏰ Автоотправка расписания", "callback_data": "sch:auto"}],
        [{"text": "🤖 ИИ-модель", "callback_data": "ai:model"}],
        [{"text": "🧹 Очистить временный кеш", "callback_data": "cache:clear"}],
        [{"text": "ℹ️ О боте", "callback_data": "set:about"}],
    ]
    payload = {"chat_id": chat, "text": "\n".join(lines), "reply_markup": {"inline_keyboard": rows}}
    if edit:
        payload["message_id"] = edit
        tg_api(token, "editMessageText", payload)
    else:
        tg_api(token, "sendMessage", payload)


def send_about(token, chat, edit=None):
    text = (
        "ℹ️ О БОТЕ\n━━━━━━━━━━━━━━\n"
        "Бот-помощник студента ВПТ (корпус №2, группа РКЛ-3).\n\n"
        "📚 Расписание из официального облака техникума (nextcloud.volpt.ru).\n"
        "📦 Посылки — отслеживание Яндекс Доставки, проверка каждые 5 часов.\n"
        "🤖 ИИ — Google Gemini API, контекст диалога сохраняется.\n"
        "⏰ Автоотправка расписания — каждые N часов, ночью тишина.\n"
        "Версия: 2.1"
    )
    if edit:
        tg_api(token, "editMessageText", {"chat_id": chat, "message_id": edit, "text": text})
    else:
        tg_send(token, chat, text, keyboard=True)


def send_stats(token, chat):
    st = load_state()
    s = st.get("stats", {})
    n_tracks = sum(len(v) for v in (st.get("tracks") or {}).values())
    hw = len(st.get("homework", []))
    sc = st.get("schedule_cache", {}) or {}
    text = (
        "📊 СТАТИСТИКА\n━━━━━━━━━━━━━━\n"
        f"💬 Команд обработано: {s.get('cmd', 0)}\n"
        f"🤖 Запросов к ИИ: {s.get('ai', 0)}\n"
        f"⏰ Автоотправок расписания: {s.get('auto', 0)}\n"
        f"📦 Отслеживаемых посылок: {n_tracks}\n"
        f"📝 Записанных заданий: {hw}\n"
        f"🗓 Кеш расписания: {len(sc)} дн.\n"
        f"👥 Разрешённых чатов: {len(st.get('allowed_chats', []) or [])}"
    )
    tg_send(token, chat, text, keyboard=True)


def send_help(token, chat):
    reply = (
        "❓ ПОМОЩЬ\n━━━━━━━━━━━━━━\n"
        "Главное меню — кнопки внизу экрана:\n"
        "📅 Расписание, 📦 Посылки, 🤖 ИИ-ассистент, 📊 Статистика,\n"
        "⚙️ Настройки, 📰 Новости, 🧹 Очистка кеша, ❓ Помощь.\n\n"
        "Команды:\n"
        "/1 /today — расписание на сегодня\n"
        "/2 /tomorrow — расписание на завтра\n"
        "/3 /посылка — отследить посылку\n"
        "/4 /lastweek — прошлая неделя\n"
        "/5 /задания — все задания\n"
        "/задание <предмет> <текст> — записать задание\n"
        "/задание удалить <номер> — удалить задание\n"
        "/week — расписание на неделю\n"
        "/ai — включить ИИ-ассистента\n"
        "/cancel — выключить ИИ-ассистента\n"
        "/6 — очистить устаревший кеш\n"
        "/7 /новости — свежие новости\n"
        "/8 — перезапуск (только администратор)\n"
        "/stats — статистика"
    )
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
    subs = {s.lower() for s in COMMON_SUBJECTS}
    st = load_state()
    for c in (st.get("schedule_cache", {}) or {}).values():
        for p in c.get("pairs") or []:
            if p and len(p) > 1 and p[1]:
                subs.add(str(p[1]).lower())
    return subs


def split_subject(rest):
    toks = rest.split()
    subs = known_subjects()
    for n in (2, 1):
        if len(toks) >= n:
            cand = " ".join(toks[:n]).lower().rstrip(":;,.—-")
            if cand in subs:
                return " ".join(toks[:n]).rstrip(":;,.—-"), " ".join(toks[n:]).strip()
    return None, rest


def save_homework(text, photo_id=None):
    subject, body = split_subject(text)
    if not body and not photo_id:
        return None, "пусто"
    with _STATE_LOCK:
        st = load_state()
        hw = st.setdefault("homework", [])
        subj_clean = subject.capitalize() if subject and subject.lower() != "общее" else "Общее"
        item = {
            "n": (hw[-1]["n"] + 1) if hw else 1,
            "date": today_msk().isoformat(),
            "subject": subj_clean,
            "text": body,
            "photo": photo_id,
        }
        hw.append(item)
        if len(hw) > HOMEWORK_MAX:
            del hw[:len(hw) - HOMEWORK_MAX]
        save_state(st)
    return item, None


def format_homework_message():
    st = load_state()
    hw = st.get("homework", [])
    if not hw:
        return (
            "📝 Заданий пока нет.\n\n"
            "Добавить: /задание <предмет> <текст>\n"
            "Например: /задание история выучить §5\n"
            "Список: /5"
        )
    lines = [f"📝 ЗАДАНИЯ (всего: {len(hw)})", "━━━━━━━━━━━━━━"]
    by_subj = {}
    for it in hw:
        by_subj.setdefault(it["subject"], []).append(it)
    for subj, items in by_subj.items():
        lines.extend(["", f"📚 {subj}:"])
        for it in items:
            d = f"{it['date'][8:10]}.{it['date'][5:7]}"
            t = it["text"] or "📷 фото"
            mark = " (+📷 фото)" if it.get("photo") and it.get("text") else ""
            lines.append(f"  {it['n']}. {t}{mark} — {d}")
    lines.extend(["", "Удалить: /задание удалить <номер>"])
    return "\n".join(lines)


def send_homework_list(token, chat, keyboard=True):
    tg_send(token, chat, format_homework_message(), keyboard=keyboard)
    st = load_state()
    for it in st.get("homework", []):
        if it.get("photo"):
            cap = f"📝 #{it['n']} {it['subject']}"
            if it.get("text"):
                cap += f": {it['text']}"
            tg_api(token, "sendPhoto", {"chat_id": chat, "photo": it["photo"], "caption": cap[:900]})


# ──────────────────────────── Разбор команд ────────────────────────────

TRACK_RE_FMT = re.compile(r"[A-Za-z0-9-]{6,40}")

COMMAND_WORDS = {
    "посылк": "/3", "посыл": "/3", "пакет": "/3", "трек-номер": "/3",
    "сегодня": "/1", "завтра": "/2",
    "прошл": "/4",
    "неделю": "/week", "неделя": "/week",
    "задания": "/5", "задание": "/5",
    "очистка кеша": "/6", "очистить кеш": "/6", "кеш": "/6",
    "новости": "/7",
    "старт": "/start", "привет": "/start", "начать": "/start",
    "помощь": "/help", "help": "/help",
    "расписан": "menu:sched",
    "ассистент": "menu:ai",
    "настройк": "menu:settings",
    "статистик": "menu:stats",
    "меню": "/start",
}


def parse_command(text):
    low = text.lower().strip()
    first_tok = low.split()[0].split("@")[0] if low.split() else ""
    if first_tok in ("/новости", "/news"):
        return "/7"
    m = re.search(r"/[a-zа-яё0-9]+", low)
    if m:
        return m.group(0)
    for word, cmd in COMMAND_WORDS.items():
        if word in low:
            return cmd
    return first_tok


def extract_track_arg(text):
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
    cs = str(chat)
    password = os.environ.get("TG_PASSWORD", "")

    with _STATE_LOCK:
        st = load_state()
        if password and cs != str(chat_id or "") and cs not in st.get("allowed_chats", []):
            if text == password:
                st.setdefault("allowed_chats", []).append(cs)
                save_state(st)
                tg_send(token, chat, "✅ Пароль принят! Теперь можешь пользоваться ботом.", keyboard=True)
            else:
                tg_send(token, chat, "🔒 Это приватный бот. Введите пароль для доступа.")
            return

    if low == "/6":
        _, state_changed = clear_temporary_cache()
        tg_send(
            token,
            chat,
            "🧹 Проверка кеша завершена.\n"
            + ("• Устаревшие технические записи очищены.\n" if state_changed else "• Устаревших технических записей нет.\n")
            + "• Полезный кеш расписания, настройки, история ИИ, задания и посылки сохранены.",
            keyboard=KB_MAIN,
        )
        return
    if low == "/7":
        send_news_digest(token, chat)
        return
    if low == "/8":
        admin_chat = str(os.environ.get("ADMIN_CHAT_ID") or os.environ.get("TG_CHAT_ID") or "")
        if not admin_chat or str(chat) != admin_chat:
            tg_send(token, chat, "🔒 Перезагрузка доступна только администратору бота.")
            return
        tg_send(token, chat, "🔄 Перезапускаю процесс бота…")
        time.sleep(0.5)
        os.execv(sys.executable, [sys.executable] + sys.argv)
        return
    if low == "/9":
        tg_send(token, chat, "ℹ️ Новости открываются командой /7 или кнопкой 📰 Новости.", keyboard=KB_MAIN)
        return

    st = load_state()
    if st.get("ai_mode", {}).get(cs):
        l = text.lower().strip()
        if (
            l in ("отмена", "❌ отмена", "/cancel", "cancel")
            or l.startswith("/start")
            or l.startswith("/help")
            or "главное меню" in l
            or l.startswith("меню")
            or "⬅️" in text
        ):
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

    if st.get("pending_track", {}).get(cs) and not low.startswith("/"):
        with _STATE_LOCK:
            st = load_state()
            st.setdefault("pending_track", {}).pop(cs, None)
            save_state(st)
        handle_track_number(token, chat, extract_track_arg(text) or text)
        return

    with _STATE_LOCK:
        st = load_state()
        stats = st.setdefault("stats", {})
        stats["cmd"] = stats.get("cmd", 0) + 1
        save_state(st)

    if low.startswith("menu:sched"):
        send_schedule_menu(token, chat)
        return
    if low.startswith("menu:ai"):
        enter_ai_mode(token, chat)
        return
    if low.startswith("menu:settings"):
        send_settings_menu(token, chat)
        return
    if low.startswith("menu:stats"):
        send_stats(token, chat)
        return

    t_cmd = time.time()
    if low.startswith("/start"):
        pairs, _, _, stale = get_day_schedule_cached(today)
        reply = (
            "⚠️ Облако техникума временно недоступно — показываю последние сохранённые данные.\n\n"
            if stale else ""
        ) + (
            "Привет! Я бот-помощник ВПТ (корпус №2, группа РКЛ-3).\n"
            "Используй кнопки внизу или /help для просмотра команд.\n\n"
            "─── Твоё расписание на сегодня ───\n\n"
            + format_day_message(today, pairs)
        )
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
    if low == "/cancel":
        tg_send(token, chat, "ℹ️ Сейчас ИИ-ассистент не активен.", keyboard=KB_MAIN)
        return
    if low.startswith("/3") or low.startswith("/посылка"):
        arg = extract_track_arg(text)
        if arg:
            handle_track_number(token, chat, arg)
        else:
            saved = st.get("tracks", {}).get(cs, [])
            if saved:
                rows = [[{"text": f"📦 {num}", "callback_data": f"track:{num}"}] for num in saved[-6:]]
                rows.append([{"text": "🆕 Новая посылка", "callback_data": "track:new"}])
                tg_api(token, "sendMessage", {
                    "chat_id": chat,
                    "text": "📦 Какие посылки отслеживаем?",
                    "reply_markup": {"inline_keyboard": rows},
                })
            else:
                with _STATE_LOCK:
                    st = load_state()
                    st.setdefault("pending_track", {})[cs] = True
                    save_state(st)
                tg_send(
                    token,
                    chat,
                    "📦 Отправь мне трек-номер посылки Яндекс Доставки (например, LO-835645864)",
                    keyboard=True,
                )
        return
    if low.startswith("/5") or low.startswith("/задан"):
        wants_save = text.lower().startswith("/задание") or "/задание " in text.lower()
        rest = text_after_command(text, ["/задание", "/задания", "/5"]) if wants_save else ""
        lowr = rest.lower()
        if not rest and not msg.get("photo"):
            send_homework_list(token, chat, keyboard=True)
        elif lowr.startswith("очист"):
            with _STATE_LOCK:
                st = load_state()
                st["homework"] = []
                save_state(st)
            tg_send(token, chat, "🗑 Все задания удалены.", keyboard=True)
        elif lowr.startswith(("удали", "удалить")):
            m = re.search(r"\d+", rest)
            if m:
                n = int(m.group())
                with _STATE_LOCK:
                    st = load_state()
                    hw = st.get("homework", [])
                    before = len(hw)
                    st["homework"] = [x for x in hw if x["n"] != n]
                    save_state(st)
                    after = len(st["homework"])
                if after < before:
                    tg_send(token, chat, f"✅ Задание #{n} удалено.", keyboard=True)
                else:
                    tg_send(token, chat, f"Задание #{n} не найдено. Список: /5", keyboard=True)
            else:
                tg_send(token, chat, "Формат: /задание удалить <номер>", keyboard=True)
        else:
            photo_id = msg.get("photo", [])[-1]["file_id"] if msg.get("photo") else None
            item, err = save_homework(rest, photo_id)
            if err:
                tg_send(token, chat, "Пустое задание. Пример: /задание история выучить §5", keyboard=True)
            else:
                what = item["text"] or "📷 фото"
                tg_send(
                    token,
                    chat,
                    f"✅ Задание сохранено: #{item['n']} {item['subject']}\n{what}\n\nВсе задания: /5",
                    keyboard=True,
                )
        return
    if low.startswith("/1") or low.startswith("/today"):
        pairs, _, _, stale = get_day_schedule_cached(today)
        reply = format_day_message(today, pairs)
        if stale:
            reply = "⚠️ Облако техникума временно недоступно — последние сохранённые данные.\n\n" + reply
    elif low.startswith("/2") or low.startswith("/tomorrow"):
        pairs, _, _, stale = get_day_schedule_cached(tomorrow)
        reply = format_day_message(tomorrow, pairs)
        if stale:
            reply = "⚠️ Облако техникума временно недоступно — последние сохранённые данные.\n\n" + reply
    elif low.startswith("/4") or low.startswith("/lastweek"):
        reply = format_last_week_message()
    elif low.startswith("/week"):
        reply = format_week_message()
    elif low.startswith("/chatid"):
        reply = f"Ваш chat_id: {chat}"
    else:
        reply = "Не понял команду 🙂\nИспользуй кнопки меню внизу или /help."

    log.info("[скорость] команда %r подготовлена за %.2fс", low[:12], time.time() - t_cmd)
    tg_send(token, chat, reply, keyboard=True)


# ──────────────────────────── Обработка нажатий на кнопки ────────────────────────────

def handle_callback_query(token, cq):
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
        if data == "sch:today":
            answer("📅 Сегодня")
            today = today_msk()
            pairs, _, _, stale = get_day_schedule_cached(today)
            reply = format_day_message(today, pairs)
            if stale:
                reply = "⚠️ Облако техникума временно недоступно — последние сохранённые данные.\n\n" + reply
            tg_send(token, chat, reply, keyboard=True)
        elif data == "sch:tomorrow":
            answer("📚 Завтра")
            tomorrow = today_msk() + dt.timedelta(days=1)
            pairs, _, _, stale = get_day_schedule_cached(tomorrow)
            reply = format_day_message(tomorrow, pairs)
            if stale:
                reply = "⚠️ Облако техникума временно недоступно — последние сохранённые данные.\n\n" + reply
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
        elif data in ("auto:on", "auto:off", "auto:i2", "auto:i4", "auto:i6"):
            cs = str(chat)
            with _STATE_LOCK:
                st = load_state()
                cfg = auto_cfg_of(st, cs)
                if data == "auto:on":
                    cfg["enabled"] = True
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
        elif data == "set:menu":
            answer()
            send_settings_menu(token, chat, edit=mid)
        elif data == "set:about":
            answer()
            send_about(token, chat, edit=mid)
        elif data == "cache:clear":
            news_cache_cleared, state_changed = clear_temporary_cache()
            details = [
                "временный кеш новостей очищен" if news_cache_cleared else "временный кеш новостей уже пуст",
                "устаревшие технические записи удалены" if state_changed else "устаревших технических записей нет",
            ]
            answer("🧹 Проверка кеша завершена")
            tg_send(
                token,
                chat,
                "🧹 ОЧИСТКА КЕША\n━━━━━━━━━━━━━━\n"
                + "\n".join("• " + x for x in details)
                + "\n\nПолезный кеш расписания, настройки, история ИИ, задания и посылки сохранены.",
                keyboard=KB_MAIN,
            )
            send_settings_menu(token, chat)
        elif data == "ai:model":
            answer("🤖 Выбор модели")
            ai_model_picker(token, chat, edit=mid)
        elif data.startswith("ai:m:"):
            idx = int(data[5:])
            name, mid_model = AI_MODELS[idx]
            with _STATE_LOCK:
                st = load_state()
                cfg = st.setdefault("ai", {}).setdefault(str(chat), {"msgs": [], "model": AI_MODELS[0][1]})
                cfg["model"] = mid_model
                save_state(st)
            answer(f"✅ Модель: {name}")
            ai_model_picker(token, chat, edit=mid)
        elif data == "ai:clear":
            answer("🧹 История очищена")
            ai_clear(token, chat)
        elif data == "track:new":
            answer()
            with _STATE_LOCK:
                st = load_state()
                st.setdefault("pending_track", {})[str(chat)] = True
                save_state(st)
            tg_send(
                token,
                chat,
                "📦 Отправь трек-номер новой посылки Яндекс Доставки (например, LO-835645864)",
                keyboard=True,
            )
        elif data.startswith("track:"):
            answer("📦 Проверяю посылку…")
            handle_track_number(token, chat, data[6:])
        else:
            answer()
    except Exception as e:
        log.exception("Ошибка обработки кнопки %s: %s", data, e)
        answer("⚠️ Ошибка, попробуй ещё раз")


# ──────────────────────────── Long polling ────────────────────────────

def poll_commands(token, chat_id, once=False, max_seconds=None, background=False):
    offset = load_state().get("tg_offset", 0)
    deadline = time.time() + max_seconds if max_seconds else None
    last_cleanup = time.time()
    log.info("[poll] запущен (offset=%s)", offset)
    while True:
        if deadline and time.time() >= deadline:
            log.info("[poll] время работы исчерпано (%sс), выхожу", max_seconds)
            return
        if background:
            try:
                service_tick()
            except Exception:
                log.exception("[service] ошибка фоновой задачи")
        if time.time() - last_cleanup > 3600:
            try:
                with _STATE_LOCK:
                    st = load_state()
                    if cleanup_state(st):
                        save_state(st)
            except Exception:
                log.exception("[очистка] ошибка")
            last_cleanup = time.time()

        res = tg_api(token, "getUpdates", {"timeout": 0 if once else 25, "offset": offset, "limit": 20})
        if not res.get("ok"):
            log.warning("getUpdates: %s", res.get("description"))
            if once:
                return
            time.sleep(2)
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

        with _STATE_LOCK:
            st = load_state()
            st["tg_offset"] = offset
            save_state(st)

        if once:
            return


# ──────────────────────────── Отправка с защитой от дублей ────────────────────────────

def send_daily(token, chat_id, target="tomorrow", force=False, only_published=False):
    today = today_msk()
    date = today if target == "today" else today + dt.timedelta(days=1)
    pairs, _, err = get_day_schedule(date)
    if err:
        log.error("Ошибка получения расписания на %s: %s", date.isoformat(), err)
        return False
    if only_published and pairs is None:
        log.info("Файл на %s ещё не опубликован — ожидание (режим watch).", date.isoformat())
        return True
    text = format_day_message(date, pairs)
    h = msg_hash(text)
    key = f"sent_{date.isoformat()}_{target}"
    with _STATE_LOCK:
        st = load_state()
        prev = st.get(key)
        if not force and prev == h:
            log.info("Дубль: такое же сообщение уже отправлено, пропускаю.")
            return True
        changed = bool(prev) and prev != h
    if changed:
        text = format_day_message(date, pairs, changed=True)
    ok, tg_err = tg_send(token, chat_id, text)
    if ok:
        with _STATE_LOCK:
            st = load_state()
            st[key] = msg_hash(text)
            st["last_send"] = dt.datetime.now().isoformat()
            save_state(st)
        log.info("Отправлено расписание на %s", date.isoformat())
    else:
        log.error("Ошибка Telegram: %s", tg_err)
    return ok


# ──────────────────────────── Единый постоянный режим ────────────────────────────

_SERVICE_LAST_AUTO = 0.0
_SERVICE_LAST_TRACK = 0.0
_SERVICE_LAST_WATCH_SLOT = ""
_SERVICE_FALLBACK_DATE = ""


def service_tick():
    global _SERVICE_LAST_AUTO, _SERVICE_LAST_TRACK
    global _SERVICE_LAST_WATCH_SLOT, _SERVICE_FALLBACK_DATE

    token = os.environ.get("TG_TOKEN", "").strip()
    chat_id = os.environ.get("TG_CHAT_ID", "").strip()
    if not token:
        return

    now = now_msk()
    epoch = time.time()
    if epoch - _SERVICE_LAST_AUTO >= 60:
        auto_job()
        _SERVICE_LAST_AUTO = epoch

    if epoch - _SERVICE_LAST_TRACK >= 5 * 3600:
        track_check_job()
        _SERVICE_LAST_TRACK = epoch

    if not chat_id:
        return

    minute_of_day = now.hour * 60 + now.minute
    if 13 * 60 <= minute_of_day <= 22 * 60 + 30 and now.minute in (0, 30):
        slot = now.strftime("%Y-%m-%d %H:%M")
        if slot != _SERVICE_LAST_WATCH_SLOT:
            send_daily(token, chat_id, "tomorrow", only_published=True)
            _SERVICE_LAST_WATCH_SLOT = slot

    date_key = now.date().isoformat()
    if minute_of_day >= 20 * 60 and _SERVICE_FALLBACK_DATE != date_key:
        send_daily(token, chat_id, "tomorrow", only_published=False)
        _SERVICE_FALLBACK_DATE = date_key


def run_service():
    token = os.environ.get("TG_TOKEN", "").strip()
    if not token:
        raise SystemExit("Нужен TG_TOKEN (токен бота из BotFather)")
    log.info("Бот запущен в постоянном режиме (время расписания — МСК).")
    poll_commands(token, os.environ.get("TG_CHAT_ID", ""), background=True)


# ──────────────────────────── main ────────────────────────────

def main():
    args = sys.argv[1:]
    cmd = args[0] if args else "run"

    if cmd in ("today", "tomorrow", "сегодня", "завтра"):
        date = today_msk() if cmd in ("today", "сегодня") else today_msk() + dt.timedelta(days=1)
        pairs, fname, err = get_day_schedule(date)
        if err:
            print("Ошибка:", err)
            sys.exit(1)
        print(format_day_message(date, pairs))
        print(f"\n[файл-источник: {fname or 'не найден'}]")
    elif cmd == "week":
        print(format_week_message())
    elif cmd == "lastweek":
        print(format_last_week_message())
    elif cmd == "changes":
        date = today_msk() + dt.timedelta(days=1)
        pairs, _, _ = get_day_schedule(date)
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
            print("Нужен TG_TOKEN (токен бота из BotFather)")
            sys.exit(1)
        if not chat_id:
            found, name = resolve_chat_id(token)
            if found is None:
                print("Не удалось определить chat_id. Отправьте боту /start и повторите.")
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
        with _STATE_LOCK:
            st = load_state()
            if cleanup_state(st):
                save_state(st)
                print("Очистка выполнена, устаревшие данные удалены.")
            else:
                print("Устаревших данных не найдено.")
    elif cmd in ("run", "service"):
        run_service()
    elif cmd == "poll":
        token = os.environ.get("TG_TOKEN", "")
        if not token:
            print("Нужен TG_TOKEN")
            sys.exit(1)
        max_s = None
        if "--max-seconds" in args:
            try:
                max_s = int(args[args.index("--max-seconds") + 1])
            except (IndexError, ValueError):
                print("Формат: --max-seconds 2400")
                sys.exit(1)
        poll_commands(token, os.environ.get("TG_CHAT_ID", ""), once="--once" in args, max_seconds=max_s)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
