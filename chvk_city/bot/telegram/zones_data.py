"""
Зоны и цены для такси Чапаевска.
Матрица цен — направленная: (from_zone, to_zone) != (to_zone, from_zone) там, где цены различаются.
Правило Озон: поездка ИЗ Озона в город дороже на ~50 руб., чем В Озон.
Исключение: Озон ↔ Губашево — цена одинакова в обе стороны.
"""

import json
import logging as _logging
from pathlib import Path

import httpx

_geo_logger = _logging.getLogger("zones_data.geocoder")

# ---------------------------------------------------------------------------
# Яндекс Геокодер
# ---------------------------------------------------------------------------
YANDEX_GEOCODER_API_KEY = "241bb853-1221-40ff-9336-2e86602627fc"
_CITY_PREFIX = "Чапаевск, "

# ---------------------------------------------------------------------------
# GeoJSON — полигоны зон Чапаевска
# Файл лежит на уровень выше этого модуля: chvk_city/bot/zones.geojson.geojson
# ---------------------------------------------------------------------------

# Сопоставление description из GeoJSON → ключ в ZONE_PRICES
_GEOJSON_ZONE_MAP: dict[str, str | None] = {
    "Нагорный":                  "Нагорный",
    "Титовка":                   None,          # пропускаем — используем Начало/Конец
    "Озон":                      "Озон",
    "Проспект":                  "Проспект",
    "Кубашева":                  "Губашево",    # название в GeoJSON отличается
    "30-й":                      "30-й",
    "Центр":                     "Центр",
    "Владимирский":              "Владимир",    # GeoJSON содержит \n — strip() уберёт
    "Берсол":                    "Берсол",
    "Луч":                       "Луч",
    "Титовка (Начало)":          "Титовка (Начало)",
    "Титовка (Конец)":           "Титовка (Конец)",
    "Лесничество Чапаевское":    "Лесничество",
}

# Список (zone_name, shapely_polygon) — заполняется при импорте
_ZONE_POLYGONS: list[tuple[str, object]] = []

