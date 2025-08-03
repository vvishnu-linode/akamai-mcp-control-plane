"""
Authentication service for MCP Control Plane

Provides token-based authentication for bridge clients accessing the control plane.
"""

import hashlib
import secrets
import time
from typing import List, Optional
import structlog

logger = structlog.get_logger("auth")


class AuthService:
    """Authentication service for validating API tokens"""
    
    def __init__(self, valid_tokens: List[str]):
        """
        Initialize authentication service
        
        Args:
            valid_tokens: List of valid bearer tokens
        """
        self.valid_tokens = set(valid_tokens)
        self.token_usage = {}  # Track token usage for monitoring
        
        logger.info("Auth service initialized", token_count=len(valid_tokens))
    
    def validate_token(self, token: str) -> bool:
        """
        Validate a bearer token
        
        Args:
            token: Bearer token to validate
            
        Returns:
            True if token is valid, False otherwise
        """
        if not token or token not in self.valid_tokens:
            logger.warning("Invalid token attempted", token_prefix=token[:8] if token else "None")
            return False
        
        # Track token usage
        token_hash = hashlib.sha256(token.encode()).hexdigest()[:16]
        self.token_usage[token_hash] = {
            "last_used": time.time(),
            "usage_count": self.token_usage.get(token_hash, {}).get("usage_count", 0) + 1
        }
        
        logger.debug("Token validated successfully", token_hash=token_hash)
        return True
    
    def get_token_stats(self) -> dict:
        """Get token usage statistics"""
        return {
            "total_tokens": len(self.valid_tokens),
            "active_tokens": len(self.token_usage),
            "usage_stats": self.token_usage
        }
    
    @staticmethod
    def generate_token() -> str:
        """Generate a new secure random token"""
        return secrets.token_urlsafe(32)
    
    def add_token(self, token: str) -> None:
        """Add a new valid token"""
        self.valid_tokens.add(token)
        logger.info("Token added", token_count=len(self.valid_tokens))
    
    def remove_token(self, token: str) -> bool:
        """Remove a token"""
        if token in self.valid_tokens:
            self.valid_tokens.remove(token)
            logger.info("Token removed", token_count=len(self.valid_tokens))
            return True
        return False