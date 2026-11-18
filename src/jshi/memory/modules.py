"""Named memory modules: code, independent storage and ingestion progress."""
from __future__ import annotations

import json
import os
import atexit
import hashlib
from pathlib import Path

_runtime_leases = {}


def acquire_module_runtime(data_dir: Path):
    """A shared JShi ledger has one writer runtime even across different modules."""
    if not os.getenv('JSHI_MEMORY_MODULE'):
        return
    import portalocker
    from .factory import MemoryStorageInUseError
    path=(Path(data_dir)/'.memory-module.lock').resolve()
    if path in _runtime_leases:
        return
    path.parent.mkdir(parents=True,exist_ok=True)
    handle=path.open('a+b')
    try:
        portalocker.lock(handle,portalocker.LOCK_EX|portalocker.LOCK_NB)
    except portalocker.exceptions.LockException:
        handle.close()
        raise MemoryStorageInUseError('同一份 JShi 经历台账已有运行实例，请正常退出后再切换记忆模块。') from None
    _runtime_leases[path]=handle
    def release():
        portalocker.unlock(handle)
        handle.close()
        _runtime_leases.pop(path,None)
    atexit.register(release)


def read_modules(data_dir: Path, path: Path | None = None) -> dict:
    path = path or Path(data_dir)/'memory-modules.json'
    if not path.is_file():
        return {'default': None, 'modules': {}}
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value.get('modules'), dict):
        raise ValueError('memory-modules.json needs a modules mapping')
    value['_base'] = str(path.resolve().parent)
    return value


def select_module(requested: str | None, data_dir: Path, path: Path | None = None) -> dict | None:
    registry=read_modules(data_dir,path)
    name=requested or os.getenv('JSHI_MEMORY_MODULE') or registry.get('default')
    if not name:
        return None
    entries=registry['modules']
    matched=[key for key,row in entries.items() if name.casefold() in
             {key.casefold(),*(str(alias).casefold() for alias in row.get('aliases',[]))}]
    if len(matched)!=1:
        raise ValueError(f'未知或重复的记忆模块 {name!r}；可选：'+', '.join(entries))
    key=matched[0]
    row=entries[key]
    backend=row.get('backend','rems3')
    if backend not in ('rems3','inprocess'):
        raise ValueError(f'记忆模块 {key} 使用了尚未注册的适配器：{backend}')
    base=Path(registry['_base'])
    source=(base/row['source_root']).resolve() if row.get('source_root') else None
    storage=(base/row['data_dir']).resolve()
    if backend=='rems3' and (source is None or not (source/'src'/'rems'/'port.py').is_file()):
        raise ValueError(f'记忆模块 {key} 缺少 REMS 接口：{source}')
    # Reject accidental sharing between named modules; aliases belong to one row.
    for other,entry in entries.items():
        other_store=(base/entry['data_dir']).resolve()
        if other!=key and (other_store==storage or other_store in storage.parents or storage in other_store.parents):
            raise ValueError(f'记忆模块 {key} 与 {other} 共用了数据目录')
    settings=row.get('settings',{})
    all_keys={setting for entry in entries.values() for setting in entry.get('settings',{})}
    if any(not setting.startswith('REMS_') for setting in all_keys):
        raise ValueError('模块 settings 只接受 REMS_ 配置；JShi 通用凭据保留在 .env')
    def cursor_key(module,entry):
        identity=[entry.get('backend','rems3'),os.path.normcase(str((base/entry['data_dir']).resolve())),
                  os.path.normcase(str((base/entry['source_root']).resolve())) if entry.get('source_root') else None]
        return module+':'+hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:16]
    seeds={cursor_key(module,entry):(None if entry['bootstrap_cursor']=='current' else entry['bootstrap_cursor'])
           for module,entry in entries.items() if 'bootstrap_cursor' in entry}
    for seed in seeds.values():
        values=seed.values() if isinstance(seed,dict) else [seed]
        if any(v is not None and (type(v) is not int or v<0) for v in values):
            raise ValueError('bootstrap_cursor 必须是 current、非负整数或主体到序号的映射')
    for setting in all_keys | {'JSHI_REMS_SOURCE_ROOT','JSHI_REMS_DATA_DIR',
                              'JSHI_INPROCESS_MEMORY_DATA_DIR','REMS_RECENT_MEMORY__PATH'}:
        os.environ.pop(setting,None)
    for setting,value in settings.items():
        os.environ[setting]=str(value).lower() if isinstance(value,bool) else str(value)
    os.environ['JSHI_MEMORY_MODULE']=key
    os.environ['JSHI_MEMORY_CURSOR_NAMESPACE']=cursor_key(key,row)
    os.environ['JSHI_MEMORY_BACKEND']=backend
    if backend=='rems3':
        os.environ['JSHI_REMS_SOURCE_ROOT']=str(source)
        os.environ['JSHI_REMS_DATA_DIR']=str(storage)
    else:
        os.environ['JSHI_INPROCESS_MEMORY_DATA_DIR']=str(storage)
    os.environ['JSHI_MEMORY_CURSOR_SEEDS']=json.dumps(seeds)
    return {'name':key,'backend':backend,'source_root':str(source) if source else None,
            'data_dir':str(storage),'description':row.get('description','')}
