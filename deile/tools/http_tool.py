"""HTTP Tool - Ferramenta para requisições HTTP completas"""

import json
import time
import urllib.parse
from typing import Dict, List, Optional, Any, Union
from datetime import datetime

try:
    import requests
    from requests.adapters import HTTPAdapter
    from requests.packages.urllib3.util.retry import Retry
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False
    requests = None

from .base import SyncTool, ToolContext, ToolResult, ToolStatus, DisplayPolicy
from ..security.secrets_scanner import SecretsScanner


class HTTPTool(SyncTool):
    """Tool para requisições HTTP com suporte completo a REST APIs"""
    
    def __init__(self):
        super().__init__()
        self.secrets_scanner = SecretsScanner()
        self.session = None
        if REQUESTS_AVAILABLE:
            self._setup_session()
    
    def _setup_session(self):
        """Configure requests session with retries and timeouts"""
        self.session = requests.Session()
        
        # Configure retry strategy
        retry_strategy = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "OPTIONS"]
        )
        
        # Mount adapter with retry strategy
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        
        # Set default headers
        self.session.headers.update({
            'User-Agent': 'DEILE-HTTP-Tool/1.0'
        })
    
    @property
    def name(self) -> str:
        return "http"
    
    @property
    def description(self) -> str:
        return "Make HTTP requests with full REST API support including GET, POST, PUT, DELETE, PATCH"
    
    @property
    def category(self) -> str:
        return "network"
    
    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": {
                    "method": {
                        "type": "string",
                        "enum": ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
                        "default": "GET",
                        "description": "HTTP method to use"
                    },
                    "url": {
                        "type": "string",
                        "description": "Target URL for the request"
                    },
                    "headers": {
                        "type": "object",
                        "description": "HTTP headers as key-value pairs"
                    },
                    "params": {
                        "type": "object",
                        "description": "URL query parameters as key-value pairs"
                    },
                    "body": {
                        "type": ["string", "object"],
                        "description": "Request body (JSON object or raw string)"
                    },
                    "json": {
                        "type": "object",
                        "description": "JSON data to send (alternative to body)"
                    },
                    "data": {
                        "type": "object",
                        "description": "Form data to send (alternative to body/json)"
                    },
                    "files": {
                        "type": "object",
                        "description": "Files to upload as key-value pairs"
                    },
                    "auth": {
                        "type": "object",
                        "properties": {
                            "type": {
                                "type": "string",
                                "enum": ["basic", "bearer", "api_key", "oauth2"]
                            },
                            "username": {"type": "string"},
                            "password": {"type": "string"},
                            "token": {"type": "string"},
                            "api_key": {"type": "string"},
                            "header_name": {"type": "string"}
                        },
                        "description": "Authentication configuration"
                    },
                    "timeout": {
                        "type": "number",
                        "default": 30.0,
                        "description": "Request timeout in seconds"
                    },
                    "follow_redirects": {
                        "type": "boolean",
                        "default": True,
                        "description": "Follow HTTP redirects"
                    },
                    "verify_ssl": {
                        "type": "boolean",
                        "default": True,
                        "description": "Verify SSL certificates"
                    },
                    "max_retries": {
                        "type": "integer",
                        "default": 3,
                        "description": "Maximum number of retries"
                    },
                    "stream": {
                        "type": "boolean",
                        "default": False,
                        "description": "Stream the response"
                    },
                    "save_to_file": {
                        "type": "string",
                        "description": "Save response content to file"
                    },
                    "cookies": {
                        "type": "object",
                        "description": "Cookies to send with request"
                    },
                    "proxy": {
                        "type": "object",
                        "properties": {
                            "http": {"type": "string"},
                            "https": {"type": "string"}
                        },
                        "description": "Proxy configuration"
                    }
                },
                "required": ["url"]
            },
            "returns": {
                "type": "object",
                "properties": {
                    "success": {"type": "boolean"},
                    "status_code": {"type": "integer"},
                    "headers": {"type": "object"},
                    "content": {"type": "string"},
                    "json": {"type": "object"},
                    "duration": {"type": "number"},
                    "url": {"type": "string"},
                    "redirects": {"type": "array"},
                    "error": {"type": "string"}
                }
            },
            "side_effects": "Makes network requests, may save files",
            "risk_level": "medium",
            "display_policy": "both"
        }
    
    def execute_sync(self, context: ToolContext) -> ToolResult:
        """Execute HTTP request"""
        if not REQUESTS_AVAILABLE:
            return ToolResult(
                status=ToolStatus.ERROR,
                data={"error": "Requests library not available"},
                message="HTTP tool requires requests package. Install with: pip install requests",
                display_policy=DisplayPolicy.BOTH
            )
        
        try:
            args = context.parsed_args
            
            # Validate required parameters
            url = args.get("url")
            if not url:
                return ToolResult(
                    status=ToolStatus.ERROR,
                    data={"error": "URL is required"},
                    message="URL parameter is required for HTTP requests",
                    display_policy=DisplayPolicy.BOTH
                )
            
            # Execute the request
            result = self._make_request(args)
            
            return ToolResult(
                status=ToolStatus.SUCCESS if result["success"] else ToolStatus.ERROR,
                data=result,
                message=self._format_response_message(result),
                display_policy=DisplayPolicy.BOTH
            )
            
        except Exception as e:
            return ToolResult(
                status=ToolStatus.ERROR,
                data={"error": str(e)},
                message=f"HTTP request failed: {str(e)}",
                display_policy=DisplayPolicy.BOTH
            )
    
    def _make_request(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Make HTTP request with all specified parameters"""
        method = args.get("method", "GET").upper()
        url = args.get("url")
        headers = args.get("headers", {})
        params = args.get("params", {})
        timeout = args.get("timeout", 30.0)
        follow_redirects = args.get("follow_redirects", True)
        verify_ssl = args.get("verify_ssl", True)
        stream = args.get("stream", False)
        cookies = args.get("cookies", {})
        proxy = args.get("proxy")
        save_to_file = args.get("save_to_file")
        
        start_time = time.time()
        
        try:
            # Prepare request parameters
            request_kwargs = {
                "method": method,
                "url": url,
                "headers": headers,
                "params": params,
                "timeout": timeout,
                "allow_redirects": follow_redirects,
                "verify": verify_ssl,
                "stream": stream,
                "cookies": cookies
            }
            
            # Add proxy if specified
            if proxy:
                request_kwargs["proxies"] = proxy
            
            # Handle authentication
            auth_config = args.get("auth")
            if auth_config:
                self._add_authentication(request_kwargs, auth_config)
            
            # Handle request body/data
            self._add_request_body(request_kwargs, args)
            
            # Scan for secrets in the request
            self._scan_request_for_secrets(request_kwargs)
            
            # Make the request
            response = self.session.request(**request_kwargs)
            
            end_time = time.time()
            duration = end_time - start_time
            
            # Process response
            return self._process_response(response, duration, save_to_file)
            
        except requests.exceptions.Timeout:
            return {
                "success": False,
                "error": f"Request timed out after {timeout} seconds",
                "duration": time.time() - start_time
            }
        except requests.exceptions.ConnectionError as e:
            return {
                "success": False,
                "error": f"Connection error: {str(e)}",
                "duration": time.time() - start_time
            }
        except requests.exceptions.RequestException as e:
            return {
                "success": False,
                "error": f"Request failed: {str(e)}",
                "duration": time.time() - start_time
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "duration": time.time() - start_time
            }
    
    def _add_authentication(self, request_kwargs: Dict[str, Any], auth_config: Dict[str, Any]):
        """Add authentication to request"""
        auth_type = auth_config.get("type", "").lower()
        
        if auth_type == "basic":
            username = auth_config.get("username")
            password = auth_config.get("password")
            if username and password:
                request_kwargs["auth"] = (username, password)
        
        elif auth_type == "bearer":
            token = auth_config.get("token")
            if token:
                request_kwargs["headers"]["Authorization"] = f"Bearer {token}"
        
        elif auth_type == "api_key":
            api_key = auth_config.get("api_key")
            header_name = auth_config.get("header_name", "X-API-Key")
            if api_key:
                request_kwargs["headers"][header_name] = api_key
        
        elif auth_type == "oauth2":
            token = auth_config.get("token")
            if token:
                request_kwargs["headers"]["Authorization"] = f"Bearer {token}"
    
    def _add_request_body(self, request_kwargs: Dict[str, Any], args: Dict[str, Any]):
        """Add request body/data to request"""
        # Handle different body types
        if "json" in args and args["json"]:
            request_kwargs["json"] = args["json"]
        elif "data" in args and args["data"]:
            request_kwargs["data"] = args["data"]
        elif "body" in args and args["body"]:
            body = args["body"]
            if isinstance(body, (dict, list)):
                request_kwargs["json"] = body
            else:
                request_kwargs["data"] = body
        
        # Handle file uploads
        files = args.get("files")
        if files:
            file_objects = {}
            for key, file_path in files.items():
                try:
                    with open(file_path, 'rb') as f:
                        file_objects[key] = f.read()
                except Exception as e:
                    # Log error but continue
                    pass
            if file_objects:
                request_kwargs["files"] = file_objects
    
    def _scan_request_for_secrets(self, request_kwargs: Dict[str, Any]):
        """Scan request for potential secrets"""
        # Check headers for secrets
        headers = request_kwargs.get("headers", {})
        for key, value in headers.items():
            if isinstance(value, str) and self.secrets_scanner.scan_text(value):
                # Don't fail the request, but log warning
                print(f"Warning: Potential secret detected in header {key}")
        
        # Check URL for secrets
        url = request_kwargs.get("url", "")
        if self.secrets_scanner.scan_text(url):
            print("Warning: Potential secret detected in URL")
        
        # Check body/data for secrets (basic check)
        if "data" in request_kwargs:
            data = request_kwargs["data"]
            if isinstance(data, str) and self.secrets_scanner.scan_text(data):
                print("Warning: Potential secret detected in request body")
    
    def _process_response(self, response: 'requests.Response', duration: float, save_to_file: Optional[str] = None) -> Dict[str, Any]:
        """Process HTTP response"""
        result = {
            "success": True,
            "status_code": response.status_code,
            "headers": dict(response.headers),
            "url": response.url,
            "duration": duration,
            "encoding": response.encoding
        }
        
        # Add redirect history
        if response.history:
            result["redirects"] = [
                {"url": r.url, "status_code": r.status_code}
                for r in response.history
            ]
        
        # Process response content
        try:
            # Try to parse as JSON first
            if response.headers.get("content-type", "").startswith("application/json"):
                result["json"] = response.json()
                result["content"] = json.dumps(result["json"], indent=2)
            else:
                result["content"] = response.text
                
                # Try to parse as JSON anyway (some APIs don't set correct content-type)
                try:
                    result["json"] = response.json()
                except:
                    pass
        
        except Exception as e:
            # Fallback to raw content
            try:
                result["content"] = response.text
            except:
                result["content"] = f"<binary content, {len(response.content)} bytes>"
            result["content_error"] = str(e)
        
        # Save to file if requested
        if save_to_file:
            try:
                with open(save_to_file, 'wb') as f:
                    f.write(response.content)
                result["saved_to"] = save_to_file
            except Exception as e:
                result["save_error"] = str(e)
        
        # Add response size info
        result["content_length"] = len(response.content)
        if "content-length" in response.headers:
            result["declared_length"] = int(response.headers["content-length"])
        
        # Determine if request was successful
        result["success"] = 200 <= response.status_code < 400
        
        return result
    
    def _format_response_message(self, result: Dict[str, Any]) -> str:
        """Format response message for display"""
        if not result["success"]:
            if "error" in result:
                return f"HTTP request failed: {result['error']}"
            else:
                status_code = result.get("status_code", "unknown")
                return f"HTTP request failed with status {status_code}"
        
        status_code = result.get("status_code", 200)
        duration = result.get("duration", 0)
        content_length = result.get("content_length", 0)
        
        message_parts = [
            f"HTTP {status_code}",
            f"Duration: {duration:.2f}s"
        ]
        
        if content_length:
            if content_length > 1024:
                size_str = f"{content_length / 1024:.1f} KB"
            else:
                size_str = f"{content_length} bytes"
            message_parts.append(f"Size: {size_str}")
        
        # Add redirect info
        if "redirects" in result:
            message_parts.append(f"Redirects: {len(result['redirects'])}")
        
        # Add content type info
        headers = result.get("headers", {})
        content_type = headers.get("content-type", "")
        if content_type:
            content_type_short = content_type.split(";")[0]
            message_parts.append(f"Type: {content_type_short}")
        
        return " | ".join(message_parts)
    
    def close(self):
        """Close HTTP session"""
        if self.session:
            self.session.close()


# Common HTTP request helpers
class HTTPHelpers:
    """Helper functions for common HTTP operations"""
    
    @staticmethod
    def build_url(base_url: str, path: str = "", params: Optional[Dict[str, Any]] = None) -> str:
        """Build URL with path and parameters"""
        url = base_url.rstrip("/")
        if path:
            url += "/" + path.lstrip("/")
        
        if params:
            url += "?" + urllib.parse.urlencode(params)
        
        return url
    
    @staticmethod
    def parse_curl_command(curl_command: str) -> Dict[str, Any]:
        """Parse curl command into HTTP tool parameters"""
        # Basic curl parsing (simplified)
        import shlex
        
        parts = shlex.split(curl_command)
        if not parts or parts[0] != "curl":
            raise ValueError("Invalid curl command")
        
        result = {
            "method": "GET",
            "headers": {},
            "params": {}
        }
        
        i = 1
        while i < len(parts):
            arg = parts[i]
            
            if arg in ["-X", "--request"]:
                if i + 1 < len(parts):
                    result["method"] = parts[i + 1]
                    i += 1
            elif arg in ["-H", "--header"]:
                if i + 1 < len(parts):
                    header = parts[i + 1]
                    if ":" in header:
                        key, value = header.split(":", 1)
                        result["headers"][key.strip()] = value.strip()
                    i += 1
            elif arg in ["-d", "--data"]:
                if i + 1 < len(parts):
                    result["body"] = parts[i + 1]
                    i += 1
            elif arg.startswith("http"):
                result["url"] = arg
            
            i += 1
        
        return result
    
    @staticmethod
    def format_headers_for_display(headers: Dict[str, str]) -> str:
        """Format headers for readable display"""
        if not headers:
            return "No headers"
        
        lines = []
        for key, value in headers.items():
            # Truncate very long header values
            if len(value) > 100:
                value = value[:97] + "..."
            lines.append(f"{key}: {value}")
        
        return "\n".join(lines)


if __name__ == "__main__":
    # Test básico
    tool = HTTPTool()
    print("HTTP Tool loaded successfully")
    print("Schema:", tool.get_schema())