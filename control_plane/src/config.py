"""
Configuration management for MCP Control Plane

Handles loading and validation of YAML configuration files for the control plane server.
"""

import os
from pathlib import Path
from typing import Any, Dict, List, Optional
import yaml
from pydantic import BaseModel, Field, validator
import structlog

logger = structlog.get_logger("config")


class MCPServerConfig(BaseModel):
    """Configuration for a single MCP server"""
    id: str = Field(..., description="Unique identifier for the MCP server")
    name: Optional[str] = Field(None, description="Human-readable name")
    type: str = Field(..., description="Server type: python, npx, or uv")
    command: List[str] = Field(..., description="Command to start the server")
    args: List[str] = Field(default_factory=list, description="Additional arguments")
    cwd: Optional[str] = Field(None, description="Working directory")
    env: Dict[str, str] = Field(default_factory=dict, description="Environment variables")
    timeout: int = Field(30, description="Startup timeout in seconds")
    restart_on_failure: bool = Field(True, description="Restart server on failure")
    enabled: bool = Field(True, description="Whether server is enabled")
    
    @validator('type')
    def validate_type(cls, v):
        if v not in ['python', 'npx', 'uv']:
            raise ValueError('Server type must be one of: python, npx, uv')
        return v
    
    @validator('command')
    def validate_command(cls, v):
        if not v or len(v) == 0:
            raise ValueError('Command cannot be empty')
        return v


class ServerConfig(BaseModel):
    """Configuration for the control plane server itself"""
    host: str = Field("0.0.0.0", description="Host to bind to")
    port: int = Field(8443, description="Port to bind to")
    workers: int = Field(1, description="Number of worker processes")
    log_level: str = Field("INFO", description="Log level")
    cors_origins: List[str] = Field(default_factory=list, description="CORS allowed origins")
    
    @validator('port')
    def validate_port(cls, v):
        if v < 1 or v > 65535:
            raise ValueError('Port must be between 1 and 65535')
        return v
    
    @validator('log_level')
    def validate_log_level(cls, v):
        if v.upper() not in ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']:
            raise ValueError('Invalid log level')
        return v.upper()


class ControlPlaneConfig(BaseModel):
    """Main configuration for the control plane"""
    server: ServerConfig = Field(default_factory=ServerConfig)
    auth_tokens: List[str] = Field(..., description="Valid authentication tokens")
    mcp_servers: List[MCPServerConfig] = Field(default_factory=list, description="MCP servers to manage")
    
    @validator('auth_tokens')
    def validate_auth_tokens(cls, v):
        if not v or len(v) == 0:
            raise ValueError('At least one auth token must be provided')
        return v
    
    @classmethod
    def load(cls, config_path: Optional[str] = None) -> "ControlPlaneConfig":
        """
        Load configuration from file or environment
        
        Args:
            config_path: Path to YAML config file. If None, looks for default locations.
            
        Returns:
            Loaded configuration
        """
        if config_path is None:
            # Look for config in default locations
            possible_paths = [
                "config/control_plane.yaml",
                "/etc/mcp/control_plane.yaml",
                os.path.expanduser("~/.mcp/control_plane.yaml"),
                "control_plane.yaml"
            ]
            
            config_path = None
            for path in possible_paths:
                if Path(path).exists():
                    config_path = path
                    break
        
        if config_path and Path(config_path).exists():
            logger.info("Loading configuration from file", path=config_path)
            with open(config_path, 'r') as f:
                config_data = yaml.safe_load(f)
        else:
            logger.info("No config file found, using environment variables")
            config_data = cls._load_from_env()
        
        return cls.model_validate(config_data)
    
    @classmethod
    def _load_from_env(cls) -> Dict[str, Any]:
        """Load configuration from environment variables"""
        auth_tokens = os.getenv("MCP_AUTH_TOKENS", "dev-token-secure-string").split(",")
        
        config = {
            "server": {
                "host": os.getenv("MCP_HOST", "0.0.0.0"),
                "port": int(os.getenv("MCP_PORT", "8443")),
                "log_level": os.getenv("MCP_LOG_LEVEL", "INFO"),
            },
            "auth_tokens": [token.strip() for token in auth_tokens],
            "mcp_servers": []
        }
        
        # Load MCP servers from environment (basic support)
        if os.getenv("MCP_FILESYSTEM_ENABLED", "false").lower() == "true":
            config["mcp_servers"].append({
                "id": "filesystem",
                "name": "Filesystem MCP Server",
                "type": "python",
                "command": ["python", "-m", "mcp_server.filesystem"],
                "env": {
                    "MCP_FILESYSTEM_ROOT": os.getenv("MCP_FILESYSTEM_ROOT", "/tmp")
                }
            })
        
        return config
    
    def save(self, config_path: str) -> None:
        """Save configuration to file"""
        config_data = self.model_dump()
        
        # Ensure directory exists
        Path(config_path).parent.mkdir(parents=True, exist_ok=True)
        
        with open(config_path, 'w') as f:
            yaml.dump(config_data, f, default_flow_style=False, indent=2)
        
        logger.info("Configuration saved", path=config_path)
    
    def get_enabled_servers(self) -> List[MCPServerConfig]:
        """Get list of enabled MCP servers"""
        return [server for server in self.mcp_servers if server.enabled]
    
    def get_server_by_id(self, server_id: str) -> Optional[MCPServerConfig]:
        """Get MCP server configuration by ID"""
        for server in self.mcp_servers:
            if server.id == server_id:
                return server
        return None