import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord import app_commands
from discord.ext import tasks

from database import Database, WorktimeError
from time_utils import format_duration, parse_local_date, parse_month, render_grass


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("worktime-bot")


def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"환경변수 {name}가 필요합니다.")
    return value


WORK_CHANNEL_ID = int(required_env("WORK_CHANNEL_ID"))


try:
    TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Asia/Seoul"))
except ZoneInfoNotFoundError as exc:
    raise RuntimeError("TIMEZONE에 올바른 IANA 시간대를 입력하세요.") from exc


class WorkCommandTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.channel_id == WORK_CHANNEL_ID:
            return True

        await interaction.response.send_message(
            f"⚠️ 근태 명령어는 <#{WORK_CHANNEL_ID}> 채널에서만 사용할 수 있어요.",
            ephemeral=True,
        )
        return False


class WorktimeBot(discord.Client):
    def __init__(self, database_url: str) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(intents=intents)
        self.tree = WorkCommandTree(self)
        self.db = Database(database_url)

    async def setup_hook(self) -> None:
        await self.db.connect()
        await self.db.initialize()

        guild_id = os.getenv("GUILD_ID")
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            logger.info("길드 %s에 명령어 %d개 동기화", guild_id, len(synced))
        else:
            synced = await self.tree.sync()
            logger.info("전역 명령어 %d개 동기화", len(synced))

        if not hourly_prompt_loop.is_running():
            hourly_prompt_loop.start()

    async def close(self) -> None:
        if hourly_prompt_loop.is_running():
            hourly_prompt_loop.cancel()
        await self.db.close()
        await super().close()


bot = WorktimeBot(required_env("DATABASE_URL"))


def now_local() -> datetime:
    return datetime.now(TIMEZONE)


def require_guild(interaction: discord.Interaction) -> int:
    if interaction.guild_id is None:
        raise WorktimeError("이 명령어는 서버 안에서만 사용할 수 있어요.")
    return interaction.guild_id


async def send_error(interaction: discord.Interaction, error: Exception) -> None:
    if isinstance(error, (WorktimeError, ValueError)):
        message = f"⚠️ {error}"
    else:
        logger.exception("명령 처리 실패", exc_info=error)
        message = "❌ 처리 중 오류가 발생했어요. 잠시 후 다시 시도해 주세요."

    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


@bot.tree.command(name="출근", description="오늘 업무를 시작합니다")
async def clock_in(interaction: discord.Interaction) -> None:
    try:
        current = now_local()
        await bot.db.clock_in(
            guild_id=require_guild(interaction),
            user_id=interaction.user.id,
            now=current,
        )
        await interaction.response.send_message(
            f"🟢 {interaction.user.mention} 출근! `{current:%Y-%m-%d %H:%M:%S}`"
        )
    except Exception as exc:
        await send_error(interaction, exc)


@bot.tree.command(name="휴식", description="휴식을 시작하거나 종료합니다")
async def toggle_break(interaction: discord.Interaction) -> None:
    try:
        current = now_local()
        result = await bot.db.toggle_break(
            guild_id=require_guild(interaction),
            user_id=interaction.user.id,
            now=current,
        )
        if result["action"] == "started":
            message = f"☕ {interaction.user.mention} 휴식 시작! `{current:%H:%M:%S}`"
        else:
            message = (
                f"🔵 {interaction.user.mention} 휴식 종료! `{current:%H:%M:%S}`\n"
                f"이번 휴식 {format_duration(result['break_seconds'])} · "
                f"오늘 총 휴식 {format_duration(result['total_break_seconds'])}"
            )
        await interaction.response.send_message(message)
    except Exception as exc:
        await send_error(interaction, exc)


@bot.tree.command(name="퇴근", description="오늘 업무를 종료하고 실작업시간을 계산합니다")
async def clock_out(interaction: discord.Interaction) -> None:
    try:
        current = now_local()
        result = await bot.db.clock_out(
            guild_id=require_guild(interaction),
            user_id=interaction.user.id,
            now=current,
        )
        auto_break = "\n진행 중이던 휴식은 자동 종료했어요." if result["closed_break"] else ""
        await interaction.response.send_message(
            f"🔴 {interaction.user.mention} 퇴근! `{current:%Y-%m-%d %H:%M:%S}`\n"
            f"실작업 {format_duration(result['work_seconds'])} · "
            f"총 휴식 {format_duration(result['total_break_seconds'])}{auto_break}"
        )
    except Exception as exc:
        await send_error(interaction, exc)


