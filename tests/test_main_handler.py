from unittest.mock import patch

from app.main import handler


def test_warmer_event_short_circuits_without_touching_mangum():
    # The whole point of the warming ping (infra/jesters_rodeo_stack.py's
    # AppWarmingScheduleRule) is to keep an execution environment alive
    # without doing real work -- it must never reach Mangum/FastAPI, which
    # would also fail outright on a non-API-Gateway-shaped event anyway.
    with patch("app.main._mangum_handler") as mock_mangum:
        result = handler({"warmer": True}, None)
    assert result == {"statusCode": 200, "body": "warm"}
    mock_mangum.assert_not_called()


def test_non_warmer_event_falls_through_to_mangum():
    fake_event = {"httpMethod": "GET", "path": "/"}
    fake_context = object()
    with patch("app.main._mangum_handler", return_value="mangum-result") as mock_mangum:
        result = handler(fake_event, fake_context)
    assert result == "mangum-result"
    mock_mangum.assert_called_once_with(fake_event, fake_context)
