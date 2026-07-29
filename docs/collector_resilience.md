# Collector reconnect resilience

The collector remains fail-closed per stream session. A failed connection is
disconnected before another session is constructed, and every new session gets
a new connection ID and requires a new order-book snapshot.

Classified transport failures (`ConnectionError`, timeout, and operating-system
socket errors) reconnect indefinitely. Delay uses exponential backoff with
jitter, is always bounded by the configured maximum, and cannot overflow after
long outages. The existing reconnect-attempt setting is the threshold at which
the persisted `DEGRADED` evidence escalates from Warning to Error; it no longer
terminates collection. Five minutes of stable session uptime resets the attempt
counter.

Cancellation exits immediately and cleanly. Desynchronization reconnects to
obtain a fresh snapshot. Database writes, parser/programming failures, and
unclassified exceptions remain fatal: the supervisor records one sanitized
Critical `HALTED` event and exits. It never retries past uncertain storage state,
drops an event silently, spins without delay, or starts a background daemon.

Freshness monitoring remains the operator-visible outage signal while a
transport reconnect is pending. This behavior does not enable trading, change
topics, alter schemas, or modify data.
