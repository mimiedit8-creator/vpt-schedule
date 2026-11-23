import aiohttp
from app.config import settings

async def track_number(number: str) -> dict:
    if not settings.parcelle_api_key:
        raise RuntimeError("Отслеживание пока не подключено: добавь API-ключ Parcelle в PARCELLE_API_KEY.")
    url = settings.parcelle_api_url.rstrip("/") + "/tracking/getsync"
    headers = {"Accept": "application/json", "X-ApiKey": settings.parcelle_api_key}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params={"n": number}, headers=headers,
                               timeout=aiohttp.ClientTimeout(total=30)) as response:
            if response.status == 429:
                raise RuntimeError("Сервис отслеживания ограничил частоту запросов. Попробуй позже.")
            response.raise_for_status()
            data = await response.json(content_type=None)
    items = data.get("Items") or data.get("items") or []
    if isinstance(items, dict):
        items = [items]
    item = items[0] if items else data
    if item.get("ErrorText") or item.get("ErrorCode"):
        raise RuntimeError(item.get("ErrorText") or item.get("ErrorCode"))
    return {
        "carrier": item.get("DeliveryCompanyName") or "Перевозчик не определён",
        "status": item.get("CurrentStatus") or item.get("DeliveryStatusName") or "Статус пока не предоставлен",
        "location": item.get("DeliveryPointAddress") or item.get("CurrentLocation") or "Местоположение не предоставлено",
        "next": item.get("NextStep") or item.get("NextAction") or "Перевозчик пока не сообщил следующий шаг.",
        "raw": item,
    }
