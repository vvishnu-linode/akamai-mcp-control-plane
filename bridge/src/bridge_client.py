#!/usr/bin/env python3
"""
MCP Bridge Client

A lightweight client that bridges stdio communication from AI clients (Claude Desktop, Cursor)
to HTTPS communication with the MCP Control Plane.

This bridge acts as a transparent proxy, maintaining full compatibility with the MCP protocol
while adding centralized management capabilities.
"""

import asyncio
import json
import logging
import sys
from typing import Any, Dict, Optional, Union
import signal
import os
from dataclasses import dataclass, field
from datetime import datetime

import structlog
import typer
from pydantic import BaseModel, ValidationError


@dataclass
class BridgeConfig:
    """Configuration for the MCP Bridge Client"""
    control_plane_url: str = "https://localhost:8443"
    auth_token: Optional[str] = None
    timeout_seconds: int = 30
    retry_attempts: int = 3
    log_level: str = "INFO"
    log_file: Optional[str] = None
    
    def __post_init__(self):
        # Load from environment if not set
        if not self.auth_token:
            self.auth_token = os.getenv("MCP_AUTH_TOKEN")
        if env_url := os.getenv("MCP_CONTROL_PLANE_URL"):
            self.control_plane_url = env_url


class MCPMessage(BaseModel):
    """MCP protocol message structure"""
    jsonrpc: str = "2.0"
    id: Optional[Union[str, int]] = None
    method: Optional[str] = None
    params: Optional[Dict[str, Any]] = None
    result: Optional[Any] = None
    error: Optional[Dict[str, Any]] = None


