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


class AttendanceBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.application_owner_id: int | None = None

    async def setup_hook(self) -> None:
        app_info = await self.application_info()
        self.application_owner_id = app_info.owner.id
        await self.tree.sync()


bot = AttendanceBot()
group = app_commands.Group(name="미반응자", description="반응하지 않은 일반 유저를 확인합니다.")
admin_group = app_commands.Group(name="관리자", description="공지용 봇 관리자 권한을 관리합니다.")


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

