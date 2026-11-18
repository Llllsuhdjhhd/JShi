import json
import os
from pathlib import Path

import pytest

from jshi.app.cli import _parser
from jshi.experienceledger import ConsumerKind, SqliteExperienceLedger
from jshi.memory.modules import select_module


@pytest.fixture(autouse=True)
def restore_memory_environment():
    def ours(k):
        return k.startswith(('JSHI_MEMORY_','JSHI_REMS_','JSHI_INPROCESS_','REMS_'))
    saved={k:v for k,v in os.environ.items() if ours(k)}
    yield
    for k in list(os.environ):
        if ours(k):
            os.environ.pop(k)
    os.environ.update(saved)


def registry(tmp_path):
    for name in ('dual-source','old-source'):
        path=tmp_path/name/'src'/'rems'
        path.mkdir(parents=True)
        (path/'port.py').write_text('',encoding='utf-8')
    value={'default':'dual','modules':{
        'dual':{'source_root':'dual-source','data_dir':'dual-data','aliases':['long-mem0'],
                'bootstrap_cursor':'current','settings':{'REMS_RECENT_MEMORY__ENABLED':True}},
        'legacy':{'source_root':'old-source','data_dir':'old-data','bootstrap_cursor':{'stone':2},
                  'settings':{'REMS_MEMORY_REFINEMENT__ENABLED':True}},
        'inprocess':{'backend':'inprocess','data_dir':'simple-data'}}}
    path=tmp_path/'memory-modules.json'
    path.write_text(json.dumps(value),encoding='utf-8')
    return value,path


def test_cli_and_profiles_choose_code_storage_and_isolate_policy(tmp_path):
    registry(tmp_path)
    args=_parser().parse_args(['--memory-module','legacy','vision','stone'])
    assert args.memory_module=='legacy'
    assert _parser().parse_args(['vision','stone','--memory-module','legacy']).memory_module=='legacy'
    first=select_module('long-mem0',tmp_path)
    first_namespace=os.environ['JSHI_MEMORY_CURSOR_NAMESPACE']
    assert first['name']=='dual'
    assert os.environ['REMS_RECENT_MEMORY__ENABLED']=='true'
    second=select_module('legacy',tmp_path)
    assert second['data_dir']!=first['data_dir']
    assert os.environ['JSHI_REMS_SOURCE_ROOT']==str(tmp_path/'old-source')
    assert 'REMS_RECENT_MEMORY__ENABLED' not in os.environ
    assert os.environ['JSHI_MEMORY_CURSOR_NAMESPACE']!=first_namespace
    select_module('inprocess',tmp_path)
    assert os.environ['JSHI_MEMORY_BACKEND']=='inprocess'
    assert 'JSHI_REMS_SOURCE_ROOT' not in os.environ
    assert os.environ['JSHI_INPROCESS_MEMORY_DATA_DIR']==str(tmp_path/'simple-data')


def test_unknown_module_fails_before_environment_changes(tmp_path):
    registry(tmp_path)
    before=dict(os.environ)
    with pytest.raises(ValueError,match='记忆模块'):
        select_module('typo',tmp_path)
    assert dict(os.environ)==before


def test_profiles_cannot_share_one_store(tmp_path):
    value,path=registry(tmp_path)
    value['modules']['legacy']['data_dir']='dual-data'
    path.write_text(json.dumps(value),encoding='utf-8')
    with pytest.raises(ValueError,match='共用'):
        select_module('legacy',tmp_path)


def test_each_module_resumes_its_own_cursor_after_switching(tmp_path):
    path=tmp_path/'ledger.sqlite3'
    original=SqliteExperienceLedger(path)
    for i in range(5):
        original.append_external('stone',actor_object_id='p',text_raw=str(i))
    original.advance_consumer_cursor('stone',ConsumerKind.MEMORY,3)
    original.advance_consumer_cursor('stone',ConsumerKind.ACTIVE_ZONE,5)
    original.advance_consumer_cursor('stone',ConsumerKind.AUDIT,5)
    seeds={'dual':None,'legacy':{'stone':2}}
    dual=SqliteExperienceLedger(path,memory_namespace='dual',memory_initial_cursors=seeds)
    assert dual.consumer_cursor('stone',ConsumerKind.MEMORY)==3
    dual.advance_consumer_cursor('stone',ConsumerKind.MEMORY,5)
    assert dual.safe_trim_point('stone')==2
    old=SqliteExperienceLedger(path,memory_namespace='legacy',memory_initial_cursors=seeds)
    assert old.consumer_cursor('stone',ConsumerKind.MEMORY)==2
    assert [s.sequence for s in old.list_experiences('stone',after_sequence=2)]==[3,4,5]
    old.advance_consumer_cursor('stone',ConsumerKind.MEMORY,4)
    again=SqliteExperienceLedger(path,memory_namespace='dual',memory_initial_cursors=seeds)
    assert again.consumer_cursor('stone',ConsumerKind.MEMORY)==5
    assert again.safe_trim_point('stone')==4


def test_inprocess_module_uses_its_own_database(tmp_path):
    from jshi.memory.factory import build_memory_backend
    from jshi.subject import SubjectRepository
    registry(tmp_path)
    select_module('inprocess',tmp_path)
    backend=build_memory_backend(SubjectRepository(tmp_path/'subject.sqlite3'),tmp_path)
    assert backend._repository.path == tmp_path/'simple-data'/'memory.sqlite3'


def test_different_modules_cannot_run_against_one_shared_ledger(tmp_path):
    import subprocess
    import sys
    from jshi.memory.modules import acquire_module_runtime

    registry(tmp_path)
    select_module('dual',tmp_path)
    acquire_module_runtime(tmp_path)
    code = """
import sys
from pathlib import Path
from jshi.memory.modules import select_module, acquire_module_runtime
from jshi.memory.factory import MemoryStorageInUseError
path=Path(sys.argv[1])
select_module('legacy',path)
try:
    acquire_module_runtime(path)
except MemoryStorageInUseError:
    sys.exit(0)
sys.exit(1)
"""
    result=subprocess.run([sys.executable,'-c',code,str(tmp_path)],
                          capture_output=True,text=True,timeout=15)
    assert result.returncode == 0, result.stderr
