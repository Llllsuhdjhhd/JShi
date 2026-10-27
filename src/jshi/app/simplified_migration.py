"""Back up and canonicalize owned working prose; resumable index work is explicit."""
from datetime import datetime
import hashlib,json,sqlite3
from pathlib import Path
from jshi.simplified import simplify_data

def canonical_working_json(data):
    canonical=simplify_data(data,prose=True)
    if isinstance(data,dict) and isinstance(data.get('event_fingerprints'),dict):
        pairs=list(zip(data.get('events',[]),canonical.get('events',[])))
        for old,new in zip(data.get('revisions',[]),canonical.get('revisions',[])):
            pairs.extend(zip(old.get('events',[]),new.get('events',[])))
        for old,new in pairs:
            if 'payload' not in old:continue
            old_hash=hashlib.sha256(json.dumps(old['payload'],ensure_ascii=False,sort_keys=True).encode()).hexdigest()
            if data['event_fingerprints'].get(old.get('id'))==old_hash:
                canonical['event_fingerprints'][old['id']]=hashlib.sha256(json.dumps(new['payload'],ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    return canonical

def migrate_database(path, policies, backup_root, *, apply=False):
    path=Path(path)
    if not path.exists():return {'path':str(path),'changed_rows':0,'event_ids':[]}
    db=sqlite3.connect(path,timeout=30);db.row_factory=sqlite3.Row
    changes=[];event_ids=set()
    for table,fields in policies.items():
        columns={r[1]:r for r in db.execute(f'PRAGMA table_info("{table}")')}
        fields=[f for f in fields if f in columns]
        if not fields:continue
        primary=[r[1] for r in columns.values() if r[5]]
        if not primary:continue
        for row in db.execute(f'SELECT * FROM "{table}"'):
            changed={};original={}
            for name in fields:
                raw=row[name]
                if raw is None:continue
                is_json=columns[name][2].upper()=='JSON' or name in {'aliases','levels','content','data','items','payload','refinement_object_levels'}
                value=raw
                if is_json:
                    try:value=json.loads(raw)
                    except (ValueError,TypeError):is_json=False
                new=simplify_data(value,prose=True)
                if new!=value:
                    original[name]=raw;changed[name]=json.dumps(new,ensure_ascii=False) if is_json else new
            if changed:
                key={f:row[f] for f in primary};changes.append((table,key,changed,original))
                if 'event_id' in row.keys():event_ids.add(row['event_id'])
    if apply and changes:
        backup=Path(backup_root);backup.mkdir(parents=True,exist_ok=True)
        dest=backup/(path.parent.name+'-'+path.name)
        with sqlite3.connect(dest) as target:db.backup(target)
        with db:
            db.execute('CREATE TABLE IF NOT EXISTS simplified_migration_originals(id TEXT PRIMARY KEY,table_name TEXT,record_key TEXT,original_json TEXT,created_at TEXT)')
            for table,key,changed,original in changes:
                # Optimistic row check: do not overwrite changes made by a live writer.
                where=' AND '.join(f'"{k}"=?' for k in key)
                current=db.execute(f'SELECT * FROM "{table}" WHERE '+where,list(key.values())).fetchone()
                if any(current[k]!=old for k,old in original.items()):raise RuntimeError('Working text changed during migration; retry')
                encoded=json.dumps(original,ensure_ascii=False)
                digest=hashlib.sha256((table+json.dumps(key,sort_keys=True)+encoded).encode()).hexdigest()
                db.execute('INSERT OR IGNORE INTO simplified_migration_originals VALUES(?,?,?,?,?)',
                    (digest,table,json.dumps(key,ensure_ascii=False),encoded,datetime.now().isoformat()))
                db.execute(f'UPDATE "{table}" SET '+','.join(f'"{k}"=?' for k in changed)+' WHERE '+where,
                    [*changed.values(),*key.values()])
        (backup/'migration-manifest.json').write_text(json.dumps({'database':str(path),'changed_rows':len(changes),'event_ids':sorted(event_ids)},ensure_ascii=False,indent=2),encoding='utf-8')
    db.close()
    return {'path':str(path),'changed_rows':len(changes),'event_ids':sorted(event_ids),'applied':apply}

def run(data_dir,*,apply=False):
    from jshi.memory.rems3 import load_rems
    load_rems()
    from rems.utils.simplified_storage import PROSE_COLUMNS
    root=Path(data_dir);backup=root/'backups'/('simplified-'+datetime.now().strftime('%Y%m%d-%H%M%S'))
    memory=migrate_database(root/'rems/rems.db',PROSE_COLUMNS,backup/'memory',apply=apply)
    subject=migrate_database(root/'subject.sqlite3',{
        'object_profiles':('label','aliases'), 'personal_items':('content',),
        'value_entries':('content',), 'value_catalog':('title','description'),
    },backup/'subject',apply=apply)
    cache=migrate_database(root/'reflection_memory.sqlite3',{
        'recall_working_items':('payload','query'),
    },backup/'cache',apply=apply)
    files=[]
    working=[root/name for name in ('zone.json','pending_scene.json','identities.json','style_pack.json')]
    working.extend((root/'jev_scenes').rglob('*.json'))
    working.append(root/'rems/jshi_person_views.json')
    working.extend((root/'rems/jshi_person_views.views').rglob('*.json'))
    for path in working:
        if not path.is_file():continue
        original=path.read_text(encoding='utf-8');data=json.loads(original)
        canonical=canonical_working_json(data)
        if canonical==data:continue
        files.append(str(path))
        if apply:
            dest=backup/'json'/path.relative_to(root);dest.parent.mkdir(parents=True,exist_ok=True)
            dest.write_text(original,encoding='utf-8')
            if path.read_text(encoding='utf-8')!=original:raise RuntimeError('Working JSON changed during migration; retry')
            temp=path.with_suffix('.simplified.tmp');temp.write_text(json.dumps(canonical,ensure_ascii=False,indent=2),encoding='utf-8');temp.replace(path)
    if apply and memory['event_ids']:
        pending=root/'rems/simplified-reindex.json'
        old=json.loads(pending.read_text(encoding='utf-8')) if pending.exists() else []
        temp=pending.with_suffix('.tmp');temp.write_text(json.dumps(sorted(set(old)|set(memory['event_ids']))),encoding='utf-8');temp.replace(pending)
    return {'memory':memory,'subject':subject,'cache':cache,'json_files':files,'backup_root':str(backup)}

if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--data-dir',default='.jshi');parser.add_argument('--apply',action='store_true')
    args=parser.parse_args();print(json.dumps(run(args.data_dir,apply=args.apply),ensure_ascii=False,indent=2))
