# MCP Control Plane

A FastAPI-based control plane that manages MCP server connections and provides centralized access control, authentication, and request routing for MCP clients.

## Features

- Centralized MCP server management
- Bearer token authentication
- Request routing and load balancing
- Health monitoring and auto-restart
- YAML-based configuration
- RESTful API for MCP operations

## Quick Start

```bash
# Install dependencies
uv sync

# Run the server
uv run python src/control_plane_server.py

# Or using uvicorn directly
uv run uvicorn src.control_plane_server:app --host 0.0.0.0 --port 8444
```

## Configuration

See `config/control_plane.yaml` for configuration options.

## API Endpoints

- `GET /health` - Health check
- `POST /mcp/initialize` - MCP initialization
- `GET /mcp/tools` - List available tools
- `POST /mcp/tools/call` - Execute tools
- `GET /mcp/resources` - List resources
- `GET /mcp/prompts` - List prompts