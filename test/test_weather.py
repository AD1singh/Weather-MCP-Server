"""Tests for the weather MCP server.

No test touches the real network: httpx2.AsyncClient.get is replaced with a
fake that looks URLs up in a table of canned responses (or raises an error).
"""

import json

import httpx2
import pytest
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError

import weather

POINTS_URL = f"{weather.NWS_API_BASE}/points/38.8977,-77.0365"
FORECAST_URL = f"{weather.NWS_API_BASE}/gridpoints/LWX/96,70/forecast"
ALERTS_URL = f"{weather.NWS_API_BASE}/alerts/active/area/CA"


# ---------------------------------------------------------------------------
# Helpers for building fake HTTP behaviour
# ---------------------------------------------------------------------------
def json_response(url: str, payload, status: int = 200) -> httpx2.Response:
    """Build a real httpx2.Response so raise_for_status() behaves normally."""
    return httpx2.Response(
        status, json=payload, request=httpx2.Request("GET", url)
    )


@pytest.fixture
def fake_network(monkeypatch):
    """Return a dict; fill it with {url: Response | Exception} per test."""
    routes: dict[str, httpx2.Response | Exception] = {}
    calls: list[str] = []

    async def fake_get(self, url, **kwargs):
        calls.append(url)
        outcome = routes[url]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(httpx2.AsyncClient, "get", fake_get)
    routes_with_calls = routes
    routes_with_calls["__calls__"] = calls  # type: ignore[assignment]
    return routes_with_calls


async def error_from(coro) -> str:
    """Await a tool call that must fail; return the ToolError message."""
    with pytest.raises(ToolError) as exc_info:
        await coro
    return str(exc_info.value)


def make_period(n: int) -> dict:
    return {
        "name": f"Period {n}",
        "temperature": 60 + n,
        "temperatureUnit": "F",
        "windSpeed": f"{n} mph",
        "windDirection": "NW",
        "detailedForecast": f"Detailed text {n}.",
    }


def forecast_payload(count: int = 7) -> dict:
    return {"properties": {"periods": [make_period(i) for i in range(1, count + 1)]}}


def alert_feature(**overrides) -> dict:
    props = {
        "event": "Heat Advisory",
        "areaDesc": "Sacramento Valley",
        "severity": "Moderate",
        "description": "Hot temperatures expected.",
        "instruction": "Drink plenty of fluids.",
    }
    props.update(overrides)
    return {"properties": props}


# ---------------------------------------------------------------------------
# 1 + 2. Coordinate validation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "lat,lon", [(38.9, -77.0), (0, 0), (90, 180), (-90, -180)]
)
def test_valid_coordinates(lat, lon):
    assert weather.validate_coordinates(lat, lon) is None


@pytest.mark.parametrize(
    "lat,lon,fragment",
    [
        (90.1, 0, "latitude"),
        (-90.1, 0, "latitude"),
        (0, 180.1, "longitude"),
        (0, -180.1, "longitude"),
        (float("nan"), 0, "latitude"),
    ],
)
def test_invalid_coordinates(lat, lon, fragment):
    error = weather.validate_coordinates(lat, lon)
    assert error is not None and fragment in error.lower()


async def test_get_forecast_rejects_bad_coordinates_without_network(fake_network):
    result = await error_from(weather.get_forecast(100.0, 0.0))
    assert "latitude" in result.lower()
    assert fake_network["__calls__"] == []  # validation happens before any request


# ---------------------------------------------------------------------------
# 3 + 4. State code validation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected", [("CA", "CA"), ("ca", "CA"), (" ny ", "NY"), ("Tx", "TX")]
)
def test_valid_state_codes_are_normalized(raw, expected):
    assert weather.normalize_state(raw) == (expected, None)


@pytest.mark.parametrize("raw", ["", "C", "CAL", "C1", "12", "C-", "éé", "  "])
def test_invalid_state_codes(raw):
    code, error = weather.normalize_state(raw)
    assert code is None and error is not None


async def test_get_alerts_rejects_bad_state_without_network(fake_network):
    result = await error_from(weather.get_alerts("California"))
    assert fake_network["__calls__"] == []


