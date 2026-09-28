"""Content-aware risk classification for generic UI actions.

browser.click and similar tools operate on whatever DOM element/text FRIDAY is
told to target — "click this button" is the exact same call whether the
button says "Next Page" or "Send". A skill's tier is assigned once, per tool,
so it can't capture that a specific *call* is more consequential than most
calls to that tool. This module inspects the actual argument text at call
time so friday.permissions can escalate just that one call to the confirm
policy, instead of gating every click behind confirmation.

Deliberately keyword-based, not ML: transparent, auditable, fast, and no new
dependency. Biased toward false positives (asking to confirm when unsure)
over false negatives, per FRIDAY's own tier philosophy — an unnecessary
confirm prompt costs a moment; a missed "this sends/deletes/pays for real"
costs the thing the tier system exists to prevent.
"""

from __future__ import annotations

# Substrings checked case-insensitively against UI text (a button/link label,
# a submit control). Deliberately broad: this is the "when in doubt, ask"
# list, not a precise one.
CONSEQUENTIAL_PHRASES = [
    # commits a message/post to someone else
    "send", "post", "publish", "share", "tweet", "reply", "forward",
    # money leaves your account or a commitment is made
    "buy", "purchase", "pay", "checkout", "place order", "donate",
    "subscribe", "upgrade plan",
    # destructive / hard to reverse
    "delete", "remove", "deactivate", "unsubscribe", "cancel subscription",
    "delete account", "close account", "empty trash",
    # security-sensitive
    "change password", "reset password", "revoke",
]


def is_consequential(text: str) -> bool:
    """Best-effort: does this UI text look like it commits an external or
    hard-to-reverse action? Empty/blank text is never flagged."""
    low = (text or "").strip().lower()
    if not low:
        return False
    return any(phrase in low for phrase in CONSEQUENTIAL_PHRASES)
