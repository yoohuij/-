"""Owner-configured meeting text-channel cleanup."""
import asyncio
from contextlib import closing
import discord
from discord import app_commands

class MeetingChat:
    def __init__(self, bot, database):
        self.bot, self.database = bot, database
        self.locks = {}

    def lock(self, guild_id):
        return self.locks.setdefault(guild_id, asyncio.Lock())

    def connect(self):
        db = self.database()
        db.execute("CREATE TABLE IF NOT EXISTS meeting_chat (guild_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL)")
        db.commit()
        return db

    def channel_id(self, guild_id):
        with closing(self.connect()) as db:
            row = db.execute("SELECT channel_id FROM meeting_chat WHERE guild_id=?", (guild_id,)).fetchone()
        return row[0] if row else None

    def save(self, guild_id, channel_id):
        with closing(self.connect()) as db, db:
            db.execute("INSERT OR REPLACE INTO meeting_chat VALUES (?,?)", (guild_id, channel_id))

    async def validate(self, guild, channel_id, settings):
        if channel_id in (settings.notice_channel_id, settings.reason_channel_id, settings.log_channel_id):
            raise ValueError("공지방·사유방·로그방은 회의채팅방으로 사용할 수 없습니다.")
        channel = await self.bot.fetch_channel(channel_id)
        if not isinstance(channel, discord.TextChannel) or channel.guild.id != guild.id or channel.is_news():
            raise ValueError("같은 서버의 일반 텍스트 채널만 지정할 수 있습니다.")
        perms = channel.permissions_for(guild.me)
        if not all((perms.view_channel, perms.read_message_history, perms.manage_messages, perms.manage_roles, perms.manage_channels)):
            raise ValueError("봇에 채널 보기·이전 메시지 보기·메시지 관리·역할 관리·채널 관리 권한이 필요합니다.")
        return channel

    def validate_voice(self, channel):
        permissions = channel.permissions_for(channel.guild.me)
        if not permissions.move_members or not permissions.view_channel:
            raise ValueError("회의방에서 봇의 채널 보기·멤버 이동 권한이 필요합니다.")
        if isinstance(channel, discord.StageChannel) and not permissions.manage_channels:
            raise ValueError("스테이지 종료를 위해 봇의 채널 관리 권한이 필요합니다.")

    async def close_voice(self, channel):
        self.validate_voice(channel)
        async with asyncio.timeout(300):
            for user_id in list(channel.voice_states):
                try:
                    member = channel.guild.get_member(user_id) or await channel.guild.fetch_member(user_id)
                    # Do not disconnect someone who has moved to another channel.
                    if member.voice and member.voice.channel and member.voice.channel.id == channel.id:
                        await member.move_to(None, reason="회의 종료: 전체 퇴장")
                except discord.NotFound:
                    continue
            if isinstance(channel, discord.StageChannel):
                try:
                    instance = await channel.fetch_instance()
                    await instance.delete(reason="회의 종료")
                except discord.NotFound:
                    pass

    async def cleanup(self, guild, settings):
        cid = self.channel_id(guild.id)
        if cid is None:
            return
        async with asyncio.timeout(600):
            channel = await self.validate(guild, cid, settings)
            overwrites = channel.overwrites
            overwrites.setdefault(guild.default_role, discord.PermissionOverwrite())
            for target, overwrite in overwrites.items():
                if target.id == guild.me.id:
                    continue
                overwrite.send_messages = False
                overwrite.send_messages_in_threads = False
                overwrite.create_public_threads = False
                overwrite.create_private_threads = False
            own = overwrites.setdefault(guild.me, discord.PermissionOverwrite())
            own.view_channel = own.read_message_history = own.send_messages = True
            # Lock first so ordinary members cannot add messages during deletion.
            await channel.edit(overwrites=overwrites, reason="회의 종료: 채팅 잠금")
            await channel.purge(limit=None, reason="회의 종료: 지정된 회의채팅방 전체 청소")

def install(bot, database, owner_check, settings_getter):
    feature = MeetingChat(bot, database)

    @bot.tree.command(name="회의채팅방지정", description="회의 종료 시 전체 메시지를 삭제하고 잠글 채팅방을 지정합니다.")
    @app_commands.guild_only()
    async def configure(interaction: discord.Interaction, 방: discord.TextChannel):
        await interaction.response.defer(ephemeral=True)
        if not owner_check(interaction):
            await interaction.followup.send("소유주만 회의채팅방을 지정할 수 있습니다.", ephemeral=True)
            return
        lock = feature.lock(interaction.guild.id)
        if lock.locked():
            await interaction.followup.send("회의 처리 중입니다. 완료 후 지정해 주세요.", ephemeral=True)
            return
        async with lock:
            try:
                await feature.validate(interaction.guild, 방.id, settings_getter(interaction.guild.id))
                feature.save(interaction.guild.id, 방.id)
            except (ValueError, discord.HTTPException) as exc:
                await interaction.followup.send(f"지정 실패: {exc}", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
                return
        await interaction.followup.send(
            f"{방.mention}을 지정했습니다. 회의 종료 시 고정 메시지를 포함한 본문 메시지가 모두 삭제되고 잠깁니다. 삭제는 복구할 수 없습니다. 기존 스레드 내용은 삭제하지 않습니다. 다음 회의에 자동으로 잠금 해제하지 않습니다.",
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
    return feature
