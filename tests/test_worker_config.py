import pytest

from worker_config import load_settings


def test_config_file_and_literal_password(tmp_path):
    path = tmp_path / 'worker.cfg'
    path.write_text('[database]\nhost = dell\nport = 3307\nuser = solver\n'
                    'password = percent% dollar$ slash\\ quote\"\n[worker]\nid = 2\n')
    settings = load_settings(path, {})
    assert settings.host == 'dell' and settings.port == 3307 and settings.worker_id == 2
    assert settings.password == 'percent% dollar$ slash\\ quote"'
    assert settings.password not in repr(settings)


def test_environment_overrides_config(tmp_path):
    path = tmp_path / 'worker.cfg'
    path.write_text('[database]\nhost = dell\npassword = from-file\n[worker]\nid = 2\n')
    settings = load_settings(environ=dict(ETERNITY_CONFIG=str(path), ETERNITY_DB_HOST='other',
                                          ETERNITY_DB_PASSWORD='from-environment', ETERNITY_WORKER_ID='3'))
    assert settings.host == 'other' and settings.password == 'from-environment' and settings.worker_id == 3


def test_explicit_missing_file_is_reported(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_settings(tmp_path / 'missing.cfg', {})


@pytest.mark.parametrize('variable,value', [('ETERNITY_DB_PORT', '0'), ('ETERNITY_WORKER_ID', '-1')])
def test_invalid_numeric_settings(tmp_path, variable, value):
    path = tmp_path / 'worker.cfg'
    path.write_text('')
    with pytest.raises(ValueError):
        load_settings(path, {variable: value})
