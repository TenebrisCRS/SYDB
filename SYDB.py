import os
import re
import math
import asyncio
from typing import Optional, Tuple

import httpx
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
)
from aiogram.exceptions import TelegramAPIError

# ====== НАСТРОЙКИ ======
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8084648234:AAEfntayQc0OTk6o8KATZcg_Zd1Dmifi6kQ").strip()
NOMINATIM_UA = "SYDB-InternalBot/1.0 (contact: it@example.com)"

# Координаты склада
SRC_LAT = 55.683037
SRC_LON = 37.661695


# ====== СОСТОЯНИЯ FSM ======
class CalcStates(StatesGroup):
    WAITING_WEIGHT = State()
    TARIFF_ASSIGNED = State()
    WAITING_ADDRESS_OR_COORDS = State()
    CONFIRMING_ADDRESS = State()
    WAITING_COORDS = State()
    CALCULATING = State()


# ====== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ======
def parse_weight_kg(text: str) -> Optional[float]:
    t = text.strip().lower().replace(" ", "")
    t = t.replace(",", ".")
    m = re.fullmatch(r"([0-9]*\.?[0-9]+)(кг|kg)?", t)
    if not m:
        return None
    try:
        val = float(m.group(1))
    except ValueError:
        return None
    if val <= 0:
        return None
    return val

def parse_order_weight(text: str):
    """
    Парсит текст заказа и возвращает кортеж:
      (total_weight_or_None, missing_items_list)

    - total_weight_or_None: float — суммарный вес по распознанным позициям (или None, если веса не найдены)
    - missing_items_list: list[str] — список названий позиций, у которых НЕ указано количество

    Поддерживает варианты записи количества: 'Количество', 'Кол-во', 'Кол.' и т.п.
    """
    if not text:
        return None, []

    t = text.lower().replace("\r", " ").replace("\n", " ")
    # Разбиваем по 'название:' (учитываем варианты с запятой перед)
    parts = re.split(r"(?:^|,)\s*название:", t)
    total = 0.0
    missing = []
    parsed_any = False

    for part in parts:
        if not part.strip():
            continue

        # ищем вес, например: '14кг', '18.5 кг'
        w_match = re.search(r"([0-9]+(?:[.,][0-9]+)?)\s*кг", part)
        if not w_match:
            # если веса нет — пропускаем этот блок (возможно не товар)
            continue

        parsed_any = True
        weight = float(w_match.group(1).replace(",", "."))

        # ищем количество — поддерживаем несколько вариантов записи
        c_match = re.search(r"(?:количество|кол-во|кол\.)\s*[:\-\s]?\s*([0-9]+)", part)
        if c_match:
            qty = int(c_match.group(1))
            total += weight * qty
        else:
            # извлекаем название товара (часть до веса или слова "количество")
            name = re.split(r"[0-9]+(?:[.,][0-9]+)?\s*кг|количество|кол-во|кол\.", part)[0].strip(" ,:-")
            # убираем слово 'товар:' если осталось
            if name.startswith("товар"):
                name = name.split(":", 1)[-1].strip()
            name = name.strip()
            missing.append(name or "Неизвестный товар")

    if not parsed_any:
        return None, []

    return total, missing






def try_parse_coords(text: str) -> Optional[Tuple[float, float]]:
    t = text.strip().replace(";", ",")
    t = re.sub(r"\s+", " ", t)
    if "," in t:
        parts = [p.strip() for p in t.split(",")]
    else:
        parts = t.split(" ")
    if len(parts) != 2:
        return None
    try:
        lat = float(parts[0].replace(",", "."))
        lon = float(parts[1].replace(",", "."))
    except ValueError:
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


async def geocode_address(q: str) -> Optional[tuple[str, float, float]]:
    url = "https://nominatim.openstreetmap.org/search"
    params = {"q": q, "format": "jsonv2", "limit": 1}
    headers = {"User-Agent": NOMINATIM_UA, "Accept-Language": "ru"}
    timeout = httpx.Timeout(10.0, read=10.0)
    try:
        async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            data = r.json()
    except Exception:
        return None

    if not data:
        return None
    top = data[0]
    try:
        name = top.get("display_name") or q
        lat = float(top["lat"])
        lon = float(top["lon"])
        return name, lat, lon
    except Exception:
        return None


def assign_tariff(weight_kg: float) -> Optional[str]:
    if weight_kg <= 20:
        return "Экспресс (до 20кг)"
    if weight_kg <= 300:
        return "Карго S (до 300кг)"
    if weight_kg <= 700:
        return "Карго M (до 700кг)"
    if weight_kg <= 1400:
        return "Карго L (до 1400кг)"
    if weight_kg <= 2000:
        return "Карго XL(до 2000кг)"
    return None


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


