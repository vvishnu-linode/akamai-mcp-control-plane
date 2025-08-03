#!/usr/bin/env python3
"""
MCP Control Plane Server

A FastAPI-based control plane that manages MCP server connections and provides
centralized access control, authentication, and request routing for MCP clients.

This server receives HTTPS requests from MCP bridge clients and forwards them
to appropriate MCP servers via stdio connections.
"""

import asyncio
import json
import logging
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Dict, List, Optional
from pathlib import Path

import structlog
import uvicorn
from fastapi import FastAPI, HTTPException, Depends, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field

from .config import ControlPlaneConfig
from .mcp_client_pool import MCPClientPool
from .auth import AuthService


# Configure structured logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stderr
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

logger = structlog.get_logger("control_plane")


# Pydantic models for API requests/responses
class MCPInitializeRequest(BaseModel):
    """MCP initialize request from bridge client"""
    method: str = "initialize"
    params: Dict[str, Any]
    id: Optional[str] = None


class MCPToolsListRequest(BaseModel):
    """MCP tools/list request from bridge client"""
    method: str = "tools/list"
    params: Dict[str, Any] = Field(default_factory=dict)
    id: Optional[str] = None


class MCPToolCallRequest(BaseModel):
    """MCP tools/call request from bridge client"""
    method: str = "tools/call"
    params: Dict[str, Any]
    id: Optional[str] = None


class MCPResourcesListRequest(BaseModel):
    """MCP resources/list request from bridge client"""
    method: str = "resources/list"
    params: Dict[str, Any] = Field(default_factory=dict)
    id: Optional[str] = None


class MCPPromptsListRequest(BaseModel):
    """MCP prompts/list request from bridge client"""
    method: str = "prompts/list"
    params: Dict[str, Any] = Field(default_factory=dict)
    id: Optional[str] = None


class MCPResponse(BaseModel):
    """Standard MCP response"""
    jsonrpc: str = "2.0"
    id: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    error: Optional[Dict[str, Any]] = None


class HealthResponse(BaseModel):
    """Health check response"""
    status: str
    timestamp: datetime
    version: str
    mcp_servers: Dict[str, str]