@bot.tree.command(name="기록", description="사용자의 날짜별 출퇴근 및 작업시간을 확인합니다")
@app_commands.describe(
    사용자="조회할 사용자 (생략 시 본인)",
    날짜="조회할 날짜 (YYYY-MM-DD, 생략 시 오늘)",
)
async def record(
    interaction: discord.Interaction,
    사용자: discord.Member | None = None,
    날짜: str | None = None,
) -> None:
    try:
        target_date = parse_local_date(날짜, now_local().date())
        target_user = 사용자 or interaction.user
        result = await bot.db.get_record(
            guild_id=require_guild(interaction),
            user_id=target_user.id,
            work_date=target_date,
            now=now_local(),
        )
        if result is None:
            raise WorktimeError(f"{target_date:%Y-%m-%d} 기록이 없어요.")

        status_labels = {
            "working": "근무 중",
            "on_break": "휴식 중",
            "completed": "퇴근 완료",
        }
        clock_out_text = result["clock_out"].astimezone(TIMEZONE).strftime("%H:%M:%S") if result["clock_out"] else "-"
        message = (
            f"📋 **{target_user.display_name} · {target_date:%Y-%m-%d} 근무 기록**\n"
            f"상태: {status_labels[result['status']]}\n"
            f"출근: `{result['clock_in'].astimezone(TIMEZONE):%H:%M:%S}` · 퇴근: `{clock_out_text}`\n"
            f"총 휴식: **{format_duration(result['total_break_seconds'])}**\n"
            f"실작업: **{format_duration(result['work_seconds'])}**"
        )
        await interaction.response.send_message(message)
    except Exception as exc:
        await send_error(interaction, exc)


@bot.tree.command(name="잔디", description="사용자의 월간 작업시간을 24시간 기준 잔디로 표시합니다")
@app_commands.describe(
    사용자="조회할 사용자 (생략 시 본인)",
    월="조회할 월 (YYYY-MM, 생략 시 이번 달)",
)
async def grass(
    interaction: discord.Interaction,
    사용자: discord.Member | None = None,
    월: str | None = None,
) -> None:
    try:
        current = now_local()
        target_user = 사용자 or interaction.user
        year, month = parse_month(월, current.date())
        records = await bot.db.get_month_records(
            guild_id=require_guild(interaction),
            user_id=target_user.id,
            year=year,
            month=month,
            now=current,
        )
        seconds_by_day = {item["work_date"]: item["work_seconds"] for item in records}
        grass_text = render_grass(year, month, seconds_by_day)
        total_seconds = sum(seconds_by_day.values())
        worked_days = sum(1 for seconds in seconds_by_day.values() if seconds > 0)
        await interaction.response.send_message(
            f"🌱 **{target_user.display_name} · {year}-{month:02d} 작업 잔디**\n"
            f"```text\n{grass_text}\n```\n"
            "`·` 범위 밖  `⬛` 0h  `🟦` <6h  `🟨` <12h  `🟧` <18h  `🟥` ≤24h\n"
            f"총 **{format_duration(total_seconds)}** · 작업일 **{worked_days}일**"
        )
    except Exception as exc:
        await send_error(interaction, exc)


async def save_hourly_log(interaction: discord.Interaction, content: str) -> None:
    try:
        current = now_local()
        result = await bot.db.set_hourly_log(
            guild_id=require_guild(interaction),
            user_id=interaction.user.id,
            now=current,
            content=content.strip(),
        )
        action = "수정" if result["updated"] else "저장"
        await interaction.response.send_message(
            f"📝 {interaction.user.mention} 업무 로그 {action}! "
            f"`{result['hour_start'].astimezone(TIMEZONE):%H:00}~"
            f"{result['hour_end'].astimezone(TIMEZONE):%H:00}`\n> {content.strip()}"
        )
    except Exception as exc:
        await send_error(interaction, exc)


@bot.tree.command(name="셋로그", description="현재 시간대에 하고 있는 일을 기록합니다")
@app_commands.describe(내용="지금 하고 있는 일 (같은 시간대에 다시 쓰면 수정)")
async def set_log_ko(
    interaction: discord.Interaction,
    내용: app_commands.Range[str, 1, 200],
) -> None:
    await save_hourly_log(interaction, 내용)


