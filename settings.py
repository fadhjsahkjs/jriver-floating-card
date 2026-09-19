"""Optional per-user configuration. Importing does not contact any service."""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get('JRIVER_CARD_CONFIG', str(ROOT / 'config.json')))
SETTINGS = json.loads(CONFIG_PATH.read_text('utf-8')) if CONFIG_PATH.exists() else {}
if not isinstance(SETTINGS, dict):
    raise ValueError('config.json must contain an object')


def local_path(value):
    path = Path(os.path.expandvars(os.path.expanduser(value)))
    return path if path.is_absolute() else CONFIG_PATH.parent / path