# ---------------------------------------------------------------------------
# 5. Successful API responses
# ---------------------------------------------------------------------------
async def test_get_forecast_success_returns_five_periods(fake_network):
    fake_network[POINTS_URL] = json_response(
        POINTS_URL, {"properties": {"forecast": FORECAST_URL}}
    )
    fake_network[FORECAST_URL] = json_response(FORECAST_URL, forecast_payload(7))

    result = await weather.get_forecast(38.8977, -77.0365)

    assert result.count("---") == 4  # five periods -> four separators
    assert "Period 5" in result and "Period 6" not in result
    assert "Temperature: 61°F" in result
    assert "Wind: 1 mph NW" in result
    assert "Detailed text 1." in result


async def test_get_forecast_rounds_coordinates_to_four_decimals(fake_network):
    fake_network[POINTS_URL] = json_response(
        POINTS_URL, {"properties": {"forecast": FORECAST_URL}}
    )
    fake_network[FORECAST_URL] = json_response(FORECAST_URL, forecast_payload(1))

    await weather.get_forecast(38.897712345, -77.036512345)

    assert fake_network["__calls__"][0] == POINTS_URL


async def test_get_alerts_success_formats_all_fields(fake_network):
    fake_network[ALERTS_URL] = json_response(
        ALERTS_URL, {"features": [alert_feature()]}
    )

    result = await weather.get_alerts("ca")  # lowercase is normalized

    for expected in (
        "Event: Heat Advisory",
        "Area: Sacramento Valley",
        "Severity: Moderate",
        "Description: Hot temperatures expected.",
        "Instructions: Drink plenty of fluids.",
    ):
        assert expected in result


async def test_get_alerts_handles_null_fields(fake_network):
    fake_network[ALERTS_URL] = json_response(
        ALERTS_URL,
        {"features": [alert_feature(instruction=None, description=None)]},
    )
    result = await weather.get_alerts("CA")
    assert "No specific instructions provided" in result
    assert "No description available" in result


# ---------------------------------------------------------------------------
# 6. Empty alerts
# ---------------------------------------------------------------------------
async def test_get_alerts_empty_list_is_clear_message(fake_network):
    fake_network[ALERTS_URL] = json_response(ALERTS_URL, {"features": []})
    result = await weather.get_alerts("CA")
    assert result == "No active weather alerts for CA."


# ---------------------------------------------------------------------------
# 7. Failed API requests
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "failure,fragment",
    [
        (httpx2.ReadTimeout("slow"), "timed out"),
        (httpx2.ConnectError("down"), "could not connect"),
    ],
)
async def test_helper_converts_exceptions_to_errors(fake_network, failure, fragment):
    fake_network[ALERTS_URL] = failure
    result = await weather.make_nws_request(ALERTS_URL)
    assert result.data is None and fragment in result.error.lower()


async def test_helper_reports_http_status(fake_network):
    fake_network[ALERTS_URL] = json_response(ALERTS_URL, {}, status=500)
    result = await weather.make_nws_request(ALERTS_URL)
    assert result.status_code == 500 and "HTTP 500" in result.error


async def test_helper_handles_invalid_json(fake_network):
    fake_network[ALERTS_URL] = httpx2.Response(
        200, content=b"<html>not json</html>", request=httpx2.Request("GET", ALERTS_URL)
    )
    result = await weather.make_nws_request(ALERTS_URL)
    assert "invalid json" in result.error.lower()


async def test_helper_handles_non_object_json(fake_network):
    fake_network[ALERTS_URL] = json_response(ALERTS_URL, ["a", "list"])
    result = await weather.make_nws_request(ALERTS_URL)
    assert "unexpected" in result.error.lower()


async def test_helper_sends_user_agent(monkeypatch):
    seen = {}

    async def fake_get(self, url, **kwargs):
        seen.update(kwargs["headers"])
        return json_response(url, {})

    monkeypatch.setattr(httpx2.AsyncClient, "get", fake_get)
    await weather.make_nws_request(ALERTS_URL)
    assert seen["User-Agent"] == weather.USER_AGENT


async def test_get_alerts_timeout_does_not_raise(fake_network):
    fake_network[ALERTS_URL] = httpx2.ReadTimeout("slow")
    result = await error_from(weather.get_alerts("CA"))
    assert "timed out" in result


async def test_get_alerts_unknown_state_404(fake_network):
    url = f"{weather.NWS_API_BASE}/alerts/active/area/ZZ"
    fake_network[url] = json_response(url, {}, status=400)
    result = await error_from(weather.get_alerts("zz"))
    assert "ZZ" in result


