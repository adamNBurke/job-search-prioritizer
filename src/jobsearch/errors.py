"""One exception type for problems the user can fix.

Purpose: a command that fails for a reason the user controls (no scan yet, a bad scoring
config, a board that cannot be reached) raises UserError so cli.main can print it as a Result.
Logic: the message is plain English and safe to show. cli.main maps it to
Result(ok=False, error=str(exc)).
Guardrails: never put a path to a secret, a stack trace or a raw response body in a message.
QA checklist: a new failure the user can fix subclasses or raises UserError, never a bare
Exception, so the user sees the sentence instead of "internal error".
"""

from __future__ import annotations


class UserError(Exception):
    """Message is plain English and safe to show the user."""
