"""
MCP Client Pool

Manages connections to multiple MCP servers, handling startup, health monitoring,
and request routing.
"""

import asyncio
import json
import os
import time
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field
from enum import Enum
import structlog

from .config import MCPServerConfig

logger = structlog.get_logger("mcp_pool")


class ServerStatus(Enum):
    """MCP Server status"""
    STARTING = "starting"
    RUNNING = "running"
    FAILED = "failed"
    STOPPED = "stopped"


@dataclass
class MCPServerInstance:
    """Represents a running MCP server instance"""
    config: MCPServerConfig
    process: Optional[asyncio.subprocess.Process] = None
    status: ServerStatus = ServerStatus.STOPPED
    start_time: Optional[float] = None
    failure_count: int = 0
    last_error: Optional[str] = None
    request_count: int = 0
    
    # Communication
    request_id_counter: int = field(default=0)
    pending_requests: Dict[str, asyncio.Future] = field(default_factory=dict)


class MCPClientPool:
    """Pool of MCP server clients"""
    
    def __init__(self, server_configs: List[MCPServerConfig]):
        """
        Initialize MCP client pool
        
        Args:
            server_configs: List of MCP server configurations
        """
        self.server_configs = server_configs
        self.servers: Dict[str, MCPServerInstance] = {}
        self.tool_registry: Dict[str, str] = {}  # tool_name -> server_id
        self.running = False
        
        logger.info("MCP client pool initialized", server_count=len(server_configs))
    
    async def start(self) -> None:
        """Start all MCP servers"""
        logger.info("Starting MCP client pool")
        self.running = True
        
        # Initialize server instances
        for config in self.server_configs:
            if config.enabled:
                self.servers[config.id] = MCPServerInstance(config=config)
        
        # Start all servers
        start_tasks = [
            self._start_server(server_id) 
            for server_id in self.servers.keys()
        ]
        
        if start_tasks:
            await asyncio.gather(*start_tasks, return_exceptions=True)
        
        # Discover tools from all running servers
        await self._discover_tools()
        
        logger.info("MCP client pool started", 
                   running_servers=len([s for s in self.servers.values() if s.status == ServerStatus.RUNNING]))
    
    async def stop(self) -> None:
        """Stop all MCP servers"""
        logger.info("Stopping MCP client pool")
        self.running = False
        
        # Stop all servers
        stop_tasks = [
            self._stop_server(server_id)
            for server_id in self.servers.keys()
        ]
        
        if stop_tasks:
            await asyncio.gather(*stop_tasks, return_exceptions=True)
        
        logger.info("MCP client pool stopped")
    
    async def _start_server(self, server_id: str) -> None:
        """Start a single MCP server"""
        server = self.servers[server_id]
        config = server.config
        
        logger.info("Starting MCP server", server_id=server_id, type=config.type)
        
        try:
            server.status = ServerStatus.STARTING
            
            # Build command
            cmd = config.command + config.args
            
            # Prepare environment
            env = dict(os.environ)
            env.update(config.env)
            
            # Start process
            server.process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=config.cwd
            )
            
            server.start_time = time.time()
            server.status = ServerStatus.RUNNING
            
            # Start background tasks for this server
            asyncio.create_task(self._monitor_server(server_id))
            asyncio.create_task(self._handle_server_output(server_id))
            
            logger.info("MCP server started successfully", server_id=server_id)
            
        except Exception as e:
            logger.error("Failed to start MCP server", server_id=server_id, error=str(e))
            server.status = ServerStatus.FAILED
            server.last_error = str(e)
            server.failure_count += 1
    
    async def _stop_server(self, server_id: str) -> None:
        """Stop a single MCP server"""
        server = self.servers[server_id]
        
        if server.process:
            logger.info("Stopping MCP server", server_id=server_id)
            
            try:
                server.process.terminate()
                await asyncio.wait_for(server.process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("Force killing MCP server", server_id=server_id)
                server.process.kill()
                await server.process.wait()
            
            server.status = ServerStatus.STOPPED
            server.process = None
    
    async def _monitor_server(self, server_id: str) -> None:
        """Monitor server health and restart if needed"""
        server = self.servers[server_id]
        
        while self.running and server.config.enabled:
            if server.process and server.process.returncode is not None:
                # Process has died
                logger.warning("MCP server process died", server_id=server_id, 
                             returncode=server.process.returncode)
                
                server.status = ServerStatus.FAILED
                server.failure_count += 1
                
                if server.config.restart_on_failure and server.failure_count < 5:
                    logger.info("Restarting failed MCP server", server_id=server_id)
                    await asyncio.sleep(min(2 ** server.failure_count, 30))  # Exponential backoff
                    await self._start_server(server_id)
                else:
                    logger.error("MCP server failed permanently", server_id=server_id)
                    break
            
            await asyncio.sleep(10)  # Check every 10 seconds
    
    async def _handle_server_output(self, server_id: str) -> None:
        """Handle output from MCP server"""
        server = self.servers[server_id]
        
        if not server.process or not server.process.stdout:
            return
        
        try:
            while self.running and server.process.returncode is None:
                line = await server.process.stdout.readline()
                if not line:
                    break
                
                try:
                    # Parse JSON-RPC response
                    response = json.loads(line.decode().strip())
                    await self._handle_server_response(server_id, response)
                except json.JSONDecodeError:
                    # Not a JSON response, might be log output
                    logger.debug("Non-JSON output from server", 
                               server_id=server_id, output=line.decode().strip())
                except Exception as e:
                    logger.error("Error handling server output", 
                               server_id=server_id, error=str(e))
        
        except Exception as e:
            logger.error("Error reading server output", server_id=server_id, error=str(e))
    
    async def _handle_server_response(self, server_id: str, response: Dict[str, Any]) -> None:
        """Handle a JSON-RPC response from MCP server"""
        server = self.servers[server_id]
        
        response_id = response.get("id")
        if response_id and str(response_id) in server.pending_requests:
            # This is a response to our request
            future = server.pending_requests.pop(str(response_id))
            future.set_result(response)
        else:
            # This might be a notification or unsolicited response
            logger.debug("Received unsolicited response", 
                        server_id=server_id, response=response)
    
    async def _send_request(self, server_id: str, method: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        """Send a JSON-RPC request to an MCP server"""
        server = self.servers.get(server_id)
        if not server or server.status != ServerStatus.RUNNING or not server.process:
            raise RuntimeError(f"MCP server {server_id} is not running")
        
        # Generate request ID
        server.request_id_counter += 1
        request_id = str(server.request_id_counter)
        
        # Build request
        request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method
        }
        if params:
            request["params"] = params
        
        # Create future for response
        future = asyncio.Future()
        server.pending_requests[request_id] = future
        
        try:
            # Send request
            request_json = json.dumps(request) + "\n"
            server.process.stdin.write(request_json.encode())
            await server.process.stdin.drain()
            
            server.request_count += 1
            
            # Wait for response with timeout
            response = await asyncio.wait_for(future, timeout=30.0)
            return response
            
        except asyncio.TimeoutError:
            server.pending_requests.pop(request_id, None)
            raise RuntimeError(f"Request to {server_id} timed out")
        except Exception as e:
            server.pending_requests.pop(request_id, None)
            raise RuntimeError(f"Error sending request to {server_id}: {str(e)}")
    
    async def _discover_tools(self) -> None:
        """Discover tools from all running servers"""
        logger.info("Discovering tools from MCP servers")
        self.tool_registry.clear()
        
        for server_id, server in self.servers.items():
            if server.status == ServerStatus.RUNNING:
                try:
                    response = await self._send_request(server_id, "tools/list")
                    
                    if "result" in response and "tools" in response["result"]:
                        tools = response["result"]["tools"]
                        for tool in tools:
                            tool_name = tool.get("name")
                            if tool_name:
                                self.tool_registry[tool_name] = server_id
                                logger.debug("Registered tool", tool=tool_name, server=server_id)
                
                except Exception as e:
                    logger.error("Error discovering tools", server_id=server_id, error=str(e))
        
        logger.info("Tool discovery complete", tool_count=len(self.tool_registry))
    
    async def get_all_tools(self) -> List[Dict[str, Any]]:
        """Get all available tools from all servers"""
        all_tools = []
        
        for server_id, server in self.servers.items():
            if server.status == ServerStatus.RUNNING:
                try:
                    response = await self._send_request(server_id, "tools/list")
                    
                    if "result" in response and "tools" in response["result"]:
                        all_tools.extend(response["result"]["tools"])
                
                except Exception as e:
                    logger.error("Error getting tools", server_id=server_id, error=str(e))
        
        return all_tools
    
    async def get_all_resources(self) -> List[Dict[str, Any]]:
        """Get all available resources from all servers"""
        all_resources = []
        
        for server_id, server in self.servers.items():
            if server.status == ServerStatus.RUNNING:
                try:
                    response = await self._send_request(server_id, "resources/list")
                    
                    if "result" in response and "resources" in response["result"]:
                        all_resources.extend(response["result"]["resources"])
                
                except Exception as e:
                    logger.error("Error getting resources", server_id=server_id, error=str(e))
        
        return all_resources
    
    async def get_all_prompts(self) -> List[Dict[str, Any]]:
        """Get all available prompts from all servers"""
        all_prompts = []
        
        for server_id, server in self.servers.items():
            if server.status == ServerStatus.RUNNING:
                try:
                    response = await self._send_request(server_id, "prompts/list")
                    
                    if "result" in response and "prompts" in response["result"]:
                        all_prompts.extend(response["result"]["prompts"])
                
                except Exception as e:
                    logger.error("Error getting prompts", server_id=server_id, error=str(e))
        
        return all_prompts
    
    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a tool call via the appropriate server"""
        server_id = self.tool_registry.get(tool_name)
        if not server_id:
            raise RuntimeError(f"Tool {tool_name} not found")
        
        params = {
            "name": tool_name,
            "arguments": arguments
        }
        
        response = await self._send_request(server_id, "tools/call", params)
        
        if "error" in response:
            raise RuntimeError(f"Tool execution failed: {response['error']}")
        
        return response.get("result", {})
    
    async def get_status(self) -> Dict[str, str]:
        """Get status of all MCP servers"""
        return {
            server_id: server.status.value
            for server_id, server in self.servers.items()
        }