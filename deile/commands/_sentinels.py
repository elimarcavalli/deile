"""Sentinel keys for the command→CLI session-switch handshake.

Single source of truth for the ``Session.context_data`` keys written by
the clear/fork/rewind/resume commands and consumed by the CLI loop
(``deile.cli``). Kept in a dependency-free module so both layers import
the same literals — a rename here propagates to every consumer.
"""

SWITCH_SESSION_KEY = "_switch_session"
POST_SWITCH_ACTION_KEY = "_post_switch_action"
