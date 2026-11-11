"""All starter tests are offline and use disposable company databases."""
import os
import socket
import subprocess

import pytest


@pytest.fixture(autouse=True)
def isolate_external_services(monkeypatch):
    for key in list(os.environ):
        if key.startswith(('MF_', 'DB_', 'PG', 'GOOGLE_')) or key == 'DATABASE_URL':
            monkeypatch.delenv(key, raising=False)
    def forbidden(*args, **kwargs):
        raise AssertionError('Live network access is forbidden in starter tests')
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    original_run = subprocess.run
    def safe_run(command, *args, **kwargs):
        if isinstance(command, (list, tuple)) and command and str(command[0]).split('/')[-1] in {'security', 'osascript', 'gcloud', 'clasp'}:
            raise AssertionError('Live credential/service access is forbidden in starter tests')
        return original_run(command, *args, **kwargs)
    monkeypatch.setattr(subprocess, 'run', safe_run)
