# -*- coding: utf-8 -*-
"""
Guards on the public API surface.
"""


def test_narrowed_names_are_private(db, empty_table):
    # issue #25: these used to be public and were renamed; the public names
    # must not quietly come back
    for name in ["cursor", "log_db_change", "is_alive", "is_read_only",
                 "can_read_write_knowls", "can_read_write_userdb",
                 "register_object", "logger"]:
        assert not hasattr(db, name), name
        assert hasattr(db, "_" + name), name
    for name in ["has_id", "log_db_change", "logger"]:
        assert not hasattr(empty_table, name), name
        assert hasattr(empty_table, "_" + name), name
