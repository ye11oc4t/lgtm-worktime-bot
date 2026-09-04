import calendar
from datetime import date, datetime, timedelta
from typing import Any

import asyncpg


class WorktimeError(Exception):
    """사용자에게 그대로 보여도 되는 근태 상태 오류."""


class Database:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self.pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        self.pool = await asyncpg.create_pool(self.database_url, min_size=1, max_size=5)

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()

    def _pool(self) -> asyncpg.Pool:
        if self.pool is None:
            raise RuntimeError("데이터베이스가 연결되지 않았습니다.")
        return self.pool

    async def initialize(self) -> None:
        await self._pool().execute(
            """
            CREATE TABLE IF NOT EXISTS work_sessions (
                id BIGSERIAL PRIMARY KEY,
                guild_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                work_date DATE NOT NULL,
                clock_in TIMESTAMPTZ NOT NULL,
                clock_out TIMESTAMPTZ,
                status TEXT NOT NULL CHECK (status IN ('working', 'on_break', 'completed')),
                total_break_seconds BIGINT NOT NULL DEFAULT 0 CHECK (total_break_seconds >= 0),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (guild_id, user_id, work_date)
            );

            CREATE TABLE IF NOT EXISTS break_periods (
                id BIGSERIAL PRIMARY KEY,
                session_id BIGINT NOT NULL REFERENCES work_sessions(id) ON DELETE CASCADE,
                started_at TIMESTAMPTZ NOT NULL,
                ended_at TIMESTAMPTZ,
                duration_seconds BIGINT CHECK (duration_seconds IS NULL OR duration_seconds >= 0)
            );

            CREATE UNIQUE INDEX IF NOT EXISTS one_open_break_per_session
                ON break_periods(session_id) WHERE ended_at IS NULL;
            CREATE INDEX IF NOT EXISTS open_session_lookup
                ON work_sessions(guild_id, user_id, status);

            CREATE TABLE IF NOT EXISTS hourly_work_logs (
                id BIGSERIAL PRIMARY KEY,
                session_id BIGINT NOT NULL REFERENCES work_sessions(id) ON DELETE CASCADE,
                hour_start TIMESTAMPTZ NOT NULL,
                content TEXT NOT NULL CHECK (char_length(content) BETWEEN 1 AND 200),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (session_id, hour_start)
            );

            CREATE TABLE IF NOT EXISTS hourly_prompts (
                guild_id BIGINT NOT NULL,
                hour_start TIMESTAMPTZ NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (guild_id, hour_start)
            );
            """
        )

    async def clock_in(self, guild_id: int, user_id: int, now: datetime) -> None:
        async with self._pool().acquire() as connection:
            async with connection.transaction():
                open_session = await connection.fetchrow(
                    """
                    SELECT id, work_date FROM work_sessions
                    WHERE guild_id = $1 AND user_id = $2 AND status <> 'completed'
                    ORDER BY clock_in DESC LIMIT 1 FOR UPDATE
                    """,
                    guild_id,
                    user_id,
                )
                if open_session:
                    raise WorktimeError(
                        f"이미 {open_session['work_date']:%Y-%m-%d} 출근 기록이 진행 중이에요."
                    )

                try:
                    await connection.execute(
                        """
                        INSERT INTO work_sessions
                            (guild_id, user_id, work_date, clock_in, status)
                        VALUES ($1, $2, $3, $4, 'working')
                        """,
                        guild_id,
                        user_id,
                        now.date(),
                        now,
                    )
                except asyncpg.UniqueViolationError as exc:
                    raise WorktimeError("오늘 출근 기록이 이미 있어요.") from exc

    async def _open_session_for_update(
        self, connection: asyncpg.Connection, guild_id: int, user_id: int
    ) -> asyncpg.Record:
        row = await connection.fetchrow(
            """
            SELECT * FROM work_sessions
            WHERE guild_id = $1 AND user_id = $2 AND status <> 'completed'
            ORDER BY clock_in DESC LIMIT 1 FOR UPDATE
            """,
            guild_id,
            user_id,
        )
        if row is None:
            raise WorktimeError("진행 중인 근무가 없어요. 먼저 `/출근`을 실행해 주세요.")
        return row

    async def toggle_break(
        self, guild_id: int, user_id: int, now: datetime
    ) -> dict[str, Any]:
        async with self._pool().acquire() as connection:
            async with connection.transaction():
                session = await self._open_session_for_update(connection, guild_id, user_id)
                if session["status"] == "working":
                    await connection.execute(
                        "INSERT INTO break_periods (session_id, started_at) VALUES ($1, $2)",
                        session["id"],
                        now,
                    )
                    await connection.execute(
                        "UPDATE work_sessions SET status = 'on_break', updated_at = NOW() WHERE id = $1",
                        session["id"],
                    )
                    return {"action": "started"}

                break_row = await connection.fetchrow(
                    """
                    SELECT * FROM break_periods
                    WHERE session_id = $1 AND ended_at IS NULL
                    ORDER BY started_at DESC LIMIT 1 FOR UPDATE
                    """,
                    session["id"],
                )
                if break_row is None:
                    raise RuntimeError("휴식 상태와 휴식 기록이 일치하지 않습니다.")

                seconds = max(0, int((now - break_row["started_at"]).total_seconds()))
                total = session["total_break_seconds"] + seconds
                await connection.execute(
                    """
                    UPDATE break_periods SET ended_at = $2, duration_seconds = $3 WHERE id = $1
                    """,
                    break_row["id"],
                    now,
                    seconds,
                )
                await connection.execute(
                    """
                    UPDATE work_sessions
                    SET status = 'working', total_break_seconds = $2, updated_at = NOW()
                    WHERE id = $1
                    """,
                    session["id"],
                    total,
                )
                return {
                    "action": "ended",
                    "break_seconds": seconds,
                    "total_break_seconds": total,
                }

    async def clock_out(
        self, guild_id: int, user_id: int, now: datetime
    ) -> dict[str, Any]:
        async with self._pool().acquire() as connection:
            async with connection.transaction():
                session = await self._open_session_for_update(connection, guild_id, user_id)
                total_break = session["total_break_seconds"]
                closed_break = False

                if session["status"] == "on_break":
                    break_row = await connection.fetchrow(
                        """
                        SELECT * FROM break_periods
                        WHERE session_id = $1 AND ended_at IS NULL
                        ORDER BY started_at DESC LIMIT 1 FOR UPDATE
                        """,
                        session["id"],
                    )
                    if break_row is None:
                        raise RuntimeError("휴식 상태와 휴식 기록이 일치하지 않습니다.")
                    break_seconds = max(0, int((now - break_row["started_at"]).total_seconds()))
                    total_break += break_seconds
                    closed_break = True
                    await connection.execute(
                        """
                        UPDATE break_periods SET ended_at = $2, duration_seconds = $3 WHERE id = $1
                        """,
                        break_row["id"],
                        now,
                        break_seconds,
                    )

                elapsed = max(0, int((now - session["clock_in"]).total_seconds()))
                work_seconds = max(0, elapsed - total_break)
                await connection.execute(
                    """
                    UPDATE work_sessions
                    SET clock_out = $2, status = 'completed', total_break_seconds = $3,
                        updated_at = NOW()
                    WHERE id = $1
                    """,
                    session["id"],
                    now,
                    total_break,
                )
                return {
                    "work_seconds": work_seconds,
                    "total_break_seconds": total_break,
                    "closed_break": closed_break,
                }

    async def get_record(
        self,
        guild_id: int,
        user_id: int,
        work_date: date,
        now: datetime,
    ) -> dict[str, Any] | None:
        async with self._pool().acquire() as connection:
            session = await connection.fetchrow(
                """
                SELECT * FROM work_sessions
                WHERE guild_id = $1 AND user_id = $2 AND work_date = $3
                """,
                guild_id,
                user_id,
                work_date,
            )
            if session is None:
                return None

            total_break = session["total_break_seconds"]
            if session["status"] == "on_break":
                open_break = await connection.fetchrow(
                    """
                    SELECT started_at FROM break_periods
                    WHERE session_id = $1 AND ended_at IS NULL
                    ORDER BY started_at DESC LIMIT 1
                    """,
                    session["id"],
                )
                if open_break:
                    total_break += max(0, int((now - open_break["started_at"]).total_seconds()))

            endpoint = session["clock_out"] or now
            elapsed = max(0, int((endpoint - session["clock_in"]).total_seconds()))
            return {
                "clock_in": session["clock_in"],
                "clock_out": session["clock_out"],
                "status": session["status"],
                "total_break_seconds": total_break,
                "work_seconds": max(0, elapsed - total_break),
            }

    async def get_month_records(
        self,
        guild_id: int,
        user_id: int,
        year: int,
        month: int,
        now: datetime,
    ) -> list[dict[str, Any]]:
        first_day = date(year, month, 1)
        last_day = date(year, month, calendar.monthrange(year, month)[1])
        async with self._pool().acquire() as connection:
            sessions = await connection.fetch(
                """
                SELECT * FROM work_sessions
                WHERE guild_id = $1 AND user_id = $2
                  AND work_date BETWEEN $3 AND $4
                ORDER BY work_date
                """,
                guild_id,
                user_id,
                first_day,
                last_day,
            )
            results: list[dict[str, Any]] = []
            for session in sessions:
                total_break = session["total_break_seconds"]
                if session["status"] == "on_break":
                    open_break = await connection.fetchrow(
                        """
                        SELECT started_at FROM break_periods
                        WHERE session_id = $1 AND ended_at IS NULL
                        ORDER BY started_at DESC LIMIT 1
                        """,
                        session["id"],
                    )
                    if open_break:
                        total_break += max(
                            0, int((now - open_break["started_at"]).total_seconds())
                        )

                endpoint = session["clock_out"] or now
                elapsed = max(0, int((endpoint - session["clock_in"]).total_seconds()))
                results.append(
                    {
                        "work_date": session["work_date"],
                        "work_seconds": max(0, elapsed - total_break),
                    }
                )
            return results

    async def set_hourly_log(
        self,
        guild_id: int,
        user_id: int,
        now: datetime,
        content: str,
    ) -> dict[str, Any]:
        if not content or len(content) > 200:
            raise WorktimeError("업무 내용은 1~200자로 입력해 주세요.")

        hour_start = now.replace(minute=0, second=0, microsecond=0)
        hour_end = hour_start + timedelta(hours=1)
        async with self._pool().acquire() as connection:
            async with connection.transaction():
                session = await connection.fetchrow(
                    """
                    SELECT id FROM work_sessions
                    WHERE guild_id = $1 AND user_id = $2
                      AND clock_in < $4
                      AND (clock_out IS NULL OR clock_out > $3)
                    ORDER BY clock_in DESC LIMIT 1
                    FOR UPDATE
                    """,
                    guild_id,
                    user_id,
                    hour_start,
                    hour_end,
                )
                if session is None:
                    raise WorktimeError(
                        f"{hour_start:%H:00}~{hour_end:%H:00}에 근무한 기록이 없어요."
                    )

                existing = await connection.fetchval(
                    """
                    SELECT id FROM hourly_work_logs
                    WHERE session_id = $1 AND hour_start = $2
                    """,
                    session["id"],
                    hour_start,
                )
                await connection.execute(
                    """
                    INSERT INTO hourly_work_logs (session_id, hour_start, content)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (session_id, hour_start)
                    DO UPDATE SET content = EXCLUDED.content, updated_at = NOW()
                    """,
                    session["id"],
                    hour_start,
                    content,
                )
                return {
                    "hour_start": hour_start,
                    "hour_end": hour_end,
                    "updated": existing is not None,
                }

    async def get_hourly_logs(
        self,
        guild_id: int,
        user_id: int,
        work_date: date,
    ) -> list[asyncpg.Record]:
        return await self._pool().fetch(
            """
            SELECT logs.hour_start, logs.hour_start + INTERVAL '1 hour' AS hour_end,
                   logs.content
            FROM hourly_work_logs AS logs
            JOIN work_sessions AS sessions ON sessions.id = logs.session_id
            WHERE sessions.guild_id = $1 AND sessions.user_id = $2
              AND sessions.work_date = $3
            ORDER BY logs.hour_start
            """,
            guild_id,
            user_id,
            work_date,
        )

    async def get_active_user_ids(self, guild_id: int) -> list[int]:
        rows = await self._pool().fetch(
            """
            SELECT DISTINCT user_id FROM work_sessions
            WHERE guild_id = $1 AND status <> 'completed'
            ORDER BY user_id
            """,
            guild_id,
        )
        return [row["user_id"] for row in rows]

    async def claim_hourly_prompt(self, guild_id: int, hour_start: datetime) -> bool:
        row = await self._pool().fetchrow(
            """
            INSERT INTO hourly_prompts (guild_id, hour_start)
            VALUES ($1, $2)
            ON CONFLICT DO NOTHING
            RETURNING guild_id
            """,
            guild_id,
            hour_start,
        )
        return row is not None

    async def release_hourly_prompt(self, guild_id: int, hour_start: datetime) -> None:
        await self._pool().execute(
            "DELETE FROM hourly_prompts WHERE guild_id = $1 AND hour_start = $2",
            guild_id,
            hour_start,
        )
