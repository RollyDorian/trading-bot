import pyarrow as pa  # type: ignore[import-untyped]

PROVENANCE_FIELDS = [
    pa.field("raw_event_id", pa.int64(), nullable=False),
    pa.field("received_at", pa.timestamp("us", tz="UTC"), nullable=False),
    pa.field("available_at", pa.timestamp("us", tz="UTC"), nullable=False),
    pa.field("exchange_at", pa.timestamp("us", tz="UTC")),
    pa.field("symbol", pa.string(), nullable=False),
    pa.field("source", pa.string(), nullable=False),
    pa.field("connection_id", pa.string()),
    pa.field("local_sequence", pa.int64()),
    pa.field("exchange_sequence", pa.int64()),
    pa.field("raw_schema_version", pa.int16(), nullable=False),
    pa.field("pipeline_version", pa.int16(), nullable=False),
    pa.field("data_quality", pa.string(), nullable=False),
]

RAW_SCHEMA = pa.schema(
    [
        pa.field("raw_event_id", pa.int64(), nullable=False),
        pa.field("received_at", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("exchange_at", pa.timestamp("us", tz="UTC")),
        pa.field("source", pa.string(), nullable=False),
        pa.field("event_type", pa.string(), nullable=False),
        pa.field("symbol", pa.string(), nullable=False),
        pa.field("connection_id", pa.string()),
        pa.field("local_sequence", pa.int64()),
        pa.field("exchange_sequence", pa.int64()),
        pa.field("raw_schema_version", pa.int16(), nullable=False),
        pa.field("legacy_sequence", pa.int64()),
        pa.field("latency_ms", pa.float64()),
        pa.field("payload_json", pa.large_string(), nullable=False),
    ]
)
BEST_QUOTES_SCHEMA = pa.schema(
    PROVENANCE_FIELDS
    + [
        pa.field("bid_price", pa.decimal128(38, 18), nullable=False),
        pa.field("bid_size", pa.decimal128(38, 18), nullable=False),
        pa.field("ask_price", pa.decimal128(38, 18), nullable=False),
        pa.field("ask_size", pa.decimal128(38, 18), nullable=False),
    ]
)
REFERENCE_PRICES_SCHEMA = pa.schema(
    PROVENANCE_FIELDS
    + [
        pa.field("price_kind", pa.string(), nullable=False),
        pa.field("price", pa.decimal128(38, 18), nullable=False),
    ]
)
FUNDING_ESTIMATES_SCHEMA = pa.schema(
    PROVENANCE_FIELDS
    + [
        pa.field("estimated_rate", pa.decimal128(38, 18), nullable=False),
        pa.field("next_funding_at", pa.timestamp("us", tz="UTC"), nullable=False),
    ]
)
LEVEL_TYPE = pa.list_(
    pa.struct(
        [
            pa.field("price", pa.decimal128(38, 18), nullable=False),
            pa.field("quantity", pa.decimal128(38, 18), nullable=False),
        ]
    )
)
ORDERBOOK_EVENTS_SCHEMA = pa.schema(
    PROVENANCE_FIELDS
    + [
        pa.field("message_type", pa.string(), nullable=False),
        pa.field("depth", pa.int16(), nullable=False),
        pa.field("granularity", pa.decimal128(38, 18), nullable=False),
        pa.field("bids", LEVEL_TYPE, nullable=False),
        pa.field("asks", LEVEL_TYPE, nullable=False),
        pa.field("changed_level_count", pa.int16(), nullable=False),
    ]
)
ERRORS_SCHEMA = pa.schema(
    [
        pa.field("raw_event_id", pa.int64(), nullable=False),
        pa.field("received_at", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("event_type", pa.string(), nullable=False),
        pa.field("pipeline_version", pa.int16(), nullable=False),
        pa.field("error_code", pa.string(), nullable=False),
        pa.field("error_detail", pa.string(), nullable=False),
    ]
)

SCHEMAS = {
    "raw": RAW_SCHEMA,
    "best_quotes": BEST_QUOTES_SCHEMA,
    "reference_prices": REFERENCE_PRICES_SCHEMA,
    "funding_estimates": FUNDING_ESTIMATES_SCHEMA,
    "orderbook_events": ORDERBOOK_EVENTS_SCHEMA,
    "normalization_errors": ERRORS_SCHEMA,
}
