import asyncpg
import logging
import os
from dotenv import load_dotenv
from typing import List, Dict, Any, Optional

# Загружаем переменные окружения
load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")

# Глобальный пул соединений для повышения производительности
pool = None


async def create_pool():
    """Инициализирует пул соединений с базой данных."""
    global pool
    if pool is None:
        try:
            pool = await asyncpg.create_pool(DATABASE_URL)
            logging.info("Пул соединений с PostgreSQL успешно создан.")
        except Exception as e:
            logging.critical(f"Не удалось создать пул соединений с PostgreSQL: {e}")
            # В реальном приложении здесь можно остановить работу бота
            exit()


async def init_db():
    """
    Создает таблицы в базе данных, если они еще не существуют.
    Выполняется один раз при старте бота.
    """
    await create_pool()  # Убедимся, что пул создан
    async with pool.acquire() as conn:
        await conn.execute('''
                           CREATE TABLE IF NOT EXISTS users
                           (
                               user_id           BIGINT PRIMARY KEY,
                               username          TEXT,
                               full_name         TEXT,
                               phone_number      TEXT,
                               status            TEXT        DEFAULT 'pending', -- pending, approved, banned
                               registration_date TIMESTAMPTZ DEFAULT NOW()
                           );
                           ''')
        await conn.execute('''
                           CREATE TABLE IF NOT EXISTS auctions
                           (
                               auction_id         SERIAL PRIMARY KEY,
                               title              TEXT,
                               description        TEXT,
                               photo_id           TEXT,
                               start_price        REAL,
                               min_step           REAL DEFAULT 1000,
                               max_step           REAL DEFAULT 10000,
                               blitz_price        REAL,
                               end_time           TIMESTAMPTZ,
                               status             TEXT DEFAULT 'active', -- active, finished, canceled
                               winner_id          BIGINT,
                               final_price        REAL,
                               channel_message_id BIGINT,
                               cooldown_minutes INTEGER DEFAULT 10,
                               cooldown_off_before_end_minutes INTEGER DEFAULT 30
                           );
                           ''')
        # На случай, если таблица уже существовала раньше — добавим новые столбцы
        await conn.execute("ALTER TABLE auctions ADD COLUMN IF NOT EXISTS cooldown_minutes INTEGER DEFAULT 10")
        await conn.execute("ALTER TABLE auctions ADD COLUMN IF NOT EXISTS cooldown_off_before_end_minutes INTEGER DEFAULT 30")

        await conn.execute('''
                           CREATE TABLE IF NOT EXISTS bids
                           (
                               bid_id     SERIAL PRIMARY KEY,
                               auction_id INTEGER REFERENCES auctions (auction_id) ON DELETE CASCADE,
                               user_id    BIGINT REFERENCES users (user_id) ON DELETE CASCADE,
                               bid_amount REAL,
                               bid_time   TIMESTAMPTZ DEFAULT NOW()
                           );
                           ''')

        # --- НОВАЯ ТАБЛИЦА SETTINGS ---
        await conn.execute('''
                           CREATE TABLE IF NOT EXISTS settings
                           (
                               setting_key   TEXT PRIMARY KEY,
                               setting_value TEXT
                           );
                           ''')
        # Устанавливаем значение по умолчанию для автопринятия (если еще не установлено)
        await conn.execute('''
                           INSERT INTO settings (setting_key, setting_value)
                           VALUES ('auto_approve_enabled', 'false')
                           ON CONFLICT (setting_key) DO NOTHING;
                           ''')
        # --- КОНЕЦ НОВОЙ ТАБЛИЦЫ ---

        logging.info("Проверка таблиц в БД завершена.")


# --- Функции для работы с пользователями (Users) ---

async def add_user_request(user_id: int, username: str, full_name: str, phone_number: str):
    """Добавляет заявку на регистрацию пользователя в статусе 'pending'."""
    sql = """
          INSERT INTO users (user_id, username, full_name, phone_number, status)
          VALUES ($1, $2, $3, $4, 'pending')
          ON CONFLICT (user_id) DO UPDATE SET full_name    = EXCLUDED.full_name,
                                              phone_number = EXCLUDED.phone_number,
                                              status       = 'pending',
                                              username     = EXCLUDED.username; \
          """
    async with pool.acquire() as conn:
        await conn.execute(sql, user_id, username, full_name, phone_number)


async def get_user_status(user_id: int) -> Optional[str]:
    """Получает статус пользователя по его ID."""
    async with pool.acquire() as conn:
        status = await conn.fetchval("SELECT status FROM users WHERE user_id = $1", user_id)
        return status


