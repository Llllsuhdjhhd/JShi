"""Read-only, allowlisted data browser for the local web console."""
from __future__ import annotations

import asyncio
import json
import sqlite3
from collections import deque
from pathlib import Path


SUBJECT_TABLES = {'histories': '输入输出与事件', 'activities': '主流程活动',
    'cognitive_contents': '认知内容', 'personal_items': '个人世界',
    'object_profiles': '人物档案', 'transitions': '状态变更', 'experience_ledger': '经历账本'}
MEMORY_TABLES = {'events': '长期记忆', 'object_portraits': '人物肖像',
    'portrait_summaries': '肖像摘要', 'object_traits': '人物特征',
    'object_states': '人物状态', 'object_dispositions': '人物倾向',
    'object_memory_entries': '人物记忆', 'person_experience_portraits': '人物经历肖像',
    'recall_traces': '回忆召回 · 后端候选', 'unclosed_events': '待整理记忆 · 未闭合事件'}
FILES = {'tool_work': ('tool_work.jsonl', '工具', '工件 / 工作事项'),
    'tool': ('tool.jsonl', '工具', '工具任务与结果'),
    'tool_metrics': ('tool_metrics.json', '工具', '工具运行指标'),
    'prompts': ('step_inputs.jsonl', '调试', '实际提示词与模型调用'),
    'timing': ('activity_timings.jsonl', '调试', '主流程耗时'),
    'write_timing': ('write_timings.jsonl', '调试', '写场耗时'),
    'recall': ('recall_traces.jsonl', '记忆', '回忆召回 · 主流程装入'),
    'zone': ('zone.json', '片场', '主认知片场'),
    'pending': ('pending_scene.json', '片场', '待写片场'),
    'effectiveness': ('effectiveness_reports.jsonl', '调试', '效果评估'),
    'tuning': ('tuning_suggestions.jsonl', '调试', '调优建议')}


def unpack(value):
    if isinstance(value, str) and value[:1] in '{[':
        try:
            return json.loads(value)
        except ValueError:
            pass
    return value


