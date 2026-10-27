def test_migration_backup_idempotency_and_ids(tmp_path):
    import sqlite3,json
    from jshi.app.simplified_migration import migrate_database
    p=tmp_path/'old.db'
    with sqlite3.connect(p) as d:
        d.execute('create table events(event_id text primary key,content_raw text,summaries JSON)')
        d.execute('insert into events values(?,?,?)',('事件-聲紋','聲紋已確認',json.dumps({'L1':'請繼續'})))
    spec={'events':('content_raw','summaries')}
    preview=migrate_database(p,spec,tmp_path/'backup')
    assert preview['changed_rows']==1
    result=migrate_database(p,spec,tmp_path/'backup',apply=True)
    assert result['event_ids']==['事件-聲紋']
    assert list((tmp_path/'backup').glob('*.db'))
    assert migrate_database(p,spec,tmp_path/'backup2',apply=True)['changed_rows']==0

def test_scene_payload_fingerprint_tracks_canonical_text_without_new_generation():
    import hashlib,json
    from jshi.app.simplified_migration import canonical_working_json
    payload={'text':'請繼續','person_id':'人物-聲紋'}
    digest=lambda x:hashlib.sha256(json.dumps(x,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    state={'events':[{'id':'e','payload':payload}],'event_fingerprints':{'e':digest(payload)},'generation':2}
    result=canonical_working_json(state)
    assert result['events'][0]['payload']=={'text':'请继续','person_id':'人物-聲紋'}
    assert result['event_fingerprints']['e']==digest(result['events'][0]['payload'])
    assert result['generation']==2
    assert state['event_fingerprints']['e']==digest(payload)
