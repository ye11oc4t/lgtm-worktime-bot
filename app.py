import logging
import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks

from database import Database, WorktimeError
from scrum_utils import (
    build_slack_payload,
    find_scrum_safety_issues,
    validate_slack_webhook_url,
)
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
SCRUM_CHANNEL_ID = int(os.getenv("SCRUM_CHANNEL_ID", str(WORK_CHANNEL_ID)))
SLACK_SCRUM_WEBHOOK_URL = os.getenv("SLACK_SCRUM_WEBHOOK_URL", "").strip()


try:
    TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Asia/Seoul"))
except ZoneInfoNotFoundError as exc:
    raise RuntimeError("TIMEZONE에 올바른 IANA 시간대를 입력하세요.") from exc


class WorkCommandTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        command_name = interaction.command.name if interaction.command else ""
        expected_channel_id = (
            SCRUM_CHANNEL_ID if command_name == "스크럼" else WORK_CHANNEL_ID
        )
        if interaction.channel_id == expected_channel_id:
            return True

        command_label = "스크럼" if command_name == "스크럼" else "근태"
        await interaction.response.send_message(
            f"⚠️ {command_label} 명령어는 <#{expected_channel_id}> 채널에서만 사용할 수 있어요.",
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


def build_today_embed(
    target_user: discord.User | discord.Member,
    work_record: dict,
    logs: list,
) -> discord.Embed:
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
        if len(timeline) > 4000:
            timeline = timeline[:3997] + "..."
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
    return embed


def build_scrum_report_embed(
    user: discord.User | discord.Member,
    work_date: date,
    report,
) -> discord.Embed:
    notes = report["blockers_notes"] or "없음"
    embed = discord.Embed(
        title=f"🗒️ {user.display_name} · 일일 업무보고",
        description=f"기준일 `{work_date:%Y-%m-%d}`",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="🧩 담당 모듈 / 작업 영역", value=report["module"], inline=False)
    embed.add_field(name="✅ 완료한 일", value=report["completed"], inline=False)
    embed.add_field(name="🔄 진행 중인 일", value=report["in_progress"], inline=False)
    embed.add_field(name="➡️ 다음에 할 일", value=report["next_tasks"], inline=False)
    embed.add_field(name="🚧 어려웠던 점 / 비고", value=notes, inline=False)
    embed.set_footer(text="/스크럼에서 같은 날짜의 보고서를 다시 열어 수정할 수 있습니다.")
    return embed


def scrum_report_values(report) -> list[str]:
    return [
        report["module"],
        report["completed"],
        report["in_progress"],
        report["next_tasks"],
        report["blockers_notes"],
    ]


def validated_slack_webhook_url() -> str:
    if not SLACK_SCRUM_WEBHOOK_URL:
        raise WorktimeError(
            "Slack 전송 설정이 아직 없어요. 관리자에게 `SLACK_SCRUM_WEBHOOK_URL` 설정을 요청해 주세요."
        )

    try:
        return validate_slack_webhook_url(SLACK_SCRUM_WEBHOOK_URL)
    except ValueError as exc:
        raise WorktimeError(str(exc)) from exc


async def send_scrum_to_slack(
    webhook_url: str,
    user: discord.User | discord.Member,
    work_date: date,
    report,
) -> None:
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            webhook_url,
            json=build_slack_payload(user.display_name, work_date, report),
        ) as response:
            await response.read()
            if response.status < 200 or response.status >= 300:
                raise RuntimeError(f"Slack Webhook 응답 상태 {response.status}")


async def check_scrum_component(
    interaction: discord.Interaction, author_id: int
) -> bool:
    if interaction.user.id != author_id:
        await interaction.response.send_message(
            "⚠️ 이 업무보고는 작성자만 조작할 수 있어요.", ephemeral=True
        )
        return False
    if interaction.channel_id != SCRUM_CHANNEL_ID:
        await interaction.response.send_message(
            f"⚠️ 스크럼 기능은 <#{SCRUM_CHANNEL_ID}> 채널에서만 사용할 수 있어요.",
            ephemeral=True,
        )
        return False
    return True


