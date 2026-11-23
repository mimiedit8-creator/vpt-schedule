import aiohttp
from bs4 import BeautifulSoup
from datetime import date, timedelta
from urllib.parse import urljoin
from app.config import settings
from app.db import db

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; VPTScheduleBot/1.0)"}

async def _get_text(session, url):
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=18), headers=HEADERS) as r:
        r.raise_for_status()
        return await r.text()

async def discover_schedule_url(session):
    if settings.schedule_url:
        return settings.schedule_url
    html = await _get_text(session, settings.vpt_students_url)
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        label = " ".join(a.get_text(" ", strip=True).lower().split())
        if "расписан" in label:
            return urljoin(settings.vpt_students_url, a["href"])
    return None

def extract_group_rows(html, group):
    soup = BeautifulSoup(html, "html.parser")
    tables = soup.find_all("table")
    results = []
    for table in tables:
        for tr in table.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
            if not cells:
                continue
            joined = " | ".join(cells)
            if group.lower() in joined.lower():
                results.append(joined)
    return results

async def refresh_schedule():
    try:
        async with aiohttp.ClientSession() as session:
            url = await discover_schedule_url(session)
            if not url:
                return False, "На странице техникума не найдена ссылка на расписание."
            html = await _get_text(session, url)
            rows = extract_group_rows(html, settings.schedule_group)
            if not rows:
                return False, "Страница расписания открылась, но таблица группы РКЛ-3 не найдена."
            today = date.today()
            payload = "\n".join(rows)
            for offset in range(7):
                day = today + timedelta(days=offset)
                await db().execute(
                    "INSERT INTO schedule_cache(day, body, updated_at) VALUES(?,?,CURRENT_TIMESTAMP) "
                    "ON CONFLICT(day) DO UPDATE SET body=excluded.body, updated_at=CURRENT_TIMESTAMP",
                    (day.isoformat(), payload)
                )
            await db().commit()
            return True, f"Расписание обновлено с сайта: {url}"
    except Exception as exc:
        return False, f"Не удалось обновить расписание: {type(exc).__name__}: {exc}"

async def get_cached_schedule(day: date):
    cur = await db().execute("SELECT body, updated_at FROM schedule_cache WHERE day=?", (day.isoformat(),))
    row = await cur.fetchone()
    return row

async def format_schedule(day: date):
    row = await get_cached_schedule(day)
    if not row:
        return f"Для {day.strftime('%d.%m.%Y')} пока нет подтверждённых данных расписания. Попробуй обновить позже."
    return f"📅 **РКЛ-3 · {day.strftime('%d.%m.%Y')}**\n\n{row['body']}\n\n_Обновлено: {row['updated_at']}_"