async def update_user_status(user_id: int, status: str):
    """Обновляет статус пользователя (approved, banned)."""
    async with pool.acquire() as conn:
        await conn.execute("UPDATE users SET status = $1 WHERE user_id = $2", status, user_id)
        logging.info(f"Статус пользователя {user_id} обновлен на {status}.")


async def update_user_username(user_id: int, username: str):
    """Обновляет username пользователя при каждом взаимодействии."""
    async with pool.acquire() as conn:
        await conn.execute("UPDATE users SET username = $1 WHERE user_id = $2 AND username IS DISTINCT FROM $1",
                           username, user_id)



async def get_pending_users() -> List[Dict[str, Any]]:
    """Возвращает список пользователей в статусе 'pending'."""
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id, username, full_name, phone_number FROM users WHERE status = 'pending'")
        return [dict(r) for r in rows]

async def bulk_update_user_status(user_ids: List[int], status: str):
    """Массово обновляет статус пользователей."""
    if not user_ids:
        return 0
    # Преобразуем список ID в строку для запроса (1, 2, 3)
    ids_tuple = tuple(user_ids)
    sql = f"UPDATE users SET status = $1 WHERE user_id = ANY($2::bigint[])"
    async with pool.acquire() as conn:
        result = await conn.execute(sql, status, ids_tuple)
        # result возвращает строку вида "UPDATE N", извлекаем N
        try:
            updated_count = int(result.split()[-1])
            logging.info(f"Массово обновлен статус {updated_count} пользователей на '{status}'.")
            return updated_count
        except (IndexError, ValueError):
            logging.warning(f"Не удалось получить количество обновленных строк при bulk_update_user_status.")
            return 0



# --- Функции для работы с аукционами (Auctions) ---

async def create_auction(data: Dict[str, Any]) -> int:
    """Создает новый аукцион и возвращает его ID."""
    sql = """
          INSERT INTO auctions
          (title, description, photo_id, start_price, min_step, cooldown_minutes, cooldown_off_before_end_minutes, blitz_price, end_time)
          VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
          RETURNING auction_id; \
          """
    async with pool.acquire() as conn:
        auction_id = await conn.fetchval(
            sql,
            data['title'],
            data['description'],
            data['photo'],
            data['start_price'],
            data['min_step'],
            data['cooldown_minutes'],
            data['cooldown_off_before_end_minutes'],
            data.get('blitz_price'),
            data['end_time']
        )
        return auction_id

async def get_auctions(limit: int = 10) -> List[Dict[str, Any]]:
    """Возвращает последние аукционы (активные и завершенные)."""
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM auctions ORDER BY auction_id DESC LIMIT $1", limit)
        return [dict(r) for r in rows]

async def count_auctions() -> int:
    """Возвращает общее количество аукционов."""
    async with pool.acquire() as conn:
        row = await conn.fetchval("SELECT COUNT(*) FROM auctions")
        return int(row)


async def get_auctions_page(limit: int, offset: int) -> List[Dict[str, Any]]:
    """Возвращает страницу аукционов."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM auctions ORDER BY auction_id DESC LIMIT $1 OFFSET $2",
            limit, offset
        )
        return [dict(r) for r in rows]



async def update_auction_end_time(auction_id: int, new_end_time):
    """Обновляет время окончания аукциона."""
    async with pool.acquire() as conn:
        await conn.execute("UPDATE auctions SET end_time = $1 WHERE auction_id = $2", new_end_time, auction_id)


async def get_user_by_username(username: str) -> Optional[Dict[str, Any]]:
    """Возвращает пользователя по username (без @)."""
    if username.startswith('@'):
        username = username[1:]
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE username = $1", username)
        return dict(row) if row else None


async def get_user_by_phone(phone: str) -> Optional[Dict[str, Any]]:
    """Возвращает пользователя по номеру телефона."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE phone_number = $1", phone)
        return dict(row) if row else None




