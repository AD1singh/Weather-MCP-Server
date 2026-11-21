"""Weather MCP server.

Exposes two tools backed by the US National Weather Service (NWS) API:

* get_forecast(latitude, longitude) - next five forecast periods
* get_alerts(state)                 - active alerts for a US state

The server talks to an MCP client over stdio. stdout is reserved for the MCP
protocol, so all logging goes to stderr and print() must never be used.
"""

import logging
import sys
from dataclasses import dataclass
from typing import Any

import httpx2
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError


# Logging: stderr only (stdout belongs to the MCP protocol)

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("weather")


# Server instance and constants

mcp = MCPServer(
    "weather",
    version="0.1.0",
    instructions=(
        "Weather tools backed by the US National Weather Service. "
        "Only US locations are supported."
    ),
)

NWS_API_BASE = "https://api.weather.gov"
# NWS asks every client to identify itself with a User-Agent.
USER_AGENT = "weather-mcp-server/0.1.0"
REQUEST_TIMEOUT_SECONDS = 20.0
FORECAST_PERIODS = 5

US_ONLY_NOTE = (
    "The National Weather Service API only covers US locations "
    "(including territories). Locations outside the US are not supported."
)



# Input validation (pure functions: easy to test, no network)

def validate_coordinates(latitude: float, longitude: float) -> str | None:
    """Return an error message if the coordinates are invalid, else None."""
    # Written as "not (a <= x <= b)" so that NaN also fails the check.
    if not (-90 <= latitude <= 90):
        return f"Invalid latitude {latitude}: must be between -90 and 90."
    if not (-180 <= longitude <= 180):
        return f"Invalid longitude {longitude}: must be between -180 and 180."
    return None


def normalize_state(state: str) -> tuple[str | None, str | None]:
    """Validate and uppercase a state code.

    Returns (code, None) on success or (None, error_message) on failure.
    """
    cleaned = state.strip() if isinstance(state, str) else ""
    if len(cleaned) != 2 or not (cleaned.isascii() and cleaned.isalpha()):
        return None, (
            f"Invalid state code {state!r}: expected exactly two letters, "
            "e.g. 'CA' or 'NY'."
        )
    return cleaned.upper(), None



# API helper

@dataclass
class NWSResponse:
    """Result of an NWS request: either `data` or `error` is set."""

    data: dict[str, Any] | None = None
    error: str | None = None
    status_code: int | None = None


async def make_nws_request(url: str) -> NWSResponse:
    """GET a URL from the NWS API and return a structured result.

    Never raises: every failure becomes an NWSResponse with an error message.
    """
    headers = {"User-Agent": USER_AGENT, "Accept": "application/geo+json"}
    try:
        async with httpx2.AsyncClient(follow_redirects=True) as client:
            response = await client.get(
                url, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS
            )
            response.raise_for_status()
            payload = response.json()
    except httpx2.TimeoutException:
        logger.warning("Timeout calling %s", url)
        return NWSResponse(error="The weather service timed out. Please try again.")
    except httpx2.HTTPStatusError as exc:
        status = exc.response.status_code
        logger.warning("HTTP %s from %s", status, url)
        return NWSResponse(
            error=f"The weather service returned HTTP {status}.", status_code=status
        )
    except httpx2.RequestError as exc:
        logger.warning("Connection problem calling %s: %s", url, exc)
        return NWSResponse(
            error="Could not connect to the weather service. Check your network."
        )
    except ValueError:  # response.json() raises ValueError on invalid JSON
        logger.warning("Invalid JSON from %s", url)
        return NWSResponse(error="The weather service returned invalid JSON.")

    if not isinstance(payload, dict):
        logger.warning("Unexpected JSON shape from %s", url)
        return NWSResponse(error="The weather service returned an unexpected response.")
    return NWSResponse(data=payload, status_code=response.status_code)



# Formatting helpers

def format_alert(feature: dict[str, Any]) -> str:
    """Format one alert feature as readable text."""
    props = feature.get("properties") or {}
    # NWS sends null (not a missing key) for absent fields, hence "or".
    return (
        f"Event: {props.get('event') or 'Unknown'}\n"
        f"Area: {props.get('areaDesc') or 'Unknown'}\n"
        f"Severity: {props.get('severity') or 'Unknown'}\n"
        f"Description: {props.get('description') or 'No description available'}\n"
        f"Instructions: {props.get('instruction') or 'No specific instructions provided'}"
    )


def format_period(period: dict[str, Any]) -> str:
    """Format one forecast period as readable text."""
    unit = period.get("temperatureUnit") or ""
    temperature = period.get("temperature")
    temp_text = f"{temperature}°{unit}" if temperature is not None else "Unknown"
    wind = f"{period.get('windSpeed') or 'Unknown'} {period.get('windDirection') or ''}"
    return (
        f"{period.get('name') or 'Unnamed period'}:\n"
        f"Temperature: {temp_text}\n"
        f"Wind: {wind.strip()}\n"
        f"Forecast: {period.get('detailedForecast') or 'No detailed forecast available'}"
    )



# Tools

@mcp.tool()
async def get_forecast(latitude: float, longitude: float) -> str:
    """Get the weather forecast for a US location.

    Returns the next five forecast periods with temperature, wind and a
    detailed description. Only US locations are supported (National Weather
    Service data).

    Args:
        latitude: Latitude between -90 and 90 (e.g. 38.8977)
        longitude: Longitude between -180 and 180 (e.g. -77.0365)
    """
    error = validate_coordinates(latitude, longitude)
    if error:
        raise ToolError(error)

    # NWS only accepts up to 4 decimal places; more triggers a redirect.
    points_url = f"{NWS_API_BASE}/points/{latitude:.4f},{longitude:.4f}"
    points = await make_nws_request(points_url)
    if points.error:
        if points.status_code in (400, 404):
            raise ToolError(f"No forecast available for this location. {US_ONLY_NOTE}")
        raise ToolError(points.error)

    forecast_url = ((points.data or {}).get("properties") or {}).get("forecast")
    if not isinstance(forecast_url, str) or not forecast_url:
        raise ToolError(
            "The weather service did not provide a forecast URL for this "
            f"location. {US_ONLY_NOTE}"
        )

    forecast = await make_nws_request(forecast_url)
    if forecast.error:
        raise ToolError(forecast.error)

    periods = ((forecast.data or {}).get("properties") or {}).get("periods")
    if not isinstance(periods, list) or not periods:
        raise ToolError("The forecast response did not contain any forecast periods.")

    return "\n---\n".join(format_period(p) for p in periods[:FORECAST_PERIODS])


@mcp.tool()
async def get_alerts(state: str) -> str:
    """Get active weather alerts for a US state.

    Only US states and territories are supported (National Weather Service
    data).

    Args:
        state: Two-letter US state code (e.g. CA, NY)
    """
    code, error = normalize_state(state)
    if error or code is None:
        raise ToolError(error or "Invalid state code.")

    result = await make_nws_request(f"{NWS_API_BASE}/alerts/active/area/{code}")
    if result.error:
        if result.status_code in (400, 404):
            raise ToolError(
                f"{code!r} is not a state or territory the weather service recognizes."
            )
        raise ToolError(result.error)

    features = (result.data or {}).get("features")
    if not isinstance(features, list):
        raise ToolError("The alerts response was missing its list of alerts.")
    if not features:
        return f"No active weather alerts for {code}."

    return "\n---\n".join(format_alert(f) for f in features)



# Entry point

def main() -> None:
    logger.info("Starting weather MCP server (stdio)")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
