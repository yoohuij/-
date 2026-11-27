import asyncio
import io

import discord
from discord import app_commands


class TransferResult(str):
    def __new__(cls, text, missing):
        result = super().__new__(cls, text)
        result.missing = missing
        return result


class MissingMembersView(discord.ui.View):
    def __init__(self, user_id, members, title, allowed):
        super().__init__(timeout=900)
        self.user_id, self.members, self.title = user_id, members, title
        self.allowed = allowed
        self.page = 0

    async def interaction_check(self, interaction):
        if interaction.user.id == self.user_id and self.allowed(interaction):
            return True
        await interaction.response.send_message('명령어를 실행한 소유주만 사용할 수 있습니다.', ephemeral=True)
        return False

    def rows(self):
        return self.members[self.page * 20:(self.page + 1) * 20]

    def embed(self):
        pages = max(1, (len(self.members) + 19) // 20)
        self.previous.disabled = self.page == 0
        self.next_page.disabled = self.page + 1 >= pages
        embed = discord.Embed(title=self.title, colour=discord.Colour.blue())
        embed.description = '\n'.join(
            f'{self.page * 20 + index}. <@{uid}> · {discord.utils.escape_markdown(name)[:100]}'
            for index, (uid, name) in enumerate(self.rows(), 1)
        ) or '미입장자가 없습니다.'
        embed.set_footer(text=f'총 {len(self.members)}명 · {self.page + 1}/{pages}페이지 · 조회 시점 기준')
        return embed

    def file(self):
        text = self.title + '\n' + '\n'.join(f'{index}. {name} | <@{uid}> | {uid}'
            for index, (uid, name) in enumerate(self.members, 1))
        return discord.File(io.BytesIO(text.encode('utf-8-sig')), filename='missing-members.txt')

    @discord.ui.button(label='이전', style=discord.ButtonStyle.secondary)
    async def previous(self, interaction, button):
        self.page = max(0, self.page - 1)
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label='다음', style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction, button):
        self.page = min((len(self.members) - 1) // 20, self.page + 1)
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label='현재 페이지 복사', style=discord.ButtonStyle.primary)
    async def copy(self, interaction, button):
        text = '\n'.join(f'<@{uid}> {uid}' for uid, _ in self.rows()) or '미입장자 없음'
        await interaction.response.send_message(f'```\n{text}\n```', ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none())

    @discord.ui.button(label='전체 목록 파일', style=discord.ButtonStyle.secondary)
    async def download(self, interaction, button):
        await interaction.response.send_message(file=self.file(), ephemeral=True)


async def incoming_missing(source, destination):
    source_members = {m.id: m.display_name async for m in source.fetch_members(limit=None) if not m.bot}
    destination_ids = {m.id async for m in destination.fetch_members(limit=None)}
    return [(uid, source_members[uid]) for uid in sorted(source_members.keys() - destination_ids)]


def register(bot, feature):
    def allowed(interaction):
        return interaction.user.id == bot.application_owner_id and not feature.blocked(interaction.user.id)

    @bot.tree.command(name='서버비교', description='·')
    @app_commands.guild_only()
    async def compare(interaction: discord.Interaction, 서버: str):
        await interaction.response.defer(ephemeral=True)
        if not allowed(interaction):
            await interaction.followup.send('봇 소유주만 사용할 수 있습니다.', ephemeral=True)
            return
        source = bot.get_guild(int(서버)) if 서버.isdecimal() and len(서버) <= 20 else None
        if source is None or source.id == interaction.guild_id or source.unavailable:
            await interaction.followup.send('봇이 가입한 다른 서버를 목록에서 선택해 주세요.', ephemeral=True)
            return
        try:
            async with asyncio.timeout(180):
                missing = await incoming_missing(source, interaction.guild)
        except (discord.HTTPException, asyncio.TimeoutError, discord.ClientException):
            await interaction.followup.send('서버원 전체 조회에 실패했습니다. 멤버 조회 권한과 연결을 확인한 뒤 다시 실행해 주세요.', ephemeral=True)
            return
        view = MissingMembersView(interaction.user.id, missing, '현재 서버 미입장자', allowed)
        await interaction.followup.send(content=f'{source.name} → {interaction.guild.name}', embed=view.embed(),
            view=view, file=view.file(), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @compare.autocomplete('서버')
    async def servers(interaction: discord.Interaction, current: str):
        if not allowed(interaction):
            return []
        query = current.strip().casefold()
        return [app_commands.Choice(name=f'{g.name[:75]} ({g.id})'[:100], value=str(g.id))
            for g in sorted(bot.guilds, key=lambda g: (g.name.casefold(), g.id))
            if g.id != interaction.guild_id and not g.unavailable
            and (query in g.name.casefold() or query in str(g.id))][:25]

