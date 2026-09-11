"""
Телеграм-бот для учёта расходов — "блокнот + калькулятор".

Просто пишешь боту сообщение вида:
    250 кофе
    100+50+30 продукты
    125.50 такси

Бот сам считает выражение (если там есть +, -, *, /) и сохраняет
запись: сумма, комментарий/категория, дата.

Команды:
    /start, /help  — помощь
    /today         — расходы за сегодня
    /week          — расходы за 7 дней
    /month         — расходы за текущий месяц по категориям
    /stats         — топ категорий за всё время
    /last [N]      — последние N записей (по умолчанию 10)
    /undo          — удалить последнюю запись
    /delete <id>   — удалить запись по номеру
    /export        — выгрузить все записи в CSV-файл
    /clear confirm — удалить вообще все свои записи
"""

import ast
import csv
import io
import logging
import operator
import os
import sqlite3
from contextlib import closing
from datetime import datetime

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("EXPENSE_BOT_DB", os.path.join(os.path.dirname(__file__), "expenses.db"))
NO_CATEGORY = "Без категории"

# ---------------------------------------------------------------------------
# Хранилище (SQLite)
# ---------------------------------------------------------------------------

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            category TEXT NOT NULL,
            raw_text TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    return conn


def add_expense(user_id: int, amount: float, category: str, raw_text: str) -> int:
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    with closing(get_conn()) as conn, conn:
        cur = conn.execute(
            "INSERT INTO expenses (user_id, amount, category, raw_text, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, amount, category, raw_text, now),
        )
        return cur.lastrowid


