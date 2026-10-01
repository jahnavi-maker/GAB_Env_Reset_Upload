import pytest

import gab_seeder.gmail as gmail_module


class RecordingGmailQuotaPacer:
    def __init__(self):
        self.units = []

    def acquire(self, units):
        self.units.append(int(units))


@pytest.fixture(autouse=True)
def _record_gmail_quota(monkeypatch):
    pacer = RecordingGmailQuotaPacer()
    monkeypatch.setattr(gmail_module, "_GMAIL_QUOTA_PACER", pacer)
    return pacer


@pytest.fixture
def gmail_quota_units(_record_gmail_quota):
    return _record_gmail_quota.units