async def get_active_auction() -> Optional[Dict[str, Any]]:
    """Возвращает данные текущего активного аукциона."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM auctions WHERE status = 'active' ORDER BY auction_id DESC LIMIT 1")
        return dict(row) if row else None


async def set_auction_message_id(auction_id: int, message_id: int):
    """Сохраняет ID сообщения аукциона в канале."""
    async with pool.acquire() as conn:
        await conn.execute("UPDATE auctions SET channel_message_id = $1 WHERE auction_id = $2", message_id, auction_id)


async def finish_auction(auction_id: int, winner_id: Optional[int], final_price: Optional[float]):
    """Завершает аукцион, обновляя его статус и данные о победителе."""
    sql = "UPDATE auctions SET status = 'finished', winner_id = $1, final_price = $2 WHERE auction_id = $3"
    async with pool.acquire() as conn:
        await conn.execute(sql, winner_id, final_price, auction_id)
        logging.info(f"Аукцион {auction_id} завершен. Победитель: {winner_id}, цена: {final_price}")


# --- Функции для работы со ставками (Bids) ---

async def add_bid(auction_id: int, user_id: int, amount: float):
    """Добавляет новую ставку в базу данных."""
    sql = "INSERT INTO bids (auction_id, user_id, bid_amount) VALUES ($1, $2, $3)"
    async with pool.acquire() as conn:
        await conn.execute(sql, auction_id, user_id, amount)


async def get_last_bid(auction_id: int) -> Optional[Dict[str, Any]]:
    """Получает последнюю (самую высокую) ставку на аукционе, включая full_name."""
    sql = """
          SELECT b.bid_amount, u.username, b.user_id, u.full_name
          FROM bids b
          JOIN users u ON b.user_id = u.user_id
          WHERE b.auction_id = $1
          ORDER BY b.bid_amount DESC, b.bid_time ASC
          LIMIT 1;
          """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(sql, auction_id)
        return dict(row) if row else None


async def get_user_last_bid_time(user_id: int, auction_id: int) -> Optional[str]:
    """Возвращает время последней ставки пользователя на конкретном аукционе."""
    sql = "SELECT bid_time FROM bids WHERE user_id = $1 AND auction_id = $2 ORDER BY bid_time DESC LIMIT 1"
    async with pool.acquire() as conn:
        return await conn.fetchval(sql, user_id, auction_id)




async def get_top_bids(auction_id: int, limit: int = 5) -> list[dict]:
    """Возвращает топ-N ставок (по сумме, затем по времени) с данными пользователя."""
    sql = (
        "SELECT b.bid_id, b.auction_id, b.user_id, b.bid_amount, b.bid_time, u.username, u.full_name "
        "FROM bids b JOIN users u ON b.user_id = u.user_id "
        "WHERE b.auction_id = $1 "
        "ORDER BY b.bid_amount DESC, b.bid_time ASC LIMIT $2"
    )
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, auction_id, limit)
        return [dict(r) for r in rows]


async def get_bid_by_id(bid_id: int) -> dict | None:
    """Возвращает одну ставку по bid_id."""
    sql = "SELECT b.*, u.username, u.full_name FROM bids b JOIN users u ON b.user_id = u.user_id WHERE bid_id = $1"
    async with pool.acquire() as conn:
        row = await conn.fetchrow(sql, bid_id)
        return dict(row) if row else None


async def get_users_with_bid_stats() -> list[dict]:
    """Возвращает список пользователей с агрегированной статистикой по ставкам."""
    sql = (
        "SELECT u.user_id, u.username, u.full_name, u.phone_number, u.status, "
        "       COALESCE(COUNT(b.bid_id), 0) AS bids_count, "
        "       COALESCE(SUM(b.bid_amount), 0) AS bids_sum "
        "FROM users u "
        "LEFT JOIN bids b ON b.user_id = u.user_id "
        "GROUP BY u.user_id, u.username, u.full_name, u.phone_number, u.status "
        "ORDER BY bids_sum DESC, bids_count DESC"
    )
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql)
        return [dict(r) for r in rows]

async def get_expired_active_auctions() -> list[dict]:
    """Возвращает список активных аукционов, время которых истекло."""
    sql = "SELECT * FROM auctions WHERE status = 'active' AND end_time <= NOW()"
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql)
        return [dict(row) for row in rows]



async def get_auto_approve_status() -> bool:
    """Проверяет, включено ли автопринятие заявок."""
    async with pool.acquire() as conn:
        value = await conn.fetchval("SELECT setting_value FROM settings WHERE setting_key = 'auto_approve_enabled'")
        return value == 'true' # Сравниваем со строкой 'true'

async def set_auto_approve_status(enabled: bool):
    """Включает или выключает автопринятие заявок."""
    value_str = 'true' if enabled else 'false'
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE settings SET setting_value = $1 WHERE setting_key = 'auto_approve_enabled'",
            value_str
        )
        logging.info(f"Автопринятие заявок установлено в: {enabled}")