"""
Bouncer MCP Server

AWS 命令審批執行系統 - stdio MCP Server 版本
"""

from .server import BouncerMCPServer, main

__version__ = '1.0.0'
__all__ = ['BouncerMCPServer', 'main']
