import os
import subprocess
import sys
from pathlib import Path

import pytest

from jshi.memory.factory import (
    MemoryStorageInUseError, build_memory_backend, check_rems_storage_available,
)


def test_live_qdrant_owner_blocks_probe_and_release_allows_start(tmp_path):
    QdrantClient = pytest.importorskip('qdrant_client').QdrantClient
    path = tmp_path / 'rems'
    lock = path/'qdrant/.lock'
    lock.parent.mkdir(parents=True)
    original = b'lock marker must remain intact'
    lock.write_bytes(original)
    client = QdrantClient(path=str(path/'qdrant'))
    try:
        with pytest.raises(MemoryStorageInUseError, match='记忆库正被另一个进程使用'):
            check_rems_storage_available(path)
        assert lock.exists()
    finally:
        client.close()
    check_rems_storage_available(path)
    assert lock.exists() and lock.read_bytes() == original


def test_first_start_probe_does_not_create_storage(tmp_path):
    target = tmp_path/'not-created'
    check_rems_storage_available(target)
    assert not target.exists()


def test_cli_duplicate_exits_before_loading_backend_without_traceback(tmp_path):
    QdrantClient = pytest.importorskip('qdrant_client').QdrantClient
    storage = tmp_path/'shared'
    client = QdrantClient(path=str(storage/'qdrant'))
    try:
        env = dict(os.environ, JSHI_MEMORY_BACKEND='rems3', JSHI_REMS_DATA_DIR=str(storage),
                   PYTHONPATH=str(Path(__file__).resolve().parents[1]/'src'), PYTHONIOENCODING='utf-8')
        result = subprocess.run([sys.executable, '-m', 'jshi.app.cli', '--data-dir',
            str(tmp_path/'other-cli'), 'state', 'stone'], capture_output=True, text=True,
            encoding='utf8', env=env, timeout=20)
        assert result.returncode != 0
        assert '本次启动已退出' in result.stderr
        assert str((storage/'qdrant').resolve()) in result.stderr
        assert 'Traceback' not in result.stderr
        assert 'Storage folder' not in result.stderr
        # The rejected startup did not disturb the original owner.
        assert client.get_collections().collections == []
    finally:
        client.close()


@pytest.mark.parametrize('message,expected', [
    ('Storage folder test is already accessed by another instance of Qdrant client.', MemoryStorageInUseError),
    ('unrelated broken configuration', RuntimeError),
])
def test_late_lock_race_is_reported_but_other_errors_preserved(tmp_path, monkeypatch, message, expected):
    monkeypatch.setenv('JSHI_MEMORY_BACKEND','rems3')
    monkeypatch.setenv('JSHI_REMS_DATA_DIR',str(tmp_path/'custom'))
    def failed(**kwargs):raise RuntimeError(message)
    monkeypatch.setattr('jshi.memory.rems3.Rems3MemoryBackend',failed)
    with pytest.raises(expected) as caught:
        build_memory_backend(None,tmp_path)
    if expected is RuntimeError:assert str(caught.value)==message
    else:assert str((tmp_path/'custom/qdrant').resolve()) in str(caught.value)
