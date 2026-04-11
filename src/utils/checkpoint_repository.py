"""
SQLite checkpoint 持久化操作模块

提供 LangGraph AsyncSqliteSaver 的辅助查询接口，包括：
- 按前缀查询会话列表
- 删除会话记录
- 清理旧 checkpoint（只保留最近 N 个）
"""

import sqlite3

import aiosqlite

_VACUUM_FREE_PAGE_RATIO_THRESHOLD = 0.35


def _is_missing_table_error(exc: sqlite3.OperationalError) -> bool:
    """判断异常是否为 'no such table' 错误。"""
    return "no such table" in str(exc).lower()


async def list_thread_ids_by_prefix(db_path: str, prefix: str) -> list[str]:
    """按 thread_id 前缀查询会话列表。

    :param db_path: SQLite 数据库路径
    :param prefix: thread_id 前缀（如 "user#session_"）
    :return: 匹配的 thread_id 列表
    """
    async with aiosqlite.connect(db_path) as db:
        try:
            cursor = await db.execute(
                "SELECT DISTINCT thread_id FROM checkpoints WHERE thread_id LIKE ? ORDER BY thread_id",
                (f"{prefix}%",),
            )
            rows = await cursor.fetchall()
        except sqlite3.OperationalError as exc:
            if _is_missing_table_error(exc):
                return []
            raise
    return [row[0] for row in rows]


async def delete_thread_records(db_path: str, thread_id: str) -> None:
    """删除指定 thread 在 checkpoints/writes 表中的所有记录。

    :param db_path: SQLite 数据库路径
    :param thread_id: 要删除的线程 ID
    """
    async with aiosqlite.connect(db_path) as db:
        for table in ("checkpoints", "writes"):
            try:
                await db.execute(f"DELETE FROM {table} WHERE thread_id = ?", (thread_id,))
            except sqlite3.OperationalError as exc:
                if not _is_missing_table_error(exc):
                    raise
        await db.commit()


async def delete_thread_records_like(db_path: str, pattern: str) -> None:
    """按 LIKE 模式删除 checkpoints/writes 记录。

    :param db_path: SQLite 数据库路径
    :param pattern: LIKE 模式（如 "user#%"）
    """
    async with aiosqlite.connect(db_path) as db:
        for table in ("checkpoints", "writes"):
            try:
                await db.execute(f"DELETE FROM {table} WHERE thread_id LIKE ?", (pattern,))
            except sqlite3.OperationalError as exc:
                if not _is_missing_table_error(exc):
                    raise
        await db.commit()


async def _maybe_vacuum_on_free_page_ratio(db: aiosqlite.Connection) -> bool:
    """Run VACUUM when freelist/page_count ratio is too high."""
    page_row = await (await db.execute("PRAGMA page_count")).fetchone()
    free_row = await (await db.execute("PRAGMA freelist_count")).fetchone()
    page_count = int(page_row[0] if page_row and page_row[0] is not None else 0)
    freelist_count = int(free_row[0] if free_row and free_row[0] is not None else 0)
    if page_count <= 0:
        return False
    free_ratio = freelist_count / page_count
    if free_ratio < _VACUUM_FREE_PAGE_RATIO_THRESHOLD:
        return False
    await db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    await db.execute("VACUUM")
    return True


async def purge_old_checkpoints(db_path: str, thread_id: str, keep: int = 1) -> int:
    """清理指定 thread 的旧 checkpoint，只保留最近 `keep` 个。

    LangGraph 的 AsyncSqliteSaver 每次 graph 执行都会写入新的 checkpoint，
    随着对话进行，checkpoint 数量会无限增长。此函数删除旧的 checkpoint 及其
    关联的 writes 记录，只保留最新的 `keep` 个。

    :param db_path: SQLite 数据库路径
    :param thread_id: 线程 ID
    :param keep: 要保留的最新 checkpoint 数量，默认为 1
    :return: 删除的 checkpoint 数量
    """
    deleted = 0
    async with aiosqlite.connect(db_path) as db:
        try:
            # 找出该 thread 的所有 checkpoint_id，按 checkpoint_id 倒序排列
            # （LangGraph 的 checkpoint_id 是递增的 UUID/时间戳，越新越大）
            cursor = await db.execute(
                "SELECT checkpoint_id FROM checkpoints "
                "WHERE thread_id = ? "
                "ORDER BY checkpoint_id DESC",
                (thread_id,),
            )
            all_ids = [row[0] for row in await cursor.fetchall()]
        except sqlite3.OperationalError as exc:
            if _is_missing_table_error(exc):
                return 0
            raise

        if len(all_ids) <= keep:
            return 0  # 没有需要删除的

        # 要删除的 checkpoint_id
        ids_to_delete = all_ids[keep:]

        # 批量删除 checkpoints 和 writes
        placeholders = ",".join("?" for _ in ids_to_delete)
        for table in ("checkpoints", "writes"):
            try:
                await db.execute(
                    f"DELETE FROM {table} WHERE thread_id = ? AND checkpoint_id IN ({placeholders})",
                    [thread_id] + ids_to_delete,
                )
            except sqlite3.OperationalError as exc:
                if not _is_missing_table_error(exc):
                    raise
        await db.commit()
        await _maybe_vacuum_on_free_page_ratio(db)
        deleted = len(ids_to_delete)

    return deleted
