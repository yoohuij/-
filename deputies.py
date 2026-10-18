"""Server-scoped limited meeting reviewers, independent of administrators."""
from contextlib import closing
import discord
from discord import app_commands


class Deputies:
    def __init__(self, database):
        self.database = database

    def connect(self):
        db = self.database()
        db.execute('CREATE TABLE IF NOT EXISTS meeting_deputies (guild_id INTEGER NOT NULL, kind TEXT NOT NULL, target_id INTEGER NOT NULL, PRIMARY KEY(guild_id, kind, target_id))')
        db.commit()
        return db

    def entries(self, guild_id):
        with closing(self.connect()) as db:
            return db.execute('SELECT kind, target_id FROM meeting_deputies WHERE guild_id=? ORDER BY kind, target_id', (guild_id,)).fetchall()

    def set(self, guild_id, kind, target_id, adding):
        with closing(self.connect()) as db, db:
            if adding:
                db.execute('INSERT OR IGNORE INTO meeting_deputies VALUES (?,?,?)', (guild_id, kind, target_id))
            else:
                db.execute('DELETE FROM meeting_deputies WHERE guild_id=? AND kind=? AND target_id=?', (guild_id, kind, target_id))

    def allows(self, interaction):
        if interaction.guild is None:
            return False
        roles = {role.id for role in getattr(interaction.user, 'roles', ())}
        return any((kind == 'user' and target_id == interaction.user.id) or
                   (kind == 'role' and target_id in roles)
                   for kind, target_id in self.entries(interaction.guild.id))


def install(bot, database, administrator_check=None):
    feature = Deputies(database)
    group = app_commands.Group(name='업자', description='회의 조회 전용 업자 권한을 관리합니다.')

    async def manager_only(interaction):
        if interaction.guild is not None and (
            interaction.user.id == bot.application_owner_id or
            (administrator_check is not None and administrator_check(interaction.guild.id, interaction.user.id))
        ):
            return True
        await interaction.response.send_message('봇 소유자 또는 봇에 등록된 관리자만 업자 권한을 관리할 수 있습니다.', ephemeral=True)
        return False

    async def change(interaction, role, user, adding):
        if not await manager_only(interaction):
            return
        if (role is None) == (user is None):
            await interaction.response.send_message('역할 또는 유저 중 하나만 지정해 주세요.', ephemeral=True)
            return
        if role is not None and role.is_default():
            await interaction.response.send_message('@everyone은 업자로 지정할 수 없습니다.', ephemeral=True)
            return
        if user is not None and user.bot:
            await interaction.response.send_message('봇 계정은 업자로 지정할 수 없습니다.', ephemeral=True)
            return
        target = role if role is not None else user
        feature.set(interaction.guild.id, 'role' if role is not None else 'user', target.id, adding)
        action = '등록' if adding else '해제'
        await interaction.response.send_message(f'{target.mention} 업자 권한을 {action}했습니다. 현재 서버에만 적용됩니다.',
                                                ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @group.command(name='부여', description='역할 또는 유저 하나를 업자로 등록합니다.')
    @app_commands.guild_only()
    async def grant(interaction: discord.Interaction, 역할: discord.Role | None = None, 유저: discord.Member | None = None):
        await change(interaction, 역할, 유저, True)

    @group.command(name='해제', description='등록한 업자 역할 또는 유저를 해제합니다.')
    @app_commands.guild_only()
    async def revoke(interaction: discord.Interaction, 역할: discord.Role | None = None, 유저: discord.Member | None = None):
        await change(interaction, 역할, 유저, False)

    @group.command(name='목록', description='현재 서버의 업자 등록 목록을 확인합니다.')
    @app_commands.guild_only()
    async def listing(interaction: discord.Interaction):
        if not await manager_only(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        rows = feature.entries(interaction.guild.id)
        for offset in range(0, max(1, len(rows)), 20):
            lines = [f'{offset+i+1}. ' + (f'역할 <@&{uid}>' if kind == 'role' else f'유저 <@{uid}>')
                     for i, (kind, uid) in enumerate(rows[offset:offset+20])]
            await interaction.followup.send(embed=discord.Embed(title='업자 목록 (현재 서버)',
                description='\n'.join(lines) or '등록된 업자가 없습니다.', colour=discord.Colour.blurple()),
                ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    bot.tree.add_command(group)
    return feature
