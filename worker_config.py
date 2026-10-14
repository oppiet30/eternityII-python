"""Per-machine worker configuration; explicit environment settings win."""
import configparser
from dataclasses import dataclass, field
import os
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    user: str
    password: str = field(repr=False)
    worker_id: int


def load_settings(path=None, environ=None):
    environ = os.environ if environ is None else environ
    explicit = path is not None or 'ETERNITY_CONFIG' in environ
    path = Path(path or environ.get('ETERNITY_CONFIG', Path(__file__).with_name('worker.cfg')))
    config = configparser.ConfigParser(interpolation=None)
    if path.exists():
        with path.open(encoding='utf-8') as handle:
            config.read_file(handle)
    elif explicit:
        raise FileNotFoundError(f'Worker configuration does not exist: {path}')

    def value(variable, section, option, default):
        return environ.get(variable, config.get(section, option, fallback=default))

    port = int(value('ETERNITY_DB_PORT', 'database', 'port', '3306'))
    worker_id = int(value('ETERNITY_WORKER_ID', 'worker', 'id', '1'))
    if not 1 <= port <= 65535 or worker_id < 1:
        raise ValueError('database port must be 1..65535 and worker ID must be positive')
    return Settings(host=value('ETERNITY_DB_HOST', 'database', 'host', 'localhost'),
                    port=port, user=value('ETERNITY_DB_USER', 'database', 'user', 'solver'),
                    password=value('ETERNITY_DB_PASSWORD', 'database', 'password', ''),
                    worker_id=worker_id)