def _load_zone_polygons() -> None:
    """Загружает полигоны из GeoJSON в _ZONE_POLYGONS. Вызывается один раз при импорте."""
    global _ZONE_POLYGONS
    try:
        from shapely.geometry import shape as _shape
    except ImportError:
        print("[GEO] shapely не установлен — определение зон по полигонам недоступно", flush=True)
        return

    geojson_path = Path(__file__).parent.parent / "zones.geojson.geojson"
    if not geojson_path.exists():
        print(f"[GEO] GeoJSON не найден: {geojson_path}", flush=True)
        return

    try:
        data = json.loads(geojson_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[GEO] Ошибка чтения GeoJSON: {e}", flush=True)
        return

    loaded = 0
    for feature in data.get("features", []):
        geom = feature.get("geometry", {})
        if geom.get("type") != "Polygon":
            continue
        desc = (feature.get("properties") or {}).get("description", "").strip()
        zone_name = _GEOJSON_ZONE_MAP.get(desc)
        if zone_name is None:
            continue  # None = явно пропускаем, missing key = неизвестная зона
        try:
            polygon = _shape(geom)
            _ZONE_POLYGONS.append((zone_name, polygon))
            loaded += 1
        except Exception as e:
            print(f"[GEO] Ошибка построения полигона '{desc}': {e}", flush=True)

    print(f"[GEO] Загружено {loaded} полигонов из GeoJSON", flush=True)


_load_zone_polygons()

# ---------------------------------------------------------------------------
# Ключевые слова улиц по районам
# ---------------------------------------------------------------------------
STREET_TO_ZONE: dict[str, list[str]] = {
    "Центр": [
        "ленина", "советская", "чапаева", "комсомольская", "красноармейская",
        "пролетарская", "володарского", "мира", "победы", "гагарина",
        "октябрьская", "первомайская", "кирова", "карла маркса",
        "рабочая", "пушкина", "куйбышева", "суворова", "фрунзе",
        "щорса", "урицкого", "розы люксембург", "дзержинского",
    ],
    "Губашево": [
        "губашево", "губаш",
    ],
    "Проспект": [
        "проспект", "проспектн",
    ],
    "30-й": [
        "30-й", "30й", "тридцат", "30-й квартал", "30 квартал",
    ],
    "Луч": [
        "луч", "луча", "лучевой",
    ],
    "Берсол": [
        "берсол", "горького", "сергиев", "казанск",
        "заводская", "промышлен", "химическ",
    ],
    "Владимир": [
        "владимир", "владимирск",
    ],
    "Титовка (Начало)": [
        "титовка", "титов",
    ],
    "Титовка (Конец)": [
        "титовка конец", "конец титовки",
    ],
    "Садовка": [
        "садовка", "садов",
        "антропова", "южная", "полевая", "луговая", "зеленая", "зелёная",
        "колхозная", "совхозная", "дачная", "огородная",
    ],
    "Нагорный": [
        "нагорн", "полигон", "испыта", "артиллерий",
    ],
    "Лесничество": [
        "лесничеств", "лесхоз", "лесник",
    ],
    "Озон": [
        "озон", "ozon", "озоновск",
    ],
}

# ---------------------------------------------------------------------------
# Словарь популярных мест (POI)
# Добавляйте новые точки самостоятельно — бот не будет обращаться к Яндексу.
# Формат ключей: строчные слова/фразы, которые вводит пользователь.
# Координаты: lon (долгота), lat (широта).
# ---------------------------------------------------------------------------
POPULAR_PLACES: dict[str, dict] = {
    "озон":       {"display": "Склад Озон", "lon": 49.745706, "lat": 52.960590, "zone": "Озон"},
    "ozon":       {"display": "Склад Озон", "lon": 49.745706, "lat": 52.960590, "zone": "Озон"},
    "склад озон": {"display": "Склад Озон", "lon": 49.745706, "lat": 52.960590, "zone": "Озон"},
    "на озон":    {"display": "Склад Озон", "lon": 49.745706, "lat": 52.960590, "zone": "Озон"},
    "амбар":      {"display": "ТЦ Амбар, Южное шоссе, 5", "lon": 50.271670, "lat": 53.183400, "zone": None},
    "тц амбар":   {"display": "ТЦ Амбар, Южное шоссе, 5", "lon": 50.271670, "lat": 53.183400, "zone": None},
}


def get_poi(address: str) -> dict | None:
    """Ищет адрес в словаре популярных мест. Возвращает dict с display/lon/lat/zone или None."""
    if not address:
        return None
    return POPULAR_PLACES.get(address.lower().strip())


# ---------------------------------------------------------------------------
# Матрица цен — СТРОГО направленная: (from, to) -> цена
# Не используй симметричный fallback для этой таблицы!
# ---------------------------------------------------------------------------
ZONE_PRICES: dict[tuple[str, str], float] = {
    # ── ГУБАШЕВО ────────────────────────────────────────────────────────────
    ("Губашево", "Губашево"): 148, ("Губашево", "Проспект"): 148, ("Губашево", "30-й"): 148,
    ("Губашево", "Центр"): 198, ("Губашево", "Луч"): 238, ("Губашево", "Берсол"): 258,
    ("Губашево", "Владимир"): 248, ("Губашево", "Титовка (Начало)"): 288, ("Губашево", "Титовка (Конец)"): 348,
    ("Губашево", "Садовка"): 398, ("Губашево", "Нагорный"): 378, ("Губашево", "Озон"): 298,
    ("Губашево", "Лесничество"): 398,

    # ── ПРОСПЕКТ ────────────────────────────────────────────────────────────
    ("Проспект", "Губашево"): 148, ("Проспект", "Проспект"): 118, ("Проспект", "30-й"): 148,
    ("Проспект", "Центр"): 198, ("Проспект", "Луч"): 238, ("Проспект", "Берсол"): 258,
    ("Проспект", "Владимир"): 248, ("Проспект", "Титовка (Начало)"): 288, ("Проспект", "Титовка (Конец)"): 348,
    ("Проспект", "Садовка"): 398, ("Проспект", "Нагорный"): 378, ("Проспект", "Озон"): 248,
    ("Проспект", "Лесничество"): 398,

    # ── 30-й РАЙОН ──────────────────────────────────────────────────────────
    ("30-й", "Губашево"): 148, ("30-й", "Проспект"): 148, ("30-й", "30-й"): 118,
    ("30-й", "Центр"): 148, ("30-й", "Луч"): 198, ("30-й", "Берсол"): 198,
    ("30-й", "Владимир"): 198, ("30-й", "Титовка (Начало)"): 238, ("30-й", "Титовка (Конец)"): 298,
    ("30-й", "Садовка"): 348, ("30-й", "Нагорный"): 298, ("30-й", "Озон"): 298,
    ("30-й", "Лесничество"): 348,

    # ── ЦЕНТР ───────────────────────────────────────────────────────────────
    ("Центр", "Губашево"): 198, ("Центр", "Проспект"): 198, ("Центр", "30-й"): 148,
    ("Центр", "Центр"): 118, ("Центр", "Луч"): 148, ("Центр", "Берсол"): 198,
    ("Центр", "Владимир"): 198, ("Центр", "Титовка (Начало)"): 198, ("Центр", "Титовка (Конец)"): 258,
    ("Центр", "Садовка"): 288, ("Центр", "Нагорный"): 248, ("Центр", "Озон"): 328,
    ("Центр", "Лесничество"): 308,

    # ── ЛУЧ ─────────────────────────────────────────────────────────────────
    ("Луч", "Губашево"): 238, ("Луч", "Проспект"): 238, ("Луч", "30-й"): 198,
    ("Луч", "Центр"): 148, ("Луч", "Луч"): 118, ("Луч", "Берсол"): 148,
    ("Луч", "Владимир"): 218, ("Луч", "Титовка (Начало)"): 178, ("Луч", "Титовка (Конец)"): 238,
    ("Луч", "Садовка"): 248, ("Луч", "Нагорный"): 198, ("Луч", "Озон"): 298,
    ("Луч", "Лесничество"): 288,

    # ── БЕРСОЛ ──────────────────────────────────────────────────────────────
    ("Берсол", "Губашево"): 258, ("Берсол", "Проспект"): 258, ("Берсол", "30-й"): 198,
    ("Берсол", "Центр"): 198, ("Берсол", "Луч"): 148, ("Берсол", "Берсол"): 118,
    ("Берсол", "Владимир"): 158, ("Берсол", "Титовка (Начало)"): 158, ("Берсол", "Титовка (Конец)"): 218,
    ("Берсол", "Садовка"): 288, ("Берсол", "Нагорный"): 218, ("Берсол", "Озон"): 328,
    ("Берсол", "Лесничество"): 268,

    # ── ВЛАДИМИР ────────────────────────────────────────────────────────────
    ("Владимир", "Губашево"): 248, ("Владимир", "Проспект"): 248, ("Владимир", "30-й"): 198,
    ("Владимир", "Центр"): 198, ("Владимир", "Луч"): 218, ("Владимир", "Берсол"): 158,
    ("Владимир", "Владимир"): 148, ("Владимир", "Титовка (Начало)"): 218, ("Владимир", "Титовка (Конец)"): 278,
    ("Владимир", "Садовка"): 298, ("Владимир", "Нагорный"): 308, ("Владимир", "Озон"): 398,
    ("Владимир", "Лесничество"): 328,

    # ── ТИТОВКА НАЧАЛО ──────────────────────────────────────────────────────
    ("Титовка (Начало)", "Губашево"): 288, ("Титовка (Начало)", "Проспект"): 288, ("Титовка (Начало)", "30-й"): 238,
    ("Титовка (Начало)", "Центр"): 198, ("Титовка (Начало)", "Луч"): 178, ("Титовка (Начало)", "Берсол"): 158,
    ("Титовка (Начало)", "Владимир"): 218, ("Титовка (Начало)", "Титовка (Начало)"): 148, ("Титовка (Начало)", "Титовка (Конец)"): 198,
    ("Титовка (Начало)", "Садовка"): 248, ("Титовка (Начало)", "Нагорный"): 248, ("Титовка (Начало)", "Озон"): 328,
    ("Титовка (Начало)", "Лесничество"): 248,

    # ── ТИТОВКА КОНЕЦ ───────────────────────────────────────────────────────
    ("Титовка (Конец)", "Губашево"): 348, ("Титовка (Конец)", "Проспект"): 358, ("Титовка (Конец)", "30-й"): 298,
    ("Титовка (Конец)", "Центр"): 258, ("Титовка (Конец)", "Луч"): 238, ("Титовка (Конец)", "Берсол"): 218,
    ("Титовка (Конец)", "Владимир"): 278, ("Титовка (Конец)", "Титовка (Начало)"): 198, ("Титовка (Конец)", "Титовка (Конец)"): 198,
    ("Титовка (Конец)", "Садовка"): 218, ("Титовка (Конец)", "Нагорный"): 248, ("Титовка (Конец)", "Озон"): 298,
    ("Титовка (Конец)", "Лесничество"): 248,

    # ── САДОВКА ─────────────────────────────────────────────────────────────
    ("Садовка", "Губашево"): 398, ("Садовка", "Проспект"): 398, ("Садовка", "30-й"): 348,
    ("Садовка", "Центр"): 288, ("Садовка", "Луч"): 248, ("Садовка", "Берсол"): 288,
    ("Садовка", "Владимир"): 298, ("Садовка", "Титовка (Начало)"): 248, ("Садовка", "Титовка (Конец)"): 218,
    ("Садовка", "Садовка"): 198, ("Садовка", "Нагорный"): 218, ("Садовка", "Озон"): 278,
    ("Садовка", "Лесничество"): 268,

    # ── НАГОРНЫЙ ────────────────────────────────────────────────────────────
    ("Нагорный", "Губашево"): 378, ("Нагорный", "Проспект"): 378, ("Нагорный", "30-й"): 298,
    ("Нагорный", "Центр"): 248, ("Нагорный", "Луч"): 198, ("Нагорный", "Берсол"): 218,
    ("Нагорный", "Владимир"): 308, ("Нагорный", "Титовка (Начало)"): 248, ("Нагорный", "Титовка (Конец)"): 248,
    ("Нагорный", "Садовка"): 218, ("Нагорный", "Нагорный"): 198, ("Нагорный", "Озон"): 248,
    ("Нагорный", "Лесничество"): 298,

    # ── ОЗОН ────────────────────────────────────────────────────────────────
    ("Озон", "Губашево"): 298, ("Озон", "Проспект"): 298, ("Озон", "30-й"): 348,
    ("Озон", "Центр"): 378, ("Озон", "Луч"): 348, ("Озон", "Берсол"): 378,
    ("Озон", "Владимир"): 448, ("Озон", "Титовка (Начало)"): 378, ("Озон", "Титовка (Конец)"): 358,
    ("Озон", "Садовка"): 328, ("Озон", "Нагорный"): 298, ("Озон", "Озон"): 248,
    ("Озон", "Лесничество"): 408,

    # ── ЛЕСНИЧЕСТВО ─────────────────────────────────────────────────────────
    ("Лесничество", "Губашево"): 98, ("Лесничество", "Проспект"): 98, ("Лесничество", "30-й"): 98,
    ("Лесничество", "Центр"): 98, ("Лесничество", "Луч"): 98, ("Лесничество", "Берсол"): 98,
    ("Лесничество", "Владимир"): 98, ("Лесничество", "Титовка (Начало)"): 98, ("Лесничество", "Титовка (Конец)"): 98,
    ("Лесничество", "Садовка"): 98, ("Лесничество", "Нагорный"): 98, ("Лесничество", "Озон"): 98,
    ("Лесничество", "Лесничество"): 98,
}

# ---------------------------------------------------------------------------
# Стандартная цена, если маршрут не опознан
# ---------------------------------------------------------------------------
DEFAULT_ZONE_PRICE = 150.0
DEFAULT_PRICE_NOTE = "Цена рассчитана по городскому тарифу"

# ---------------------------------------------------------------------------
# Время поездки по маршруту (минуты) — симметрично, fallback разрешён
# ---------------------------------------------------------------------------
ZONE_RIDE_MINUTES: dict[tuple[str, str], int] = {
    ("Центр", "Центр"):              8,
    ("Центр", "Губашево"):           15,
    ("Центр", "Проспект"):           10,
    ("Центр", "30-й"):               12,
    ("Центр", "Луч"):                12,
    ("Центр", "Берсол"):             15,
    ("Центр", "Владимир"):           18,
    ("Центр", "Титовка (Начало)"):   20,
    ("Центр", "Титовка (Конец)"):    25,
    ("Центр", "Садовка"):            20,
    ("Центр", "Нагорный"):           20,
    ("Центр", "Лесничество"):        15,
    ("Центр", "Озон"):               35,
    ("Губашево", "Озон"):            30,
    ("30-й", "Озон"):                30,
    ("Луч", "Озон"):                 30,
    ("Проспект", "Озон"):            32,
    ("Берсол", "Озон"):              35,
}
DEFAULT_RIDE_MINUTES = 15

# ---------------------------------------------------------------------------
# Загородный тариф
# ---------------------------------------------------------------------------
INTERCITY_RATE_PER_KM = 28.0          # ₽ за км
INTERCITY_ROAD_FACTOR = 1.3           # коэффициент дороги (прямая → реальный путь)
INTERCITY_NOTE = "🚕 Маршрут за пределами города. Расчет: 28 ₽/км"


# ---------------------------------------------------------------------------
# Публичные функции
# ---------------------------------------------------------------------------

async def reverse_geocode(lat: float, lon: float) -> str | None:
    """
    Обратное геокодирование: координаты → читаемый адрес через Яндекс Геокодер.
    Возвращает строку вида 'улица Ленина, 10' или None при ошибке.
    """
    print(f"[GEO] Reverse geocode: lat={lat:.5f}, lon={lon:.5f}", flush=True)
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                "https://geocode-maps.yandex.ru/1.x/",
                params={
                    "apikey": YANDEX_GEOCODER_API_KEY,
                    "geocode": f"{lon},{lat}",
                    "format": "json",
                    "results": 1,
                    "lang": "ru_RU",
                },
            )
        resp.raise_for_status()
        data = resp.json()
        members = (
            data.get("response", {})
                .get("GeoObjectCollection", {})
                .get("featureMember", [])
        )
        if not members:
            return None
        meta = members[0]["GeoObject"].get("metaDataProperty", {}).get("GeocoderMetaData", {})
        # Prefer short formatted address, fall back to full text
        formatted = (
            meta.get("Address", {}).get("formatted")
            or meta.get("text")
        )
        print(f"[GEO] Reverse geocode result: {formatted!r}", flush=True)
        return formatted or None
    except Exception as e:
        print(f"[GEO] Reverse geocode error ({type(e).__name__}): {e}", flush=True)
        return None