# ====== КАЛИБРОВАННАЯ МОДЕЛЬ ======
# Формула: PRICE_RAW = BASE + RATE * distance_km
# Потом: PRICE_PADDED = PRICE_RAW * 1.10
# Итог: округление вверх до 500 ₽
TARIFF_PRICING = {
    "Экспресс (до 20кг)": {"base": 1500, "per_km": 60},
    "Карго S (до 300кг)": {"base": 1550, "per_km": 63},
    "Карго M (до 700кг)": {"base": 1850, "per_km": 75},
    "Карго L (до 1400кг)": {"base": 2100, "per_km": 85},
    "Карго XL(до 2000кг)": {"base": 3200, "per_km": 130},
}


def ceil_to_500(amount: float) -> int:
    if amount <= 0:
        return 0
    return int(math.ceil(amount / 500.0) * 500)


def calculate_price_by_km_and_tariff(tariff: str, distance_km: float) -> dict:
    model = TARIFF_PRICING[tariff]
    base = model["base"]
    rate = model["per_km"]

    raw = base + rate * max(distance_km, 0.0)
    padded = raw * 1.20
    final_amount = ceil_to_500(padded)

    return {
        "currency": "RUB",
        "amount": final_amount,
        "explain": (
            f"({base} баз.) + {rate} ₽/км × {distance_km:.2f} км = {raw:.0f} ₽; "
            f"+10% → {padded:.0f} ₽; округление ↑ до 500 → {final_amount} ₽"
        ),
    }


def yes_no_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Да")], [KeyboardButton(text="Нет")]],
        resize_keyboard=True,
        one_time_keyboard=True,
        input_field_placeholder="Выберите: Да / Нет",
    )


# ====== ХЭНДЛЕРЫ ======
dp = Dispatcher()


@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Введите вес материалов (например: 230кг или 230), "
        "или вставьте текст заказа — бот попытается посчитать общий вес автоматически.\n\n"
        "Короткий пример формата заказа:\n"
        "Название: Краска ... 14кг\n"
        "Количество: 3\n"
        "Название: Штукатурка ... 18кг\n"
        "Количество: 11\n\n"
        "Наберите /help для подробностей.",
        reply_markup=ReplyKeyboardRemove(),
    )
    await state.set_state(CalcStates.WAITING_WEIGHT)


@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer(
        "Бот для расчёта стоимости доставки.\n"
        "Шаги:\n"
        "1) Введите вес → будет присвоен тариф.\n"
        "   Можно также прислать текст заказа с полями '...<вес>кг' и 'Количество: N' — бот посчитает суммарный вес.\n"
        "2) Введите адрес или координаты.\n"
        "3) Подтвердите адрес (если вводили адрес).\n"
        "4) Мы посчитаем расстояние от точки: 55.683037, 37.661695 и стоимость.\n\n"
        "Пример заказа:\n"
        "Название: Краска ... 14кг\n"
        "Количество: 3\n"
        "Название: Штукатурка ... 18кг\n"
        "Количество: 11\n\n"
        "Команды: /start — новый расчёт, /cancel — отменить."
    )



@dp.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено. Наберите /start для нового расчёта.", reply_markup=ReplyKeyboardRemove())


@dp.message(CalcStates.WAITING_WEIGHT)
async def handle_weight(message: types.Message, state: FSMContext):
    text = message.text or ""
    # сначала пробуем простой формат: '230' или '230кг'
    weight = parse_weight_kg(text)
    if weight is None:
        total, missing = parse_order_weight(text)

        # совсем ничего не распознано — старое поведение
        if total is None and not missing:
            await message.answer(
                "Не понял вес.\n"
                "Введите, например: 230 или 230кг, "
                "или вставьте текст заказа в формате:\n"
                "Название: ... 14кг\nКоличество: 3\n(бот попробует посчитать суммарный вес)"
            )
            return

        # найдены распознанные позиции, но есть позиции без количества
        if missing:
            # Ограничим список для вывода (вдруг длинный)
            shown = missing[:20]
            items_text = "\n".join(f"- {itm}" for itm in shown)
            more_note = f"\n...и ещё {len(missing)-len(shown)} позиций" if len(missing) > len(shown) else ""
            await message.answer(
                "Внимание — у следующих товаров не указано количество:\n"
                f"{items_text}{more_note}\n\n"
                "Пожалуйста, проверьте заказ и пришлите его снова, указав количество для каждого товара."
            )
            return

        # всё ок — используем посчитанный вес
        weight = total
        await message.answer(f"Общий вес по заказу: {weight:.0f} кг.")

    tariff = assign_tariff(weight)
    if not tariff:
        await message.answer("Вес превышает лимиты тарифной сетки (> 2000 кг).")
        return

    await state.update_data(weight_kg=weight, tariff=tariff)
    await state.set_state(CalcStates.TARIFF_ASSIGNED)
    await message.answer(
        f"Тариф присвоен: <b>{tariff}</b>\n\n"
        "Теперь введите адрес или координаты доставки.",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove(),
    )
    await state.set_state(CalcStates.WAITING_ADDRESS_OR_COORDS)



