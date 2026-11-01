pass
import asyncio
import logging
from contextlib import closing

import discord
from discord import app_commands


def category_memberships(roles, category_ids):
    pass
    groups = {}
    current = None
    for role in reversed(roles):
        if role.is_default():
            continue
        if role.id in category_ids:
            current = role.id
            groups[current] = set()
        elif current is not None:
            groups[current].add(role.id)
    return groups


class RoleCategories:
    table = 'role_categories'

    def __init__(self, bot, database, get_exemptions=None, get_exemption_roles=None):
        self.bot, self.database = bot, database
        self.get_exemptions = get_exemptions or (lambda guild_id: set())
        self.get_exemption_roles = get_exemption_roles or (lambda guild_id: set())
        self.pending = {}
        self.full = set()
        self.workers = {}
        self.locks = {}
        self.special = None

    def connect(self):
        db = self.database()
        db.execute(f'CREATE TABLE IF NOT EXISTS {self.table} (guild_id INTEGER NOT NULL, role_id INTEGER NOT NULL, PRIMARY KEY(guild_id, role_id))')
        db.execute('CREATE TABLE IF NOT EXISTS special_category_end (guild_id INTEGER PRIMARY KEY, role_id INTEGER NOT NULL)')
        db.commit()
        return db

    def ids(self, guild_id):
        with closing(self.connect()) as db:
            return {row[0] for row in db.execute(f'SELECT role_id FROM {self.table} WHERE guild_id=?', (guild_id,))}

    def configure(self, guild_id, role_ids, add):
        with closing(self.connect()) as db, db:
            query = (f'INSERT OR IGNORE INTO {self.table} VALUES (?,?)' if add else
                     f'DELETE FROM {self.table} WHERE guild_id=? AND role_id=?')
            db.executemany(query, [(guild_id, rid) for rid in role_ids])

    def queue(self, guild, member=None):
        if self.special is not None:
            self.special.queue(guild, member)
        if not self.ids(guild.id):
            return
        if member is None:
            self.full.add(guild.id)
        else:
            self.pending.setdefault(guild.id, set()).add(member.id)
        if guild.id not in self.workers:
            self.workers[guild.id] = asyncio.create_task(self.run(guild))

    async def reconcile(self, guild, member):
        
        async with self.locks.setdefault(guild.id, asyncio.Lock()):
            member = guild.get_member(member.id) or member
            me = guild.me
            if me is None or not me.guild_permissions.manage_roles:
                return
            if member.id == guild.owner_id or member.top_role >= me.top_role:
                return
            groups = self.groups(guild)
            held = {r.id for r in member.roles}
            for rid, children in groups.items():
                role = guild.get_role(rid)
                if role is None or role.managed or role >= me.top_role:
                    continue
                exempt = member.id in self.get_exemptions(guild.id) or bool(held & self.get_exemption_roles(guild.id))
                wanted = not exempt and bool(held & children)
                if wanted == (rid in held):
                    continue
                try:
                    if wanted:
                        guard = getattr(self.bot, 'antiraid_feature', None)
                        if guard and not guard.automatic_role_allowed(member.id, role):
                            continue
                        await member.add_roles(role, reason='역할 카테고리 자동 부여')
                    else:
                        await member.remove_roles(role, reason='예외 인원 또는 하위 역할 없음: 카테고리 자동 회수')
                except discord.HTTPException:
                    logging.getLogger(__name__).exception('카테고리 갱신 실패: guild=%s user=%s role=%s', guild.id, member.id, rid)

    def groups(self, guild):
        return category_memberships(guild.roles, self.ids(guild.id))

    async def run(self, guild):
        try:
            while guild.id in self.full or self.pending.get(guild.id):
                if guild.id in self.full:
                    self.full.discard(guild.id)
                    
                    members = list(guild.members) if guild.chunked else [m async for m in guild.fetch_members(limit=None)]
                else:
                    ids = self.pending.pop(guild.id, set())
                    members = [guild.get_member(uid) for uid in ids]
                for member in members:
                    if member is not None:
                        await self.reconcile(guild, member)
                        await asyncio.sleep(0.2)
        except Exception:
            logging.getLogger(__name__).exception('카테고리 전체 갱신 실패: guild=%s. 권한과 Server Members Intent를 확인하세요.', guild.id)
        finally:
            self.workers.pop(guild.id, None)


