"""Owner-configured, administrator-operated temporary text channel."""
import asyncio
from contextlib import closing

import discord
from discord import app_commands


def opening_text(template, user):
    # Only the selected user and the explicit everyone mention may notify.
    return '|| @everyone ||\n' + discord.utils.escape_mentions(template).replace('{user}', user.mention)


class Lawless:
    def __init__(self, bot, database, owner_check, manager_check):
        self.bot, self.database = bot, database
        self.owner_check, self.manager_check = owner_check, manager_check
        self.locks = {}

    def connect(self):
        db = self.database()
        db.execute('''CREATE TABLE IF NOT EXISTS lawless_settings (
            guild_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL,
            notice TEXT NOT NULL, target_template TEXT NOT NULL,
            notice_id INTEGER, is_open INTEGER NOT NULL DEFAULT 0)''')
        db.commit()
        return db

    def settings(self, guild_id):
        with closing(self.connect()) as db:
            row = db.execute('SELECT channel_id, notice, target_template, notice_id, is_open FROM lawless_settings WHERE guild_id=?', (guild_id,)).fetchone()
        return dict(zip(('channel_id', 'notice', 'target_template', 'notice_id', 'is_open'), row)) if row else None

    def save(self, guild_id, config):
        with closing(self.connect()) as db, db:
            db.execute('INSERT OR REPLACE INTO lawless_settings VALUES (?,?,?,?,?,?)',
                       (guild_id, config['channel_id'], config['notice'], config['target_template'], config['notice_id'], config['is_open']))

    async def channel(self, guild, channel_id):
        channel = await self.bot.fetch_channel(channel_id)
        if not isinstance(channel, discord.TextChannel) or channel.guild.id != guild.id:
            raise ValueError('같은 서버의 일반 텍스트 채널을 지정해 주세요.')
        if channel.is_news():
            raise ValueError('공지 채널이 아닌 일반 텍스트 채널을 지정해 주세요.')
        permissions = channel.permissions_for(guild.me)
        if not all((permissions.view_channel, permissions.read_message_history,
                    permissions.manage_channels, permissions.manage_roles, permissions.manage_messages,
                    getattr(permissions, 'pin_messages', permissions.manage_messages))):
            raise ValueError('봇에 채널 보기·이전 메시지 보기·채널 관리·역할 관리·메시지 관리·메시지 고정 권한이 필요합니다.')
        return channel

    async def set_writable(self, channel, opened):
        # One channel edit avoids leaving only some role overwrites updated.
        overwrites = channel.overwrites
        everyone = overwrites.setdefault(channel.guild.default_role, discord.PermissionOverwrite())
        everyone.send_messages = opened
        everyone.send_messages_in_threads = opened
        everyone.create_public_threads = False
        everyone.create_private_threads = False
        for target, overwrite in overwrites.items():
            if target.id == channel.guild.me.id:
                continue
            for name in ('send_messages', 'send_messages_in_threads'):
                if getattr(overwrite, name) is not None:
                    setattr(overwrite, name, opened)
            # Keep this feature's conversations in the main channel.
            overwrite.create_public_threads = False
            overwrite.create_private_threads = False
        own = overwrites.setdefault(channel.guild.me, discord.PermissionOverwrite())
        own.view_channel = own.read_message_history = own.send_messages = True
        await channel.edit(overwrites=overwrites, reason='무법지대 오픈' if opened else '무법지대 잠금')

    async def ensure_notice(self, channel, guild_id, config):
        message = None
        if config['notice_id']:
            try:
                message = await channel.fetch_message(config['notice_id'])
            except discord.NotFound:
                pass
        if message is None:
            message = await channel.send(config['notice'], allowed_mentions=discord.AllowedMentions.none())
            config['notice_id'] = message.id
            self.save(guild_id, config)
        else:
            await message.edit(content=config['notice'], allowed_mentions=discord.AllowedMentions.none())
        if not message.pinned:
            await message.pin(reason='무법지대 고정 공지')
        return message

    async def operate(self, interaction, target=None):
        if not self.manager_check(interaction):
            await interaction.response.send_message('소유주 또는 봇에 등록된 관리자만 사용할 수 있습니다.', ephemeral=True)
            return
        lock = self.locks.setdefault(interaction.guild.id, asyncio.Lock())
        if lock.locked():
            await interaction.response.send_message('이 서버의 무법지대를 처리 중입니다. 잠시 후 다시 시도해 주세요.', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        async with lock:
            config = self.settings(interaction.guild.id)
            if config is None:
                await interaction.followup.send('먼저 `/무법지대지정`을 해 주세요.', ephemeral=True)
                return
            try:
                async with asyncio.timeout(600):
                    channel = await self.channel(interaction.guild, config['channel_id'])
                    if target is not None:
                        if config['is_open']:
                            raise ValueError('이미 열려 있습니다. 종료 후 다시 열어 주세요.')
                        if not channel.permissions_for(interaction.guild.me).mention_everyone:
                            raise ValueError('봇에 @everyone 멘션 권한이 필요합니다.')
                        await self.ensure_notice(channel, interaction.guild.id, config)
                        await self.set_writable(channel, True)
                        # Persist before sending so failures/restarts remain closable.
                        config['is_open'] = 1
                        self.save(interaction.guild.id, config)
                        try:
                            await channel.send(opening_text(config['target_template'], target),
                                               allowed_mentions=discord.AllowedMentions(users=[target], roles=False, everyone=True))
                        except discord.HTTPException:
                            await self.set_writable(channel, False)
                            config['is_open'] = 0
                            self.save(interaction.guild.id, config)
                            raise
                        result = f'{channel.mention} 무법지대를 열었습니다.'
                    else:
                        # Lock first; retries after a partial deletion are safe.
                        await self.set_writable(channel, False)
                        config['is_open'] = 0
                        self.save(interaction.guild.id, config)
                        notice = await self.ensure_notice(channel, interaction.guild.id, config)
                        deleted = await channel.purge(limit=None, check=lambda message: message.id != notice.id,
                                                      reason=f'무법지대 종료: {interaction.user.id}')
                        result = f'{channel.mention} 무법지대를 잠그고 공지 외 메시지 {len(deleted)}개를 삭제했습니다.'
            except ValueError as exc:
                result = str(exc)
            except (discord.HTTPException, asyncio.TimeoutError):
                result = '처리를 끝내지 못했습니다. 채널 상태와 봇 권한을 확인해 주세요. 일부 작업만 반영됐을 수 있습니다. 종료 중이었다면 `/무법지대종료`로 남은 메시지 삭제를 다시 시도할 수 있습니다.'
            await interaction.followup.send(result, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


class SetupModal(discord.ui.Modal, title='무법지대 설정'):
    notice = discord.ui.TextInput(label='공지메시지', style=discord.TextStyle.paragraph, max_length=1800)
    target_template = discord.ui.TextInput(label='대상메시지 ({user} 필수)', style=discord.TextStyle.paragraph,
                                          placeholder='{user}님과 대화할 무법지대가 열렸습니다.', max_length=1500)

    def __init__(self, feature, channel):
        super().__init__()
        self.feature, self.channel_id = feature, channel.id
        config = feature.settings(channel.guild.id)
        if config:
            self.notice.default = config['notice']
            self.target_template.default = config['target_template']

    async def on_submit(self, interaction):
        if not self.feature.owner_check(interaction):
            await interaction.response.send_message('소유주만 설정할 수 있습니다.', ephemeral=True)
            return
        if not self.notice.value.strip() or '{user}' not in self.target_template.value:
            await interaction.response.send_message('공지 내용을 입력하고 대상메시지에 `{user}`를 넣어 주세요.', ephemeral=True)
            return
        lock = self.feature.locks.setdefault(interaction.guild.id, asyncio.Lock())
        if lock.locked():
            await interaction.response.send_message('처리 중입니다. 잠시 후 다시 설정해 주세요.', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        async with lock:
            previous = self.feature.settings(interaction.guild.id)
            if previous and previous['is_open']:
                await interaction.followup.send('열려 있는 무법지대를 먼저 종료해 주세요.', ephemeral=True)
                return
            config = dict(channel_id=self.channel_id, notice=self.notice.value.strip(),
                          target_template=self.target_template.value.strip(), is_open=0,
                          notice_id=previous['notice_id'] if previous and previous['channel_id'] == self.channel_id else None)
            try:
                channel = await self.feature.channel(interaction.guild, self.channel_id)
                await self.feature.set_writable(channel, False)
                self.feature.save(interaction.guild.id, config)
                await self.feature.ensure_notice(channel, interaction.guild.id, config)
                text = f'{channel.mention}에 공지를 고정하고 채팅을 잠갔습니다. 종료하면 이 채널 본문의 공지 외 모든 메시지가 삭제됩니다. 기존 스레드의 메시지는 삭제하지 않습니다.'
            except ValueError as exc:
                text = str(exc)
            except discord.HTTPException as exc:
                text = f'설정을 완료하지 못했습니다. 권한 확인 후 다시 지정해 주세요. 일부 설정이 반영됐을 수 있습니다. ({type(exc).__name__})'
            await interaction.followup.send(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


def install(bot, database, owner_check, manager_check):
    feature = Lawless(bot, database, owner_check, manager_check)

    @bot.tree.command(name='무법지대지정', description='무법지대 채널과 공지·대상 메시지를 설정합니다.')
    @app_commands.guild_only()
    async def configure(interaction: discord.Interaction, 방멘션: discord.TextChannel):
        if not owner_check(interaction):
            await interaction.response.send_message('소유주만 설정할 수 있습니다.', ephemeral=True)
            return
        await interaction.response.send_modal(SetupModal(feature, 방멘션))

    @bot.tree.command(name='무법지대오픈', description='대상을 알리고 무법지대 채팅을 엽니다.')
    @app_commands.guild_only()
    async def open_zone(interaction: discord.Interaction, 대상멘션: discord.Member):
        await feature.operate(interaction, 대상멘션)

    @bot.tree.command(name='무법지대종료', description='채팅을 잠그고 공지 외 메시지를 삭제합니다.')
    @app_commands.guild_only()
    async def close_zone(interaction: discord.Interaction):
        await feature.operate(interaction)

    return feature