def get_zone_by_coords(lon: float, lat: float) -> str | None:
    """
    Определяет зону по координатам (WGS-84) через полигоны из GeoJSON.
    Возвращает название зоны или None.
    """
    if not _ZONE_POLYGONS:
        print("[GEO] get_zone_by_coords: полигоны не загружены", flush=True)
        return None
    try:
        from shapely.geometry import Point as _Point
        pt_normal  = _Point(lon, lat)   # стандартный порядок GeoJSON: x=lon, y=lat
        pt_swapped = _Point(lat, lon)   # тест: вдруг Яндекс путает порядок

        zone_normal  = None
        zone_swapped = None
        for zone_name, polygon in _ZONE_POLYGONS:
            if zone_normal  is None and polygon.contains(pt_normal):
                zone_normal = zone_name
            if zone_swapped is None and polygon.contains(pt_swapped):
                zone_swapped = zone_name
            if zone_normal and zone_swapped:
                break

        result = zone_normal or zone_swapped
        print(
            f"[GEO] Coords: lon={lon:.5f}, lat={lat:.5f} | "
            f"Zone(normal)={zone_normal!r} | Zone(swapped)={zone_swapped!r} | Result={result!r}",
            flush=True,
        )
        return result
    except Exception as e:
        print(f"[GEO] Ошибка point-in-polygon: {e}", flush=True)
    return None


