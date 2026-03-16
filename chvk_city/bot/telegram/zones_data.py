"""
Зоны и цены для такси Чапаевска.
Ключевые слова улиц по районам для гибкого поиска.
"""

# Ключи — названия районов, значения — списки корней названий улиц
STREET_TO_ZONE: dict[str, list[str]] = {
    "Центр": [
        "ленина", "советская", "чапаева", "комсомольская", "красноармейская",
        "пролетарская", "володарского", "мир", "победы", "гагарина",
    ],
    "Берсол": [
        "берсол", "горького", "сергиев", "казанск", "петр", "феврон",
        "коммун", "заводская", "промышлен", "химическ",
    ],
    "Соцгород": [
        "соцгород", "соцгородск", "молодёжн", "строител", "парковая",
    ],
    "Соцпосёлок": [
        "соцпосёл", "соцпосел", "рабоч", "труд",
    ],
    "Нагорный": [
        "нагорн", "полигон", "испыта", "артиллерий",
    ],
    "Берсоль": [
        "берсол", "горького", "сергиев", "казанск", "петр", "феврон",
        "коммун", "заводская", "промышлен", "химическ",
    ],
}

# Цены по маршрутам (откуда, куда): (зона_откуда, зона_куда) -> цена
ZONE_PRICES: dict[tuple[str, str], float] = {
    ("Центр", "Центр"): 150,
    ("Центр", "Берсол"): 200,
    ("Центр", "Берсоль"): 200,
    ("Берсол", "Центр"): 200,
    ("Берсол", "Берсол"): 100,
    ("Берсоль", "Берсоль"): 100,
    ("Берсоль", "Центр"): 200,
    ("Центр", "Соцгород"): 180,
    ("Соцгород", "Центр"): 180,
    ("Центр", "Соцпосёлок"): 170,
    ("Соцпосёлок", "Центр"): 170,
    ("Центр", "Нагорный"): 250,
    ("Нагорный", "Центр"): 250,
}

# Стандартная цена, если адрес не опознан
DEFAULT_ZONE_PRICE = 150.0

# Примерное время поездки по маршруту (зона_откуда, зона_куда) -> минуты
ZONE_RIDE_MINUTES: dict[tuple[str, str], int] = {
    ("Центр", "Центр"): 10,
    ("Центр", "Берсол"): 15,
    ("Центр", "Берсоль"): 15,
    ("Берсол", "Центр"): 15,
    ("Берсол", "Берсол"): 5,
    ("Берсоль", "Берсоль"): 5,
    ("Берсоль", "Центр"): 15,
    ("Центр", "Соцгород"): 12,
    ("Соцгород", "Центр"): 12,
    ("Центр", "Нагорный"): 20,
    ("Нагорный", "Центр"): 20,
}
DEFAULT_RIDE_MINUTES = 15


def get_ride_minutes(from_address: str, to_address: str) -> int:
    """Возвращает примерное время поездки в минутах по зонам."""
    from_zone = get_zone_by_address(from_address)
    to_zone = get_zone_by_address(to_address)
    if from_zone and to_zone:
        key = (from_zone, to_zone)
        if key in ZONE_RIDE_MINUTES:
            return ZONE_RIDE_MINUTES[key]
        key_rev = (to_zone, from_zone)
        if key_rev in ZONE_RIDE_MINUTES:
            return ZONE_RIDE_MINUTES[key_rev]
    return DEFAULT_RIDE_MINUTES

# Текст приписки при стандартной цене
DEFAULT_PRICE_NOTE = "Цена рассчитана по городскому тарифу"


def get_zone_by_address(address_text: str) -> str | None:
    """
    Определяет район по тексту адреса.
    Очищает текст (нижний регистр) и проверяет вхождение ключевых слов.
    Возвращает название района или None, если не опознан.
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


def get_zone_price(from_zone: str | None, to_zone: str | None) -> tuple[float, bool]:
    """
    Возвращает (цена, признак_опознания).
    Если оба адреса опознаны — цена из ZONE_PRICES.
    Иначе — DEFAULT_ZONE_PRICE и признак False.
    """
    if from_zone and to_zone:
        key = (from_zone, to_zone)
        if key in ZONE_PRICES:
            return ZONE_PRICES[key], True
        # Попробуем обратный маршрут
        key_rev = (to_zone, from_zone)
        if key_rev in ZONE_PRICES:
            return ZONE_PRICES[key_rev], True
    return DEFAULT_ZONE_PRICE, False
