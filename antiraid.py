"""Audit-log based, opt-in server protection with durable backups and suspensions."""
import asyncio
import json
import logging
import time
from contextlib import closing
import discord
from discord import app_commands

LOG = logging.getLogger(__name__)
WINDOW = 10
DELETE_LIMIT = 3
MEMBER_LIMIT = 5
DANGEROUS = ("administrator", "manage_guild", "manage_roles", "manage_channels",
             "manage_webhooks", "ban_members", "kick_members", "moderate_members")
SUPPORTED = (discord.CategoryChannel, discord.TextChannel, discord.VoiceChannel, discord.StageChannel)

def dangerous(role):
    return any(getattr(role.permissions, name, False) for name in DANGEROUS)

def event_kind(entry):
    if entry.action in (discord.AuditLogAction.channel_delete, discord.AuditLogAction.role_delete):
        return "delete"
    if entry.action == discord.AuditLogAction.member_role_update and getattr(entry.before, "roles", []):
        return "remove_roles"
    return None

class AntiRaid:
    def __init__(self, bot, database, owner_check):
        self.bot, self.database, self.owner_check = bot, database, owner_check
        self.locks, self.actor_locks, self.log_locks = {}, {}, {}
        self.resolving = set()
        with closing(database()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS raid_backups(guild_id INTEGER PRIMARY KEY, saved REAL NOT NULL, data TEXT NOT NULL, maps TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS raid_config(guild_id INTEGER PRIMARY KEY, log_channel INTEGER);
                CREATE TABLE IF NOT EXISTS raid_events(id INTEGER PRIMARY KEY, guild_id INTEGER, actor INTEGER, target INTEGER, kind TEXT, stamp REAL);
                CREATE TABLE IF NOT EXISTS raid_suspects(guild_id INTEGER, user_id INTEGER, roles TEXT NOT NULL, active INTEGER NOT NULL, PRIMARY KEY(guild_id,user_id));
                CREATE TABLE IF NOT EXISTS raid_incidents(id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, actor INTEGER, details TEXT, result TEXT, stamp REAL, restored INTEGER NOT NULL DEFAULT 0);
            """)
            self.suspended = {(g,u) for g,u in db.execute("SELECT guild_id,user_id FROM raid_suspects WHERE active=1")}

    def blocked(self, user_id):
        return any(u == user_id for _,u in self.suspended)

    def automatic_role_allowed(self, user_id, role):
        return not self.blocked(user_id) or (role is not None and not dangerous(role))

    def restore_views(self):
        with closing(self.database()) as db:
            incidents = db.execute("SELECT id FROM raid_incidents").fetchall()
        for (iid,) in incidents:
            self.bot.add_view(RecoveryView(self, iid))

    def lock(self, guild_id):
        return self.locks.setdefault(guild_id, asyncio.Lock())

    def registered(self, user_id):
        with closing(self.database()) as db:
            return db.execute("SELECT 1 FROM global_administrators WHERE user_id=?", (user_id,)).fetchone() is not None

    def manager(self, interaction):
        return (interaction.guild is not None and not self.blocked(interaction.user.id)
                and (self.owner_check(interaction) or self.registered(interaction.user.id)))

    def backup(self, guild_id):
        with closing(self.database()) as db:
            row = db.execute("SELECT saved,data,maps FROM raid_backups WHERE guild_id=?", (guild_id,)).fetchone()
        return (row[0], json.loads(row[1]), json.loads(row[2])) if row else None

    async def log_channel(self, guild):
        async with self.log_locks.setdefault(guild.id, asyncio.Lock()):
            with closing(self.database()) as db:
                row = db.execute("SELECT log_channel FROM raid_config WHERE guild_id=?", (guild.id,)).fetchone()
                admins = [r[0] for r in db.execute("SELECT user_id FROM global_administrators")]
            channel = None
            if row and row[0]:
                try:
                    channel = await self.bot.fetch_channel(row[0])
                except discord.NotFound:
                    pass
            if channel is not None and (not isinstance(channel, discord.TextChannel) or channel.guild.id != guild.id):
                raise ValueError("보안 로그 채널이 올바르지 않습니다.")
            overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False),
                          guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)}
            ids = set(admins) | {guild.owner_id, self.bot.application_owner_id}
            for uid in ids:
                if not uid or uid == guild.me.id:
                    continue
                try:
                    member = guild.get_member(uid) or await guild.fetch_member(uid)
                except discord.NotFound:
                    continue
                overwrites[member] = discord.PermissionOverwrite(view_channel=not self.blocked(uid),
                    send_messages=not self.blocked(uid), read_message_history=not self.blocked(uid))
            if channel is None:
                channel = await guild.create_text_channel("admin", overwrites=overwrites, reason="테러 감지 비공개 기록")
                with closing(self.database()) as db, db:
                    db.execute("INSERT OR REPLACE INTO raid_config VALUES (?,?)", (guild.id,channel.id))
            else:
                await channel.edit(overwrites=overwrites, reason="보안 로그 접근 권한 갱신")
            return channel

    async def report(self, guild, incident_id, actor, details, result):
        embed = discord.Embed(title="테러 의심 감지", colour=discord.Colour.blue(),
                              timestamp=discord.utils.utcnow())
        embed.add_field(name="의심자", value=f"<@{actor}> (ID: {actor})" if actor else "실행자 확인 불가 — 자동 제재하지 않음", inline=False)
        embed.add_field(name="행동", value=details[:1000], inline=False)
        embed.add_field(name="처리 결과", value=result[:1000], inline=False)
        embed.set_footer(text=f"사건 #{incident_id} · 권한 복귀는 소유주의 /해결 · 아래 버튼은 서버 백업 복구")
        try:
            channel = await self.log_channel(guild)
            await channel.send(embed=embed, view=RecoveryView(self, incident_id),
                               allowed_mentions=discord.AllowedMentions.none())
        except (discord.HTTPException, ValueError):
            LOG.exception("보안 로그 전송 실패: 사건 %s", incident_id)
            # Incident remains in DB even if the channel or bot permissions are lost.

    def count_event(self, entry, kind):
        now = time.time()
        stamp = entry.created_at.timestamp()
        if abs(now-stamp) > 60 or not entry.user_id or not getattr(entry.target,"id",None):
            return None
        with closing(self.database()) as db, db:
            cur = db.execute("INSERT OR IGNORE INTO raid_events VALUES (?,?,?,?,?,?)",
                (entry.id,entry.guild.id,entry.user_id,entry.target.id,kind,stamp))
            if not cur.rowcount:
                return None
            # Event timestamps, not arrival spacing; handle reordered gateway delivery.
            rows = db.execute("SELECT target,kind,stamp FROM raid_events WHERE guild_id=? AND actor=? AND stamp BETWEEN ? AND ?",
                              (entry.guild.id,entry.user_id,stamp-WINDOW,stamp+WINDOW)).fetchall()
            db.execute("DELETE FROM raid_events WHERE stamp < ?", (now-120,))
        # Evaluate every window including the current event.
        for end in sorted({r[2] for r in rows if r[2]>=stamp}):
            window = [r for r in rows if end-WINDOW <= r[2] <= end]
            deleted = len({(r[0],r[1]) for r in window if r[1]=="delete"})
            removed = len({r[0] for r in window if r[1]=="remove_roles"})
            if deleted >= DELETE_LIMIT or removed >= MEMBER_LIMIT:
                return f"10초 이내 채널·역할 삭제 {deleted}개 / 역할 제거 대상 {removed}명"
        return None

    async def quarantine(self, member):
        key = (member.guild.id,member.id)
        roles = [r for r in member.roles if not r.is_default() and dangerous(r)]
        with closing(self.database()) as db, db:
            row = db.execute("SELECT roles FROM raid_suspects WHERE guild_id=? AND user_id=?",key).fetchone()
            saved = set(json.loads(row[0])) if row else set()
            saved.update(r.id for r in roles)
            db.execute("INSERT OR REPLACE INTO raid_suspects VALUES (?,?,?,1)", (*key,json.dumps(sorted(saved))))
        self.suspended.add(key)  # Durable suspension before any Discord mutations.
        failures = []
        for role in roles:
            if role.managed or role >= member.guild.me.top_role:
                failures.append(str(role.id))
                continue
            try:
                await member.remove_roles(role, reason="테러 의심: 관리 권한 임시 정지")
            except discord.HTTPException:
                failures.append(str(role.id))
        result = "의심자로 분류. 봇 관리자 기능 정지, 관리 권한 역할 회수. 밴하지 않았습니다."
        if failures or dangerous(member.guild.default_role):
            result += "\n회수 불가 권한이 남을 수 있습니다. 봇보다 높은 역할·자동 역할·기본 역할을 소유주가 확인하세요. 실패 역할: " + ", ".join(failures)
        return result

    async def enforce(self, member):
        if (member.guild.id,member.id) not in self.suspended or member.id in self.resolving:
            return
        if any(dangerous(r) for r in member.roles if not r.is_default()):
            async with self.actor_locks.setdefault((member.guild.id,member.id),asyncio.Lock()):
                if (member.guild.id,member.id) in self.suspended and member.id not in self.resolving:
                    try:
                        fresh = await member.guild.fetch_member(member.id)
                        await self.quarantine(fresh)
                    except discord.NotFound:
                        pass

    async def role_changed(self, role):
        if dangerous(role):
            for guild_id,user_id in list(self.suspended):
                if guild_id == role.guild.id:
                    try:
                        await self.enforce(await role.guild.fetch_member(user_id))
                    except discord.NotFound:
                        pass

    async def audit(self, entry):
        kind = event_kind(entry)
        if not kind or self.backup(entry.guild.id) is None:
            return
        # Only this bot's own operations are exempt, not arbitrary bot accounts.
        if entry.user_id == getattr(self.bot.user,"id",None):
            return
        async with self.actor_locks.setdefault((entry.guild.id,entry.user_id),asyncio.Lock()):
            details = self.count_event(entry,kind)
            if not details:
                return
            with closing(self.database()) as db:
                if db.execute("SELECT 1 FROM raid_incidents WHERE guild_id=? AND actor=? AND stamp>?",
                              (entry.guild.id,entry.user_id,time.time()-30)).fetchone():
                    return
            actor = entry.user_id
            with closing(self.database()) as db, db:
                incident_id = db.execute("INSERT INTO raid_incidents(guild_id,actor,details,result,stamp) VALUES (?,?,?,?,?)",
                    (entry.guild.id,actor,details,"처리 시작",time.time())).lastrowid
            try:
                if actor in (entry.guild.owner_id,self.bot.application_owner_id):
                    result = "소유주 행동 감지 — 자동 밴·권한 회수 제외. 직접 확인이 필요합니다."
                else:
                    try:
                        member = await entry.guild.fetch_member(actor)
                    except discord.NotFound:
                        member = None
                    if self.registered(actor) or self.blocked(actor):
                        if member is None:
                            with closing(self.database()) as db, db:
                                db.execute("INSERT INTO raid_suspects VALUES (?,?,?,1) ON CONFLICT(guild_id,user_id) DO UPDATE SET active=1",(entry.guild.id,actor,"[]"))
                            self.suspended.add((entry.guild.id,actor))
                            result = "등록 관리자: 서버에 없어 역할 회수 불가. 봇 권한은 정지했고 밴하지 않았습니다."
                        else:
                            result = await self.quarantine(member)
                    else:
                        removed, failed = 0,0
                        if member:
                            for role in member.roles:
                                if role.is_default() or not role.permissions.administrator:
                                    continue
                                if role.managed or role >= entry.guild.me.top_role:
                                    failed += 1
                                    continue
                                try:
                                    await member.remove_roles(role, reason="테러 감지: 관리자 역할 회수")
                                    removed += 1
                                except discord.HTTPException:
                                    failed += 1
                        try:
                            await entry.guild.ban(discord.Object(actor), delete_message_seconds=0, reason=f"테러 감지 사건 #{incident_id}: {details}")
                            result = f"밴 완료. 관리자 역할 회수 {removed}개 / 회수 실패 {failed}개."
                        except discord.HTTPException:
                            result = f"밴 실패: 봇 권한·역할 순위를 확인하세요. 역할 회수 {removed}개 / 실패 {failed}개."
            except Exception as exc:
                LOG.exception("보안 제재 실패")
                result = f"자동 처리 일부 또는 전체 실패 ({type(exc).__name__}). 소유주 확인 필요."
            with closing(self.database()) as db, db:
                db.execute("UPDATE raid_incidents SET result=? WHERE id=?", (result,incident_id))
            await self.report(entry.guild,incident_id,actor,details,result)

    async def save_backup(self, guild):
        if any(g==guild.id for g,u in self.suspended):
            raise ValueError("미해결 의심자가 있어 기존 백업을 덮어쓸 수 없습니다.")
        roles = await guild.fetch_roles()
        channels = await guild.fetch_channels()
        members = [m async for m in guild.fetch_members(limit=None)]
        data = {"name":guild.name,"roles":[],"channels":[],"members":{},"unsupported":[]}
        for r in roles:
            data["roles"].append(dict(id=r.id,name=r.name,permissions=r.permissions.value,
                colour=r.colour.value,hoist=r.hoist,mentionable=r.mentionable,position=r.position,managed=r.managed))
        for c in channels:
            if not isinstance(c,SUPPORTED) or (isinstance(c,discord.TextChannel) and c.is_news()):
                data["unsupported"].append(c.id)
                continue
            ow = [[target.id,isinstance(target,discord.Role),*map(lambda p:p.value,overwrite.pair())]
                  for target,overwrite in c.overwrites.items()]
            item = dict(id=c.id,name=c.name,type=c.type.value,category=c.category_id,position=c.position,overwrites=ow)
            if isinstance(c,discord.TextChannel):
                item.update(topic=c.topic,nsfw=c.nsfw,slowmode_delay=c.slowmode_delay,
                            default_auto_archive_duration=c.default_auto_archive_duration,
                            default_thread_slowmode_delay=c.default_thread_slowmode_delay)
            if isinstance(c,(discord.VoiceChannel,discord.StageChannel)):
                item.update(bitrate=c.bitrate,user_limit=c.user_limit,rtc_region=c.rtc_region)
            data["channels"].append(item)
        for member in members:
            data["members"][str(member.id)] = [r.id for r in member.roles if not r.is_default() and not r.managed]
        with closing(self.database()) as db, db:
            db.execute("INSERT OR REPLACE INTO raid_backups VALUES (?,?,?,?)",(guild.id,time.time(),json.dumps(data),json.dumps({"roles":{},"channels":{}})))
        return f"역할 {len(roles)}개, 채널 {len(data['channels'])}개, 멤버 {len(members)}명 저장. 포럼·미디어·공지 채널 등 복구 미지원 {len(data['unsupported'])}개."

    def save_maps(self,guild_id,maps):
        with closing(self.database()) as db, db:
            db.execute("UPDATE raid_backups SET maps=? WHERE guild_id=?", (json.dumps(maps),guild_id))

    async def restore(self,guild):
        backup = self.backup(guild.id)
        if not backup:
            raise ValueError("먼저 소유주가 /저장으로 백업해야 합니다.")
        _,data,maps=backup
        errors=[]
        try:
            await guild.edit(name=data["name"],reason="저장된 서버 이름 복구")
        except discord.HTTPException:
            errors.append("서버 이름 복구 실패")
        current_roles={r.id:r for r in await guild.fetch_roles()}
        held_suspect_roles=set()
        for g,uid in self.suspended:
            if g==guild.id:
                try:
                    suspect=await guild.fetch_member(uid)
                    held_suspect_roles.update(r.id for r in suspect.roles)
                except discord.NotFound:
                    pass
        for r in sorted(data["roles"],key=lambda r:r["position"]):
            if r["managed"]:
                continue
            rid=maps["roles"].get(str(r["id"]),r["id"])
            role=current_roles.get(rid)
            if rid in held_suspect_roles and any(getattr(discord.Permissions(r["permissions"]),p) for p in DANGEROUS):
                errors.append(f"의심자 보유 역할 {r['name']} 복구 보류")
                continue
            if role and not role.is_default() and role >= guild.me.top_role:
                errors.append(f"상위 역할 {r['name']} 건너뜀")
                continue
            try:
                kwargs=dict(permissions=discord.Permissions(r["permissions"]),reason="저장된 서버 역할 복구")
                if r["id"]!=guild.id:
                    kwargs.update(name=r["name"],colour=discord.Colour(r["colour"]),hoist=r["hoist"],mentionable=r["mentionable"])
                if role:
                    role=await role.edit(**kwargs)
                else:
                    role=await guild.create_role(**kwargs)
                    maps["roles"][str(r["id"])]=role.id
                    self.save_maps(guild.id,maps)
                current_roles[role.id]=role
            except discord.HTTPException:
                errors.append(f"역할 {r['name']} 복구 실패")
        positions={current_roles[maps["roles"].get(str(r["id"]),r["id"])]:r["position"] for r in data["roles"]
                   if not r["managed"] and r["id"]!=guild.id
                   and maps["roles"].get(str(r["id"]),r["id"]) in current_roles
                   and current_roles[maps["roles"].get(str(r["id"]),r["id"])] < guild.me.top_role}
        try:
            if positions: await guild.edit_role_positions(positions=positions,reason="백업 역할 순서 복구")
        except discord.HTTPException:
            errors.append("일부 역할 위치 복구 실패")
        current_channels={c.id:c for c in await guild.fetch_channels()}
        for c in sorted(data["channels"],key=lambda c:(c["type"]!=4,c["position"])):
            cid=maps["channels"].get(str(c["id"]),c["id"])
            channel=current_channels.get(cid)
            category_id=maps["channels"].get(str(c["category"]),c["category"])
            category=current_channels.get(category_id)
            if c["category"] and category is None:
                errors.append(f"상위 카테고리 누락: {c['name']}")
                continue
            overwrites={}
            for uid,isrole,allow,deny in c["overwrites"]:
                target=current_roles.get(maps["roles"].get(str(uid),uid)) if isrole else guild.get_member(uid)
                if target is None and not isrole:
                    try: target=await guild.fetch_member(uid)
                    except discord.NotFound: continue
                if target is not None:
                    overwrites[target]=discord.PermissionOverwrite.from_pair(discord.Permissions(allow),discord.Permissions(deny))
            kwargs=dict(name=c["name"],overwrites=overwrites,reason="저장된 서버 채널 복구")
            if c["type"]!=4: kwargs["category"]=category
            for key in ("topic","nsfw","slowmode_delay","default_auto_archive_duration","default_thread_slowmode_delay","bitrate","user_limit","rtc_region"):
                if key in c: kwargs[key]=c[key]
            if "bitrate" in kwargs: kwargs["bitrate"]=min(kwargs["bitrate"],guild.bitrate_limit)
            try:
                if channel:
                    channel=await channel.edit(**kwargs)
                else:
                    creator={4:guild.create_category,0:guild.create_text_channel,2:guild.create_voice_channel,13:guild.create_stage_channel}[c["type"]]
                    channel=await creator(**kwargs)
                    maps["channels"][str(c["id"])]=channel.id
                    self.save_maps(guild.id,maps)
                current_channels[channel.id]=channel
                await channel.edit(position=c["position"],reason="백업 채널 위치 복구")
            except discord.HTTPException:
                errors.append(f"채널 {c['name']} 복구 실패")
        # Add missing roles; never kick users or remove roles added after the backup.
        for uid,role_ids in data["members"].items():
            if self.blocked(int(uid)):
                continue  # /복구 must never undo a suspect suspension.
            try:
                member=await guild.fetch_member(int(uid))
                desired=[current_roles[maps["roles"].get(str(rid),rid)] for rid in role_ids
                         if maps["roles"].get(str(rid),rid) in current_roles]
                desired=[r for r in desired if r not in member.roles and not r.managed and r < guild.me.top_role]
                if desired: await member.add_roles(*desired,reason="백업 멤버 역할 복구")
            except discord.NotFound:
                continue  # Restore does not undo bans or invite users back.
            except discord.HTTPException:
                errors.append(f"멤버 {uid} 역할 복구 실패")
        self.remap_settings(guild.id,maps)
        await self.log_channel(guild) # Never leave admin logs public after restore.
        return "백업 복구 처리 완료. 밴·의심자 정지는 유지됩니다.\n" + ("주의: "+" / ".join(errors[:15]) if errors else "지원되는 채널·역할·멤버 역할 복구 완료.") + f"\n미지원 채널 {len(data['unsupported'])}개. 메시지·스레드·서버 자체는 복구하지 않습니다."

    def remap_settings(self,guild_id,maps):
        # Only known guild-local ID columns; never rewrite user IDs or global records.
        channel_columns={"meeting_settings":["meeting_channel_id","log_channel_id","notice_channel_id","reason_channel_id"],
                         "meeting_chat":["channel_id"],"raid_config":["log_channel"]}
        role_columns={"warning_settings":["role_id"],"nickname_excluded_roles":["role_id"],
                      "donation_supporter_role":["role_id"],"donation_tiers":["role_id"],
                      "role_categories":["role_id"],
                      "donation_rank_roles":["first_role","second_role","third_role"],
                      "donation_role_assignments":["role_id"],"donation_tier_assignments":["role_id"],
                      "donation_supporter_assignments":["role_id"]}
        channel_columns.update({"donation_settings":["notice_channel","ranking_channel"],
                                "lawless_settings":["channel_id"],"voice_activity":["channel_id"]})
        with closing(self.database()) as db,db:
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for mapping,configs in ((maps["channels"],channel_columns),(maps["roles"],role_columns)):
                for table,columns in configs.items():
                    if table not in tables: continue
                    actual={r[1] for r in db.execute(f"PRAGMA table_info({table})")}
                    if "guild_id" not in actual: continue
                    for column in columns:
                        if column not in actual: continue
                        for old,new in mapping.items():
                            db.execute(f"UPDATE OR IGNORE {table} SET {column}=? WHERE guild_id=? AND {column}=?",(new,guild_id,int(old)))
            if "meeting_deputies" in tables:
                for old,new in maps["roles"].items():
                    db.execute("UPDATE OR IGNORE meeting_deputies SET target_id=? WHERE guild_id=? AND kind='role' AND target_id=?", (new,guild_id,int(old)))

    async def resolve(self,guild,user_id):
        key=(guild.id,user_id)
        with closing(self.database()) as db:
            row=db.execute("SELECT roles FROM raid_suspects WHERE guild_id=? AND user_id=? AND active=1",key).fetchone()
        if not row: raise ValueError("이 서버의 미해결 의심자가 아닙니다.")
        try: member=await guild.fetch_member(user_id)
        except discord.NotFound: raise ValueError("유저가 서버에 없습니다. 재가입 후 해결해 주세요.")
        backup=self.backup(guild.id)
        mapping=backup[2]["roles"] if backup else {}
        roles=[]
        for rid in json.loads(row[0]):
            role=guild.get_role(mapping.get(str(rid),rid))
            if role is None or role.managed or role >= guild.me.top_role:
                raise ValueError("복원할 역할이 없거나 봇보다 높습니다. 역할을 확인한 뒤 다시 해결해 주세요.")
            roles.append(role)
        self.resolving.add(user_id)
        try:
            if roles: await member.add_roles(*roles,reason="소유주 승인: 의심자 관리 권한 복귀")
            with closing(self.database()) as db,db:
                db.execute("UPDATE raid_suspects SET active=0 WHERE guild_id=? AND user_id=?",key)
            self.suspended.discard(key)
        except discord.HTTPException:
            # A multi-role request may have partially succeeded: retain suspension.
            try:
                await self.quarantine(await guild.fetch_member(user_id))
            except discord.HTTPException:
                LOG.exception("권한 복귀 실패 후 재회수 실패")
            raise
        finally:
            self.resolving.discard(user_id)
        await self.log_channel(guild)
        return "의심자 정지를 해제하고 저장된 역할을 복원했습니다. 기존 봇 관리자 등록을 다시 사용할 수 있습니다."

class RecoveryView(discord.ui.View):
    def __init__(self,feature,incident_id):
        super().__init__(timeout=None)
        self.feature,self.incident_id=feature,incident_id
        button=discord.ui.Button(label="잘못한 행동인가요?",style=discord.ButtonStyle.primary,custom_id=f"raid_restore:{incident_id}")
        button.callback=self.recover
        self.add_item(button)

    async def recover(self,interaction):
        await interaction.response.defer(ephemeral=True)
        if not self.feature.manager(interaction):
            await interaction.followup.send("정지되지 않은 등록 관리자·소유주만 복구할 수 있습니다.",ephemeral=True)
            return
        with closing(self.feature.database()) as db:
            row=db.execute("SELECT guild_id FROM raid_incidents WHERE id=?",(self.incident_id,)).fetchone()
        if not row or row[0]!=interaction.guild.id:
            await interaction.followup.send("현재 서버의 사건이 아닙니다.",ephemeral=True)
            return
        await interaction.followup.send("서버 설정을 마지막 백업으로 복구할까요? 밴 해제·의심자 권한 복귀는 하지 않습니다.",
            view=ConfirmRestore(self.feature,interaction.user.id,self.incident_id),ephemeral=True)

class ConfirmRestore(discord.ui.View):
    def __init__(self,feature,user_id,incident_id=None):
        super().__init__(timeout=120)
        self.feature,self.user_id,self.incident_id=feature,user_id,incident_id
    @discord.ui.button(label="백업 복구 확인",style=discord.ButtonStyle.danger)
    async def confirm(self,interaction,button):
        await interaction.response.defer(ephemeral=True)
        if interaction.user.id!=self.user_id or not self.feature.manager(interaction):
            await interaction.followup.send("권한이 없습니다.",ephemeral=True); return
        lock=self.feature.lock(interaction.guild.id)
        if lock.locked():
            await interaction.followup.send("저장·복구 처리 중입니다.",ephemeral=True); return
        async with lock:
            try:
                async with asyncio.timeout(720):
                    result=await self.feature.restore(interaction.guild)
            except (ValueError,discord.HTTPException,asyncio.TimeoutError) as exc:
                LOG.exception("백업 복구 실패")
                result=f"복구가 일부만 반영됐을 수 있습니다 ({type(exc).__name__}). 권한 확인 후 /복구로 재시도하세요."
            button.disabled=True
            await interaction.edit_original_response(view=self)
            await interaction.followup.send(result,ephemeral=True,allowed_mentions=discord.AllowedMentions.none())

def install(bot,database,owner_check):
    feature=AntiRaid(bot,database,owner_check)
    bot.antiraid_feature=feature
    previous=bot.tree.interaction_check
    async def command_check(interaction):
        if feature.blocked(interaction.user.id):
            await interaction.response.send_message("테러 의심으로 봇 권한이 임시 정지되었습니다. 소유주에게 /해결을 요청하세요.",ephemeral=True)
            return False
        return await previous(interaction)
    bot.tree.interaction_check=command_check

    @bot.event
    async def on_audit_log_entry_create(entry):
        try: await feature.audit(entry)
        except Exception: LOG.exception("테러 감지 처리 실패")

    @bot.tree.command(name="저장",description="소유주 전용 서버 백업 및 테러 감지 활성화")
    @app_commands.guild_only()
    async def save(interaction:discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not owner_check(interaction):
            await interaction.followup.send("소유주만 저장할 수 있습니다.",ephemeral=True); return
        lock=feature.lock(interaction.guild.id)
        if lock.locked():
            await interaction.followup.send("저장·복구 처리 중입니다.",ephemeral=True); return
        async with lock:
            try:
                perms=interaction.guild.me.guild_permissions
                if not all((perms.view_audit_log,perms.manage_roles,perms.manage_channels,perms.ban_members)):
                    raise ValueError("봇에 감사 로그 보기·역할 관리·채널 관리·멤버 차단 권한이 필요합니다.")
                async with asyncio.timeout(720):
                    channel=await feature.log_channel(interaction.guild)
                    result=await feature.save_backup(interaction.guild)
                result+=f"\n감지 활성화: 10초 내 삭제 3개 또는 역할 제거 5명. 기록방: {channel.mention}"
            except (ValueError,discord.HTTPException,asyncio.TimeoutError) as exc:
                result=f"저장 실패: {exc}"
            await interaction.followup.send(result[:1900],ephemeral=True,allowed_mentions=discord.AllowedMentions.none())

    @bot.tree.command(name="복구",description="저장된 채널·역할·멤버 역할 복구")
    @app_commands.guild_only()
    async def restore(interaction:discord.Interaction):
        if not feature.manager(interaction):
            await interaction.response.send_message("등록 관리자·소유주만 사용할 수 있습니다.",ephemeral=True); return
        await interaction.response.send_message("마지막 백업의 채널·역할 설정으로 복구합니다. 밴 해제나 의심자 권한 복귀는 하지 않습니다.",
            view=ConfirmRestore(feature,interaction.user.id),ephemeral=True)

    @bot.tree.command(name="해결",description="소유주 전용 의심자 정지 해제 및 관리 권한 복귀")
    @app_commands.guild_only()
    async def resolve(interaction:discord.Interaction,유저:discord.User):
        await interaction.response.defer(ephemeral=True)
        if not owner_check(interaction):
            await interaction.followup.send("소유주만 의심자의 권한을 복원할 수 있습니다.",ephemeral=True); return
        async with feature.actor_locks.setdefault((interaction.guild.id,유저.id),asyncio.Lock()):
            try:
                result=await feature.resolve(interaction.guild,유저.id)
            except (ValueError,discord.HTTPException) as exc:
                result=f"해결 실패: {exc}"
        await interaction.followup.send(result,ephemeral=True,allowed_mentions=discord.AllowedMentions.none())
    return feature