@bot.tree.command(name="setlog", description="현재 시간대에 하고 있는 일을 기록합니다")
@app_commands.describe(content="지금 하고 있는 일 (같은 시간대에 다시 쓰면 수정)")
async def set_log_en(
    interaction: discord.Interaction,
    content: app_commands.Range[str, 1, 200],
) -> None:
    await save_hourly_log(interaction, content)


@bot.tree.command(name="today", description="오늘의 시간대별 업무 로그와 작업시간을 확인합니다")
@app_commands.describe(사용자="조회할 사용자 (생략 시 본인)")
async def today(
    interaction: discord.Interaction,
    사용자: discord.Member | None = None,
) -> None:
    try:
        current = now_local()
        target_user = 사용자 or interaction.user
        guild_id = require_guild(interaction)
        work_record = await bot.db.get_record(
            guild_id=guild_id,
            user_id=target_user.id,
            work_date=current.date(),
            now=current,
        )
        if work_record is None:
            raise WorktimeError("오늘 근무 기록이 없어요.")

        logs = await bot.db.get_hourly_logs(
            guild_id=guild_id,
            user_id=target_user.id,
            work_date=current.date(),
        )
        if logs:
            timeline_lines = []
            for item in logs:
                start = item["hour_start"].astimezone(TIMEZONE)
                end = item["hour_end"].astimezone(TIMEZONE)
                content = item["content"].replace("\n", " ")
                if len(content) > 120:
                    content = content[:117] + "..."
                timeline_lines.append(f"`{start:%H:%M}~{end:%H:%M}` {content}")
            timeline = "\n".join(timeline_lines)
        else:
            timeline = "아직 작성한 업무 로그가 없어요."

        status_labels = {
            "working": "근무 중",
            "on_break": "휴식 중",
            "completed": "퇴근 완료",
        }
        embed = discord.Embed(
            title=f"📅 {target_user.display_name} · 오늘 한 일",
            description=timeline,
            color=discord.Color.green(),
        )
        embed.add_field(name="상태", value=status_labels[work_record["status"]], inline=True)
        embed.add_field(
            name="실작업", value=format_duration(work_record["work_seconds"]), inline=True
        )
        embed.add_field(
            name="총 휴식",
            value=format_duration(work_record["total_break_seconds"]),
            inline=True,
        )
        await interaction.response.send_message(embed=embed)
    except Exception as exc:
        await send_error(interaction, exc)


@tasks.loop(seconds=30)
async def hourly_prompt_loop() -> None:
    current = now_local()
    if current.minute != 0:
        return

    hour_start = current.replace(minute=0, second=0, microsecond=0)
    hour_end = hour_start + timedelta(hours=1)
    try:
        channel = bot.get_channel(WORK_CHANNEL_ID) or await bot.fetch_channel(
            WORK_CHANNEL_ID
        )
    except Exception:
        logger.exception("WORK_CHANNEL_ID 채널을 불러오지 못했습니다.")
        return
    guild = getattr(channel, "guild", None)
    if guild is None:
        logger.error("WORK_CHANNEL_ID가 서버 텍스트 채널이 아닙니다.")
        return

    guild_id = guild.id
    active_users = await bot.db.get_active_user_ids(guild_id)
    if not active_users:
        return
    claimed = await bot.db.claim_hourly_prompt(guild_id, hour_start)
    if not claimed:
        return
    try:
        mentions = " ".join(f"<@{user_id}>" for user_id in active_users)
        await channel.send(
            f"⏰ **{hour_start:%H:%M}~{hour_end:%H:%M} 업무 로그 시간!**\n"
            f"{mentions}\n"
            "이 시간 안에 `/셋로그 내용:지금 하는 일` 또는 `/setlog`를 입력해 주세요.",
            allowed_mentions=discord.AllowedMentions(
                users=True, roles=False, everyone=False
            ),
        )
    except Exception:
        logger.exception("길드 %s 시간별 알림 전송 실패", guild_id)
        await bot.db.release_hourly_prompt(guild_id, hour_start)


@hourly_prompt_loop.before_loop
async def before_hourly_prompt_loop() -> None:
    await bot.wait_until_ready()


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
) -> None:
    await send_error(interaction, error)


@bot.event
async def on_ready() -> None:
    logger.info("로그인 완료: %s (%s)", bot.user, bot.user.id if bot.user else "unknown")


if __name__ == "__main__":
    bot.run(required_env("DISCORD_TOKEN"), log_handler=None)
