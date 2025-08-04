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
    control_plane_url: str = "http://localhost:8444"
    auth_token: Optional[str] = None
    timeout_seconds: int = 30
    retry_attempts: int = 3
    log_level: str = "INFO"
    log_file: Optional[str] = None
    
    def __post_init__(self):
        # Load from environment if not set
        if not self.auth_token:
            self.auth_token = os.getenv("MCP_AUTH_TOKEN")
        # Only override URL if explicitly set and different from default
        if env_url := os.getenv("MCP_CONTROL_PLANE_URL"):
            if env_url != "https://localhost:8443":  # Don't override if it's the old default
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
            # Route different methods to appropriate endpoints
            if message.method == "initialize":
                return await self._call_control_plane_initialize(message)
            elif message.method == "tools/list":
                return await self._call_control_plane_get(f"/mcp/tools?request_id={message.id}")
            elif message.method == "tools/call":
                return await self._call_control_plane_post("/mcp/tools/call", message)
            elif message.method == "resources/list":
                return await self._call_control_plane_get(f"/mcp/resources?request_id={message.id}")
            elif message.method == "prompts/list":
                return await self._call_control_plane_get(f"/mcp/prompts?request_id={message.id}")
            else:
                return MCPMessage(
                    id=message.id,
                    error={
                        "code": -32601,
                        "message": f"Method not found: {message.method}"
                    }
                )
                
        except Exception as e:
            self.logger.error("Error forwarding to control plane", error=str(e), method=message.method)
            # Return error response instead of raising
            return MCPMessage(
                id=message.id,
                error={
                    "code": -32603,
                    "message": f"Control plane error: {str(e)}"
                }
            )
    
    async def _call_control_plane_initialize(self, message: MCPMessage) -> MCPMessage:
        """Handle initialize request to control plane"""
        try:
            request_data = {
                "method": message.method,
                "params": message.params or {},
                "id": message.id
            }
            
            response = await self.session.post(
                f"{self.config.control_plane_url}/mcp/initialize",
                json=request_data
            )
            response.raise_for_status()
            
            result = response.json()
            return MCPMessage.model_validate(result)
            
        except Exception as e:
            self.logger.error("Initialize request failed", error=str(e))
            raise
    
    async def _call_control_plane_post(self, endpoint: str, message: MCPMessage) -> MCPMessage:
        """Make POST request to control plane"""
        try:
            request_data = {
                "method": message.method,
                "params": message.params or {},
                "id": message.id
            }
            
            response = await self.session.post(
                f"{self.config.control_plane_url}{endpoint}",
                json=request_data
            )
            response.raise_for_status()
            
            result = response.json()
            return MCPMessage.model_validate(result)
            
        except Exception as e:
            self.logger.error("POST request failed", endpoint=endpoint, error=str(e))
            raise
    
    async def _call_control_plane_get(self, endpoint: str) -> MCPMessage:
        """Make GET request to control plane"""
        try:
            response = await self.session.get(
                f"{self.config.control_plane_url}{endpoint}"
            )
            response.raise_for_status()
            
            result = response.json()
            return MCPMessage.model_validate(result)
            
        except Exception as e:
            self.logger.error("GET request failed", endpoint=endpoint, error=str(e))
            raise
    
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
        control_plane_url: str = typer.Option("http://localhost:8444", help="Control plane URL"),
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