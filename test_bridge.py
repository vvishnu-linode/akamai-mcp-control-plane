#!/usr/bin/env python3
"""
Test script for MCP Bridge Client

This script tests the bridge client's basic functionality including:
- JSON-RPC message parsing
- MCP protocol handling
- stdio communication simulation
"""

import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path
import pytest


class MockMCPClient:
    """Mock MCP client for testing bridge communication"""
    
    def __init__(self, bridge_path: str):
        self.bridge_path = bridge_path
        self.process = None
    
    async def start_bridge(self):
        """Start the bridge process"""
        self.process = await asyncio.create_subprocess_exec(
            sys.executable, self.bridge_path,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
    
    async def send_message(self, message: dict) -> dict:
        """Send a message to the bridge and wait for response"""
        if not self.process:
            raise RuntimeError("Bridge not started")
        
        # Send message
        message_json = json.dumps(message) + "\n"
        self.process.stdin.write(message_json.encode())
        await self.process.stdin.drain()
        
        # Read response
        response_line = await self.process.stdout.readline()
        return json.loads(response_line.decode().strip())
    
    async def stop_bridge(self):
        """Stop the bridge process"""
        if self.process:
            self.process.terminate()
            await self.process.wait()


async def test_initialize_handshake():
    """Test MCP initialize handshake"""
    bridge_path = Path(__file__).parent.parent / "src" / "bridge_client.py"
    client = MockMCPClient(str(bridge_path))
    
    try:
        await client.start_bridge()
        
        # Send initialize request
        init_request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {
                    "name": "test-client",
                    "version": "1.0.0"
                }
            }
        }
        
        response = await client.send_message(init_request)
        
        # Validate response
        assert response["jsonrpc"] == "2.0"
        assert response["id"] == 1
        assert "result" in response
        assert response["result"]["protocolVersion"] == "2024-11-05"
        assert "serverInfo" in response["result"]
        
        print("✅ Initialize handshake test passed")
        
    finally:
        await client.stop_bridge()


async def test_tools_list():
    """Test tools/list method"""
    bridge_path = Path(__file__).parent.parent / "src" / "bridge_client.py"
    client = MockMCPClient(str(bridge_path))
    
    try:
        await client.start_bridge()
        
        # Send tools/list request
        tools_request = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
            "params": {}
        }
        
        response = await client.send_message(tools_request)
        
        # Validate response
        assert response["jsonrpc"] == "2.0"
        assert response["id"] == 2
        assert "result" in response
        assert "tools" in response["result"]
        assert isinstance(response["result"]["tools"], list)
        
        # Check that we have the expected tools
        tools = response["result"]["tools"]
        tool_names = [tool["name"] for tool in tools]
        expected_tools = ["query_database", "read_file", "send_slack_message", "get_weather", "search_codebase", "create_jira_ticket"]
        
        for expected_tool in expected_tools:
            assert expected_tool in tool_names, f"Expected tool {expected_tool} not found"
        
        # Validate tool schema structure
        for tool in tools:
            assert "name" in tool
            assert "description" in tool
            assert "inputSchema" in tool
            assert "type" in tool["inputSchema"]
            assert "properties" in tool["inputSchema"]
        
        print(f"✅ Tools list test passed - found {len(tools)} tools")
        
    finally:
        await client.stop_bridge()


async def test_unknown_method():
    """Test handling of unknown methods"""
    bridge_path = Path(__file__).parent.parent / "src" / "bridge_client.py"
    client = MockMCPClient(str(bridge_path))
    
    try:
        await client.start_bridge()
        
        # Send unknown method request
        unknown_request = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "unknown/method",
            "params": {}
        }
        
        response = await client.send_message(unknown_request)
        
        # Validate error response
        assert response["jsonrpc"] == "2.0"
        assert response["id"] == 3
        assert "error" in response
        assert response["error"]["code"] == -32601
        assert "Method not found" in response["error"]["message"]
        
        print("✅ Unknown method test passed")
        
    finally:
        await client.stop_bridge()