class ScrumReportModal(discord.ui.Modal):
    def __init__(
        self,
        database: Database,
        guild_id: int,
        author_id: int,
        work_date: date,
        existing=None,
    ) -> None:
        super().__init__(title=f"{work_date:%Y-%m-%d} 일일 업무보고", timeout=900)
        self.database = database
        self.guild_id = guild_id
        self.author_id = author_id
        self.work_date = work_date

        def initial(key: str) -> str | None:
            return existing[key] if existing else None

        self.module = discord.ui.TextInput(
            label="담당 모듈 / 작업 영역",
            placeholder="예: Attack Rule Engine / Kubernetes API Scanner",
            default=initial("module"),
            max_length=300,
        )
        self.completed = discord.ui.TextInput(
            label="완료한 일",
            placeholder="완료한 작업, 결과, 검증 내용까지 구체적으로 작성",
            default=initial("completed"),
            style=discord.TextStyle.paragraph,
            max_length=1000,
        )
        self.in_progress = discord.ui.TextInput(
            label="진행 중인 일",
            placeholder="현재 진행 상황, 남은 범위, 예상 완료 조건을 작성",
            default=initial("in_progress"),
            style=discord.TextStyle.paragraph,
            max_length=1000,
        )
        self.next_tasks = discord.ui.TextInput(
            label="다음에 할 일",
            placeholder="다음 작업과 우선순위, 필요한 협업 사항을 작성",
            default=initial("next_tasks"),
            style=discord.TextStyle.paragraph,
            max_length=1000,
        )
        self.blockers_notes = discord.ui.TextInput(
            label="어려웠던 점 / 비고사항",
            placeholder="막힌 점, 의사결정 필요 사항, 리스크 및 공유할 내용",
            default=initial("blockers_notes"),
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=1000,
        )
        for item in (
            self.module,
            self.completed,
            self.in_progress,
            self.next_tasks,
            self.blockers_notes,
        ):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await check_scrum_component(interaction, self.author_id):
            return

        draft = {
            "module": str(self.module).strip(),
            "completed": str(self.completed).strip(),
            "in_progress": str(self.in_progress).strip(),
            "next_tasks": str(self.next_tasks).strip(),
            "blockers_notes": str(self.blockers_notes).strip(),
        }
        issues = find_scrum_safety_issues(scrum_report_values(draft))
        if issues:
            warning = (
                "🚨 **외부 공유 전 안전 점검에서 확인이 필요합니다.**\n"
                f"감지 항목: {', '.join(issues)}\n"
                "내용은 저장하거나 전송하지 않았습니다. `내용 수정`을 눌러 제거해 주세요."
            )
            await interaction.response.send_message(
                content=warning,
                embed=build_scrum_report_embed(interaction.user, self.work_date, draft),
                view=ScrumReviewView(
                    self.database,
                    self.guild_id,
                    self.author_id,
                    self.work_date,
                    draft,
                    publish_allowed=False,
                    draft_saved=False,
                ),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        try:
            report = await self.database.upsert_daily_scrum_report(
                guild_id=self.guild_id,
                user_id=self.author_id,
                work_date=self.work_date,
                **draft,
            )
            action = "수정" if report["updated"] else "저장"
            await interaction.response.send_message(
                content=(
                    f"🛡️ 업무보고 초안을 {action}했습니다. 아래 내용을 확인한 뒤 "
                    "`완성 및 Slack 전송`을 눌러 주세요. 누르기 전에는 외부에 공유되지 않으며, "
                    "완성 후에는 수정할 수 없습니다."
                ),
                embed=build_scrum_report_embed(interaction.user, self.work_date, report),
                view=ScrumReviewView(
                    self.database,
                    self.guild_id,
                    self.author_id,
                    self.work_date,
                    report,
                ),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as exc:
            await send_error(interaction, exc)

    async def on_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:
        await send_error(interaction, error)


class ScrumStartView(discord.ui.View):
    def __init__(
        self,
        database: Database,
        guild_id: int,
        author_id: int,
        work_date: date,
        existing=None,
    ) -> None:
        super().__init__(timeout=900)
        self.database = database
        self.guild_id = guild_id
        self.author_id = author_id
        self.work_date = work_date
        self.existing = existing
        self.open_modal.label = "업무보고 수정" if existing else "일일 업무보고 작성"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await check_scrum_component(interaction, self.author_id)

    @discord.ui.button(label="일일 업무보고 작성", style=discord.ButtonStyle.primary)
    async def open_modal(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(
            ScrumReportModal(
                self.database,
                self.guild_id,
                self.author_id,
                self.work_date,
                self.existing,
            )
        )


class ScrumReviewView(discord.ui.View):
    def __init__(
        self,
        database: Database,
        guild_id: int,
        author_id: int,
        work_date: date,
        report,
        publish_allowed: bool = True,
        editable: bool = True,
        draft_saved: bool = True,
    ) -> None:
        super().__init__(timeout=900)
        self.database = database
        self.guild_id = guild_id
        self.author_id = author_id
        self.work_date = work_date
        self.report = report
        self.draft_saved = draft_saved
        self.complete.disabled = not publish_allowed
        self.edit.disabled = not editable

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await check_scrum_component(interaction, self.author_id)

    @discord.ui.button(
        label="완성 및 Slack 전송", style=discord.ButtonStyle.success, row=0
    )
    async def complete(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        issues = find_scrum_safety_issues(scrum_report_values(self.report))
        if issues:
            await interaction.response.send_message(
                f"🚨 안전 점검을 통과하지 못했습니다: {', '.join(issues)}",
                ephemeral=True,
            )
            return

        try:
            webhook_url = validated_slack_webhook_url()
        except Exception as exc:
            await send_error(interaction, exc)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        report_id = None
        try:
            report = await self.database.claim_daily_scrum_publish(
                self.guild_id,
                self.author_id,
                self.work_date,
                self.report["updated_at"],
            )
            report_id = report["id"]
            if report["busy"]:
                await interaction.followup.send(
                    "⏳ 전송이 이미 진행 중이에요. 잠시 후 확인해 주세요.", ephemeral=True
                )
                return
            if report["already_published"]:
                await interaction.edit_original_response(view=None)
                await interaction.followup.send(
                    "✅ 이미 Discord와 Slack에 전송된 업무보고예요.", ephemeral=True
                )
                return

            if report["slack_published_at"] is None:
                await send_scrum_to_slack(
                    webhook_url, interaction.user, self.work_date, report
                )
                await self.database.mark_scrum_slack_published(report_id)

            if report["discord_message_id"] is None:
                channel = bot.get_channel(SCRUM_CHANNEL_ID) or await bot.fetch_channel(
                    SCRUM_CHANNEL_ID
                )
                published_embed = build_scrum_report_embed(
                    interaction.user, self.work_date, report
                )
                published_embed.set_footer(text="완성됨 · Slack 동시 전송 완료")
                message = await channel.send(
                    embed=published_embed,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                await self.database.mark_scrum_discord_published(report_id, message.id)

            await self.database.finish_scrum_publish(report_id)
            completed_embed = build_scrum_report_embed(
                interaction.user, self.work_date, report
            )
            completed_embed.set_footer(text="완성됨 · Discord 및 Slack 전송 완료")
            await interaction.edit_original_response(embed=completed_embed, view=None)
            await interaction.followup.send(
                "✅ 일일 업무보고를 스크럼 채널과 Slack에 안전하게 전송했습니다.",
                ephemeral=True,
            )
        except WorktimeError as exc:
            await interaction.followup.send(f"⚠️ {exc}", ephemeral=True)
        except Exception as exc:
            logger.exception("일일 업무보고 전송 실패", exc_info=exc)
            if report_id is not None:
                try:
                    await self.database.fail_scrum_publish(report_id, str(exc))
                except Exception:
                    logger.exception("일일 업무보고 실패 상태 저장 오류")
            await interaction.followup.send(
                "❌ 전송에 실패했습니다. 내용은 중복 전송되지 않도록 상태를 기록했습니다. "
                "잠시 후 같은 버튼을 눌러 미전송 대상만 다시 시도해 주세요.",
                ephemeral=True,
            )

    @discord.ui.button(label="내용 수정", style=discord.ButtonStyle.secondary, row=0)
    async def edit(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(
            ScrumReportModal(
                self.database,
                self.guild_id,
                self.author_id,
                self.work_date,
                self.report,
            )
        )

    @discord.ui.button(label="취소", style=discord.ButtonStyle.danger, row=0)
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        content = (
            "업무보고 전송을 취소했습니다. 저장된 초안은 `/스크럼`에서 다시 열 수 있어요."
            if self.draft_saved
            else "업무보고 작성을 취소했습니다. 안전 점검에 걸린 내용은 저장되지 않았어요."
        )
        await interaction.response.edit_message(
            content=content,
            embed=None,
            view=None,
        )


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
        await interaction.response.send_message(
            embed=build_today_embed(target_user, work_record, logs)
        )
    except Exception as exc:
        await send_error(interaction, exc)


@bot.tree.command(
    name="스크럼", description="오늘 업무 로그를 바탕으로 일일 업무보고를 작성합니다"
)
async def scrum(interaction: discord.Interaction) -> None:
    try:
        current = now_local()
        guild_id = require_guild(interaction)
        work_record = await bot.db.get_record(
            guild_id=guild_id,
            user_id=interaction.user.id,
            work_date=current.date(),
            now=current,
        )
        if work_record is None:
            raise WorktimeError(
                "오늘 근무 기록이 없어요. 근태 채널에서 먼저 `/출근`을 실행해 주세요."
            )

        logs = await bot.db.get_hourly_logs(
            guild_id=guild_id,
            user_id=interaction.user.id,
            work_date=current.date(),
        )
        existing = await bot.db.get_daily_scrum_report(
            guild_id=guild_id,
            user_id=interaction.user.id,
            work_date=current.date(),
        )
        today_embed = build_today_embed(interaction.user, work_record, logs)
        today_embed.title = f"📋 {interaction.user.display_name} · 스크럼 작성 자료"

        if existing and existing["finalized_at"] is not None:
            report_embed = build_scrum_report_embed(
                interaction.user, current.date(), existing
            )
            fully_published = (
                existing["discord_message_id"] is not None
                and existing["slack_published_at"] is not None
            )
            if fully_published:
                report_embed.set_footer(
                    text="완성됨 · 중복 전송 방지를 위해 수정할 수 없습니다."
                )
                content = "✅ 오늘 일일 업무보고는 이미 완성되어 공유됐습니다."
                view = None
            else:
                report_embed.set_footer(
                    text="완성 처리됨 · 아직 전송되지 않은 대상을 다시 시도할 수 있습니다."
                )
                content = (
                    "⚠️ 업무보고가 완성됐지만 일부 전송이 끝나지 않았습니다. "
                    "`미전송 대상 재시도`를 눌러 주세요."
                )
                view = ScrumReviewView(
                    bot.db,
                    guild_id,
                    interaction.user.id,
                    current.date(),
                    existing,
                    editable=False,
                )
                view.complete.label = "미전송 대상 재시도"
            await interaction.response.send_message(
                content=content,
                embeds=[today_embed, report_embed],
                view=view,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        today_embed.set_footer(
            text="아래 버튼을 누르면 상세 업무보고 작성 모달이 열립니다."
        )
        await interaction.response.send_message(
            content=(
                "오늘의 `/today` 기록입니다. 내용을 확인한 뒤 일일 업무보고를 작성해 주세요. "
                "완성 버튼을 누르기 전까지 Discord 공개 채널과 Slack에는 전송되지 않습니다."
            ),
            embed=today_embed,
            view=ScrumStartView(
                bot.db,
                guild_id,
                interaction.user.id,
                current.date(),
                existing,
            ),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
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