class MCPBridgeClient:
    """
    MCP Bridge Client that translates stdio MCP messages to HTTPS requests
    to the control plane.
    """
    
    def __init__(self, config: BridgeConfig):
        self.config = config
        self.logger = self._setup_logging()
        self.running = False
        self.session = None
        
        # Message correlation
        self.pending_requests: Dict[str, asyncio.Future] = {}
        
        # Performance tracking
        self.stats = {
            "messages_processed": 0,
            "errors": 0,
            "start_time": datetime.now()
        }
    
    def _setup_logging(self) -> structlog.stdlib.BoundLogger:
        """Configure structured logging"""
        logging.basicConfig(
            level=getattr(logging, self.config.log_level.upper()),
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            handlers=[
                logging.StreamHandler(sys.stderr),
                *([] if not self.config.log_file else [logging.FileHandler(self.config.log_file)])
            ]
        )
        
        structlog.configure(
            processors=[
                structlog.stdlib.filter_by_level,
                structlog.stdlib.add_logger_name,
                structlog.stdlib.add_log_level,
                structlog.stdlib.PositionalArgumentsFormatter(),
                structlog.processors.StackInfoRenderer(),
                structlog.processors.format_exc_info,
                structlog.processors.UnicodeDecoder(),
                structlog.processors.JSONRenderer()
            ],
            context_class=dict,
            logger_factory=structlog.stdlib.LoggerFactory(),
            wrapper_class=structlog.stdlib.BoundLogger,
            cache_logger_on_first_use=True,
        )
        
        return structlog.get_logger("mcp.bridge")
    
    async def start(self):
        """Start the bridge client"""
        self.logger.info("Starting MCP Bridge Client", config=self.config.__dict__)
        
        try:
            # Set up signal handlers for graceful shutdown
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, self._signal_handler)
            
            self.running = True
            
            # Initialize HTTP session for control plane communication
            await self._init_http_session()
            
            # Start the main message loop
            await self._message_loop()
            
        except Exception as e:
            self.logger.error("Bridge client failed to start", error=str(e), exc_info=True)
            raise
        finally:
            await self._cleanup()
    
    def _signal_handler(self, signum, frame):
        """Handle shutdown signals gracefully"""
        self.logger.info("Received shutdown signal", signal=signum)
        self.running = False
    
    async def _init_http_session(self):
        """Initialize HTTP session for control plane communication"""
        import httpx
        
        try:
            self.session = httpx.AsyncClient(
            timeout=httpx.Timeout(self.config.timeout_seconds),
            headers={
                "Authorization": f"Bearer {self.config.auth_token}",
                "Content-Type": "application/json",
                "User-Agent": "MCP-Bridge/1.0"
            }
        )
            self.logger.info("HTTP session initialized", url=self.config.control_plane_url)
        except Exception as e:
            self.logger.error("Failed to initialize HTTP session", error=str(e))
            raise

    
    async def _message_loop(self):
        """Main message processing loop"""
        self.logger.info("Starting message loop")
        
        try:
            while self.running:
                # Read message from stdin
                line = await asyncio.get_event_loop().run_in_executor(
                    None, sys.stdin.readline
                )
                
                if not line:
                    self.logger.info("EOF received, shutting down")
                    break
                
                line = line.strip()
                if not line:
                    continue
                
                await self._process_message(line)
                
        except asyncio.CancelledError:
            self.logger.info("Message loop cancelled")
        except Exception as e:
            self.logger.error("Error in message loop", error=str(e), exc_info=True)
            self.stats["errors"] += 1
    
    async def _process_message(self, raw_message: str):
        """Process a single MCP message"""
        try:
            # Parse JSON-RPC message
            message_data = json.loads(raw_message)
            message = MCPMessage.model_validate(message_data)
            
            self.logger.debug("Processing message", method=message.method, id=message.id)
            self.stats["messages_processed"] += 1
            
            # Handle different message types
            if message.method:
                # Check if this is a notification (no id field)
                if message.id is None:
                    # This is a notification - just log it, no response needed
                    self.logger.debug("Received notification", method=message.method)
                    if message.method == "notifications/initialized":
                        self.logger.info("Client initialized successfully")
                    return
                else:
                    # This is a request - forward to control plane
                    await self._handle_request(message)
            elif message.result is not None or message.error is not None:
                # This is a response - handle correlation
                await self._handle_response(message)
            else:
                self.logger.warning("Unknown message type", message=message_data)
        
        except json.JSONDecodeError as e:
            self.logger.error("Invalid JSON received", error=str(e), raw_message=raw_message)
            if self.running:
                await self._send_error_response(None, -32700, "Parse error")
        except ValidationError as e:
            self.logger.error("Invalid MCP message", error=str(e), raw_message=raw_message)
            if self.running:
                await self._send_error_response(None, -32600, "Invalid Request")
        except Exception as e:
            self.logger.error("Error processing message", error=str(e), exc_info=True)
            self.stats["errors"] += 1
    
    async def _handle_request(self, message: MCPMessage):
        """Handle MCP request by forwarding to control plane"""
        try:
            # Forward request to control plane
            response = await self._forward_to_control_plane(message)
            
            # Send response back to client
            await self._send_response(response)
            
        except Exception as e:
            self.logger.error("Error handling request", error=str(e), method=message.method)
            # Only send error response if we're still running (avoid broken pipe cascades)
            if self.running:
                await self._send_error_response(
                    message.id, -32603, f"Internal error: {str(e)}"
                )
    
    async def _handle_response(self, message: MCPMessage):
        """Handle MCP response (for future use with bidirectional communication)"""
        if message.id and str(message.id) in self.pending_requests:
            future = self.pending_requests.pop(str(message.id))
            future.set_result(message)
        else:
            self.logger.warning("Received response for unknown request", id=message.id)
    
    async def _forward_to_control_plane(self, message: MCPMessage) -> MCPMessage:
        """Forward MCP message to control plane and return response"""
        if not self.session:
            raise RuntimeError("HTTP session not initialized")
        
        try:
            # For now, implement a simple pass-through
            # In future phases, this will be actual HTTP communication
            # Currently returning a basic response for testing
            
            if message.method == "initialize":
                return MCPMessage(
                    id=message.id,
                    result={
                        "protocolVersion": "2025-06-18",
                        "capabilities": {
                            "tools": {},
                            "resources": {},
                            "prompts": {}
                        },
                        "serverInfo": {
                            "name": "MCP Bridge",
                            "version": "1.0.0"
                        }
                    }
                )
            elif message.method == "tools/list":
                return MCPMessage(
                    id=message.id,
                    result={
                        "tools": [
                            {
                                "name": "query_database",
                                "description": "Execute SQL queries against the company database",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {
                                        "query": {
                                            "type": "string",
                                            "description": "SQL query to execute"
                                        },
                                        "database": {
                                            "type": "string",
                                            "description": "Database name (optional)",
                                            "default": "main"
                                        }
                                    },
                                    "required": ["query"]
                                }
                            },
                            {
                                "name": "read_file",
                                "description": "Read contents of a file from the filesystem",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {
                                        "file_path": {
                                            "type": "string",
                                            "description": "Path to the file to read"
                                        },
                                        "encoding": {
                                            "type": "string",
                                            "description": "File encoding",
                                            "default": "utf-8"
                                        }
                                    },
                                    "required": ["file_path"]
                                }
                            },
                            {
                                "name": "send_slack_message",
                                "description": "Send a message to a Slack channel",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {
                                        "channel": {
                                            "type": "string",
                                            "description": "Slack channel name or ID"
                                        },
                                        "message": {
                                            "type": "string",
                                            "description": "Message content to send"
                                        },
                                        "thread_ts": {
                                            "type": "string",
                                            "description": "Timestamp of parent message (for replies)",
                                            "optional": True
                                        }
                                    },
                                    "required": ["channel", "message"]
                                }
                            },
                            {
                                "name": "get_weather",
                                "description": "Get current weather information for a location",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {
                                        "location": {
                                            "type": "string",
                                            "description": "City name or coordinates"
                                        },
                                        "units": {
                                            "type": "string",
                                            "description": "Temperature units",
                                            "enum": ["celsius", "fahrenheit"],
                                            "default": "celsius"
                                        }
                                    },
                                    "required": ["location"]
                                }
                            },
                            {
                                "name": "search_codebase",
                                "description": "Search for code patterns across the repository",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {
                                        "pattern": {
                                            "type": "string",
                                            "description": "Search pattern or regex"
                                        },
                                        "file_types": {
                                            "type": "array",
                                            "items": {"type": "string"},
                                            "description": "File extensions to search",
                                            "default": ["py", "js", "ts", "go", "java"]
                                        },
                                        "case_sensitive": {
                                            "type": "boolean",
                                            "description": "Whether search should be case sensitive",
                                            "default": False
                                        }
                                    },
                                    "required": ["pattern"]
                                }
                            },
                            {
                                "name": "create_jira_ticket",
                                "description": "Create a new JIRA ticket",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {
                                        "project": {
                                            "type": "string",
                                            "description": "JIRA project key"
                                        },
                                        "summary": {
                                            "type": "string",
                                            "description": "Ticket summary/title"
                                        },
                                        "description": {
                                            "type": "string",
                                            "description": "Detailed ticket description"
                                        },
                                        "issue_type": {
                                            "type": "string",
                                            "description": "Type of issue",
                                            "enum": ["Bug", "Task", "Story", "Epic"],
                                            "default": "Task"
                                        },
                                        "priority": {
                                            "type": "string",
                                            "description": "Issue priority",
                                            "enum": ["Low", "Medium", "High", "Critical"],
                                            "default": "Medium"
                                        }
                                    },
                                    "required": ["project", "summary", "description"]
                                }
                            }
                        ]
                    }
                )
            elif message.method == "tools/call":
                # Handle tool execution with mock responses
                tool_name = message.params.get("name") if message.params else None
                arguments = message.params.get("arguments", {}) if message.params else {}
                
                return await self._handle_tool_call(message.id, tool_name, arguments)
            elif message.method == "resources/list":
                return MCPMessage(
                    id=message.id,
                    result={
                        "resources": []
                    }
                )
            elif message.method == "prompts/list":
                return MCPMessage(
                    id=message.id,
                    result={
                        "prompts": []
                    }
                )
            else:
                return MCPMessage(
                    id=message.id,
                    error={
                        "code": -32601,
                        "message": f"Method not found: {message.method}"
                    }
                )
                
        except Exception as e:
            self.logger.error("Error forwarding to control plane", error=str(e))
            raise
    
    async def _handle_tool_call(self, request_id: Union[str, int], tool_name: str, arguments: Dict[str, Any]) -> MCPMessage:
        """Handle tool execution with mock responses for testing"""
        self.logger.info("Handling tool call", tool=tool_name, args=arguments)
        
        try:
            if tool_name == "query_database":
                query = arguments.get("query", "")
                database = arguments.get("database", "main")
                return MCPMessage(
                    id=request_id,
                    result={
                        "content": [
                            {
                                "type": "text",
                                "text": f"Mock database query executed successfully:\nQuery: {query}\nDatabase: {database}\n\nResults:\n+----+----------+-------+\n| id | name     | value |\n+----+----------+-------+\n|  1 | example  | 100   |\n|  2 | test     | 200   |\n+----+----------+-------+\n\n2 rows returned"
                            }
                        ]
                    }
                )
            
            elif tool_name == "read_file":
                file_path = arguments.get("file_path", "")
                encoding = arguments.get("encoding", "utf-8")
                return MCPMessage(
                    id=request_id,
                    result={
                        "content": [
                            {
                                "type": "text",
                                "text": f"Mock file content for: {file_path}\nEncoding: {encoding}\n\n# Example File Content\n\ndef example_function():\n    return 'This is a mock file response'\n\nif __name__ == '__main__':\n    print(example_function())\n"
                            }
                        ]
                    }
                )
            
            elif tool_name == "send_slack_message":
                channel = arguments.get("channel", "")
                message = arguments.get("message", "")
                thread_ts = arguments.get("thread_ts")
                return MCPMessage(
                    id=request_id,
                    result={
                        "content": [
                            {
                                "type": "text",
                                "text": f"Mock Slack message sent successfully!\nChannel: {channel}\nMessage: {message}\n" + 
                                       (f"Thread: {thread_ts}\n" if thread_ts else "") + 
                                       "Message ID: mock_msg_1234567890.123\nTimestamp: 2024-01-15T10:30:00Z"
                            }
                        ]
                    }
                )
            
            elif tool_name == "get_weather":
                location = arguments.get("location", "")
                units = arguments.get("units", "celsius")
                temp_symbol = "°C" if units == "celsius" else "°F"
                temp_value = "22" if units == "celsius" else "72"
                return MCPMessage(
                    id=request_id,
                    result={
                        "content": [
                            {
                                "type": "text",
                                "text": f"Mock weather data for: {location}\n\nCurrent Weather:\n- Temperature: {temp_value}{temp_symbol}\n- Condition: Partly Cloudy\n- Humidity: 65%\n- Wind: 10 km/h NW\n- Pressure: 1013 mb\n- Visibility: 15 km\n\nLast updated: 2024-01-15 10:30 UTC"
                            }
                        ]
                    }
                )
            
            elif tool_name == "search_codebase":
                pattern = arguments.get("pattern", "")
                file_types = arguments.get("file_types", ["py", "js", "ts"])
                case_sensitive = arguments.get("case_sensitive", False)
                return MCPMessage(
                    id=request_id,
                    result={
                        "content": [
                            {
                                "type": "text",
                                "text": f"Mock codebase search results:\nPattern: {pattern}\nFile types: {', '.join(file_types)}\nCase sensitive: {case_sensitive}\n\nResults found in 3 files:\n\n1. src/main.py:45\n   def {pattern}_handler():\n       return 'example match'\n\n2. tests/test_main.py:12\n   # Testing {pattern} functionality\n   assert {pattern}_handler() == 'expected'\n\n3. docs/api.md:89\n   The {pattern} method is used for...\n\nTotal matches: 3 files, 5 occurrences"
                            }
                        ]
                    }
                )
            
            elif tool_name == "create_jira_ticket":
                project = arguments.get("project", "")
                summary = arguments.get("summary", "")
                description = arguments.get("description", "")
                issue_type = arguments.get("issue_type", "Task")
                priority = arguments.get("priority", "Medium")
                return MCPMessage(
                    id=request_id,
                    result={
                        "content": [
                            {
                                "type": "text",
                                "text": f"Mock JIRA ticket created successfully!\n\nTicket Details:\n- Key: {project}-1234\n- Project: {project}\n- Summary: {summary}\n- Type: {issue_type}\n- Priority: {priority}\n- Status: Open\n- Reporter: mock.user@example.com\n- Created: 2024-01-15T10:30:00Z\n\nDescription:\n{description}\n\nURL: https://company.atlassian.net/browse/{project}-1234"
                            }
                        ]
                    }
                )
            
            else:
                return MCPMessage(
                    id=request_id,
                    error={
                        "code": -32602,
                        "message": f"Unknown tool: {tool_name}"
                    }
                )
                
        except Exception as e:
            self.logger.error("Error in tool call", tool=tool_name, error=str(e))
            return MCPMessage(
                id=request_id,
                error={
                    "code": -32603,
                    "message": f"Tool execution failed: {str(e)}"
                }
            )
    
    async def _send_response(self, message: MCPMessage):
        """Send response message to stdout"""
        try:
            response_json = message.model_dump(exclude_none=True)
            response_str = json.dumps(response_json)
            
            print(response_str, flush=True)
            self.logger.debug("Sent response", id=message.id)
        except BrokenPipeError:
            # Client has closed the connection - stop gracefully
            self.logger.info("Client disconnected (broken pipe)")
            self.running = False
        except Exception as e:
            self.logger.error("Error sending response", error=str(e), id=message.id)
            # Don't try to send another error response - could cause infinite loop
    
    async def _send_error_response(self, request_id: Optional[Union[str, int]], 
                                  code: int, message: str):
        """Send error response to stdout"""
        try:
            error_response = MCPMessage(
                id=request_id,
                error={"code": code, "message": message}
            )
            await self._send_response(error_response)
        except Exception as e:
            # If we can't send error response, just log and continue
            self.logger.error("Failed to send error response", error=str(e), request_id=request_id)
    
    async def _cleanup(self):
        """Clean up resources"""
        self.logger.info("Cleaning up bridge client")
        
        if self.session:
            await self.session.aclose()
        
        # Log final statistics
        uptime = datetime.now() - self.stats["start_time"]
        self.logger.info(
            "Bridge client shutdown complete",
            uptime_seconds=uptime.total_seconds(),
            messages_processed=self.stats["messages_processed"],
            errors=self.stats["errors"]
        )


def main():
    """Main entry point for the bridge client"""
    app = typer.Typer()
    
    @app.command()
    def run(
        control_plane_url: str = typer.Option("https://localhost:8443", help="Control plane URL"),
        auth_token: Optional[str] = typer.Option(None, help="Authentication token"),
        log_level: str = typer.Option("INFO", help="Log level"),
        log_file: Optional[str] = typer.Option(None, help="Log file path"),
    ):
        """Run the MCP Bridge Client"""
        config = BridgeConfig(
            control_plane_url=control_plane_url,
            auth_token=auth_token,
            log_level=log_level,
            log_file=log_file
        )
        
        bridge = MCPBridgeClient(config)
        
        try:
            asyncio.run(bridge.start())
        except KeyboardInterrupt:
            print("Bridge client interrupted", file=sys.stderr)
        except Exception as e:
            print(f"Bridge client failed: {e}", file=sys.stderr)
            sys.exit(1)
    
    app()


if __name__ == "__main__":
    main()