class WebData:
    def __init__(self, root, subject_id, subject_path=None):
        self.root = Path(root)
        self.subject_id = subject_id
        self.subject_path = Path(subject_path) if subject_path else self.root/'subject.sqlite3'

    def catalog(self):
        result = []
        for db, tables, group, relative in [('subject', SUBJECT_TABLES, '主流程', 'subject.sqlite3'),
                ('memory', MEMORY_TABLES, '记忆', 'rems/rems.db')]:
            path = self.subject_path if db == 'subject' else self.root / relative
            available = set()
            if path.is_file():
                with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as conn:
                    available = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table, label in tables.items():
                result.append({'id': f'{db}:{table}', 'group': group, 'label': label,
                               'available': table in available, 'path': relative})
        result.extend({'id': key, 'group': group, 'label': label, 'available': (self.root/path).is_file(),
                       'path': path} for key, (path, group, label) in FILES.items())
        result.append({'id': 'jev_scenes', 'group': '片场', 'label': 'JEV 会话片场',
                       'available': (self.root/'jev_scenes').is_dir(), 'path': 'jev_scenes/'})
        result.append({'id':'memory_placement','group':'记忆','label':'输入落位 · 人物与来源',
                       'available':any(r['id']=='subject:histories' and r['available'] for r in result),
                       'path': '主体数据库 / histories（输入与人物落位）'})
        return result

    def diagnostics(self, *, section='turns', activity_id='', part='all', purpose='all'):
        timings = [r for r in self.read('timing', size=50)['rows']
                   if isinstance(r, dict) and r.get('activity_id') and r.get('subject_id', self.subject_id) == self.subject_id]
        if section == 'turns':
            return {'turns': [{'activity_id': r['activity_id'], 'label': r.get('started_at', '')}
                              for r in timings]}
        snapshot = next((r for r in timings if r['activity_id'] == activity_id), None) if activity_id else next(iter(timings), None)
        if snapshot is None and activity_id:
            snapshot = next((r for r in self.read('timing', query=activity_id, size=50)['rows']
                             if r.get('activity_id') == activity_id), None)
        activity_id = activity_id or (snapshot or {}).get('activity_id', '')
        sections = []
        if not activity_id:
            value = '还没有已保存的轮次记录。'
        elif section == 'timing':
            value = json.dumps(snapshot, ensure_ascii=False, indent=2) if snapshot else '本轮没有已保存的耗时记录。'
        elif section == 'prompt':
            import re
            from jshi.app.talk_session import _extract_prompt_section, _extract_json_schema, _PROMPT_SECTION_ALIASES
            ids = {activity_id}
            ids.update(c['prompt_activity_id'] for c in (snapshot or {}).get('voice', {}).get('jev_calls', []) if c.get('prompt_activity_id'))
            calls = []
            for linked in ids:
                calls.extend(r for r in self.read('prompts', query=linked, size=50)['rows']
                             if r.get('kind') == 'call' and r.get('activity_id') == linked
                             and r.get('subject_id', self.subject_id) == self.subject_id)
            rendered = []
            for r in calls:
                if purpose != 'all' and r.get('purpose') != purpose:
                    continue
                system, user = r.get('system_text', ''), r.get('user_text', '')
                sections.extend(re.findall(r'^【([^\n】]+)】\s*$', system+'\n'+user, re.M))
                if part == 'all': body = 'system\n'+system+'\nuser\n'+user
                elif part == 'system': body = system
                elif part == 'user': body = user
                elif part == 'schema': body = _extract_json_schema(system) or '没有找到 JSON Schema。'
                else:
                    name = _PROMPT_SECTION_ALIASES.get(part, part)
                    body = _extract_prompt_section(system, name) or _extract_prompt_section(user, name) or '没有找到区块：'+part
                rendered.append(f"[{r.get('purpose')} · {r.get('model')}]\n{body}")
            value = '\n\n'.join(rendered) or '本轮没有该调用的已保存提示词；后台JEV记录可在“保存数据 → 实际提示词”中查看。'
        elif section in {'tool','input','scene','identity','delivery','jev','jev_scene','response'}:
            source = {'tool':'tool','input':'subject:histories','scene':'zone','identity':'subject:histories',
                      'delivery':'subject:histories','jev':'timing','jev_scene':'jev_scenes','response':'subject:cognitive_contents'}[section]
            data = self.read(source, query='' if section in {'scene','jev_scene'} else activity_id, size=50)
            value = ('当前保存的片场（不是所选历史轮次的快照）：\n' if section in {'scene','jev_scene'} else '已保存记录（当前语音连接的完整快照需连接后查看）：\n')
            value += json.dumps(data['rows'],ensure_ascii=False,indent=2) if data['rows'] else '未找到对应保存记录。'
        else:
            raise ValueError('未知调试项目')
        return {'type':'debug','section':section,'activity_id':activity_id,'text':value,'sections':list(dict.fromkeys(sections))}

    def read(self, source, *, query='', page=0, size=20):
        if source not in {r['id'] for r in self.catalog()}:
            raise ValueError('未知数据分类')
        page, size = max(0, int(page)), min(50, max(1, int(size)))
        query = str(query).strip()[:200]
        placement = source == 'memory_placement'
        if placement:
            source = 'subject:histories'
        if ':' in source:
            db, table = source.split(':')
            relative = 'subject.sqlite3' if db == 'subject' else 'rems/rems.db'
            path = self.subject_path if db == 'subject' else self.root / relative
            if not path.is_file():
                return {'rows': [], 'total': 0, 'page': page, 'size': size}
            with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=3) as conn:
                conn.row_factory = sqlite3.Row
                columns = [r['name'] for r in conn.execute(f'PRAGMA table_info("{table}")')]
                if not columns:
                    return {'rows': [], 'total': 0, 'page': page, 'size': size}
                conditions, args = [], []
                if placement:
                    if 'event_type' not in columns:
                        return {'rows':[], 'total':0, 'page':page, 'size':size}
                    conditions.append("event_type IN ('external_input','object_resolved','object_rejected')")
                if 'subject_id' in columns:
                    conditions.append('subject_id = ?')
                    args.append(self.subject_id)
                if query:
                    conditions.append('(' + ' OR '.join(f'CAST("{col}" AS TEXT) LIKE ? ESCAPE char(92)' for col in columns) + ')')
                    escaped = query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
                    args.extend(['%' + escaped + '%'] * len(columns))
                where = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
                total = conn.execute(f'SELECT count(*) FROM "{table}"{where}', args).fetchone()[0]
                order = next((k for k in ('sequence', 'created_at', 'create_time', 'updated_at', 'id', 'event_id') if k in columns), columns[0])
                rows = [dict(r) for r in conn.execute(f'SELECT * FROM "{table}"{where} ORDER BY "{order}" DESC LIMIT ? OFFSET ?',
                                                    [*args, size, page*size])]
                rows = [{k: unpack(v) for k, v in row.items()} for row in rows]
            return {'rows': rows, 'total': total, 'page': page, 'size': size}
        rows, limited = [], False
        if source == 'jev_scenes':
            paths = sorted((self.root/'jev_scenes').glob('*/*.json'), key=lambda p:p.stat().st_mtime, reverse=True)
            for path in paths[:200]:
                rows.append({'file': path.relative_to(self.root).as_posix(), 'data': json.loads(path.read_text(encoding='utf-8'))})
            limited = len(paths) > 200
        else:
            path = self.root / FILES[source][0]
            if path.is_file():
                if path.suffix == '.jsonl':
                    # Bounded result memory; retain latest records, never rewrite logs.
                    tail = deque(maxlen=2000)
                    count = 0
                    with path.open(encoding='utf-8') as handle:
                        for line in handle:
                            try:
                                tail.append(json.loads(line))
                                count += 1
                            except ValueError:
                                continue  # A writer may still be appending its final line.
                    rows = list(reversed(tail))
                    limited = count > 2000
                    if source == 'prompts':
                        systems = {r.get('hash'): r.get('text') for r in rows if isinstance(r, dict) and r.get('kind') == 'prompt'}
                        rows = [{**r, 'system_text': systems.get(r.get('system_hash'), '系统正文不在当前日志窗口中')}
                                if isinstance(r, dict) and r.get('kind') == 'call' else r for r in rows]
                else:
                    data = json.loads(path.read_text(encoding='utf-8'))
                    rows = data if isinstance(data, list) else [{'key': k, 'value': v} for k,v in data.items()] if isinstance(data, dict) else [data]
        if query:
            rows = [r for r in rows if query.casefold() in json.dumps(r, ensure_ascii=False).casefold()]
        return {'rows': rows[page*size:(page+1)*size], 'total': len(rows), 'page': page, 'size': size,
                'limited': limited, 'note': '日志检索范围为最近2000条；JEV片场最多200份。' if limited else ''}


