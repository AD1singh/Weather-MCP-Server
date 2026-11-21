# Weather MCP Server

A small [Model Context Protocol](https://modelcontextprotocol.io) (MCP) server, written in Python, that lets an AI assistant look up US weather forecasts and active weather alerts. It uses the free [National Weather Service API](https://www.weather.gov/documentation/services-web-api) and talks to the AI client over stdio.

**Limitation:** the National Weather Service only covers the United States (including territories). Requests for places outside the US return a clear error message.

## Tools

| Tool | Input | What it returns |
|---|---|---|
| `get_forecast` | `latitude` (float, -90 to 90), `longitude` (float, -180 to 180) | The next five forecast periods: temperature, temperature unit, wind speed, wind direction, detailed forecast |
| `get_alerts` | `state` (two-letter US code, for example `CA`; case does not matter) | Each active alert: event, area, severity, description, instructions. If there are none, it says so |

Failures (bad input, network problem, timeout, unsupported location, unexpected API data) are reported to the client as tool errors (`isError: true`) with a readable message. The server itself does not crash.

## Requirements

- Python 3.13
- [uv](https://docs.astral.sh/uv/)
- Node.js (only for MCP Inspector)

## Install

```bash
cd weather-mcp-server
uv sync
```

`uv sync` creates `.venv/` and installs everything listed in `pyproject.toml` and `uv.lock`.

## Run

```bash
uv run weather.py
```

The server starts and silently waits for an MCP client to talk to it over stdin/stdout. Nothing else is printed; that is normal. Logs go to stderr. Press Ctrl+C to stop.

## Test

Automated tests (no internet needed; HTTP is mocked):

```bash
uv run pytest -v
```

### Try the real tools with MCP Inspector

Inspector is an official tool for calling an MCP server by hand.

Interactive web UI:

```bash
npx @modelcontextprotocol/inspector uv run weather.py
```

Open the URL it prints. Click **Connect**, open the **Tools** tab, click **List Tools** (you should see `get_forecast` and `get_alerts`), choose a tool, fill in the inputs, and click **Run Tool**.

Good inputs to try:

- `get_forecast` with latitude `38.8977`, longitude `-77.0365` (Washington, DC)
- `get_alerts` with state `CA`
- Bad inputs: latitude `999`, or state `C1`. You should get an error message, not a crash

Command line version (good for scripts):

```bash
npx @modelcontextprotocol/inspector --cli uv run weather.py --method tools/list
npx @modelcontextprotocol/inspector --cli uv run weather.py --method tools/call \
  --tool-name get_alerts --tool-arg state=CA
```

## Connect to an MCP host (Claude Desktop)

Open the Claude Desktop config file:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Linux: `~/.config/Claude/claude_desktop_config.json`
- Windows: `%AppData%\Claude\claude_desktop_config.json`

Add the server (use the absolute path to this folder; on Windows use `\\` or `/` in the path):

```json
{
  "mcpServers": {
    "weather": {
      "command": "uv",
      "args": [
        "--directory",
        "/ABSOLUTE/PATH/TO/weather-mcp-server",
        "run",
        "weather.py"
      ]
    }
  }
}
```

Save the file and restart Claude Desktop. If it cannot find `uv`, put the full path in `command` (run `which uv` on macOS/Linux or `where uv` on Windows).

## How it works

```
weather-mcp-server/
├── weather.py          the whole server
├── pyproject.toml      project metadata and dependencies
├── uv.lock             exact locked versions (commit this)
├── tests/
│   └── test_weather.py automated tests
└── README.md
```

Inside `weather.py`, top to bottom:

1. **Logging setup.** Python's standard `logging`, pointed at stderr. stdout is the MCP message channel, so printing to it would corrupt the protocol. Never use `print()` in this server.
2. **`MCPServer("weather")`.** The server object from the official SDK (v2). It reads each tool function's type hints and docstring and builds the tool description the AI client sees.
3. **Validation functions.** `validate_coordinates` and `normalize_state` are plain functions with no network calls, so they are quick to test. They run before any request is made.
4. **`make_nws_request`.** One reusable async helper for every API call. It sends a `User-Agent` (NWS asks for one), uses a 20 second timeout, and turns every failure (timeout, connection error, HTTP error, invalid JSON, unexpected shape) into a result with an error message instead of an exception.
5. **`get_forecast`.** Calls `/points/{lat},{lon}`, reads the forecast URL from the answer, fetches that URL, and formats the first five periods. Coordinates are rounded to four decimals because NWS redirects longer ones.
6. **`get_alerts`.** Calls `/alerts/active/area/{STATE}` and formats each alert. An empty list is a normal result: "No active weather alerts for CA."
7. **`mcp.run(transport="stdio")`.** Starts the server and keeps it listening.

`@mcp.tool()` is a decorator that registers a function as an MCP tool. `async def` means the function can wait on the network without blocking the server.

### Dependencies

- `mcp[cli]>=2,<3`: the official MCP Python SDK, version 2.
- `httpx2`: the HTTP client. SDK v2 depends on `httpx2` and no longer installs the older `httpx` package, so this project imports `httpx2` directly and lists it explicitly.
- Dev only: `pytest`, `pytest-asyncio`.

Note for v1 users: v1's `FastMCP` class (`mcp.server.fastmcp`) no longer exists. v2 uses `MCPServer` (`mcp.server.mcpserver`).

## Troubleshooting

| Problem | Likely cause and fix |
|---|---|
| Server seems to hang after `uv run weather.py` | Normal. It is waiting for an MCP client. |
| Client shows garbled or broken protocol errors | Something wrote to stdout. Remove any `print()`; use `logger` instead. |
| Claude Desktop does not show the tools | Check the path in the config is absolute and correct, use the full path to `uv`, then fully restart Claude Desktop. |
| `ModuleNotFoundError: mcp.server.fastmcp` | v1 import in v2 code. Use `from mcp.server.mcpserver import MCPServer`. |
| `ModuleNotFoundError: httpx` | v2 no longer installs it. Use `httpx2`. |
| "Could not connect to the weather service" | No internet, or a proxy or firewall is blocking `api.weather.gov`. |
| "weather service timed out" | NWS is slow or down. Try again in a moment. |
| "No forecast available for this location" | The point is outside NWS coverage (not in the US). |
| "HTTP 5xx" errors | The NWS API is having problems. Try again later. |
| Tests fail on import | Run them from the project folder with `uv run pytest`. |
