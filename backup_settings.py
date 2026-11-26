from contextlib import closing
import re
import json
import base64


CONFIG = {
    'meeting_settings': {'meeting_channel_id': 'channels', 'log_channel_id': 'channels', 'notice_channel_id': 'channels', 'reason_channel_id': 'channels'},
    'meeting_chat': {'channel_id': 'channels'},
    'warning_settings': {'role_id': 'roles'},
    'role_exemptions': {'role_id': 'roles'},
    'nickname_auto': {},
    'nickname_excluded_roles': {'role_id': 'roles'},
    'nickname_history': {},
    'nickname_prefixes': {},
    'role_categories': {'role_id': 'roles'},
    'special_role_categories': {'role_id': 'roles'},
    'special_category_end': {'role_id': 'roles'},
    'donation_settings': {'notice_channel': 'channels', 'ranking_channel': 'channels'},
    'donation_rank_roles': {'first_role': 'roles', 'second_role': 'roles', 'third_role': 'roles'},
    'donation_tiers': {'role_id': 'roles'},
    'donation_supporter_role': {'role_id': 'roles'},
    'lawless_settings': {'channel_id': 'channels'},
    'voice_activity': {'channel_id': 'channels'},
    'meeting_deputies': {},
    'raid_config': {'log_channel': 'channels'},
    'approved_absences': {},
}

SHARED = {'global_administrators', 'global_exemptions', 'donation_records',
          'donation_adjustments', 'donation_corrections'}


def archived_value(value):
    if isinstance(value, bytes):
        return {'base64': base64.b64encode(value).decode('ascii')}
    return value


def snapshot(database, guild_id):
    result = {}
    with closing(database()) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in CONFIG:
            if table not in tables:
                continue
            cursor = db.execute(f'SELECT * FROM {table} WHERE guild_id=?', (guild_id,))
            columns = [c[0] for c in cursor.description]
            result[table] = [dict(zip(columns, row)) for row in cursor]
        records = {}
        for table in sorted(tables):
            if table.startswith('sqlite_') or table in {'raid_backups', 'server_transfer_archives'}:
                continue
            quoted = '"' + table.replace('"', '""') + '"'
            columns = [r[1] for r in db.execute(f'PRAGMA table_info({quoted})')]
            scoped = 'guild_id' in columns and table not in SHARED
            cursor = db.execute(f'SELECT * FROM {quoted}' + (' WHERE guild_id=?' if scoped else ''),
                                (guild_id,) if scoped else ())
            records[table] = {'columns': columns, 'rows': [[archived_value(v) for v in row] for row in cursor],
                              'scope': 'server' if scoped else 'shared'}
        result['__database__'] = {'source_guild_id': guild_id, 'tables': records}
    return result


def automatic_roles(settings):
    result = set()
    for table in ('role_categories', 'special_role_categories', 'donation_tiers', 'donation_supporter_role'):
        result.update(row['role_id'] for row in settings.get(table, []))
    for row in settings.get('donation_rank_roles', []):
        result.update(row.get(key) for key in ('first_role', 'second_role', 'third_role'))
    result.discard(None)
    return result


def name_maps(data, roles, channels):
    def unique(items, key):
        grouped = {}
        for item in items:
            grouped.setdefault(key(item), []).append(item)
        return grouped
    source_roles = unique(data['roles'], lambda r: r['name'])
    target_roles = unique(roles, lambda r: r.name)
    role_map = {}
    for name, saved in source_roles.items():
        targets = target_roles.get(name, [])
        if len(saved) == len(targets) == 1 and not targets[0].managed and not targets[0].is_default():
            role_map[str(saved[0]['id'])] = targets[0].id
    source_by_id = {c['id']: c for c in data['channels']}
    def source_key(c):
        parent = source_by_id.get(c.get('category'))
        return c['type'], c['name'], parent['name'] if parent else None
    source_channels = unique(data['channels'], source_key)
    target_channels = unique(channels, lambda c: (c.type.value, c.name, c.category.name if c.category else None))
    channel_map = {}
    for key, saved in source_channels.items():
        targets = target_channels.get(key, [])
        if len(saved) == len(targets) == 1:
            channel_map[str(saved[0]['id'])] = targets[0].id
    return {'roles': role_map, 'channels': channel_map}


def apply(database, guild_id, settings, maps, *, moving=False):
    problems, applied = [], 0
    with closing(database()) as db, db:
        archive = settings.get('__database__')
        if archive:
            db.execute('CREATE TABLE IF NOT EXISTS server_transfer_archives (guild_id INTEGER NOT NULL, source_guild_id INTEGER NOT NULL, data TEXT NOT NULL, PRIMARY KEY(guild_id,source_guild_id))')
            db.execute('INSERT OR REPLACE INTO server_transfer_archives VALUES (?,?,?)',
                       (guild_id, archive['source_guild_id'], json.dumps(archive, ensure_ascii=False)))
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, source in settings.items():
            if table not in CONFIG or table not in tables:
                continue
            columns = {r[1] for r in db.execute(f'PRAGMA table_info({table})')}
            prepared, valid = [], True
            for saved in source:
                row = {k: v for k, v in saved.items() if k in columns}
                row['guild_id'] = guild_id
                references = dict(CONFIG[table])
                if table == 'meeting_deputies' and row.get('kind') == 'role':
                    references['target_id'] = 'roles'
                for column, kind in references.items():
                    old = row.get(column)
                    if old is not None:
                        new = maps[kind].get(str(old))
                        if new is None:
                            problems.append(f'{table}: {column} 연결 불가')
                            valid = False
                        row[column] = new
                for key in ('notice_template', 'ranking_template', 'notice', 'target_template'):
                    if row.get(key):
                        row[key] = re.sub(r'<(@&|#)(\d+)>', lambda match:
                            f"<{match[1]}{maps['roles' if match[1] == '@&' else 'channels'].get(match[2], match[2])}>", row[key])
                if table == 'donation_settings':
                    if moving or row.get('ranking_channel') != saved.get('ranking_channel'):
                        existing = db.execute('SELECT ranking_message FROM donation_settings WHERE guild_id=? AND ranking_channel=?',
                                              (guild_id, row.get('ranking_channel'))).fetchone()
                        row['ranking_message'] = existing[0] if existing else None
                if table == 'lawless_settings':
                    if moving or row.get('channel_id') != saved.get('channel_id'):
                        existing = db.execute('SELECT notice_id FROM lawless_settings WHERE guild_id=? AND channel_id=?',
                                              (guild_id, row.get('channel_id'))).fetchone()
                        row.update(notice_id=existing[0] if existing else None, is_open=0)
                prepared.append(row)
            if not valid:
                continue
            db.execute(f'DELETE FROM {table} WHERE guild_id=?', (guild_id,))
            for row in prepared:
                names = list(row)
                db.execute(f"INSERT INTO {table} ({','.join(names)}) VALUES ({','.join('?' for _ in names)})", list(row.values()))
            applied += 1
    archived = len(settings.get('__database__', {}).get('tables', {}))
    return f'설정·명단 {applied}종 적용 · 원본 DB 기록 {archived}종 보관' + (' · 확인 필요: ' + ' / '.join(dict.fromkeys(problems))[:650] if problems else '')