@dp.message(CalcStates.WAITING_ADDRESS_OR_COORDS)
async def handle_address_or_coords(message: types.Message, state: FSMContext):
    text = (message.text or "").strip()

    coords = try_parse_coords(text)
    if coords:
        lat, lon = coords
        await state.update_data(dest_lat=lat, dest_lon=lon, confirmed_address=None)
        await proceed_to_calculation(message, state)
        return

    await message.answer("Ищу адрес, секунду…")
    candidate = await geocode_address(text)
    if not candidate:
        await message.answer("Не удалось определить адрес. Введите координаты вручную (55.7558, 37.6173).")
        await state.set_state(CalcStates.WAITING_COORDS)
        return

    display_name, lat, lon = candidate
    await state.update_data(candidate_address=display_name, candidate_lat=lat, candidate_lon=lon)
    await message.answer(
        f"Адрес доставки:\n\n<b>{display_name}</b>\n\nПодтвердить?",
        parse_mode="HTML",
        reply_markup=yes_no_kb(),
    )
    await state.set_state(CalcStates.CONFIRMING_ADDRESS)


@dp.message(CalcStates.CONFIRMING_ADDRESS, F.text.lower() == "да")
async def confirm_address_yes(message: types.Message, state: FSMContext):
    data = await state.get_data()
    lat = data.get("candidate_lat")
    lon = data.get("candidate_lon")
    addr = data.get("candidate_address")
    await state.update_data(dest_lat=lat, dest_lon=lon, confirmed_address=addr)
    await proceed_to_calculation(message, state)


@dp.message(CalcStates.CONFIRMING_ADDRESS, F.text.lower() == "нет")
async def confirm_address_no(message: types.Message, state: FSMContext):
    await message.answer("Введите координаты вручную (55.7558, 37.6173)", reply_markup=ReplyKeyboardRemove())
    await state.set_state(CalcStates.WAITING_COORDS)


@dp.message(CalcStates.CONFIRMING_ADDRESS)
async def confirm_address_other(message: types.Message):
    await message.answer("Пожалуйста, выберите «Да» или «Нет».", reply_markup=yes_no_kb())


@dp.message(CalcStates.WAITING_COORDS)
async def handle_coords_after_no(message: types.Message, state: FSMContext):
    coords = try_parse_coords(message.text or "")
    if not coords:
        await message.answer("Не понял координаты. Пример: 55.7558, 37.6173")
        return
    lat, lon = coords
    await state.update_data(dest_lat=lat, dest_lon=lon, confirmed_address=None)
    await proceed_to_calculation(message, state)


async def proceed_to_calculation(message: types.Message, state: FSMContext):
    await state.set_state(CalcStates.CALCULATING)
    data = await state.get_data()

    weight_kg = data["weight_kg"]
    tariff = data.get("tariff") or "—"
    lat = data["dest_lat"]
    lon = data["dest_lon"]
    addr = data.get("confirmed_address")

    distance_km = haversine_km(SRC_LAT, SRC_LON, lat, lon)
    result = calculate_price_by_km_and_tariff(tariff, distance_km)

    address_line = f"\nАдрес: {addr}" if addr else f"\nКоординаты: {lat:.6f}, {lon:.6f}"
    text = (
        f"✅ Расчёт выполнен:\n"
        f"Вес: {weight_kg} кг\n"
        f"Тариф: {tariff}"
        f"{address_line}\n"
        f"Расстояние от склада: ~{distance_km:.2f} км\n\n"
        f"Стоимость: {result['amount']} {result['currency']}\n"
        f"Новый расчёт — /start"
    )
    await message.answer(text, reply_markup=ReplyKeyboardRemove())
    await state.clear()


# ====== ЗАПУСК ======
async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан. Установите переменную окружения BOT_TOKEN.")
    bot = Bot(BOT_TOKEN)
    try:
        await bot.get_me()
    except TelegramAPIError as e:
        raise RuntimeError("Проверьте BOT_TOKEN — он некорректен.") from e

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
