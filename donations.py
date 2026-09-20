"""Global donation ledger with per-server announcement and ranking configuration."""
import asyncio
import re
from contextlib import contextmanager

import discord
from discord import app_commands


def render(template: str, user_id: int, money: int, rank: int | None = None) -> str:
    # Only replace supported tokens; arbitrary braces in user text remain intact.
    value = discord.utils.escape_mentions(template)
    if rank is not None and '{rank}' not in value:
        value = re.sub(r'^(\s*#{0,3}\s*)\d+\.', lambda m: m[1] + str(rank) + '.', value, count=1)
    return (value.replace('{user}', f'<@{user_id}>').replace('{money}', str(money))
            .replace('{moeny}', str(money)).replace('{rank}', str(rank or 1)))


class DonationStore:
    def __init__(self, database):
        self.database = database

    @contextmanager
    def connection(self):
        db = self.database()
        try:
            with db:
                db.execute('CREATE TABLE IF NOT EXISTS donation_records (interaction_id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, amount INTEGER NOT NULL CHECK(amount > 0), guild_id INTEGER NOT NULL, recorded_by INTEGER NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
                db.execute('CREATE TABLE IF NOT EXISTS donation_settings (guild_id INTEGER PRIMARY KEY, notice_channel INTEGER, ranking_channel INTEGER, notice_template TEXT, ranking_template TEXT, ranking_message INTEGER)')
                yield db
        finally:
            db.close()

    def settings(self, guild_id):
        with self.connection() as db:
            row = db.execute('SELECT notice_channel, ranking_channel, notice_template, ranking_template, ranking_message FROM donation_settings WHERE guild_id=?', (guild_id,)).fetchone()
        return dict(zip(('notice_channel', 'ranking_channel', 'notice_template', 'ranking_template', 'ranking_message'), row or (None,) * 5))

    def configure(self, guild_id, **values):
        allowed = {'notice_channel', 'ranking_channel', 'notice_template', 'ranking_template', 'ranking_message'}
        if not values or not set(values) <= allowed:
            raise ValueError('Invalid setting')
        with self.connection() as db:
            db.execute('INSERT OR IGNORE INTO donation_settings(guild_id) VALUES (?)', (guild_id,))
            db.execute('UPDATE donation_settings SET ' + ', '.join(f'{key}=?' for key in values) + ' WHERE guild_id=?', (*values.values(), guild_id))

    def record(self, interaction_id, user_id, amount, guild_id, actor):
        with self.connection() as db:
            if amount <= 0 or amount > 10**12:
                raise ValueError('금액은 1원 이상 1조 원 이하로 입력해 주세요.')
            return db.execute('INSERT OR IGNORE INTO donation_records(interaction_id,user_id,amount,guild_id,recorded_by) VALUES (?,?,?,?,?)', (interaction_id, user_id, amount, guild_id, actor)).rowcount == 1

    def totals(self):
        with self.connection() as db:
            return db.execute('SELECT user_id, SUM(amount) AS total FROM donation_records GROUP BY user_id ORDER BY total DESC, user_id ASC').fetchall()

    def ranking_guilds(self):
        with self.connection() as db:
            return [r[0] for r in db.execute('SELECT guild_id FROM donation_settings WHERE ranking_channel IS NOT NULL AND ranking_template IS NOT NULL')]


class DonationList(discord.ui.View):
    def __init__(self, owner_id, totals):
        super().__init__(timeout=900)
        self.owner_id, self.totals, self.page = owner_id, totals, 0
        self.update_buttons()

    def update_buttons(self):
        self.previous.disabled = self.page == 0
        self.next.disabled = (self.page + 1) * 20 >= len(self.totals)

    def embed(self):
        start = self.page * 20
        lines = [f'{i}. <@{uid}> - {money}원' for i, (uid, money) in enumerate(self.totals[start:start + 20], start + 1)]
        embed = discord.Embed(title='후원 목록', description='\n'.join(lines) or '등록된 후원 기록이 없습니다.', colour=discord.Colour.gold())
        embed.set_footer(text=f'총 {len(self.totals)}명 · {self.page + 1}/{max(1, (len(self.totals) + 19) // 20)}페이지 · 봇 전체 누적 금액')
        return embed

    async def interaction_check(self, interaction):
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message('조회한 사람만 페이지를 넘길 수 있습니다.', ephemeral=True)
        return False

    @discord.ui.button(label='이전', style=discord.ButtonStyle.secondary)
    async def previous(self, interaction, button):
        self.page = max(0, self.page - 1)
        self.update_buttons()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label='다음', style=discord.ButtonStyle.secondary)
    async def next(self, interaction, button):
        self.page = min(max(0, (len(self.totals) - 1) // 20), self.page + 1)
        self.update_buttons()
        await interaction.response.edit_message(embed=self.embed(), view=self)


class DonationFeature:
    def __init__(self, bot, database):
        self.bot = bot
        self.store = DonationStore(database)
        self.lock = asyncio.Lock()

    async def owner_check(self, interaction):
        if interaction.guild and interaction.user.id == self.bot.application_owner_id:
            return True
        await interaction.response.send_message('서버에서 봇 소유자만 후원 설정·기록을 변경할 수 있습니다.', ephemeral=True)
        return False

    async def channel(self, channel_id):
        if not channel_id:
            raise ValueError('먼저 해당 후원 채널을 등록해 주세요.')
        channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            raise ValueError('등록된 텍스트 채널을 찾을 수 없습니다.')
        return channel

    async def update_ranking(self, guild_id):
        config = self.store.settings(guild_id)
        channel = await self.channel(config['ranking_channel'])
        template = config['ranking_template']
        if not template:
            raise ValueError('먼저 `/후원랭킹메시지`로 양식을 등록해 주세요.')
        content = '\n\n'.join(render(template, uid, amount, rank) for rank, (uid, amount) in enumerate(self.store.totals()[:3], 1)) or '아직 후원 기록이 없습니다.'
        if len(content) > 2000:
            raise ValueError('랭킹 양식이 너무 깁니다. 더 짧게 등록해 주세요.')
        if config['ranking_message']:
            try:
                message = await channel.fetch_message(config['ranking_message'])
                await message.edit(content=content, allowed_mentions=discord.AllowedMentions.none())
                return
            except discord.NotFound:
                pass
        message = await channel.send(content, allowed_mentions=discord.AllowedMentions.none())
        self.store.configure(guild_id, ranking_message=message.id)


class DonationTemplateModal(discord.ui.Modal):
    def __init__(self, feature, ranking):
        super().__init__(title='후원 랭킹 양식 등록' if ranking else '후원 기록 공지 등록')
        self.feature, self.ranking = feature, ranking
        self.body = discord.ui.TextInput(label='출력 양식', style=discord.TextStyle.paragraph,
            placeholder='# {rank}. {user} : {money}원' if ranking else '🎉 {user}님 {money}원 후원 감사합니다 🎉',
            max_length=400 if ranking else 1500)
        self.add_item(self.body)

    async def on_submit(self, interaction):
        if not await self.feature.owner_check(interaction):
            return
        template = self.body.value.strip()
        if '{user}' not in template or not any(t in template for t in ('{money}', '{moeny}')):
            await interaction.response.send_message('양식에 {user}와 {money}를 넣어 주세요. {moeny}도 금액으로 인식합니다.', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        async with self.feature.lock:
            key = 'ranking_template' if self.ranking else 'notice_template'
            self.feature.store.configure(interaction.guild.id, **{key: template})
            if self.ranking:
                try:
                    await self.feature.update_ranking(interaction.guild.id)
                except (ValueError, discord.HTTPException) as error:
                    await interaction.followup.send(f'양식은 저장했지만 랭킹을 게시하지 못했습니다. 채널 설정·권한을 확인하고 다시 실행해 주세요. ({type(error).__name__})', ephemeral=True)
                    return
        await interaction.followup.send('랭킹 양식을 저장하고 상위 3명 메시지를 갱신했습니다.' if self.ranking else '후원 공지 양식을 저장했습니다.', ephemeral=True)


def install(bot, database):
    feature = DonationFeature(bot, database)

    async def register_channel(interaction, channel, ranking):
        if not await feature.owner_check(interaction):
            return
        perms = channel.permissions_for(interaction.guild.me)
        if not perms.view_channel or not perms.send_messages or (ranking and not perms.read_message_history):
            await interaction.response.send_message('봇의 채널 보기·메시지 보내기 권한을 확인해 주세요. 랭킹방에는 메시지 기록 보기 권한도 필요합니다.', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        async with feature.lock:
            if ranking:
                config = feature.store.settings(interaction.guild.id)
                feature.store.configure(interaction.guild.id, ranking_channel=channel.id,
                    ranking_message=config['ranking_message'] if config['ranking_channel'] == channel.id else None)
            else:
                feature.store.configure(interaction.guild.id, notice_channel=channel.id)
        await interaction.followup.send(f'{channel.mention}을 후원 {"랭킹" if ranking else "공지"}방으로 등록했습니다.', ephemeral=True)

    @bot.tree.command(name='후원공지방등록', description='후원 기록 공지를 보낼 채널을 지정합니다.')
    @app_commands.guild_only()
    async def notice_channel(interaction: discord.Interaction, 방: discord.TextChannel):
        await register_channel(interaction, 방, False)

    @bot.tree.command(name='후원랭킹방등록', description='후원 상위 3명 랭킹을 게시할 채널을 지정합니다.')
    @app_commands.guild_only()
    async def ranking_channel(interaction: discord.Interaction, 방: discord.TextChannel):
        await register_channel(interaction, 방, True)

    @bot.tree.command(name='후원기록공지등록', description='후원 공지에 사용할 {user}, {money} 양식을 등록합니다.')
    @app_commands.guild_only()
    async def notice_template(interaction: discord.Interaction):
        if await feature.owner_check(interaction):
            await interaction.response.send_modal(DonationTemplateModal(feature, False))

    @bot.tree.command(name='후원랭킹메시지', description='상위 3명 랭킹의 한 줄 양식을 등록하고 메시지를 게시합니다.')
    @app_commands.guild_only()
    async def ranking_template(interaction: discord.Interaction):
        if await feature.owner_check(interaction):
            await interaction.response.send_modal(DonationTemplateModal(feature, True))

    @bot.tree.command(name='후원기록', description='후원 금액을 누적 저장하고 후원 공지와 랭킹을 갱신합니다.')
    @app_commands.guild_only()
    async def record(interaction: discord.Interaction, 유저: discord.User, 금액: app_commands.Range[int, 1, 10**12]):
        if not await feature.owner_check(interaction):
            return
        if 유저.bot:
            await interaction.response.send_message('봇 계정에는 후원을 기록할 수 없습니다.', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        async with feature.lock:
            config = feature.store.settings(interaction.guild.id)
            if not config['notice_template']:
                await interaction.followup.send('먼저 `/후원기록공지등록`으로 공지 양식을 등록해 주세요.', ephemeral=True)
                return
            try:
                channel = await feature.channel(config['notice_channel'])
                perms = channel.permissions_for(interaction.guild.me)
                if not perms.view_channel or not perms.send_messages:
                    raise ValueError('후원 공지방 권한이 없습니다.')
            except (ValueError, discord.HTTPException):
                await interaction.followup.send('후원 공지방 등록과 봇 권한을 확인해 주세요. 기록은 저장되지 않았습니다.', ephemeral=True)
                return
            if not feature.store.record(interaction.id, 유저.id, 금액, interaction.guild.id, interaction.user.id):
                await interaction.followup.send('이미 처리한 후원 요청입니다. 중복 합산하지 않았습니다.', ephemeral=True)
                return
            problems = []
            try:
                await channel.send(render(config['notice_template'], 유저.id, 금액),
                    allowed_mentions=discord.AllowedMentions(users=[유저], roles=False, everyone=False))
            except discord.HTTPException:
                problems.append('후원 공지 전송 실패')
            for guild_id in feature.store.ranking_guilds():
                try:
                    await feature.update_ranking(guild_id)
                except (ValueError, discord.HTTPException):
                    problems.append(f'랭킹 갱신 실패 (서버 {guild_id})')
            result = f'{유저.mention}님의 {금액}원 후원을 저장했습니다.'
            if problems:
                result += '\n' + '\n'.join(problems[:10]) + '\n기록은 이미 반영되었습니다. 같은 후원을 다시 기록하면 중복 합산됩니다. 랭킹은 `/후원랭킹메시지`로 다시 갱신할 수 있습니다.'
            await interaction.followup.send(result, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @bot.tree.command(name='후원목록', description='봇 전체 누적 후원금액 순으로 20명씩 확인합니다.')
    @app_commands.guild_only()
    async def donation_list(interaction: discord.Interaction):
        view = DonationList(interaction.user.id, feature.store.totals())
        await interaction.response.send_message(embed=view.embed(), view=view, allowed_mentions=discord.AllowedMentions.none())

    return feature