class SpecialRoleCategories(RoleCategories):
    table = 'special_role_categories'

    def end_id(self, guild_id):
        with closing(self.connect()) as db:
            row = db.execute('SELECT role_id FROM special_category_end WHERE guild_id=?', (guild_id,)).fetchone()
        return row[0] if row else None

    def set_end(self, guild_id, role_id):
        with closing(self.connect()) as db, db:
            db.execute('INSERT OR REPLACE INTO special_category_end VALUES (?,?)', (guild_id, role_id))

    def groups(self, guild):
        end_id = self.end_id(guild.id)
        if end_id is None or guild.get_role(end_id) is None:
            return {}
        ids = self.ids(guild.id)
        groups = {r.id: set() for r in reversed(guild.roles) if r.id in ids}
        current = None
        for role in reversed(guild.roles):
            if role.id == end_id:
                break
            if role.is_default():
                continue
            if role.id in ids:
                current = role.id
            elif current is not None:
                groups[current].add(role.id)
        return groups


def install(bot, database, is_owner, get_exemptions=None, get_exemption_roles=None):
    feature = RoleCategories(bot, database, get_exemptions, get_exemption_roles)
    special = SpecialRoleCategories(bot, database, get_exemptions, get_exemption_roles)
    feature.special = special
    special.locks = feature.locks

    @bot.tree.command(name='역할카테고리설정', description='·')
    @app_commands.guild_only()
    @app_commands.choices(동작=[app_commands.Choice(name='추가', value='add'), app_commands.Choice(name='제거', value='remove')])
    async def configure(interaction: discord.Interaction, 동작: app_commands.Choice[str],
                        역할1: discord.Role, 역할2: discord.Role | None = None,
                        역할3: discord.Role | None = None, 역할4: discord.Role | None = None,
                        역할5: discord.Role | None = None, 역할6: discord.Role | None = None,
                        역할7: discord.Role | None = None, 역할8: discord.Role | None = None,
                        역할9: discord.Role | None = None, 역할10: discord.Role | None = None):
        if not is_owner(interaction):
            await interaction.response.send_message('봇 소유자 또는 서버 소유자만 사용할 수 있습니다.', ephemeral=True)
            return
        guild = interaction.guild
        roles = {r.id: r for r in (역할1, 역할2, 역할3, 역할4, 역할5, 역할6, 역할7, 역할8, 역할9, 역할10) if r is not None}
        add = 동작.value == 'add'
        if add and set(roles) & feature.get_exemption_roles(guild.id):
            await interaction.response.send_message('예외 역할은 자동 카테고리로 등록할 수 없습니다.', ephemeral=True)
            return
        if any(r.guild.id != guild.id or r.is_default() for r in roles.values()):
            await interaction.response.send_message('현재 서버의 @everyone 이외 역할을 선택해 주세요.', ephemeral=True)
            return
        if add and (guild.me is None or not guild.me.guild_permissions.manage_roles
                    or any(r.managed or r >= guild.me.top_role for r in roles.values())):
            await interaction.response.send_message('봇에 역할 관리 권한이 필요합니다. 봇의 최상위 역할보다 아래인 일반 역할만 등록할 수 있습니다.', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        async with feature.locks.setdefault(guild.id, asyncio.Lock()):
            if add and (set(roles) & special.ids(guild.id) or special.end_id(guild.id) in roles):
                await interaction.followup.send('특수 카테고리 또는 끝 표시 역할은 일반 카테고리와 겸용할 수 없습니다.', ephemeral=True)
                return
            feature.configure(guild.id, roles, add)
        feature.queue(guild)
        note = ('등록한 역할의 기존 권한은 그대로이므로, 구분용 역할에 관리자 권한을 넣지 마세요.' if add else
                '제거는 자동 관리 등록만 해제합니다. 이미 가진 해당 역할은 유지되며, 남은 카테고리 구간은 다시 계산됩니다.')
        await interaction.followup.send(
            ('카테고리를 누적 등록했습니다.' if add else '카테고리 등록을 해제했습니다.')
            + '\n전체 인원 갱신을 예약했습니다. 인원수에 따라 시간이 걸립니다.\n' + note,
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @bot.tree.command(name='역할카테고리목록', description='·')
    @app_commands.guild_only()
    async def listing(interaction: discord.Interaction):
        if not is_owner(interaction):
            await interaction.response.send_message('봇 소유자 또는 서버 소유자만 사용할 수 있습니다.', ephemeral=True)
            return
        groups = category_memberships(interaction.guild.roles, feature.ids(interaction.guild.id))
        lines = [f'{i}. <@&{rid}> — 하위 역할 {len(children)}개' for i, (rid, children) in enumerate(groups.items(), 1)]
        await interaction.response.defer(ephemeral=True)
        for start in range(0, max(1, len(lines)), 20):
            await interaction.followup.send('\n'.join(lines[start:start + 20]) or '등록된 카테고리가 없습니다.',
                                            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @bot.tree.command(name='특수카테고리설정', description='·')
    @app_commands.guild_only()
    @app_commands.choices(동작=[app_commands.Choice(name='추가', value='add'), app_commands.Choice(name='제거', value='remove')])
    async def special_configure(interaction: discord.Interaction, 동작: app_commands.Choice[str],
                                역할1: discord.Role, 역할2: discord.Role | None = None,
                                역할3: discord.Role | None = None, 역할4: discord.Role | None = None,
                                역할5: discord.Role | None = None, 역할6: discord.Role | None = None,
                                역할7: discord.Role | None = None, 역할8: discord.Role | None = None,
                                역할9: discord.Role | None = None, 역할10: discord.Role | None = None):
        if not is_owner(interaction):
            await interaction.response.send_message('봇 소유자 또는 서버 소유자만 사용할 수 있습니다.', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        roles = {r.id: r for r in (역할1, 역할2, 역할3, 역할4, 역할5, 역할6, 역할7, 역할8, 역할9, 역할10) if r is not None}
        add = 동작.value == 'add'
        async with special.locks.setdefault(guild.id, asyncio.Lock()):
            if any(r.guild.id != guild.id or r.is_default() for r in roles.values()):
                await interaction.followup.send('현재 서버의 @everyone 이외 역할을 선택해 주세요.', ephemeral=True)
                return
            if add and (set(roles) & (feature.ids(guild.id) | special.get_exemption_roles(guild.id)) or special.end_id(guild.id) in roles):
                await interaction.followup.send('일반 카테고리·예외 역할·끝 표시는 특수 카테고리로 겸용할 수 없습니다.', ephemeral=True)
                return
            if add and (guild.me is None or not guild.me.guild_permissions.manage_roles or any(r.managed or r >= guild.me.top_role for r in roles.values())):
                await interaction.followup.send('역할 관리 권한과 봇보다 아래인 일반 역할을 확인해 주세요.', ephemeral=True)
                return
            end = guild.get_role(special.end_id(guild.id))
            if add and end is not None and any(end >= r for r in roles.values()):
                await interaction.followup.send('특수 카테고리는 끝 표시 역할보다 위에 있어야 합니다.', ephemeral=True)
                return
            special.configure(guild.id, roles, add)
        special.queue(guild)
        note = '끝 표시가 없거나 삭제됐으면 자동 갱신을 보류합니다. /마지막특수카테고리로 지정해 주세요.' if end is None else '특수 카테고리 전체 갱신을 예약했습니다.'
        if not add:
            note += '\n제거는 자동 관리 등록만 해제하며 이미 가진 역할은 유지합니다.'
        await interaction.followup.send(('특수 카테고리를 누적 등록했습니다.' if add else '특수 카테고리 등록을 해제했습니다.') + '\n' + note, ephemeral=True)

    @bot.tree.command(name='마지막특수카테고리', description='·')
    @app_commands.guild_only()
    async def special_end(interaction: discord.Interaction, 역할: discord.Role):
        if not is_owner(interaction):
            await interaction.response.send_message('봇 소유자 또는 서버 소유자만 사용할 수 있습니다.', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        async with special.locks.setdefault(guild.id, asyncio.Lock()):
            ids = special.ids(guild.id)
            if 역할.guild.id != guild.id or 역할.is_default() or 역할.id in ids | feature.ids(guild.id):
                await interaction.followup.send('현재 서버의 카테고리가 아닌 별도 끝 표시 역할을 선택해 주세요.', ephemeral=True)
                return
            if any(역할 >= r for r in guild.roles if r.id in ids):
                await interaction.followup.send('끝 표시는 등록된 모든 특수 카테고리보다 아래에 있어야 합니다.', ephemeral=True)
                return
            special.set_end(guild.id, 역할.id)
        special.queue(guild)
        await interaction.followup.send(f'특수 카테고리 끝 표시: {역할.mention}\n이 역할 자체와 아래 역할은 특수 범위에서 제외합니다. 일반 카테고리 범위는 변경하지 않습니다.', ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @bot.tree.command(name='특수카테고리목록', description='·')
    @app_commands.guild_only()
    async def special_listing(interaction: discord.Interaction):
        if not is_owner(interaction):
            await interaction.response.send_message('봇 소유자 또는 서버 소유자만 사용할 수 있습니다.', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        ids = special.ids(guild.id)
        groups = special.groups(guild)
        end = guild.get_role(special.end_id(guild.id))
        lines = [f'{i}. {r.mention} — 하위 역할 {len(groups.get(r.id, set()))}개' for i, r in enumerate((r for r in reversed(guild.roles) if r.id in ids), 1)]
        footer = f'끝 표시: {end.mention} (범위 제외)' if end else '끝 표시 없음/삭제됨: 자동 갱신 보류'
        for start in range(0, max(1, len(lines)), 20):
            await interaction.followup.send(('\n'.join(lines[start:start + 20]) or '등록된 특수 카테고리가 없습니다.') + '\n\n' + footer, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    return feature