async def get_zone_by_address_geocoded(address: str) -> str | None:
    """
    Определяет зону через Яндекс Геокодер + полигоны GeoJSON:
      1. Логирует исходный адрес.
      2. Добавляет префикс «Чапаевск, » и отправляет в API.
      3. Логирует координаты и полный ответ Яндекса.
      4. Определяет зону по полигонам (shapely), при неудаче — по ключевым словам.
      5. Логирует итоговую зону.
    """
    original = (address or "").strip()

    # Проверяем POI — если адрес известен, сразу возвращаем зону без запроса в Яндекс
    poi = get_poi(original)
    if poi:
        print(f"[GEO] POI: {original!r} → zone={poi['zone']!r}, coords=({poi['lon']}, {poi['lat']})", flush=True)
        return poi["zone"]

    full_address = _CITY_PREFIX + original
    print(f"[GEO] Исходный адрес: {original!r}", flush=True)
    print(f"[GEO] Запрос в Яндекс: {full_address!r}", flush=True)

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                "https://geocode-maps.yandex.ru/1.x/",
                params={
                    "apikey": YANDEX_GEOCODER_API_KEY,
                    "geocode": full_address,
                    "format": "json",
                    "results": 1,
                    "lang": "ru_RU",
                },
            )
        resp.raise_for_status()
        data = resp.json()

        members = (
            data
            .get("response", {})
            .get("GeoObjectCollection", {})
            .get("featureMember", [])
        )
        if not members:
            print(f"[GEO] Яндекс не нашёл адрес: {full_address!r}", flush=True)
            return None

        geo = members[0]["GeoObject"]
        pos = geo.get("Point", {}).get("pos", "")
        formatted = (
            geo.get("metaDataProperty", {})
               .get("GeocoderMetaData", {})
               .get("text", "")
        )

        parts = pos.split()
        lon_str = parts[0] if len(parts) > 0 else None
        lat_str = parts[1] if len(parts) > 1 else None
        print(f"[GEO] Координаты: lon={lon_str}, lat={lat_str}", flush=True)
        print(f"[GEO] Полный адрес от Яндекс: {formatted!r}", flush=True)

        # Определяем зону по полигонам (точнее), с фоллбэком на ключевые слова
        zone = None
        if lon_str and lat_str:
            try:
                zone = get_zone_by_coords(float(lon_str), float(lat_str))
            except ValueError:
                pass
        if zone is None:
            zone = get_zone_by_address(formatted)
            print(f"[GEO] Зона по ключевым словам (fallback): {zone!r}", flush=True)
        else:
            print(f"[GEO] Зона по полигону: {zone!r}", flush=True)
        return zone

    except Exception as e:
        print(f"[GEO] Ошибка геокодинга ({type(e).__name__}): {e}", flush=True)
        return None