def test_message_validation():
    """Test MCPMessage validation"""
    from bridge.src.bridge_client import MCPMessage
    
    # Valid message
    valid_msg = MCPMessage(
        jsonrpc="2.0",
        id=1,
        method="test",
        params={"key": "value"}
    )
    assert valid_msg.jsonrpc == "2.0"
    assert valid_msg.id == 1
    
    # Response message
    response_msg = MCPMessage(
        jsonrpc="2.0",
        id=1,
        result={"status": "ok"}
    )
    assert response_msg.result == {"status": "ok"}
    
    # Error message
    error_msg = MCPMessage(
        jsonrpc="2.0",
        id=1,
        error={"code": -32603, "message": "Internal error"}
    )
    assert error_msg.error["code"] == -32603
    
    print("✅ Message validation test passed")


async def test_tool_execution():
    """Test tool execution with various tools"""
    bridge_path = Path(__file__).parent.parent / "src" / "bridge_client.py"
    client = MockMCPClient(str(bridge_path))
    
    try:
        await client.start_bridge()
        
        # Test query_database tool
        db_request = {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "query_database",
                "arguments": {
                    "query": "SELECT * FROM users WHERE active = 1",
                    "database": "production"
                }
            }
        }
        
        response = await client.send_message(db_request)
        assert response["jsonrpc"] == "2.0"
        assert response["id"] == 4
        assert "result" in response
        assert "content" in response["result"]
        assert response["result"]["content"][0]["type"] == "text"
        assert "Mock database query executed successfully" in response["result"]["content"][0]["text"]
        
        # Test get_weather tool
        weather_request = {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": "get_weather",
                "arguments": {
                    "location": "San Francisco",
                    "units": "fahrenheit"
                }
            }
        }
        
        response = await client.send_message(weather_request)
        assert response["jsonrpc"] == "2.0"
        assert response["id"] == 5
        assert "result" in response
        assert "San Francisco" in response["result"]["content"][0]["text"]
        assert "°F" in response["result"]["content"][0]["text"]
        
        # Test unknown tool
        unknown_tool_request = {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {
                "name": "nonexistent_tool",
                "arguments": {}
            }
        }
        
        response = await client.send_message(unknown_tool_request)
        assert response["jsonrpc"] == "2.0"
        assert response["id"] == 6
        assert "error" in response
        assert response["error"]["code"] == -32602
        assert "Unknown tool" in response["error"]["message"]
        
        print("✅ Tool execution tests passed")
        
    finally:
        await client.stop_bridge()


async def test_slack_tool():
    """Test Slack message tool specifically"""
    bridge_path = Path(__file__).parent.parent / "src" / "bridge_client.py"
    client = MockMCPClient(str(bridge_path))
    
    try:
        await client.start_bridge()
        
        slack_request = {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {
                "name": "send_slack_message",
                "arguments": {
                    "channel": "#general",
                    "message": "Hello from MCP Bridge!",
                    "thread_ts": "1234567890.123"
                }
            }
        }
        
        response = await client.send_message(slack_request)
        assert response["jsonrpc"] == "2.0"
        assert response["id"] == 7
        assert "result" in response
        
        content = response["result"]["content"][0]["text"]
        assert "#general" in content
        assert "Hello from MCP Bridge!" in content
        assert "1234567890.123" in content
        assert "Mock Slack message sent successfully" in content
        
        print("✅ Slack tool test passed")
        
    finally:
        await client.stop_bridge()


async def run_all_tests():
    """Run all tests"""
    print("Running MCP Bridge tests...")
    
    # Unit tests
    test_message_validation()
    
    # Integration tests (require the bridge to be runnable)
    try:
        await test_initialize_handshake()
        await test_tools_list() 
        await test_unknown_method()
        await test_tool_execution()
        await test_slack_tool()
        print("\n🎉 All tests passed!")
    except Exception as e:
        print(f"\n❌ Tests failed: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(run_all_tests())