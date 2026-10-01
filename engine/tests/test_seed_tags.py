import re

from gab_seeder.seeder import _new_seed_tag


def test_each_baseline_replay_gets_a_fresh_seed_tag():
    first = _new_seed_tag()
    second = _new_seed_tag()
    assert first != second
    assert re.fullmatch(r"[0-9a-f]{20}", first)
    assert re.fullmatch(r"[0-9a-f]{20}", second)