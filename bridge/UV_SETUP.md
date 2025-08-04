# UV-Based MCP Bridge Setup

The MCP Bridge now uses [UV](https://github.com/astral-sh/uv) for fast, reliable dependency management. This eliminates the dependency issues you experienced with Claude Desktop.

## Prerequisites

1. **Install UV** (if not already installed):
   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

2. **Verify UV installation**:
   ```bash
   uv --version
   ```

## Setup

1. **Install dependencies**:
   ```bash
   cd bridge
   uv sync
   ```

2. **Test the bridge**:
   ```bash
   uv run python src/bridge_client.py --help
   ```

## Claude Desktop Configuration

Add this configuration to your Claude Desktop config file:

**Location:** `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS)

```json
{
  "mcpServers": {
    "mcp-bridge": {
      "command": "uv",
      "args": [
        "run",
        "--project",
        "/Users/vvishnu/repository/personal/mcp_control_plane/bridge",
        "python",
        "src/bridge_client.py"
      ],
      "env": {
        "MCP_CONTROL_PLANE_URL": "https://localhost:8444",
        "MCP_AUTH_TOKEN": "dev-token-secure-string"
      },
      "stderr": true
    }
  }
}
```

**Important:** Update the path `/Users/vvishnu/repository/personal/mcp_control_plane/bridge` to match your actual installation directory.

## How It Works

1. **Claude Desktop** spawns the `uv` command as a subprocess
2. **UV** automatically creates/activates a virtual environment
3. **UV** installs all dependencies from `pyproject.toml`
4. **UV** runs the bridge with all dependencies available
5. **Bridge** communicates with Claude Desktop via stdio

## Benefits of UV Approach

- ✅ **Fast dependency resolution** (Rust-based)
- ✅ **Automatic virtual environment** management
- ✅ **Reproducible builds** with lock files
- ✅ **No manual activation** required
- ✅ **Works seamlessly** with Claude Desktop
- ✅ **Eliminates "module not found"** errors

## Available Tools

Once running, the bridge provides these mock tools for testing:

1. **`query_database`** - Execute SQL queries
2. **`read_file`** - Read file contents
3. **`send_slack_message`** - Send Slack messages
4. **`get_weather`** - Get weather information
5. **`search_codebase`** - Search code patterns
6. **`create_jira_ticket`** - Create JIRA tickets

## Troubleshooting

### UV not found
```bash
# Check if UV is in PATH
which uv

# If not found, add to PATH
export PATH="$HOME/.local/bin:$PATH"
```

### Permission issues
```bash
# Make sure UV is executable
chmod +x ~/.local/bin/uv
```

### Dependencies not syncing
```bash
# Force resync
uv sync --force
```

### Claude Desktop connection issues
1. Check the path in `claude_desktop_config.json` is correct
2. Restart Claude Desktop after config changes
3. Check logs in Claude Desktop for error details

## Development

### Run tests
```bash
uv run python tests/test_bridge.py
```

### Run demo
```bash
uv run python demo_interaction.py
```

### Add new dependencies
```bash
# Add to pyproject.toml, then:
uv sync
```

## Project Structure

```
bridge/
├── src/
│   └── bridge_client.py      # Main bridge implementation
├── tests/
│   └── test_bridge.py        # Test suite
├── config/
│   └── claude_desktop_config.json  # Claude Desktop configuration
├── pyproject.toml            # UV project configuration
├── uv.lock                   # UV lock file (auto-generated)
└── README.md                 # Main documentation
```

## Next Steps

With UV setup complete, the bridge is ready for:

1. **Claude Desktop integration testing**
2. **Real MCP server development** (Phase 2)
3. **Control plane server implementation**
4. **Production deployment**

The dependency management issues are now resolved! 🎉