def register_data_routes(app, process, subject_id):
    from aiohttp import web
    browser = WebData(process.repository.path.parent, subject_id, process.repository.path)

    async def page(request):
        return web.FileResponse(Path(__file__).with_name('data_ui.html'))

    async def data(request):
        if request.headers.get('Sec-Fetch-Site') == 'cross-site':
            raise web.HTTPForbidden(text='数据查看需要同源请求')
        try:
            if not request.query.get('source'):
                value = await asyncio.to_thread(browser.catalog)
                return web.json_response({'sources': value}, headers={'Cache-Control': 'no-store'})
            value = await asyncio.to_thread(browser.read, request.query['source'],
                query=request.query.get('q',''), page=int(request.query.get('page',0)))
            return web.json_response(value, headers={'Cache-Control': 'no-store'}, dumps=lambda x: json.dumps(x, ensure_ascii=False, default=str))
        except (ValueError, sqlite3.Error, OSError) as exc:
            raise web.HTTPBadRequest(text=str(exc))

    async def diagnostics(request):
        if request.headers.get('Sec-Fetch-Site') == 'cross-site':
            raise web.HTTPForbidden(text='调试查看需要同源请求')
        try:
            value = await asyncio.to_thread(browser.diagnostics, section=request.query.get('section','turns'),
                activity_id=request.query.get('activity_id',''), part=request.query.get('part','all'),
                purpose=request.query.get('purpose','all'))
            return web.json_response(value, headers={'Cache-Control':'no-store'})
        except (ValueError, sqlite3.Error, OSError) as exc:
            raise web.HTTPBadRequest(text=str(exc))

    app.router.add_get('/data', page)
    app.router.add_get('/api/data', data)
    app.router.add_get('/api/debug', diagnostics)
