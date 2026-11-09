"""Evidence-backed acquaintance and scoped identity links; source events stay immutable."""
from contextlib import closing
import json
import re
import sqlite3
from time import time
from uuid import uuid4
from pathlib import Path
from functools import wraps
from threading import RLock, Lock


_LOCKS={}
_LOCKS_GUARD=Lock()

def serialized(method):
    @wraps(method)
    def run(self,*args,**kwargs):
        key=str(Path(self.path).resolve()).casefold()
        with _LOCKS_GUARD:
            lock=_LOCKS.setdefault(key,RLock())
        with lock: return method(self,*args,**kwargs)
    return run


class AcquaintanceStore:
    def __init__(self, path):
        self.path = str(path)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS acquaintance_events(
                    subject TEXT NOT NULL, id TEXT NOT NULL, person TEXT NOT NULL,
                    kind TEXT NOT NULL, payload TEXT NOT NULL, at REAL NOT NULL,
                    PRIMARY KEY(subject,id));
                CREATE TABLE IF NOT EXISTS person_links(
                    id TEXT PRIMARY KEY, subject TEXT NOT NULL, source TEXT NOT NULL,
                    target TEXT NOT NULL, inputs TEXT NOT NULL, basis TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1, at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS person_provenance(
                    subject TEXT NOT NULL, id TEXT NOT NULL, sources TEXT NOT NULL, PRIMARY KEY(subject,id));
                CREATE TABLE IF NOT EXISTS person_link_facts(
                    link TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS person_link_jobs(
                    link TEXT NOT NULL, module TEXT NOT NULL, version INTEGER NOT NULL,
                    status TEXT NOT NULL, error TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(link,module));
            """)

    def connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def record(self, subject, event_id, person, kind, payload, *, at=None):
        if not person or person.startswith(('input:', 'unassigned')):
            return
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO acquaintance_events VALUES(?,?,?,?,?,?)',
                (subject,event_id,person,kind,json.dumps(payload,ensure_ascii=False),time() if at is None else at))

    def reply_delivered(self, subject, person, event_id, text, *, medium):
        if re.search(r'[？?]',text) and re.search(r'怎么称呼|如何称呼|叫什么(?:名字)?|你的名字|您叫什么',text):
            self.record(subject,event_id,person,'name_question_played' if medium=='audio' else 'name_question_sent',
                {'text':text,'medium':medium})

    def observe_text(self, subject, person, event_id, text, *, session, encounter=True):
        if encounter:
            self.record(subject,'encounter:'+session+':'+person,person,'encounter',
                {'text':text,'input_id':event_id,'session':session})
        if re.fullmatch(r'(?:我)?(?:不想说|不告诉你|不方便说|先不说|不想透露)[。！! ]*',text.strip()) and self.context(subject,person)['name_asked']:
            self.record(subject,event_id,person,'name_declined',{'text':text,'input_id':event_id})

    def events(self, subject, person):
        with closing(self.connect()) as db:
            rows = db.execute('SELECT id,kind,payload,at FROM acquaintance_events WHERE subject=? AND person=? ORDER BY at,id',
                (subject,person)).fetchall()
        result=[dict(id=i,kind=k,payload=json.loads(p),at=t) for i,k,p,t in rows]
        for link in self.links(subject):
            if link['target']!=person: continue
            with closing(self.connect()) as db:
                extra=db.execute('SELECT id,kind,payload,at FROM acquaintance_events WHERE subject=? AND person=?',
                    (subject,link['source'])).fetchall()
            for ident,kind,raw,at in extra:
                payload=json.loads(raw)
                if payload.get('input_id') in link['inputs']:
                    result.append(dict(id=ident,kind=kind,payload=payload,at=at))
        return sorted({r['id']:r for r in result}.values(),key=lambda r:(r['at'],r['id']))

    def context(self, subject, person):
        rows = self.events(subject,person)
        names = [r['payload']['name'] for r in rows if r['kind']=='reported_name']
        questions = [r for r in rows if r['kind'] in {'name_question_played','name_question_sent'}]
        visits = {r['payload'].get('session') for r in rows if r['kind']=='encounter'}
        return {'recorded_encounters':len(visits), 'reported_name': names[-1] if names else '',
                'name_asked':bool(questions), 'name_declined':any(r['kind']=='name_declined' for r in rows), 'first_encounter_recorded':bool(visits),
                'basis':'已记录的交往；无记录不证明从未相识'}

    @serialized
    def link(self, subject, source, target, input_ids, *, basis, modules=(), link_id=None):
        ids = tuple(dict.fromkeys(input_ids))
        if not source or not target or source==target or not ids or any(not isinstance(i,str) or not i for i in ids):
            raise ValueError('人物关联需要不同对象及明确输入范围')
        if basis not in {'manual_selection','manual_annotation','verified_voice'}:
            raise ValueError('自报姓名或语义猜测不能合并人物')
        key = link_id or uuid4().hex
        values = (key,subject,source,target,json.dumps(ids),basis)
        with self.connect() as db:
            for prior_target,prior_inputs in db.execute('SELECT target,inputs FROM person_links WHERE subject=? AND source=? AND active=1 AND id!=?',(subject,source,key)):
                if prior_target!=target and set(ids).intersection(json.loads(prior_inputs)):
                    raise ValueError('同一发言已有不同人物关联，请先撤销原关联')
            old = db.execute('SELECT id,subject,source,target,inputs,basis FROM person_links WHERE id=?',(key,)).fetchone()
            if old and old != values:
                raise ValueError('关联ID不能用于另一组证据')
            db.execute('INSERT OR IGNORE INTO person_links VALUES(?,?,?,?,?,?,1,?)',(*values,time()))
            for module in modules:
                db.execute("INSERT OR IGNORE INTO person_link_jobs VALUES(?,?,1,'pending','')",(key,module))
        return key

    def facts(self, link_id, payload=None):
        with self.connect() as db:
            if payload is not None:
                db.execute('INSERT OR IGNORE INTO person_link_facts VALUES(?,?)',
                    (link_id,json.dumps(payload,ensure_ascii=False)))
            row=db.execute('SELECT payload FROM person_link_facts WHERE link=?',(link_id,)).fetchone()
        return json.loads(row[0]) if row else {}

    def provenance(self, subject, ident, sources):
        refs=set(filter(None,sources))
        if not ident or not refs: return
        with self.connect() as db:
            old=db.execute('SELECT sources FROM person_provenance WHERE subject=? AND id=?',(subject,ident)).fetchone()
            if old: refs.update(json.loads(old[0]))
            db.execute('INSERT OR REPLACE INTO person_provenance VALUES(?,?,?)',(subject,ident,json.dumps(sorted(refs))))

    def proven_sources(self, subject, roots):
        """Expand host event IDs only through wholly covered provenance."""
        known=set(roots)
        with closing(self.connect()) as db:
            rows=[(i,refs,'{}') for i,refs in db.execute('SELECT id,sources FROM person_provenance WHERE subject=?',(subject,))]
        while True:
            before=len(known)
            for ident,raw,content in rows:
                refs=set(json.loads(raw))
                data=json.loads(content)
                if data.get('input_id'): refs.add(data['input_id'])
                if refs and refs.issubset(known): known.add(ident)
            if len(known)==before: return known

    def links(self, subject, *, active_only=True):
        with closing(self.connect()) as db:
            db.row_factory=sqlite3.Row
            rows=[dict(r) for r in db.execute('SELECT * FROM person_links WHERE subject=?'+
                (' AND active=1' if active_only else '')+' ORDER BY at,id',(subject,))]
        for row in rows: row['inputs']=json.loads(row['inputs'])
        return rows

    def pending(self, subject):
        with closing(self.connect()) as db:
            return [r[0] for r in db.execute("SELECT DISTINCT l.id FROM person_links l JOIN person_link_jobs j ON j.link=l.id WHERE l.subject=? AND j.status!='done'",(subject,))]

    def project(self, subject, person, sources):
        """Project only when ALL provenance units support the same identity link."""
        evidence=set(sources)
        if not evidence: return person
        targets={r['target'] for r in self.links(subject) if r['source']==person and evidence.issubset(self.proven_sources(subject,r['inputs']))}
        if len(targets)>1: raise ValueError('历史材料存在冲突的人物关联')
        return next(iter(targets),person)

    def projector(self, subject):
        """Same result as project(), reading links once for a whole listing."""
        links=self.links(subject)
        if not links: return lambda person, sources: person
        proven={}
        def project(person, sources):
            evidence=set(sources)
            if not evidence: return person
            targets=set()
            for row in links:
                if row['source']!=person: continue
                if row['id'] not in proven: proven[row['id']]=self.proven_sources(subject,row['inputs'])
                if evidence.issubset(proven[row['id']]): targets.add(row['target'])
            if len(targets)>1: raise ValueError('历史材料存在冲突的人物关联')
            return next(iter(targets),person)
        return project

    def jobs(self, link_id):
        with closing(self.connect()) as db:
            return [dict(module=m,version=v,status=s,error=e) for m,v,s,e in db.execute(
                'SELECT module,version,status,error FROM person_link_jobs WHERE link=?',(link_id,))]

    def resolve(self, subject, person, input_id):
        with closing(self.connect()) as db:
            rows = db.execute('SELECT target,inputs FROM person_links WHERE subject=? AND source=? AND active=1 ORDER BY at DESC',
                (subject,person)).fetchall()
        matches = {target for target,ids in rows if input_id in json.loads(ids)}
        if len(matches)>1:
            raise ValueError('同一输入存在冲突的人物关联')
        return next(iter(matches),person)

    @serialized
    def revoke(self, link_id):
        with self.connect() as db:
            changed=db.execute('UPDATE person_links SET active=0 WHERE id=? AND active=1',(link_id,)).rowcount
            if not changed: return
            db.execute("UPDATE person_link_jobs SET version=version+1,status='pending',error='' WHERE link=?",(link_id,))

    @serialized
    def apply(self, link_id, handlers):
        with closing(self.connect()) as db:
            db.row_factory=sqlite3.Row
            row=dict(db.execute('SELECT * FROM person_links WHERE id=?',(link_id,)).fetchone())
            jobs=[dict(r) for r in db.execute("SELECT * FROM person_link_jobs WHERE link=? AND status!='done'",(link_id,))]
        row['inputs']=json.loads(row['inputs'])
        for job in jobs:
            try:
                handlers[job['module']](row)
                status,error='done',''
            except Exception as exc:
                status,error='failed',str(exc)
            with self.connect() as db:
                db.execute('UPDATE person_link_jobs SET status=?,error=? WHERE link=? AND module=? AND version=?',
                    (status,error,link_id,job['module'],job['version']))


def acquaintance_store(process):
    value=getattr(process,'acquaintance',None)
    if value is None:
        value=AcquaintanceStore(process.repository.path)
        process.acquaintance=value
    return value
