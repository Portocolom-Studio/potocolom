"""Pure controls for the lock-order helpers in tests/test_totp_flow.py.

The helpers must count a table only at SQL identifier boundaries: the
realtime_sessions table name contains the substring sessions, so a substring
check counted it as a lock on sessions and the operator order guards asserted
an order the routes never took. These controls call the helpers directly on
recorded statements, need no database, and carry no db mark.
"""

from tests.test_totp_flow import _lock_orders, _order_within

TABLES = ("auth_tokens", "sessions")


def test_realtime_sessions_select_is_not_the_sessions_table():
    statements = [
        "SELECT user_id FROM realtime_sessions WHERE id = 1 FOR UPDATE",
        "UPDATE auth_tokens SET token_hash = 'x' WHERE user_id = 1",
        "UPDATE sessions SET data = 'y' WHERE id = 2",
    ]
    assert _order_within(statements, TABLES) == ["auth_tokens", "sessions"]


def test_reversed_real_locks_still_report_sessions_first():
    statements = [
        "SELECT user_id FROM realtime_sessions WHERE id = 1 FOR UPDATE",
        "UPDATE sessions SET data = 'y' WHERE id = 2",
        "UPDATE auth_tokens SET token_hash = 'x' WHERE user_id = 1",
    ]
    assert _order_within(statements, TABLES) == ["sessions", "auth_tokens"]


def test_a_transaction_short_one_real_table_is_omitted():
    transactions = [[
        "SELECT user_id FROM realtime_sessions WHERE id = 1 FOR UPDATE",
        "UPDATE auth_tokens SET token_hash = 'x' WHERE user_id = 1",
    ]]
    assert _lock_orders(transactions, TABLES) == []


def test_a_plain_select_locks_nothing():
    statements = [
        "SELECT token_hash FROM auth_tokens WHERE user_id = 1",
        "SELECT data FROM sessions WHERE id = 2",
    ]
    assert _order_within(statements, TABLES) == []


def test_quoted_and_schema_qualified_names_still_count():
    statements = [
        "UPDATE public.auth_tokens SET token_hash = 'x' WHERE user_id = 1",
        'UPDATE "sessions" SET data = \'y\' WHERE id = 2',
    ]
    assert _order_within(statements, TABLES) == ["auth_tokens", "sessions"]


def test_transactions_are_judged_separately():
    transactions = [
        ["UPDATE auth_tokens SET token_hash = 'x' WHERE user_id = 1",
         "UPDATE sessions SET data = 'y' WHERE id = 2"],
        ["UPDATE sessions SET data = 'y' WHERE id = 2",
         "UPDATE auth_tokens SET token_hash = 'x' WHERE user_id = 1"],
    ]
    assert _lock_orders(transactions, TABLES) == [
        ["auth_tokens", "sessions"],
        ["sessions", "auth_tokens"],
    ]
