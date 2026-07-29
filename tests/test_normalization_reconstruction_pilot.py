import asyncio

from trading_bot.normalization.pilot import reconstruction_summary


def test_reconstruction_summary_is_bounded_for_empty_input() -> None:
    class EmptyStream:
        def __aiter__(self) -> "EmptyStream":
            return self

        async def __anext__(self) -> object:
            raise StopAsyncIteration

    class Session:
        async def stream_scalars(self, statement: object) -> EmptyStream:
            return EmptyStream()

    class SessionContext:
        async def __aenter__(self) -> Session:
            return Session()

        async def __aexit__(self, *args: object) -> None:
            return None

    class Factory:
        def __call__(self) -> SessionContext:
            return SessionContext()

    result = asyncio.run(
        reconstruction_summary(
            Factory()  # type: ignore[arg-type]
        )
    )
    assert result == {
        "events": 0,
        "final_state": "no_orderbook_events",
        "state_counts": {},
    }
