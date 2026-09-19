import asyncio
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import discord
from discord import app_commands
from dotenv import load_dotenv


# .env를 실행 폴더가 아닌 bot.py와 동일한 폴더에서만 읽습니다.
ENVIRONMENT_PATH = Path(__file__).resolve().with_name(".env")
load_dotenv(ENVIRONMENT_PATH)

# 로컬에서는 봇 파일 옆에 저장하고, Railway에서는 DATABASE_PATH(/data/...)를 사용합니다.
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", Path(__file__).resolve().with_name("attendance.sqlite3")))
EMBED_LIMIT = 3_700
MENTION_CHUNK_LIMIT = 1_850
NOTICE_CONTENT_LIMIT = 1_800
# Discord 임베드 필드 값은 최대 1,024자입니다.
NOTICE_RESULT_LIMIT = 900
NOTICE_RETRY_COUNT = 5


@dataclass(frozen=True)
class CheckTarget:
    guild_id: int
    channel_id: int
    message_id: int
    emoji: str | None


@dataclass(frozen=True)
class MeetingSettings:
    guild_id: int
    meeting_channel_id: int | None
    log_channel_id: int | None
    notice_channel_id: int | None
    reason_channel_id: int | None


def database() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE_PATH)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS check_targets (
            guild_id INTEGER PRIMARY KEY,
            channel_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            emoji TEXT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS notice_exemptions (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            PRIMARY KEY (guild_id, user_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS bot_administrators (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            PRIMARY KEY (guild_id, user_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS meeting_settings (
            guild_id INTEGER PRIMARY KEY,
            meeting_channel_id INTEGER,
            log_channel_id INTEGER,
            notice_channel_id INTEGER,
            reason_channel_id INTEGER
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS absence_requests (
            request_id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            reason TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            reason_channel_id INTEGER,
            message_id INTEGER
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS approved_absences (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            reason TEXT NOT NULL,
            approved_by INTEGER NOT NULL,
            PRIMARY KEY (guild_id, user_id)
        )
        """
    )
    return connection


def save_target(target: CheckTarget) -> None:
    with database() as connection:
        connection.execute(
            """
            INSERT INTO check_targets (guild_id, channel_id, message_id, emoji)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                channel_id = excluded.channel_id,
                message_id = excluded.message_id,
                emoji = excluded.emoji
            """,
            (target.guild_id, target.channel_id, target.message_id, target.emoji),
        )


def get_target(guild_id: int) -> CheckTarget | None:
    with database() as connection:
        row = connection.execute(
            "SELECT guild_id, channel_id, message_id, emoji FROM check_targets WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()
    return CheckTarget(*row) if row else None


def get_meeting_settings(guild_id: int) -> MeetingSettings:
    with database() as connection:
        row = connection.execute(
            """
            SELECT guild_id, meeting_channel_id, log_channel_id, notice_channel_id, reason_channel_id
            FROM meeting_settings WHERE guild_id = ?
            """,
            (guild_id,),
        ).fetchone()
        if row is None:
            connection.execute("INSERT INTO meeting_settings (guild_id) VALUES (?)", (guild_id,))
            return MeetingSettings(guild_id, None, None, None, None)
    return MeetingSettings(*row)


def set_meeting_setting(guild_id: int, column: str, channel_id: int) -> None:
    allowed_columns = {
        "meeting_channel_id",
        "log_channel_id",
        "notice_channel_id",
        "reason_channel_id",
    }
    if column not in allowed_columns:
        raise ValueError("허용되지 않은 설정 항목입니다.")
    get_meeting_settings(guild_id)
    with database() as connection:
        connection.execute(f"UPDATE meeting_settings SET {column} = ? WHERE guild_id = ?", (channel_id, guild_id))


def create_absence_request(guild_id: int, user_id: int, reason: str) -> int:
    with database() as connection:
        cursor = connection.execute(
            "INSERT INTO absence_requests (guild_id, user_id, reason) VALUES (?, ?, ?)",
            (guild_id, user_id, reason),
        )
    return int(cursor.lastrowid)


def set_absence_request_message(request_id: int, channel_id: int, message_id: int) -> None:
    with database() as connection:
        connection.execute(
            "UPDATE absence_requests SET reason_channel_id = ?, message_id = ? WHERE request_id = ?",
            (channel_id, message_id, request_id),
        )


def get_absence_request(request_id: int) -> tuple[int, int, str, str] | None:
    with database() as connection:
        row = connection.execute(
            "SELECT guild_id, user_id, reason, status FROM absence_requests WHERE request_id = ?",
            (request_id,),
        ).fetchone()
    return row


def approve_absence_request(request_id: int, approver_id: int) -> tuple[int, int, str] | None:
    request = get_absence_request(request_id)
    if request is None or request[3] != "pending":
        return None
    guild_id, user_id, reason, _ = request
    with database() as connection:
        connection.execute("UPDATE absence_requests SET status = 'approved' WHERE request_id = ?", (request_id,))
        connection.execute(
            """
            INSERT INTO approved_absences (guild_id, user_id, reason, approved_by)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                reason = excluded.reason, approved_by = excluded.approved_by
            """,
            (guild_id, user_id, reason, approver_id),
        )
    return guild_id, user_id, reason


def reject_absence_request(request_id: int) -> tuple[int, int] | None:
    request = get_absence_request(request_id)
    if request is None or request[3] != "pending":
        return None
    with database() as connection:
        connection.execute("DELETE FROM absence_requests WHERE request_id = ?", (request_id,))
    return request[0], request[1]


def get_approved_absence(guild_id: int, user_id: int) -> str | None:
    with database() as connection:
        row = connection.execute(
            "SELECT reason FROM approved_absences WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchone()
    return row[0] if row else None


def get_approved_absences(guild_id: int) -> list[tuple[int, str]]:
    with database() as connection:
        rows = connection.execute(
            "SELECT user_id, reason FROM approved_absences WHERE guild_id = ? ORDER BY user_id",
            (guild_id,),
        ).fetchall()
    return [(int(user_id), str(reason)) for user_id, reason in rows]


def get_pending_request_message_ids() -> list[tuple[int, int]]:
    with database() as connection:
        rows = connection.execute(
            """
            SELECT request_id, message_id FROM absence_requests
            WHERE status = 'pending' AND message_id IS NOT NULL
            """
        ).fetchall()
    return [(int(request_id), int(message_id)) for request_id, message_id in rows]


def get_notice_exemptions(guild_id: int) -> set[int]:
    with database() as connection:
        rows = connection.execute(
            "SELECT user_id FROM notice_exemptions WHERE guild_id = ?", (guild_id,)
        ).fetchall()
    return {row[0] for row in rows}


def set_notice_exemption(guild_id: int, user_id: int, is_exempt: bool) -> None:
    with database() as connection:
        if is_exempt:
            connection.execute(
                "INSERT OR IGNORE INTO notice_exemptions (guild_id, user_id) VALUES (?, ?)",
                (guild_id, user_id),
            )
        else:
            connection.execute(
                "DELETE FROM notice_exemptions WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )


def is_bot_administrator(guild_id: int, user_id: int) -> bool:
    with database() as connection:
        row = connection.execute(
            "SELECT 1 FROM bot_administrators WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchone()
    return row is not None


def set_bot_administrator(guild_id: int, user_id: int, is_administrator: bool) -> None:
    with database() as connection:
        if is_administrator:
            connection.execute(
                "INSERT OR IGNORE INTO bot_administrators (guild_id, user_id) VALUES (?, ?)",
                (guild_id, user_id),
            )
        else:
            connection.execute(
                "DELETE FROM bot_administrators WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )


def get_bot_administrator_ids(guild_id: int) -> list[int]:
    with database() as connection:
        rows = connection.execute(
            "SELECT user_id FROM bot_administrators WHERE guild_id = ? ORDER BY user_id",
            (guild_id,),
        ).fetchall()
    return [row[0] for row in rows]


def can_manage(interaction: discord.Interaction) -> bool:
    member = interaction.user
    return isinstance(member, discord.Member) and member.guild_permissions.manage_guild


def can_use_notice_admin(interaction: discord.Interaction) -> bool:
    """봇 소유자, 서버 소유자 또는 지정된 공지 관리자만 공지 기능을 사용합니다."""
    return (
        interaction.guild is not None
        and (
            interaction.user.id == bot.application_owner_id
            or interaction.user.id == interaction.guild.owner_id
            or is_bot_administrator(interaction.guild.id, interaction.user.id)
        )
    )


def is_owner(interaction: discord.Interaction) -> bool:
    """봇 애플리케이션 소유자와 현재 서버 소유자를 모두 소유주로 취급합니다."""
    return (
        interaction.guild is not None
        and (
            interaction.user.id == bot.application_owner_id
            or interaction.user.id == interaction.guild.owner_id
        )
    )


async def fetch_target_message(bot: discord.Client, target: CheckTarget) -> discord.Message:
    channel = bot.get_channel(target.channel_id)
    if channel is None:
        channel = await bot.fetch_channel(target.channel_id)
    if not isinstance(channel, (discord.TextChannel, discord.Thread, discord.VoiceChannel)):
        raise ValueError("메시지를 조회할 수 있는 서버 채널이 아닙니다.")
    return await channel.fetch_message(target.message_id)


async def get_non_responders(
    bot: discord.Client, guild: discord.Guild, target: CheckTarget
) -> tuple[discord.Message, list[discord.Member], int]:
    message = await fetch_target_message(bot, target)
    responded_ids: set[int] = set()

    for reaction in message.reactions:
        if target.emoji is not None and str(reaction.emoji) != target.emoji:
            continue
        async for user in reaction.users(limit=None):
            if not user.bot:
                responded_ids.add(user.id)

    # fetch_members는 캐시에 없는 현재 서버 멤버까지 포함합니다.
    members = [member async for member in guild.fetch_members(limit=None) if not member.bot]
    non_responders = [member for member in members if member.id not in responded_ids]
    return message, non_responders, len(members)


def safe_name(member: discord.Member) -> str:
    # '<@123>'처럼 해석될 수 있는 문자열도 일반 텍스트로 표시합니다.
    return discord.utils.escape_markdown(discord.utils.escape_mentions(member.display_name))


def make_pages(members: list[discord.Member]) -> list[str]:
    if not members:
        return ["모든 일반 유저가 반응했습니다."]

    pages: list[str] = []
    lines: list[str] = []
    size = 0
    for member in members:
        line = f"• {safe_name(member)} (`{member.name}` · `{member.id}`)"
        if lines and size + len(line) + 1 > EMBED_LIMIT:
            pages.append("\n".join(lines))
            lines, size = [], 0
        lines.append(line)
        size += len(line) + 1
    if lines:
        pages.append("\n".join(lines))
    return pages


def make_mention_pages(members: list[discord.Member]) -> list[str]:
    """알림을 보내지 않는 실패 인원 멘션 목록을 임베드 페이지 크기로 나눕니다."""
    if not members:
        return ["없음"]

    pages: list[str] = []
    mentions: list[str] = []
    size = 0
    for member in members:
        mention = member.mention
        if mentions and size + len(mention) + 1 > NOTICE_RESULT_LIMIT:
            pages.append(" ".join(mentions))
            mentions, size = [], 0
        mentions.append(mention)
        size += len(mention) + 1
    if mentions:
        pages.append(" ".join(mentions))
    return pages


async def send_direct_notice(member: discord.Member, notice: str) -> bool:
    """DM 차단은 즉시 포기하고, 제한·일시 오류만 제한적으로 재시도합니다."""
    for attempt in range(NOTICE_RETRY_COUNT):
        try:
            await member.send(
                notice,
                # 공지 내용에 들어 있는 @everyone, 역할, 다른 사용자 멘션은 알리지 않습니다.
                allowed_mentions=discord.AllowedMentions(users=[member], roles=False, everyone=False),
            )
            return True
        except (discord.Forbidden, discord.NotFound):
            # 사용자가 서버 DM을 차단했거나 탈퇴한 경우: 즉시 실패 처리합니다.
            return False
        except discord.HTTPException as error:
            status = getattr(error, "status", None)
            # discord.py는 대부분의 429 제한을 내부에서 기다린 뒤 처리합니다.
            # 그래도 제한(429) 또는 Discord의 일시 서버 오류(5xx)가 전달되면 재시도합니다.
            should_retry = status == 429 or (isinstance(status, int) and status >= 500)
            if not should_retry or attempt == NOTICE_RETRY_COUNT - 1:
                return False
            await asyncio.sleep(2 ** attempt)
    return False


class ResultView(discord.ui.View):
    def __init__(
        self, bot: discord.Client, owner_id: int, guild: discord.Guild, target: CheckTarget,
        members: list[discord.Member], total_members: int, message: discord.Message,
    ):
        super().__init__(timeout=900)
        self.bot = bot
        self.owner_id = owner_id
        self.guild = guild
        self.target = target
        self.members = members
        self.total_members = total_members
        self.source_message = message
        self.pages = make_pages(members)
        self.page = 0
        self.update_buttons()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id or can_manage(interaction):
            return True
        await interaction.response.send_message("명령어를 실행한 사람 또는 서버 관리 권한자만 사용할 수 있습니다.", ephemeral=True)
        return False

    def update_buttons(self) -> None:
        self.previous.disabled = self.page == 0
        self.next.disabled = self.page >= len(self.pages) - 1
        self.mention.disabled = not self.members

    def embed(self) -> discord.Embed:
        emoji_label = self.target.emoji or "모든 이모지"
        embed = discord.Embed(title="반응 미확인자 조회", colour=discord.Colour.blurple())
        embed.add_field(name="확인 메시지", value=f"[메시지 보기]({self.source_message.jump_url})", inline=False)
        embed.add_field(name="선택 이모지", value=emoji_label, inline=True)
        embed.add_field(name="전체 일반 유저", value=f"{self.total_members}명", inline=True)
        embed.add_field(name="미반응자", value=f"{len(self.members)}명", inline=True)
        embed.add_field(name="미반응자 목록", value=self.pages[self.page], inline=False)
        embed.set_footer(text=f"페이지 {self.page + 1}/{len(self.pages)} · 목록에서는 멘션되지 않습니다.")
        return embed

    async def redraw(self, interaction: discord.Interaction) -> None:
        self.update_buttons()
        await interaction.response.edit_message(embed=self.embed(), view=self, allowed_mentions=discord.AllowedMentions.none())

    @discord.ui.button(label="이전", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.page -= 1
        await self.redraw(interaction)

    @discord.ui.button(label="다음", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.page += 1
        await self.redraw(interaction)

    @discord.ui.button(label="새로고침", style=discord.ButtonStyle.primary)
    async def refresh(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer()
        try:
            self.source_message, self.members, self.total_members = await get_non_responders(self.bot, self.guild, self.target)
        except (discord.HTTPException, ValueError) as error:
            await interaction.followup.send(f"새로고침하지 못했습니다: {error}", ephemeral=True)
            return
        self.pages = make_pages(self.members)
        self.page = min(self.page, len(self.pages) - 1)
        self.update_buttons()
        await interaction.edit_original_response(embed=self.embed(), view=self, allowed_mentions=discord.AllowedMentions.none())

    @discord.ui.button(label="미반응자 멘션", style=discord.ButtonStyle.danger)
    async def mention(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        # 실제 알림은 이 버튼을 눌렀을 때만, 일반 채널 메시지로 보냅니다.
        await interaction.response.defer(ephemeral=True)
        chunks: list[str] = []
        current: list[str] = []
        current_size = 0
        for member in self.members:
            token = member.mention
            if current and current_size + len(token) + 1 > MENTION_CHUNK_LIMIT:
                chunks.append(" ".join(current))
                current, current_size = [], 0
            current.append(token)
            current_size += len(token) + 1
        if current:
            chunks.append(" ".join(current))

        for index, chunk in enumerate(chunks, start=1):
            await interaction.channel.send(
                f"반응 확인 부탁드립니다. ({index}/{len(chunks)})\n{chunk}",
                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
            )
            await asyncio.sleep(0.5)
        await interaction.followup.send(f"미반응자 {len(self.members)}명을 멘션했습니다.", ephemeral=True)


def make_member_pages(members: list[discord.Member], empty_message: str) -> list[str]:
    if not members:
        return [empty_message]
    pages: list[str] = []
    lines: list[str] = []
    size = 0
    for member in members:
        line = f"• {safe_name(member)} (`{member.name}` · `{member.id}`)"
        if lines and size + len(line) + 1 > EMBED_LIMIT:
            pages.append("\n".join(lines))
            lines, size = [], 0
        lines.append(line)
        size += len(line) + 1
    if lines:
        pages.append("\n".join(lines))
    return pages


class MemberResultView(discord.ui.View):
    """회의 미참여자·잠수 의심자처럼 특정 멤버 목록을 보여주고 멘션합니다."""

    def __init__(
        self,
        owner_id: int,
        members: list[discord.Member],
        total_members: int,
        title: str,
        count_label: str,
        empty_message: str,
        mention_message: str,
    ) -> None:
        super().__init__(timeout=900)
        self.owner_id = owner_id
        self.members = members
        self.total_members = total_members
        self.title = title
        self.count_label = count_label
        self.mention_message = mention_message
        self.pages = make_member_pages(members, empty_message)
        self.page = 0
        self.mention.label = f"{count_label} 멘션"
        self.update_buttons()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id or can_manage(interaction):
            return True
        await interaction.response.send_message("명령어를 실행한 사람 또는 서버 관리 권한자만 사용할 수 있습니다.", ephemeral=True)
        return False

    def update_buttons(self) -> None:
        self.previous.disabled = self.page == 0
        self.next.disabled = self.page >= len(self.pages) - 1
        self.mention.disabled = not self.members

    def embed(self) -> discord.Embed:
        embed = discord.Embed(title=self.title, colour=discord.Colour.orange())
        embed.add_field(name="대상 인원", value=f"{self.total_members}명", inline=True)
        embed.add_field(name=self.count_label, value=f"{len(self.members)}명", inline=True)
        embed.description = self.pages[self.page]
        embed.set_footer(text=f"페이지 {self.page + 1}/{len(self.pages)} · 목록에서는 멘션되지 않습니다.")
        return embed

    @discord.ui.button(label="이전", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.page -= 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.embed(), view=self, allowed_mentions=discord.AllowedMentions.none())

    @discord.ui.button(label="다음", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.page += 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.embed(), view=self, allowed_mentions=discord.AllowedMentions.none())

    @discord.ui.button(label="대상자 멘션", style=discord.ButtonStyle.danger)
    async def mention(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        chunks: list[str] = []
        current: list[str] = []
        current_size = 0
        for member in self.members:
            mention = member.mention
            if current and current_size + len(mention) + 1 > MENTION_CHUNK_LIMIT:
                chunks.append(" ".join(current))
                current, current_size = [], 0
            current.append(mention)
            current_size += len(mention) + 1
        if current:
            chunks.append(" ".join(current))
        if interaction.channel is not None:
            for chunk in chunks:
                await interaction.channel.send(
                    f"{self.mention_message}\n{chunk}",
                    allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
                )
                await asyncio.sleep(0.5)
        await interaction.followup.send(f"{self.count_label} {len(self.members)}명을 멘션했습니다.", ephemeral=True)


class AbsenceReasonModal(discord.ui.Modal, title="불참 사유 신청"):
    reason = discord.ui.TextInput(label="불참 사유", style=discord.TextStyle.paragraph, max_length=1000)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("서버에서만 신청할 수 있습니다.", ephemeral=True)
            return
        settings = get_meeting_settings(interaction.guild.id)
        if settings.reason_channel_id is None:
            await interaction.response.send_message("사유방이 아직 등록되지 않았습니다.", ephemeral=True)
            return
        reason_channel = bot.get_channel(settings.reason_channel_id)
        if reason_channel is None:
            try:
                reason_channel = await bot.fetch_channel(settings.reason_channel_id)
            except discord.HTTPException:
                reason_channel = None
        if not isinstance(reason_channel, discord.TextChannel):
            await interaction.response.send_message("등록된 사유방을 찾을 수 없습니다. 소유주에게 다시 등록해 달라고 해주세요.", ephemeral=True)
            return

        reason = self.reason.value
        request_id = create_absence_request(interaction.guild.id, interaction.user.id, reason)
        embed = discord.Embed(title="불참 사유 신청", colour=discord.Colour.gold())
        embed.add_field(name="신청자", value=f"{interaction.user.mention} (`{interaction.user.id}`)", inline=False)
        embed.add_field(name="사유", value=discord.utils.escape_mentions(reason), inline=False)
        try:
            request_message = await reason_channel.send(
                embed=embed,
                view=AbsenceDecisionView(request_id),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            reject_absence_request(request_id)
            await interaction.response.send_message("사유방에 신청을 보낼 수 없습니다. 봇 권한을 확인해 주세요.", ephemeral=True)
            return
        set_absence_request_message(request_id, reason_channel.id, request_message.id)
        await interaction.response.send_message("불참 사유 신청을 보냈습니다. 소유주의 검토를 기다려 주세요.", ephemeral=True)


class MeetingAnnouncementModal(discord.ui.Modal, title="회의 공지 작성"):
    time = discord.ui.TextInput(label="회의 시간", placeholder="예: 9월 20일 오후 8시", max_length=100)
    content = discord.ui.TextInput(label="공지 내용", style=discord.TextStyle.paragraph, max_length=1500)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
            return
        settings = get_meeting_settings(interaction.guild.id)
        if settings.notice_channel_id is None:
            await interaction.response.send_message("먼저 `/공지방등록`으로 공지방을 지정해 주세요.", ephemeral=True)
            return
        notice_channel = bot.get_channel(settings.notice_channel_id)
        if notice_channel is None:
            try:
                notice_channel = await bot.fetch_channel(settings.notice_channel_id)
            except discord.HTTPException:
                notice_channel = None
        if not isinstance(notice_channel, discord.TextChannel):
            await interaction.response.send_message("등록된 공지방을 찾을 수 없습니다. 다시 등록해 주세요.", ephemeral=True)
            return

        try:
            await notice_channel.send(
                content="|| @everyone ||\n# 회의 공지",
                embed=discord.Embed(
                    description=(
                        f"## 회의시간 : {discord.utils.escape_mentions(self.time.value)}\n\n"
                        f"{discord.utils.escape_mentions(self.content.value)}"
                    ),
                    colour=discord.Colour.blurple(),
                ).set_footer(
                    text="사정으로 불참시 불참 사유 신청을 통해 사유 신청해주십시오."
                ),
                view=AbsenceApplyView(),
                allowed_mentions=discord.AllowedMentions(users=False, roles=False, everyone=True),
            )
        except discord.Forbidden:
            await interaction.response.send_message("공지방에 메시지를 보내거나 @everyone을 멘션할 권한이 없습니다.", ephemeral=True)
            return
        await interaction.response.send_message(f"회의 공지를 {notice_channel.mention}에 올렸습니다.", ephemeral=True)


class MeetingApplyRow(discord.ui.ActionRow):
    @discord.ui.button(label="불참사유신청", style=discord.ButtonStyle.secondary,
                       custom_id="meeting_absence_apply_box")
    async def apply(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(AbsenceReasonModal())


class MeetingAnnouncementView(discord.ui.LayoutView):
    def __init__(self, time: str = "회의 시간", content: str = "회의 내용") -> None:
        super().__init__(timeout=None)
        heading = discord.utils.escape_mentions(time).replace("\n", " ").replace("\r", " ")
        self.add_item(discord.ui.TextDisplay("|| @everyone ||"))
        self.add_item(discord.ui.Container(
            discord.ui.TextDisplay(f"# {heading} 시작\n\n{discord.utils.escape_mentions(content)}"),
            MeetingApplyRow(),
        ))


class AbsenceApplyView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="불참 사유 신청", style=discord.ButtonStyle.secondary, custom_id="meeting_absence_apply")
    async def apply(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(AbsenceReasonModal())


class AbsenceDecisionButton(discord.ui.Button):
    def __init__(self, request_id: int, approved: bool) -> None:
        super().__init__(
            label="사유 수락" if approved else "사유 거절",
            style=discord.ButtonStyle.success if approved else discord.ButtonStyle.danger,
            custom_id=f"meeting_absence_{'approve' if approved else 'reject'}_{request_id}",
        )
        self.approved = approved

    async def callback(self, interaction: discord.Interaction) -> None:
        assert isinstance(self.view, AbsenceDecisionView)
        await self.view.handle_decision(interaction, self.approved)


class AbsenceDecisionView(discord.ui.View):
    def __init__(self, request_id: int) -> None:
        super().__init__(timeout=None)
        self.request_id = request_id
        self.add_item(AbsenceDecisionButton(request_id, approved=True))
        self.add_item(AbsenceDecisionButton(request_id, approved=False))

    async def handle_decision(self, interaction: discord.Interaction, approved: bool) -> None:
        if not is_owner(interaction):
            await interaction.response.send_message("봇 소유자 또는 서버 소유자만 사유를 처리할 수 있습니다.", ephemeral=True)
            return
        result = approve_absence_request(self.request_id, interaction.user.id) if approved else reject_absence_request(self.request_id)
        if result is None:
            await interaction.response.send_message("이미 처리되었거나 존재하지 않는 신청입니다.", ephemeral=True)
            return
        for item in self.children:
            item.disabled = True
        if approved:
            embed = discord.Embed(title="불참 사유 승인됨", colour=discord.Colour.green())
            embed.description = "승인된 신청자는 불참자 명단에 추가되었습니다."
        else:
            embed = discord.Embed(title="불참 사유 거절됨", colour=discord.Colour.red())
            embed.description = "신청 자료는 폐기되었으며 불참자 명단에 추가되지 않았습니다."
        await interaction.response.edit_message(embed=embed, view=self, allowed_mentions=discord.AllowedMentions.none())


class AttendanceBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True
        intents.voice_states = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.application_owner_id: int | None = None

    async def setup_hook(self) -> None:
        app_info = await self.application_info()
        self.application_owner_id = app_info.owner.id
        self.add_view(AbsenceApplyView())
        self.add_view(MeetingAnnouncementView())
        for request_id, message_id in get_pending_request_message_ids():
            self.add_view(AbsenceDecisionView(request_id), message_id=message_id)
        await self.tree.sync()


bot = AttendanceBot()
group = app_commands.Group(name="미반응자", description="반응하지 않은 일반 유저를 확인합니다.")
admin_group = app_commands.Group(name="관리자", description="공지용 봇 관리자 권한을 관리합니다.")


@bot.event
async def on_voice_state_update(
    member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
) -> None:
    """등록된 회의방의 입장·퇴장을 등록된 로그방으로 기록합니다."""
    if member.bot or member.guild is None:
        return
    settings = get_meeting_settings(member.guild.id)
    if settings.meeting_channel_id is None or settings.log_channel_id is None:
        return
    was_in_meeting = before.channel is not None and before.channel.id == settings.meeting_channel_id
    is_in_meeting = after.channel is not None and after.channel.id == settings.meeting_channel_id
    # 같은 회의방에서의 음소거, 화면공유 등의 상태 변화는 로그로 남기지 않습니다.
    if was_in_meeting == is_in_meeting:
        return
    log_channel = bot.get_channel(settings.log_channel_id)
    if log_channel is None:
        try:
            log_channel = await bot.fetch_channel(settings.log_channel_id)
        except discord.HTTPException:
            return
    if not isinstance(log_channel, discord.TextChannel):
        return
    if is_in_meeting:
        title, description, colour = (
            "회의방 입장",
            f"{member.mention} 님이 회의방에 입장했습니다.",
            discord.Colour.green(),
        )
    else:
        title, description, colour = (
            "회의방 퇴장",
            f"{member.mention} 님이 회의방에서 퇴장했습니다.",
            discord.Colour.red(),
        )
    await log_channel.send(
        embed=discord.Embed(title=title, description=description, colour=colour),
        allowed_mentions=discord.AllowedMentions.none(),
    )


async def get_meeting_members(guild: discord.Guild, meeting_channel_id: int) -> list[discord.Member]:
    return [
        member
        async for member in guild.fetch_members(limit=None)
        if not member.bot
        and member.voice is not None
        and member.voice.channel is not None
        and member.voice.channel.id == meeting_channel_id
    ]


async def get_meeting_non_participants(
    guild: discord.Guild, meeting_channel_id: int
) -> tuple[list[discord.Member], int]:
    members = [member async for member in guild.fetch_members(limit=None) if not member.bot]
    approved_absence_ids = {user_id for user_id, _ in get_approved_absences(guild.id)}
    eligible_members = [member for member in members if member.id not in approved_absence_ids]
    non_participants = [
        member
        for member in eligible_members
        if member.voice is None
        or member.voice.channel is None
        or member.voice.channel.id != meeting_channel_id
    ]
    return non_participants, len(eligible_members)


def make_absence_pages(absences: list[tuple[int, str]]) -> list[str]:
    if not absences:
        return ["승인된 불참자가 없습니다."]
    pages: list[str] = []
    lines: list[str] = []
    size = 0
    for user_id, _ in absences:
        line = f"• <@{user_id}> (`{user_id}`)"
        if lines and size + len(line) + 1 > EMBED_LIMIT:
            pages.append("\n".join(lines))
            lines, size = [], 0
        lines.append(line)
        size += len(line) + 1
    if lines:
        pages.append("\n".join(lines))
    return pages


@bot.tree.command(name="회의방등록", description="회의에 사용할 음성 채널을 등록합니다.")
@app_commands.describe(회의방="회의를 진행할 음성 채널")
async def register_meeting_channel(interaction: discord.Interaction, 회의방: discord.VoiceChannel | discord.StageChannel) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
        return
    if not is_owner(interaction):
        await interaction.response.send_message("봇 소유자 또는 서버 소유자만 회의방을 등록할 수 있습니다.", ephemeral=True)
        return
    set_meeting_setting(interaction.guild.id, "meeting_channel_id", 회의방.id)
    await interaction.response.send_message(f"회의방을 {회의방.mention}으로 등록했습니다.", ephemeral=True)


@bot.tree.command(name="로그방등록", description="회의방 입퇴장 로그를 보낼 채널을 등록합니다.")
@app_commands.describe(로그방="회의방 입퇴장 로그를 보낼 텍스트 채널")
async def register_log_channel(interaction: discord.Interaction, 로그방: discord.TextChannel) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
        return
    if not is_owner(interaction):
        await interaction.response.send_message("봇 소유자 또는 서버 소유자만 로그방을 등록할 수 있습니다.", ephemeral=True)
        return
    set_meeting_setting(interaction.guild.id, "log_channel_id", 로그방.id)
    await interaction.response.send_message(f"로그방을 {로그방.mention}으로 등록했습니다.", ephemeral=True)


@bot.tree.command(name="공지방등록", description="회의 공지를 올릴 채널을 등록합니다.")
@app_commands.describe(공지방="회의 공지를 올릴 텍스트 채널")
async def register_meeting_notice_channel(interaction: discord.Interaction, 공지방: discord.TextChannel) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
        return
    if not is_owner(interaction):
        await interaction.response.send_message("봇 소유자 또는 서버 소유자만 공지방을 등록할 수 있습니다.", ephemeral=True)
        return
    set_meeting_setting(interaction.guild.id, "notice_channel_id", 공지방.id)
    await interaction.response.send_message(f"공지방을 {공지방.mention}으로 등록했습니다.", ephemeral=True)


@bot.tree.command(name="사유방등록", description="불참 사유 신청을 받을 채널을 등록합니다.")
@app_commands.describe(사유방="불참 사유 신청을 받을 텍스트 채널")
async def register_reason_channel(interaction: discord.Interaction, 사유방: discord.TextChannel) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
        return
    if not is_owner(interaction):
        await interaction.response.send_message("봇 소유자 또는 서버 소유자만 사유방을 등록할 수 있습니다.", ephemeral=True)
        return
    set_meeting_setting(interaction.guild.id, "reason_channel_id", 사유방.id)
    await interaction.response.send_message(f"사유방을 {사유방.mention}으로 등록했습니다.", ephemeral=True)


@bot.tree.command(name="회의공지", description="시간과 내용을 입력해 회의 공지를 올립니다.")
async def meeting_notice(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
        return
    if not is_owner(interaction):
        await interaction.response.send_message("봇 소유자 또는 서버 소유자만 회의 공지를 보낼 수 있습니다.", ephemeral=True)
        return
    if get_meeting_settings(interaction.guild.id).notice_channel_id is None:
        await interaction.response.send_message("먼저 `/공지방등록`으로 공지방을 지정해 주세요.", ephemeral=True)
        return
    await interaction.response.send_modal(MeetingAnnouncementModal())


@bot.tree.command(name="불참자명단", description="승인된 불참자 명단을 확인합니다.")
async def approved_absence_list(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
        return
    if not can_manage(interaction):
        await interaction.response.send_message("서버 관리 권한이 필요합니다.", ephemeral=True)
        return
    pages = make_absence_pages(get_approved_absences(interaction.guild.id))
    for index, page in enumerate(pages, start=1):
        embed = discord.Embed(title="승인된 불참자 명단", description=page, colour=discord.Colour.gold())
        if len(pages) > 1:
            embed.set_footer(text=f"페이지 {index}/{len(pages)}")
        if index == 1:
            await interaction.response.send_message(
                embed=embed, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
            )
        else:
            await interaction.followup.send(
                embed=embed, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
            )


@bot.tree.command(name="사유확인", description="승인된 불참자의 사유를 확인합니다.")
@app_commands.describe(유저="불참 사유를 확인할 유저")
async def check_absence_reason(interaction: discord.Interaction, 유저: discord.Member) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
        return
    if not can_manage(interaction):
        await interaction.response.send_message("서버 관리 권한이 필요합니다.", ephemeral=True)
        return
    reason = get_approved_absence(interaction.guild.id, 유저.id)
    if reason is None:
        await interaction.response.send_message("불참자가 아닙니다.", ephemeral=True)
        return
    embed = discord.Embed(title="불참 사유 확인", colour=discord.Colour.gold())
    embed.add_field(name="유저", value=f"{discord.utils.escape_mentions(유저.display_name)} (`{유저.id}`)", inline=False)
    embed.add_field(name="사유", value=discord.utils.escape_mentions(reason), inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="미참여자확인", description="등록된 회의방에 현재 없는 사람을 확인합니다.")
async def check_meeting_non_participants(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
        return
    if not can_manage(interaction):
        await interaction.response.send_message("서버 관리 권한이 필요합니다.", ephemeral=True)
        return
    meeting_channel_id = get_meeting_settings(interaction.guild.id).meeting_channel_id
    if meeting_channel_id is None:
        await interaction.response.send_message("먼저 `/회의방등록`으로 회의방을 지정해 주세요.", ephemeral=True)
        return
    await interaction.response.defer()
    members, total = await get_meeting_non_participants(interaction.guild, meeting_channel_id)
    view = MemberResultView(
        interaction.user.id,
        members,
        total,
        "회의 미참여자 확인",
        "미참여자",
        "등록된 불참자를 제외한 모든 대상자가 회의방에 참여 중입니다.",
        "회의방 참여 부탁드립니다.",
    )
    await interaction.followup.send(embed=view.embed(), view=view, allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="잠수확인", description="회의방 참여자 중 메시지 반응을 누르지 않은 사람을 확인합니다.")
@app_commands.describe(메시지_id="반응을 확인할 메시지 ID")
async def check_idle_members(interaction: discord.Interaction, 메시지_id: str) -> None:
    if interaction.guild is None or interaction.channel is None:
        await interaction.response.send_message("서버 채널에서만 사용할 수 있습니다.", ephemeral=True)
        return
    if not can_manage(interaction):
        await interaction.response.send_message("서버 관리 권한이 필요합니다.", ephemeral=True)
        return
    meeting_channel_id = get_meeting_settings(interaction.guild.id).meeting_channel_id
    if meeting_channel_id is None:
        await interaction.response.send_message("먼저 `/회의방등록`으로 회의방을 지정해 주세요.", ephemeral=True)
        return
    try:
        message = await interaction.channel.fetch_message(int(메시지_id))
    except (ValueError, discord.NotFound):
        await interaction.response.send_message("현재 채널에서 해당 메시지 ID를 찾지 못했습니다.", ephemeral=True)
        return
    except discord.HTTPException:
        await interaction.response.send_message("메시지를 읽지 못했습니다. 봇 권한을 확인해 주세요.", ephemeral=True)
        return
    await interaction.response.defer()
    reacted_ids: set[int] = set()
    for reaction in message.reactions:
        async for user in reaction.users(limit=None):
            if not user.bot:
                reacted_ids.add(user.id)
    meeting_members = await get_meeting_members(interaction.guild, meeting_channel_id)
    idle_members = [member for member in meeting_members if member.id not in reacted_ids]
    view = MemberResultView(
        interaction.user.id,
        idle_members,
        len(meeting_members),
        "회의방 잠수 확인",
        "미반응자",
        "현재 회의방에 참여한 모든 사람이 메시지에 반응했습니다.",
        "반응 확인 부탁드립니다.",
    )
    await interaction.followup.send(embed=view.embed(), view=view, allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="인원체크", description="현재 채널의 메시지를 반응 확인 대상으로 등록합니다.")
@app_commands.describe(message_id="반응을 확인할 메시지 ID", emoji="반응으로 인정할 이모지 하나")
async def set_check_target(interaction: discord.Interaction, message_id: str, emoji: str) -> None:
    if interaction.guild is None or interaction.channel is None:
        await interaction.response.send_message("서버 채널에서만 사용할 수 있습니다.", ephemeral=True)
        return
    if not can_manage(interaction):
        await interaction.response.send_message("서버 관리 권한이 필요합니다.", ephemeral=True)
        return
    try:
        numeric_message_id = int(message_id)
        target = CheckTarget(interaction.guild.id, interaction.channel.id, numeric_message_id, emoji)
        message = await fetch_target_message(bot, target)
    except (ValueError, discord.NotFound):
        await interaction.response.send_message("현재 채널에서 해당 메시지 ID를 찾지 못했습니다.", ephemeral=True)
        return
    except discord.HTTPException:
        await interaction.response.send_message("메시지를 읽지 못했습니다. 봇 권한을 확인해 주세요.", ephemeral=True)
        return
    save_target(target)
    await interaction.response.send_message(
        f"인원 체크 메시지를 등록했습니다: [메시지 보기]({message.jump_url})\n인정 기준: `{emoji}` 반응",
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@group.command(name="확인", description="등록된 메시지의 미반응자를 확인합니다.")
async def check_non_responders(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
        return
    if not can_manage(interaction):
        await interaction.response.send_message("서버 관리 권한이 필요합니다.", ephemeral=True)
        return
    target = get_target(interaction.guild.id)
    if target is None:
        await interaction.response.send_message("먼저 `/인원체크`로 대상 메시지를 등록해 주세요.", ephemeral=True)
        return
    await interaction.response.defer()
    try:
        message, members, total_members = await get_non_responders(bot, interaction.guild, target)
    except discord.NotFound:
        await interaction.followup.send("등록된 메시지 또는 채널을 찾지 못했습니다. 다시 등록해 주세요.", ephemeral=True)
        return
    except discord.Forbidden:
        await interaction.followup.send("메시지·반응·멤버 목록을 볼 권한이 없습니다.", ephemeral=True)
        return
    except discord.HTTPException as error:
        await interaction.followup.send(f"조회 중 오류가 발생했습니다: {error}", ephemeral=True)
        return
    view = ResultView(bot, interaction.user.id, interaction.guild, target, members, total_members, message)
    await interaction.followup.send(embed=view.embed(), view=view, allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="공지", description="서버 일반 유저에게 공지 내용을 DM으로 보냅니다.")
@app_commands.describe(내용="DM으로 보낼 공지 내용")
async def send_notice(interaction: discord.Interaction, 내용: str) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
        return
    if not can_use_notice_admin(interaction):
        await interaction.response.send_message("공지 봇 관리자 권한이 필요합니다.", ephemeral=True)
        return
    if len(내용) > NOTICE_CONTENT_LIMIT:
        await interaction.response.send_message(
            f"공지 내용은 {NOTICE_CONTENT_LIMIT}자 이하로 입력해 주세요.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    sent_count = 0
    failed_members: list[discord.Member] = []
    total_members = 0
    exempt_count = 0
    exempt_ids = get_notice_exemptions(interaction.guild.id)

    # fetch_members로 캐시에 없는 현재 서버 멤버도 포함합니다.
    async for member in interaction.guild.fetch_members(limit=None):
        if member.bot:
            continue
        if member.id in exempt_ids:
            exempt_count += 1
            continue
        total_members += 1
        notice = f"# [외지주 공지]\n\n{내용}\n\n-# 공지 DM입니다 {member.mention}"
        if await send_direct_notice(member, notice):
            sent_count += 1
        else:
            failed_members.append(member)
        # 짧은 간격만 두고, Discord의 실제 제한은 라이브러리가 처리합니다.
        await asyncio.sleep(0.1)

    failed_count = len(failed_members)
    mention_pages = make_mention_pages(failed_members)
    for page_number, mention_page in enumerate(mention_pages, start=1):
        embed = discord.Embed(title="공지 DM 전송 결과", colour=discord.Colour.green())
        embed.add_field(name="전체 인원", value=f"{total_members}명", inline=True)
        embed.add_field(name="성공 인원", value=f"{sent_count}명", inline=True)
        embed.add_field(name="실패 인원", value=f"{failed_count}명", inline=True)
        embed.add_field(name="공지 예외 인원", value=f"{exempt_count}명", inline=True)
        embed.add_field(name="실패 인원 멘션", value=mention_page, inline=False)
        if len(mention_pages) > 1:
            embed.set_footer(text=f"실패 목록 {page_number}/{len(mention_pages)}")
        await interaction.followup.send(
            embed=embed,
            ephemeral=True,
            # 실패 목록은 보기용이며, 실패한 사람에게 다시 알리지 않습니다.
            allowed_mentions=discord.AllowedMentions.none(),
        )


@bot.tree.command(name="공지예외인원설정", description="특정 유저를 공지 DM 대상에서 추가하거나 제거합니다.")
@app_commands.describe(유저="공지 DM 예외로 설정할 유저", 동작="예외 명단에 추가하거나 제거합니다.")
@app_commands.choices(
    동작=[
        app_commands.Choice(name="추가", value="add"),
        app_commands.Choice(name="제거", value="remove"),
    ]
)
async def set_notice_exempt_member(
    interaction: discord.Interaction,
    유저: discord.Member,
    동작: app_commands.Choice[str],
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
        return
    if not can_use_notice_admin(interaction):
        await interaction.response.send_message("공지 봇 관리자 권한이 필요합니다.", ephemeral=True)
        return
    if 유저.bot:
        await interaction.response.send_message("봇 계정은 공지 대상이 아니므로 설정할 수 없습니다.", ephemeral=True)
        return

    is_exempt = 동작.value == "add"
    set_notice_exemption(interaction.guild.id, 유저.id, is_exempt)
    action_text = "공지 예외 명단에 추가했습니다" if is_exempt else "공지 예외 명단에서 제거했습니다"
    await interaction.response.send_message(
        f"{discord.utils.escape_mentions(유저.display_name)}님을 {action_text}.",
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@admin_group.command(name="부여", description="유저에게 공지용 봇 관리자 권한을 부여합니다.")
@app_commands.describe(유저="공지 봇 관리자로 지정할 유저")
async def grant_bot_administrator(interaction: discord.Interaction, 유저: discord.Member) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
        return
    if not is_owner(interaction):
        await interaction.response.send_message("봇 소유자 또는 서버 소유자만 관리자 권한을 부여할 수 있습니다.", ephemeral=True)
        return
    if 유저.bot:
        await interaction.response.send_message("봇 계정에는 권한을 부여할 수 없습니다.", ephemeral=True)
        return
    set_bot_administrator(interaction.guild.id, 유저.id, True)
    await interaction.response.send_message(
        f"{discord.utils.escape_mentions(유저.display_name)}님에게 공지 봇 관리자 권한을 부여했습니다.",
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@admin_group.command(name="해제", description="유저의 공지용 봇 관리자 권한을 해제합니다.")
@app_commands.describe(유저="공지 봇 관리자 권한을 해제할 유저")
async def revoke_bot_administrator(interaction: discord.Interaction, 유저: discord.Member) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
        return
    if not is_owner(interaction):
        await interaction.response.send_message("봇 소유자 또는 서버 소유자만 관리자 권한을 해제할 수 있습니다.", ephemeral=True)
        return
    set_bot_administrator(interaction.guild.id, 유저.id, False)
    await interaction.response.send_message(
        f"{discord.utils.escape_mentions(유저.display_name)}님의 공지 봇 관리자 권한을 해제했습니다.",
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@admin_group.command(name="목록", description="공지용 봇 관리자 목록을 확인합니다.")
async def list_bot_administrators(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("서버에서만 사용할 수 있습니다.", ephemeral=True)
        return
    if not is_owner(interaction):
        await interaction.response.send_message("봇 소유자 또는 서버 소유자만 관리자 목록을 확인할 수 있습니다.", ephemeral=True)
        return

    entries = [
        f"• 봇 소유자 (`{bot.application_owner_id}`)",
        f"• 서버 소유자 (`{interaction.guild.owner_id}`)",
    ]
    for user_id in get_bot_administrator_ids(interaction.guild.id):
        member = interaction.guild.get_member(user_id)
        if member is None:
            try:
                member = await interaction.guild.fetch_member(user_id)
            except discord.HTTPException:
                entries.append(f"• 알 수 없는 사용자 (`{user_id}`)")
                continue
        entries.append(f"• {safe_name(member)} (`{user_id}`)")

    embed = discord.Embed(title="공지 봇 관리자 목록", description="\n".join(entries), colour=discord.Colour.blurple())
    embed.set_footer(text="봇 소유자와 서버 소유자는 항상 공지 관리 권한을 가집니다.")
    await interaction.response.send_message(
        embed=embed,
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


bot.tree.add_command(group)
bot.tree.add_command(admin_group)


if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token or token == "붙여넣을_봇_토큰":
        raise RuntimeError(f"{ENVIRONMENT_PATH} 파일에 DISCORD_TOKEN을 설정해 주세요.")
    bot.run(token)