async def test_get_forecast_unsupported_location_mentions_us_only(fake_network):
    url = f"{weather.NWS_API_BASE}/points/51.5074,-0.1278"
    fake_network[url] = json_response(url, {"title": "Not Found"}, status=404)
    result = await error_from(weather.get_forecast(51.5074, -0.1278))
    assert "US" in result


async def test_get_forecast_second_request_failure(fake_network):
    fake_network[POINTS_URL] = json_response(
        POINTS_URL, {"properties": {"forecast": FORECAST_URL}}
    )
    fake_network[FORECAST_URL] = httpx2.ConnectError("down")
    result = await error_from(weather.get_forecast(38.8977, -77.0365))
    assert "connect" in result.lower()


# ---------------------------------------------------------------------------
# 8. Missing forecast / response data
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "points_payload",
    [{}, {"properties": {}}, {"properties": None}, {"properties": {"forecast": None}}],
)
async def test_get_forecast_missing_forecast_url(fake_network, points_payload):
    fake_network[POINTS_URL] = json_response(POINTS_URL, points_payload)
    result = await error_from(weather.get_forecast(38.8977, -77.0365))
    assert "forecast URL" in result


@pytest.mark.parametrize(
    "forecast_payload_",
    [{}, {"properties": {}}, {"properties": {"periods": []}}, {"properties": {"periods": "x"}}],
)
async def test_get_forecast_missing_periods(fake_network, forecast_payload_):
    fake_network[POINTS_URL] = json_response(
        POINTS_URL, {"properties": {"forecast": FORECAST_URL}}
    )
    fake_network[FORECAST_URL] = json_response(FORECAST_URL, forecast_payload_)
    result = await error_from(weather.get_forecast(38.8977, -77.0365))
    assert "periods" in result


async def test_get_forecast_period_with_missing_fields_still_formats(fake_network):
    fake_network[POINTS_URL] = json_response(
        POINTS_URL, {"properties": {"forecast": FORECAST_URL}}
    )
    fake_network[FORECAST_URL] = json_response(
        FORECAST_URL, {"properties": {"periods": [{"name": "Tonight"}]}}
    )
    result = await weather.get_forecast(38.8977, -77.0365)
    assert "Tonight:" in result and "Temperature: Unknown" in result


async def test_get_alerts_missing_features_key(fake_network):
    fake_network[ALERTS_URL] = json_response(ALERTS_URL, {"title": "oops"})
    result = await error_from(weather.get_alerts("CA"))
    assert "missing" in result


# ---------------------------------------------------------------------------
# Through the MCP protocol layer: an in-memory MCP Client talks to the server
# exactly as a real host would (minus the stdio pipe).
# ---------------------------------------------------------------------------
async def test_protocol_lists_both_tools():
    async with Client(weather.mcp) as client:
        listed = await client.list_tools()
    assert {t.name for t in listed.tools} == {"get_forecast", "get_alerts"}


async def test_protocol_success_is_not_flagged_as_error(fake_network):
    fake_network[ALERTS_URL] = json_response(ALERTS_URL, {"features": []})
    async with Client(weather.mcp) as client:
        result = await client.call_tool("get_alerts", {"state": "CA"})
    assert result.is_error is False
    assert "No active weather alerts for CA." in result.content[0].text


async def test_protocol_failure_is_flagged_as_error(fake_network):
    async with Client(weather.mcp) as client:
        result = await client.call_tool("get_alerts", {"state": "C1"})
    assert result.is_error is True
    assert "Invalid state code" in result.content[0].text
    assert fake_network["__calls__"] == []  # rejected before any request


async def test_protocol_forecast_end_to_end(fake_network):
    fake_network[POINTS_URL] = json_response(
        POINTS_URL, {"properties": {"forecast": FORECAST_URL}}
    )
    fake_network[FORECAST_URL] = json_response(FORECAST_URL, forecast_payload(6))
    async with Client(weather.mcp) as client:
        result = await client.call_tool(
            "get_forecast", {"latitude": 38.8977, "longitude": -77.0365}
        )
    assert result.is_error is False
    text = result.content[0].text
    assert "Period 5" in text and "Period 6" not in text