def delete_last(user_id: int):
    with closing(get_conn()) as conn, conn:
        row = conn.execute(
            "SELECT id, amount, category FROM expenses WHERE user_id=? "
            "ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        if not row:
            return None
        conn.execute("DELETE FROM expenses WHERE id=?", (row[0],))
        return row


def delete_by_id(user_id: int, expense_id: int) -> bool:
    with closing(get_conn()) as conn, conn:
        cur = conn.execute(
            "DELETE FROM expenses WHERE id=? AND user_id=?", (expense_id, user_id)
        )
        return cur.rowcount > 0


def clear_all(user_id: int):
    with closing(get_conn()) as conn, conn:
        conn.execute("DELETE FROM expenses WHERE user_id=?", (user_id,))


def query_period(user_id: int, since_sql: str):
    with closing(get_conn()) as conn:
        rows = conn.execute(
            f"SELECT id, amount, category, raw_text, created_at FROM expenses "
            f"WHERE user_id=? AND created_at >= {since_sql} ORDER BY created_at",
            (user_id,),
        ).fetchall()
        return rows


def query_today(user_id: int):
    with closing(get_conn()) as conn:
        rows = conn.execute(
            "SELECT id, amount, category, raw_text, created_at FROM expenses "
            "WHERE user_id=? AND date(created_at) = date('now') ORDER BY created_at",
            (user_id,),
        ).fetchall()
        return rows


def query_last(user_id: int, limit: int):
    with closing(get_conn()) as conn:
        rows = conn.execute(
            "SELECT id, amount, category, raw_text, created_at FROM expenses "
            "WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        return rows


def query_stats(user_id: int, since_sql: str = None, limit: int = 10):
    where = "WHERE user_id=?"
    params = [user_id]
    if since_sql:
        where += f" AND created_at >= {since_sql}"
    with closing(get_conn()) as conn:
        rows = conn.execute(
            f"SELECT category, SUM(amount), COUNT(*) FROM expenses {where} "
            f"GROUP BY category ORDER BY SUM(amount) DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        return rows


def query_all(user_id: int):
    with closing(get_conn()) as conn:
        rows = conn.execute(
            "SELECT id, amount, category, raw_text, created_at FROM expenses "
            "WHERE user_id=? ORDER BY created_at",
            (user_id,),
        ).fetchall()
        return rows


# ---------------------------------------------------------------------------
# Безопасный калькулятор (без eval)
# ---------------------------------------------------------------------------

_ALLOWED_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def _eval_node(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_OPS:
        return _ALLOWED_OPS[type(node.op)](_eval_node(node.left), _eval_node(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_OPS:
        return _ALLOWED_OPS[type(node.op)](_eval_node(node.operand))
    raise ValueError("Неподдерживаемое выражение")


def safe_eval(expr: str) -> float:
    tree = ast.parse(expr, mode="eval")
    return float(_eval_node(tree.body))


_ALLOWED_CHARS = set("0123456789+-*/(). \t,")


def split_amount_and_comment(text: str):
    """Отделяет ведущее арифметическое выражение от текста-комментария."""
    i = 0
    while i < len(text) and text[i] in _ALLOWED_CHARS:
        i += 1
    expr_part = text[:i].strip()
    comment = text[i:].strip()
    return expr_part, comment


def parse_message(text: str):
    """Возвращает (amount, category) либо кидает ValueError с понятным текстом."""
    expr_part, comment = split_amount_and_comment(text.strip())
    if not expr_part or not any(ch.isdigit() for ch in expr_part):
        raise ValueError(
            "Не нашёл сумму в начале сообщения.\n"
            "Пример: 250 кофе  или  100+50+30 продукты"
        )
    normalized = expr_part.replace(",", ".")
    try:
        amount = safe_eval(normalized)
    except Exception:
        raise ValueError(
            f"Не смог посчитать «{expr_part}». Проверь выражение (можно +, -, *, /, скобки)."
        )
    category = comment if comment else NO_CATEGORY
    return amount, category


# ---------------------------------------------------------------------------
# Форматирование
# ---------------------------------------------------------------------------

def fmt_amount(a: float) -> str:
    if abs(a - round(a)) < 1e-9:
        return f"{int(round(a))}"
    return f"{a:.2f}".rstrip("0").rstrip(".")


def fmt_rows(rows) -> str:
    lines = []
    for _id, amount, category, raw_text, created_at in rows:
        lines.append(f"#{_id} · {fmt_amount(amount)} · {category} · {created_at[:16]}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Хендлеры команд
# ---------------------------------------------------------------------------

HELP_TEXT = (
    "Я — блокнот с калькулятором для расходов.\n\n"
    "Просто напиши сумму и (не обязательно) что это было:\n"
    "  250 кофе\n"
    "  100+50+30 продукты\n"
    "  125.50 такси домой\n"
    "  -100 возврат   (минус — если нужно вычесть)\n\n"
    "Можно прислать сразу несколько строк одним сообщением — "
    "каждая строка запишется отдельной тратой (удобно для синхронизации "
    "офлайн-блокнота).\n\n"
    "Команды:\n"
    "/today — расходы за сегодня\n"
    "/week — расходы за 7 дней\n"
    "/month — расходы за текущий месяц по категориям\n"
    "/stats — топ категорий за всё время\n"
    "/last [N] — последние N записей (по умолчанию 10)\n"
    "/undo — удалить последнюю запись\n"
    "/delete <id> — удалить запись по номеру\n"
    "/export — выгрузить все записи в CSV\n"
    "/clear confirm — удалить все свои записи"
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! " + HELP_TEXT)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    rows = query_today(user_id)
    if not rows:
        await update.message.reply_text("Сегодня расходов ещё нет.")
        return
    total = sum(r[1] for r in rows)
    await update.message.reply_text(
        f"Сегодня: {fmt_amount(total)}\n\n" + fmt_rows(rows)
    )


async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    rows = query_period(user_id, "datetime('now', '-7 days')")
    if not rows:
        await update.message.reply_text("За последние 7 дней расходов нет.")
        return
    total = sum(r[1] for r in rows)
    stats = query_stats(user_id, "datetime('now', '-7 days')")
    stats_lines = "\n".join(f"{cat}: {fmt_amount(s)} ({cnt})" for cat, s, cnt in stats)
    await update.message.reply_text(
        f"За 7 дней: {fmt_amount(total)}\n\nПо категориям:\n{stats_lines}"
    )


async def cmd_month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    rows = query_period(user_id, "datetime('now', 'start of month')")
    if not rows:
        await update.message.reply_text("В этом месяце расходов ещё нет.")
        return
    total = sum(r[1] for r in rows)
    stats = query_stats(user_id, "datetime('now', 'start of month')")
    stats_lines = "\n".join(f"{cat}: {fmt_amount(s)} ({cnt})" for cat, s, cnt in stats)
    await update.message.reply_text(
        f"За этот месяц: {fmt_amount(total)}\n\nПо категориям:\n{stats_lines}"
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    stats = query_stats(user_id)
    if not stats:
        await update.message.reply_text("Пока нет ни одной записи.")
        return
    total = sum(s for _, s, _ in stats)
    lines = "\n".join(f"{cat}: {fmt_amount(s)} ({cnt})" for cat, s, cnt in stats)
    await update.message.reply_text(f"Всего: {fmt_amount(total)}\n\n{lines}")


async def cmd_last(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    limit = 10
    if context.args:
        try:
            limit = max(1, min(100, int(context.args[0])))
        except ValueError:
            pass
    rows = query_last(user_id, limit)
    if not rows:
        await update.message.reply_text("Записей ещё нет.")
        return
    rows = list(reversed(rows))
    await update.message.reply_text(fmt_rows(rows))


async def cmd_undo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    row = delete_last(user_id)
    if not row:
        await update.message.reply_text("Нечего отменять — записей нет.")
        return
    _id, amount, category = row
    await update.message.reply_text(f"Удалил #{_id}: {fmt_amount(amount)} · {category}")


async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text("Укажи номер записи: /delete 12")
        return
    try:
        expense_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Номер записи должен быть числом.")
        return
    ok = delete_by_id(user_id, expense_id)
    await update.message.reply_text("Удалено." if ok else "Не нашёл такую запись.")


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    rows = query_all(user_id)
    if not rows:
        await update.message.reply_text("Пока нечего выгружать.")
        return
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "amount", "category", "raw_text", "created_at_utc"])
    writer.writerows(rows)
    data = io.BytesIO(buf.getvalue().encode("utf-8-sig"))
    data.name = "expenses.csv"
    await update.message.reply_document(document=data, filename="expenses.csv")


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args or context.args[0].lower() != "confirm":
        await update.message.reply_text(
            "Это удалит ВСЕ твои записи безвозвратно.\n"
            "Если уверен — напиши: /clear confirm"
        )
        return
    clear_all(user_id)
    await update.message.reply_text("Все записи удалены.")


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает сообщение. Поддерживает несколько строк за раз —
    так офлайн-блокнот на телефоне может прислать накопленные записи
    одним сообщением при синхронизации."""
    user_id = update.effective_user.id
    text = update.message.text or ""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    if len(lines) <= 1:
        try:
            amount, category = parse_message(text)
        except ValueError as e:
            await update.message.reply_text(str(e))
            return
        expense_id = add_expense(user_id, amount, category, text)
        await update.message.reply_text(
            f"Записал #{expense_id}: {fmt_amount(amount)} · {category}"
        )
        return

    added = []
    failed = []
    for line in lines:
        try:
            amount, category = parse_message(line)
        except ValueError:
            failed.append(line)
            continue
        expense_id = add_expense(user_id, amount, category, line)
        added.append(f"#{expense_id} · {fmt_amount(amount)} · {category}")

    reply = f"Синхронизировано записей: {len(added)}\n" + "\n".join(added)
    if failed:
        reply += "\n\nНе распознал:\n" + "\n".join(failed)
    await update.message.reply_text(reply)


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        token_file = os.path.join(os.path.dirname(__file__), "token.txt")
        if os.path.exists(token_file):
            with open(token_file, "r", encoding="utf-8") as f:
                token = f.read().strip()
    if not token:
        raise SystemExit(
            "Не найден токен бота. Установи переменную окружения TELEGRAM_BOT_TOKEN "
            "или создай файл token.txt рядом с bot.py с токеном внутри."
        )

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("week", cmd_week))
    app.add_handler(CommandHandler("month", cmd_month))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("last", cmd_last))
    app.add_handler(CommandHandler("undo", cmd_undo))
    app.add_handler(CommandHandler("delete", cmd_delete))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    logger.info("Бот запущен, используется БД: %s", DB_PATH)
    app.run_polling()


if __name__ == "__main__":
    main()
