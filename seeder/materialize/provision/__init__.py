"""Queue-based first-upload provisioning.

Drive, Calendar, and Gmail run as independent worker pools over shared queues.
The seeder UI and OAuth backends are unchanged; this package replaces the
per-account thread pools used for the initial push.
"""

from materialize.provision.pipeline import AccountWork, provision_accounts

__all__ = ["AccountWork", "provision_accounts"]
