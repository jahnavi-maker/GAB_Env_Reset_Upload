"""GAB environment reset service.

A thin HTTP + audit layer around Vishal's ``gab_seeder`` reset engine:

    POST /api/environment/reset   -> generate a reset_session_id, log it to
                                     Supabase, run the reset, record timestamps.
    GET  /api/environment/reset/{reset_session_id}  -> poll status.

Scope of this build (steps 1-3 of the design doc):
  1. Public API that accepts {email, persona, password, task_allocation_id}.
  2. Triggers the existing reset flow (``gab-seed`` CLI) unchanged.
  3. Records session id + timestamps + email + persona in Supabase.

Parallelism, EC2 packaging and OAuth publishing are deliberately out of scope
here and tracked separately.
"""

__all__ = ["__version__"]
__version__ = "0.1.0"
