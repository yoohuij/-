"""Server-wide human voice count, with a persistent display channel."""
import asyncio
import logging
from contextlib import closing
import discord
from discord import app_commands
from discord.ext import tasks


async def human_voice_count(guild):
    ids = {uid for channel in [*guild.voice_channels, *guild.stage_channels]
           for uid in channel.voice_states}
    count = 0
    for uid in ids:
        member = guild.get_member(uid)
        if member is None:
            try:
                member = await guild.fetch_member(uid)
            except discord.NotFound:
                continue
        if not member.bot:
            count += 1
    return count


class VoiceActivity:
    def __init__(self, bot, database):
        self.bot, self.database = bot, database
        self.locks = {}

    def connect(self):
        db = self.database()
        db.execute('CREATE TABLE IF NOT EXISTS voice_activity (guild_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL)')
        db.commit()
        return db

    def channel_id(self, guild_id):
        with closing(self.connect()) as db:
            row = db.execute('SELECT channel_id FROM voice_activity WHERE guild_id=?', (guild_id,)).fetchone()
        return row[0] if row else None

    def save(self, guild_id, channel_id):
        with closing(self.connect()) as db, db:
            db.execute('INSERT OR REPLACE INTO voice_activity VALUES (?,?)', (guild_id, channel_id))

    def lock(self, guild_id):
        return self.locks.setdefault(guild_id, asyncio.Lock())

    async def get_channel(self, guild):
        cid = self.channel_id(guild.id)
        if cid is None:
            return None
        channel = guild.get_channel(cid)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(cid)
            except discord.NotFound:
                return None
        if not isinstance(channel, discord.VoiceChannel) or channel.guild.id != guild.id:
            raise ValueError('등록된 표시 채널을 확인해 주세요.')
        return channel

    async def update(self, guild):
        async with self.lock(guild.id):
            channel = await self.get_channel(guild)
            if channel is None:
                return
            name = f'통방 활동률 : {await human_voice_count(guild)}명'
            if channel.name != name:
                await channel.edit(name=name, reason='서버 전체 통방 접속 인원 갱신')

    @tasks.loop(minutes=5)
    async def refresh(self):
        for guild in self.bot.guilds:
            try:
                async with asyncio.timeout(120):
                    await self.update(guild)
            except (discord.HTTPException, ValueError, asyncio.TimeoutError):
                logging.getLogger(__name__).exception('통방 활동률 갱신 실패: %s', guild.id)

    @refresh.before_loop
    async def before_refresh(self):
        await self.bot.wait_until_ready()

    def start(self):
        if not self.refresh.is_running():
            self.refresh.start()


def install(bot, database, manager_check):
    feature = VoiceActivity(bot, database)

    @bot.tree.command(name='통방인원체크', description='서버 전체 음성 접속 인원 표시 채널을 설정합니다.')
    @app_commands.guild_only()
    async def configure(interaction: discord.Interaction, 카테고리: discord.CategoryChannel):
        if not manager_check(interaction):
            await interaction.response.send_message('소유주 또는 봇에 등록된 관리자만 설정할 수 있습니다.', ephemeral=True)
            return
        if 카테고리.guild.id != interaction.guild.id:
            await interaction.response.send_message('현재 서버의 카테고리를 선택해 주세요.', ephemeral=True)
            return
        perms = 카테고리.permissions_for(interaction.guild.me)
        if not perms.manage_channels or not perms.view_channel:
            await interaction.response.send_message('봇에 채널 보기·채널 관리 권한이 필요합니다.', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            async with asyncio.timeout(120):
                async with feature.lock(interaction.guild.id):
                    channel = await feature.get_channel(interaction.guild)
                    if channel is None:
                        count = await human_voice_count(interaction.guild)
                        channel = await interaction.guild.create_voice_channel(
                            f'통방 활동률 : {count}명', category=카테고리,
                            overwrites={interaction.guild.default_role: discord.PermissionOverwrite(view_channel=True, connect=False),
                                        interaction.guild.me: discord.PermissionOverwrite(view_channel=True, manage_channels=True)},
                            reason='통방 활동률 표시 채널 생성')
                        # Save before moving; retry cannot create a duplicate.
                        feature.save(interaction.guild.id, channel.id)
                    await channel.move(beginning=True, category=카테고리, sync_permissions=False,
                                       reason='카테고리 음성 채널 목록 맨 위로 이동')
            await interaction.followup.send(
                f'{channel.mention} 설정 완료. 서버 전체 음성방·스테이지의 인원을 봇 제외로 집계하며 약 5분마다 자동 갱신합니다. 다시 지정하면 기존 표시 채널을 이동합니다.',
                ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        except (discord.HTTPException, ValueError, asyncio.TimeoutError):
            await interaction.followup.send('설정을 완료하지 못했습니다. 채널 관리 권한 확인 후 다시 실행해 주세요. 생성된 표시 채널은 재사용합니다.', ephemeral=True)

    return feature
