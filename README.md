# laughing-octo-guacamole

A minimal Python 3.11+ WebSocket server and client demo using `uv` for dependency management.

## Features

- **WebSocket Server**: Accepts connections, logs all incoming messages verbatim, and replies with a static JSON message
- **WebSocket Client**: Connects to the server, sends a hardcoded JSON payload, prints the response, and exits
- **Modern Dependency Management**: Uses `uv` for fast, reliable Python package and environment management

## Prerequisites

- Python 3.11 or higher
- `uv` (Python package manager)

### Installing uv

If you don't have `uv` installed, you can install it using:

```bash
# On macOS and Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# On Windows
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"

# Or using pip
pip install uv
```

For more installation options, visit: https://docs.astral.sh/uv/getting-started/installation/

## Setup

1. Clone the repository:
```bash
git clone https://github.com/scaryPonens/laughing-octo-guacamole.git
cd laughing-octo-guacamole
```

2. Create a virtual environment and install dependencies using `uv`:
```bash
uv sync
```

This will create a `.venv` directory and install all required dependencies.

## Usage

### Running the WebSocket Server

In one terminal, start the server:

```bash
# Using uv run
uv run python server.py

# Or activate the virtual environment and run directly
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
python server.py
```

The server will start on `ws://localhost:8765` and log all activity.

### Running the WebSocket Client

In another terminal, run the client:

```bash
# Using uv run
uv run python client.py

# Or activate the virtual environment and run directly
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
python client.py
```

The client will:
1. Connect to the server
2. Send a hardcoded JSON payload
3. Print the server's response
4. Exit

## Example Output

**Server output:**
```
2024-01-17 12:00:00 - INFO - Starting WebSocket server on ws://localhost:8765
2024-01-17 12:00:00 - INFO - Server is running. Press Ctrl+C to stop.
2024-01-17 12:00:05 - INFO - Client connected from ('127.0.0.1', 54321)
2024-01-17 12:00:05 - INFO - Received message: {"action": "test", "data": {...}}
2024-01-17 12:00:05 - INFO - Sent response: {"status": "ok", "message": "Message received", "server": "websocket-demo"}
```

**Client output:**
```
Connecting to ws://localhost:8765...
Connected!

Sending: {"action": "test", "data": {"user": "demo", "timestamp": "2024-01-17T12:00:00Z", "message": "Hello from WebSocket client"}}

Received: {"status": "ok", "message": "Message received", "server": "websocket-demo"}

Parsed response:
{
  "status": "ok",
  "message": "Message received",
  "server": "websocket-demo"
}

Connection closed.
```

## Project Structure

```
.
├── README.md           # This file
├── pyproject.toml      # Project configuration and dependencies
├── server.py           # WebSocket server implementation
└── client.py           # WebSocket client implementation
```

## Development

### Adding Dependencies

To add new dependencies:

```bash
uv add <package-name>
```

### Updating Dependencies

To update all dependencies:

```bash
uv sync --upgrade
```

## License

See LICENSE file for details.