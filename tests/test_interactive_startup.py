import json
import sys

import pytest

from jshi.app import cli
from jshi.identity import IdentityProfile, IdentityRepository


@pytest.fixture
def data_dir(tmp_path):
    identities = IdentityRepository(tmp_path / 'identities.json')
    identities.create(IdentityProfile('stone', '匠石', 'test'))
    return tmp_path


@pytest.mark.parametrize('command', ['vision', 'voice'])
def test_invalid_subject_exits_before_loading_memory(command, data_dir, monkeypatch):
    def runtime(*args, **kwargs):
        pytest.fail('invalid subject must not initialize memory')
    monkeypatch.setattr(cli, '_runtime', runtime)
    monkeypatch.setattr(cli, '_load_local_env', lambda: None)
    monkeypatch.setattr(sys, 'argv', ['jshi', '--data-dir', str(data_dir), command, 'lux'])
    with pytest.raises(SystemExit) as error:
        cli.main()
    message = str(error.value)
    assert 'lux' in message and 'stone（匠石）' in message


def test_valid_subject_and_saved_session(data_dir):
    assert cli._interactive_subject_id(data_dir, 'stone') == 'stone'
    (data_dir / 'cli_session.json').write_text(json.dumps({'subject_id': 'stone'}), encoding='utf-8')
    assert cli._interactive_subject_id(data_dir, None) == 'stone'


def test_empty_subject_directory_is_actionable(tmp_path):
    with pytest.raises(SystemExit, match='先创建主体'):
        cli._interactive_subject_id(tmp_path, 'lux')