def get_zone_by_address(address_text: str) -> str | None:
    """
    Определяет район по тексту адреса (поиск по ключевым словам).
    Возвращает название района или None.
    """
    if not address_text or not isinstance(address_text, str):
        return None
    cleaned = address_text.lower().strip()
    if not cleaned:
        return None
    for zone, keywords in STREET_TO_ZONE.items():
        for kw in keywords:
            if kw in cleaned:
                return zone
    return None


def haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Прямое расстояние между двумя точками WGS-84 (км)."""
    from math import radians, sin, cos, sqrt, atan2
    R = 6371.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


async def get_driving_distance_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """
    Получает расстояние по дороге через Yandex Router API.
    При ошибке — Haversine × INTERCITY_ROAD_FACTOR.
    """
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                "https://api.routing.yandex.net/v2/route",
                params={
                    "apikey": YANDEX_GEOCODER_API_KEY,
                    "waypoints": f"{lon1},{lat1}|{lon2},{lat2}",
                    "mode": "driving",
                },
            )
        if resp.status_code == 200:
            rdata = resp.json()
            legs = rdata.get("route", {}).get("legs", [])
            total_m = sum(
                step.get("length", {}).get("value", 0)
                for leg in legs
                for step in leg.get("steps", [])
            )
            if total_m > 0:
                km = total_m / 1000.0
                print(f"[GEO] Yandex Router: {km:.1f} км", flush=True)
                return km
    except Exception as e:
        print(f"[GEO] Yandex Router error ({type(e).__name__}): {e}", flush=True)

    # Fallback: Haversine × 1.3
    km = haversine_km(lon1, lat1, lon2, lat2) * INTERCITY_ROAD_FACTOR
    print(f"[GEO] Haversine fallback: {km:.1f} км", flush=True)
    return km


async def geocode_full(address: str) -> dict:
    """
    Полное геокодирование: возвращает {"zone": str|None, "lon": float|None, "lat": float|None}.
    Порядок: POI → Яндекс + полигоны → ключевые слова.
    """
    original = (address or "").strip()

    # POI
    poi = get_poi(original)
    if poi:
        print(f"[GEO] geocode_full POI: {original!r} → {poi['zone']!r}", flush=True)
        return {"zone": poi["zone"], "lon": poi["lon"], "lat": poi["lat"]}

    result: dict = {"zone": None, "lon": None, "lat": None}
    full_address = _CITY_PREFIX + original
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                "https://geocode-maps.yandex.ru/1.x/",
                params={
                    "apikey": YANDEX_GEOCODER_API_KEY,
                    "geocode": full_address,
                    "format": "json",
                    "results": 1,
                    "lang": "ru_RU",
                },
            )
        resp.raise_for_status()
        data = resp.json()
        members = (
            data.get("response", {})
                .get("GeoObjectCollection", {})
                .get("featureMember", [])
        )
        if members:
            geo = members[0]["GeoObject"]
            pos = geo.get("Point", {}).get("pos", "")
            parts = pos.split()
            if len(parts) >= 2:
                lon, lat = float(parts[0]), float(parts[1])
                result["lon"] = lon
                result["lat"] = lat
                zone = get_zone_by_coords(lon, lat)
                if zone is None:
                    formatted = (
                        geo.get("metaDataProperty", {})
                           .get("GeocoderMetaData", {})
                           .get("text", "")
                    )
                    zone = get_zone_by_address(formatted)
                result["zone"] = zone
                print(f"[GEO] geocode_full: {original!r} → zone={zone!r}, lon={lon:.5f}, lat={lat:.5f}", flush=True)
    except Exception as e:
        print(f"[GEO] geocode_full error ({type(e).__name__}): {e}", flush=True)
        result["zone"] = get_zone_by_address(original)

    return result


def _shorten_address(text: str) -> str:
    """
    Убирает бюрократические слова из адреса Яндекс.
    Оставляет 2–3 значимые части: Город/Объект + Улица + Дом.
    """
    # 1. Убираем страну и федеральные округа (prefix-strip)
    noise_prefixes = (
        "Россия, ", "Russia, ",
        "Приволжский федеральный округ, ",
        "Центральный федеральный округ, ",
        "Северо-Западный федеральный округ, ",
        "Южный федеральный округ, ",
        "Уральский федеральный округ, ",
        "Сибирский федеральный округ, ",
        "Дальневосточный федеральный округ, ",
        "Северо-Кавказский федеральный округ, ",
    )
    for prefix in noise_prefixes:
        if text.startswith(prefix):
            text = text[len(prefix):]
            break

    # 2. Убираем мусорные слова-термины из всей строки
    noise_words = (
        "городской округ ",
        "поселение ",
        "муниципальный район ",
        "имени И.А. Безрукова",
        "имени И. А. Безрукова",
    )
    for w in noise_words:
        text = text.replace(w, "")

    # 3. Специальный случай: аэропорт Курумоч
    if "Курумоч" in text:
        # Убираем лишние части, оставляем "Самара, аэропорт Курумоч"
        if "аэропорт" in text.lower():
            return "Самара, аэропорт Курумоч"
        # Посёлок или ж/д станция
        parts = [p.strip() for p in text.split(",") if p.strip()]
        return ", ".join(parts[-2:]) if len(parts) >= 2 else text.strip()

    # 4. Убираем дублирующееся слово (напр. "Самара, Самара, ул. Ленина")
    parts = [p.strip() for p in text.split(",") if p.strip()]
    deduped: list[str] = []
    seen: set[str] = set()
    for p in parts:
        key = p.lower()
        if key not in seen:
            deduped.append(p)
            seen.add(key)
    parts = deduped

    # 5. Берём последние 2–3 части (Город + Улица + Дом)
    if len(parts) > 3:
        parts = parts[-3:]

    return ", ".join(parts)


async def geocode_suggest(query: str, n: int = 4) -> list[dict]:
    """
    Возвращает до n вариантов адреса с координатами (для кнопок-подсказок).
    Использует гео-смещение к Чапаевску, чтобы сначала показывать местные адреса,
    но не ограничивает — межгород (Курумоч, Самара) тоже найдёт.
    Каждый элемент: {"display": str, "lon": float, "lat": float, "zone": str|None}
    """
    original = (query or "").strip()
    if not original:
        return []

    # POI — один точный результат, подтверждение не нужно
    poi = get_poi(original)
    if poi:
        return [{"display": poi["display"], "lon": poi["lon"], "lat": poi["lat"], "zone": poi["zone"]}]

    items: list[dict] = []
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            resp = await client.get(
                "https://geocode-maps.yandex.ru/1.x/",
                params={
                    "apikey": YANDEX_GEOCODER_API_KEY,
                    "geocode": original,
                    "format": "json",
                    "results": n + 4,
                    "lang": "ru_RU",
                    "ll": "49.794231,52.984168",   # центр Чапаевска
                    "spn": "2.0,1.5",              # охват ~Самарская обл.
                    "rspn": "1",                   # строго внутри bbox
                },
            )
        resp.raise_for_status()
        data = resp.json()
        members = (
            data.get("response", {})
                .get("GeoObjectCollection", {})
                .get("featureMember", [])
        )
        _SAMARA_KEYWORDS = ("самар", "чапаевск", "новокуйбышевск", "сызрань", "тольятти", "курумоч")
        for member in members:
            if len(items) >= n:
                break
            geo_obj = member.get("GeoObject", {})
            pos = geo_obj.get("Point", {}).get("pos", "")
            parts = pos.split()
            if len(parts) < 2:
                continue
            lon, lat = float(parts[0]), float(parts[1])
            full_text = (
                geo_obj.get("metaDataProperty", {})
                       .get("GeocoderMetaData", {})
                       .get("text", "")
                or original
            )
            # Фильтр: только Самарская область и крупные города региона
            if not any(kw in full_text.lower() for kw in _SAMARA_KEYWORDS):
                continue
            display = _shorten_address(full_text)
            zone = get_zone_by_coords(lon, lat)
            if zone is None:
                zone = get_zone_by_address(full_text)
            items.append({"display": display, "lon": lon, "lat": lat, "zone": zone})
            print(
                f"[SUGGEST] {display!r} → zone={zone!r}, lon={lon:.5f}, lat={lat:.5f}",
                flush=True,
            )
    except Exception as e:
        print(f"[GEO] geocode_suggest error ({type(e).__name__}): {e}", flush=True)

    return items


def get_zone_price(from_zone: str | None, to_zone: str | None) -> tuple[float, bool]:
    """
    Возвращает (цена, признак_опознания).
    Матрица СТРОГО направленная — обратный fallback НЕ применяется.
    """
    if from_zone and to_zone:
        key = (from_zone, to_zone)
        if key in ZONE_PRICES:
            return ZONE_PRICES[key], True
    return DEFAULT_ZONE_PRICE, False


def get_ride_minutes(from_address: str, to_address: str) -> int:
    """Возвращает примерное время поездки в минутах по зонам."""
    from_zone = get_zone_by_address(from_address)
    to_zone = get_zone_by_address(to_address)
    if from_zone and to_zone:
        key = (from_zone, to_zone)
        if key in ZONE_RIDE_MINUTES:
            return ZONE_RIDE_MINUTES[key]
        # Для времени симметрия допустима
        key_rev = (to_zone, from_zone)
        if key_rev in ZONE_RIDE_MINUTES:
            return ZONE_RIDE_MINUTES[key_rev]
    return DEFAULT_RIDE_MINUTES