# Global instances
config: Optional[ControlPlaneConfig] = None
mcp_pool: Optional[MCPClientPool] = None
auth_service: Optional[AuthService] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager"""
    global config, mcp_pool, auth_service
    
    logger.info("Starting MCP Control Plane server")
    
    try:
        # Load configuration
        config = ControlPlaneConfig.load()
        logger.info("Configuration loaded", server_count=len(config.mcp_servers))
        
        # Initialize authentication service
        auth_service = AuthService(config.auth_tokens)
        
        # Initialize MCP client pool
        mcp_pool = MCPClientPool(config.mcp_servers)
        await mcp_pool.start()
        
        logger.info("Control plane server started successfully")
        yield
        
    except Exception as e:
        logger.error("Failed to start control plane server", error=str(e))
        raise
    finally:
        # Cleanup
        if mcp_pool:
            await mcp_pool.stop()
        logger.info("Control plane server shutdown complete")


# Create FastAPI application
app = FastAPI(
    title="MCP Control Plane",
    description="Centralized management server for Model Context Protocol (MCP) servers",
    version="1.0.0",
    lifespan=lifespan
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure appropriately for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Security
security = HTTPBearer(auto_error=False)


async def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
) -> str:
    """Authenticate the request using Bearer token"""
    if not credentials or not credentials.credentials:
        logger.warning("Missing authentication token", client_ip=request.client.host)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    if not auth_service or not auth_service.validate_token(credentials.credentials):
        logger.warning("Invalid authentication token", client_ip=request.client.host)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    return credentials.credentials


# API Endpoints

@app.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    """Health check endpoint"""
    mcp_status = {}
    if mcp_pool:
        mcp_status = await mcp_pool.get_status()
    
    return HealthResponse(
        status="healthy",
        timestamp=datetime.now(),
        version="1.0.0",
        mcp_servers=mcp_status
    )


@app.post("/mcp/initialize", response_model=MCPResponse)
async def mcp_initialize(
    request: MCPInitializeRequest,
    token: str = Depends(get_current_user)
) -> MCPResponse:
    """Handle MCP initialize request"""
    logger.info("MCP initialize request", client_token=token[:10] + "...")
    
    # For initialize, we return our own capabilities
    return MCPResponse(
        id=request.id,
        result={
            "protocolVersion": "2025-06-18",
            "capabilities": {
                "tools": {},
                "resources": {},
                "prompts": {}
            },
            "serverInfo": {
                "name": "MCP Control Plane",
                "version": "1.0.0"
            }
        }
    )


@app.get("/mcp/tools", response_model=MCPResponse)
async def mcp_tools_list(
    request_id: Optional[str] = None,
    token: str = Depends(get_current_user)
) -> MCPResponse:
    """List all available tools from MCP servers"""
    logger.info("Tools list request", client_token=token[:10] + "...")
    
    if not mcp_pool:
        raise HTTPException(status_code=500, detail="MCP pool not initialized")
    
    try:
        # Aggregate tools from all MCP servers
        all_tools = await mcp_pool.get_all_tools()
        
        return MCPResponse(
            id=request_id,
            result={"tools": all_tools}
        )
    except Exception as e:
        logger.error("Error listing tools", error=str(e))
        return MCPResponse(
            id=request_id,
            error={"code": -32603, "message": f"Internal error: {str(e)}"}
        )


@app.post("/mcp/tools/call", response_model=MCPResponse)
async def mcp_tool_call(
    request: MCPToolCallRequest,
    token: str = Depends(get_current_user)
) -> MCPResponse:
    """Execute a tool call via appropriate MCP server"""
    tool_name = request.params.get("name")
    arguments = request.params.get("arguments", {})
    
    logger.info("Tool call request", tool=tool_name, client_token=token[:10] + "...")
    
    if not mcp_pool:
        raise HTTPException(status_code=500, detail="MCP pool not initialized")
    
    if not tool_name:
        return MCPResponse(
            id=request.id,
            error={"code": -32602, "message": "Missing tool name"}
        )
    
    try:
        # Execute tool via MCP pool
        result = await mcp_pool.call_tool(tool_name, arguments)
        
        return MCPResponse(
            id=request.id,
            result=result
        )
    except Exception as e:
        logger.error("Error executing tool", tool=tool_name, error=str(e))
        return MCPResponse(
            id=request.id,
            error={"code": -32603, "message": f"Tool execution failed: {str(e)}"}
        )


@app.get("/mcp/resources", response_model=MCPResponse)
async def mcp_resources_list(
    request_id: Optional[str] = None,
    token: str = Depends(get_current_user)
) -> MCPResponse:
    """List all available resources from MCP servers"""
    logger.info("Resources list request", client_token=token[:10] + "...")
    
    if not mcp_pool:
        raise HTTPException(status_code=500, detail="MCP pool not initialized")
    
    try:
        # Aggregate resources from all MCP servers
        all_resources = await mcp_pool.get_all_resources()
        
        return MCPResponse(
            id=request_id,
            result={"resources": all_resources}
        )
    except Exception as e:
        logger.error("Error listing resources", error=str(e))
        return MCPResponse(
            id=request_id,
            error={"code": -32603, "message": f"Internal error: {str(e)}"}
        )


@app.get("/mcp/prompts", response_model=MCPResponse)
async def mcp_prompts_list(
    request_id: Optional[str] = None,
    token: str = Depends(get_current_user)
) -> MCPResponse:
    """List all available prompts from MCP servers"""
    logger.info("Prompts list request", client_token=token[:10] + "...")
    
    if not mcp_pool:
        raise HTTPException(status_code=500, detail="MCP pool not initialized")
    
    try:
        # Aggregate prompts from all MCP servers
        all_prompts = await mcp_pool.get_all_prompts()
        
        return MCPResponse(
            id=request_id,
            result={"prompts": all_prompts}
        )
    except Exception as e:
        logger.error("Error listing prompts", error=str(e))
        return MCPResponse(
            id=request_id,
            error={"code": -32603, "message": f"Internal error: {str(e)}"}
        )


def main():
    """Main entry point for the control plane server"""
    import argparse
    
    parser = argparse.ArgumentParser(description="MCP Control Plane Server")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--port", type=int, default=8443, help="Port to bind to")
    parser.add_argument("--config", help="Path to configuration file")
    parser.add_argument("--log-level", default="INFO", help="Log level")
    
    args = parser.parse_args()
    
    # Configure logging level
    logging.getLogger().setLevel(getattr(logging, args.log_level.upper()))
    
    logger.info("Starting MCP Control Plane server", host=args.host, port=args.port)
    
    # Run the server
    uvicorn.run(
        "control_plane_server:app",
        host=args.host,
        port=args.port,
        log_level=args.log_level.lower(),
        access_log=True,
        reload=False
    )


if __name__ == "__main__":
    main()