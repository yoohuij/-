"""Server-scoped category roles, bounded by the next category in role order."""
import asyncio
import logging
from contextlib import closing

import discord
from discord import app_commands


def category_memberships(roles, category_ids):
    """guild.roles is Discord's authoritative low-to-high hierarchy order."""
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
    def __init__(self, bot, database):
        self.bot, self.database = bot, database
        self.pending = {}
        self.full = set()
        self.workers = {}
        self.locks = {}

    def connect(self):
        db = self.database()
        db.execute('CREATE TABLE IF NOT EXISTS role_categories (guild_id INTEGER NOT NULL, role_id INTEGER NOT NULL, PRIMARY KEY(guild_id, role_id))')
        db.commit()
        return db

    def ids(self, guild_id):
        with closing(self.connect()) as db:
            return {row[0] for row in db.execute('SELECT role_id FROM role_categories WHERE guild_id=?', (guild_id,))}

    def configure(self, guild_id, role_ids, add):
        with closing(self.connect()) as db, db:
            query = ('INSERT OR IGNORE INTO role_categories VALUES (?,?)' if add else
                     'DELETE FROM role_categories WHERE guild_id=? AND role_id=?')
            db.executemany(query, [(guild_id, rid) for rid in role_ids])

    def queue(self, guild, member=None):
        if not self.ids(guild.id):
            return
        if member is None:
            self.full.add(guild.id)
        else:
            self.pending.setdefault(guild.id, set()).add(member.id)
        if guild.id not in self.workers:
            self.workers[guild.id] = asyncio.create_task(self.run(guild))

    async def reconcile(self, guild, member):
        # Re-read configuration and cached membership after taking the lock.
        async with self.locks.setdefault(guild.id, asyncio.Lock()):
            member = guild.get_member(member.id) or member
            me = guild.me
            if me is None or not me.guild_permissions.manage_roles:
                return
            if member.id == guild.owner_id or member.top_role >= me.top_role:
                return
            groups = category_memberships(guild.roles, self.ids(guild.id))
            held = {r.id for r in member.roles}
            for rid, children in groups.items():
                role = guild.get_role(rid)
                if role is None or role.managed or role >= me.top_role:
                    continue
                wanted = bool(held & children)
                if wanted == (rid in held):
                    continue
                try:
                    if wanted:
                        await member.add_roles(role, reason='역할 카테고리 자동 부여')
                    else:
                        await member.remove_roles(role, reason='하위 역할 없음: 카테고리 자동 회수')
                except discord.HTTPException:
                    logging.getLogger(__name__).exception('카테고리 갱신 실패: guild=%s user=%s role=%s', guild.id, member.id, rid)

    async def run(self, guild):
        try:
            while guild.id in self.full or self.pending.get(guild.id):
                if guild.id in self.full:
                    self.full.discard(guild.id)
                    # REST enumeration avoids silently missing uncached members.
                    members = [m async for m in guild.fetch_members(limit=None)]
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


def install(bot, database, is_owner):
    feature = RoleCategories(bot, database)

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
        if any(r.guild.id != guild.id or r.is_default() for r in roles.values()):
            await interaction.response.send_message('현재 서버의 @everyone 이외 역할을 선택해 주세요.', ephemeral=True)
            return
        if add and (guild.me is None or not guild.me.guild_permissions.manage_roles
                    or any(r.managed or r >= guild.me.top_role for r in roles.values())):
            await interaction.response.send_message('봇에 역할 관리 권한이 필요합니다. 봇의 최상위 역할보다 아래인 일반 역할만 등록할 수 있습니다.', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        async with feature.locks.setdefault(guild.id, asyncio.Lock()):
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

    return feature
