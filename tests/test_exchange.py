from unittest.mock import Mock, patch

from trading_bot.exchange import HibachiPublicExchange


def test_exchange_strips_trailing_url_slashes() -> None:
    client = Mock()
    with patch("hibachi_xyz.HibachiApiClient", return_value=client) as constructor:
        HibachiPublicExchange("https://api.example/", "https://data.example/")
    constructor.assert_called_once_with(
        api_url="https://api.example",
        data_api_url="https://data.example",
    )
