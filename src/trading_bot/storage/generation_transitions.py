"""Validated generation-state transitions for RAW partition rotation.

Invalid transitions fail closed. Durable state lives in
``market_event_generations``; recovery is catalog + metadata, never guessed.
"""

from __future__ import annotations

from trading_bot.storage.partitions import GenerationState, PartitionLifecycleError

# Exact permitted edges for the first continuous-operating model.
ALLOWED_TRANSITIONS: dict[GenerationState, frozenset[GenerationState]] = {
    GenerationState.PROVISIONED: frozenset({GenerationState.ACTIVE}),
    GenerationState.ACTIVE: frozenset({GenerationState.CLOSED_UNARCHIVED}),
    GenerationState.CLOSED_UNARCHIVED: frozenset(
        {
            GenerationState.ARCHIVING,
            GenerationState.ARCHIVE_FAILED,
        }
    ),
    GenerationState.ARCHIVING: frozenset(
        {
            GenerationState.VERIFIED,
            GenerationState.ARCHIVE_FAILED,
            GenerationState.VERIFY_FAILED,
            # Operator/crash abort of an in-flight archive without guessing success.
            GenerationState.CLOSED_UNARCHIVED,
        }
    ),
    GenerationState.ARCHIVE_FAILED: frozenset(
        {
            GenerationState.CLOSED_UNARCHIVED,
            GenerationState.ARCHIVING,
        }
    ),
    GenerationState.VERIFY_FAILED: frozenset(
        {
            GenerationState.ARCHIVING,
            GenerationState.CLOSED_UNARCHIVED,
        }
    ),
    GenerationState.VERIFIED: frozenset({GenerationState.DROP_ELIGIBLE}),
    GenerationState.DROP_ELIGIBLE: frozenset({GenerationState.DROPPED}),
    GenerationState.DROPPED: frozenset(),
}


def assert_transition_allowed(
    current: GenerationState,
    new_state: GenerationState,
) -> None:
    """Raise unless ``current → new_state`` is an approved edge."""

    if current == new_state:
        return
    allowed = ALLOWED_TRANSITIONS.get(current, frozenset())
    if new_state not in allowed:
        raise PartitionLifecycleError(
            f"invalid generation transition {current.value} → {new_state.value}"
        )
