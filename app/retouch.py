"""Optional MCP retouching. No provider is silently presented as AI."""
import base64
import io
import json
import os
from pathlib import Path
from urllib.parse import urlparse
import requests
from PIL import Image
from app.database import DB_PATH

SETTINGS_PATH = Path(DB_PATH).parent / 'editor-settings.json'
DEFAULT_INSTRUCTIONS = 'Preserve identity and composition. Apply subject masking, balanced lighting, natural skin retouch, and subtle color grading.'


def settings():
    if SETTINGS_PATH.exists():
        return json.loads(SETTINGS_PATH.read_text())
    return {'enabled': False, 'tool': 'retouch_image', 'instructions': DEFAULT_INSTRUCTIONS}


def save_settings(config):
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = SETTINGS_PATH.with_suffix('.tmp')
    temporary.write_text(json.dumps(config))
    temporary.chmod(0o600)
    os.replace(temporary, SETTINGS_PATH)


class MCPConnection:
    """Streamable HTTP client for an explicitly configured image-edit tool."""
    def __init__(self):
        self.url = os.getenv('RETOUCH_MCP_URL', '')
        if urlparse(self.url).scheme != 'https':
            raise ValueError('Configure RETOUCH_MCP_URL with an HTTPS MCP endpoint')
        self.http = requests.Session()
        self.http.headers.update({'Accept': 'application/json, text/event-stream', 'Content-Type': 'application/json'})
        token = os.getenv('RETOUCH_MCP_TOKEN', '')
        if token:
            self.http.headers['Authorization'] = f'Bearer {token}'
        self.counter = 0

    def __enter__(self):
        try:
            result = self.rpc('initialize', {'protocolVersion': '2025-03-26', 'capabilities': {},
                'clientInfo': {'name': 'photostudio', 'version': '1.0'}})
            self.http.headers['MCP-Protocol-Version'] = result.get('protocolVersion', '2025-03-26')
            self.rpc('notifications/initialized', notification=True)
            return self
        except Exception:
            self.http.close()
            raise

    def __exit__(self, *args):
        self.http.close()

    def rpc(self, method, params=None, notification=False):
        self.counter += 1
        payload = {'jsonrpc': '2.0', 'method': method}
        if params is not None:
            payload['params'] = params
        if not notification:
            payload['id'] = self.counter
        with self.http.post(self.url, json=payload, timeout=(10, 180), allow_redirects=False, stream=True) as response:
            if response.is_redirect:
                raise RuntimeError('MCP redirects are not accepted; configure the final endpoint')
            response.raise_for_status()
            if response.headers.get('Mcp-Session-Id'):
                self.http.headers['Mcp-Session-Id'] = response.headers['Mcp-Session-Id']
            if notification:
                return {}
            if 'text/event-stream' in response.headers.get('Content-Type', ''):
                message = None
                size = 0
                for line in response.iter_lines():
                    size += len(line)
                    if size > 64 * 1024 * 1024:
                        raise RuntimeError('MCP response exceeds image size limit')
                    if line.startswith(b'data:'):
                        candidate = json.loads(line[5:].strip())
                        if candidate.get('id') == self.counter:
                            message = candidate
                            break
            else:
                body = bytearray()
                for chunk in response.iter_content(65536):
                    body.extend(chunk)
                    if len(body) > 64 * 1024 * 1024:
                        raise RuntimeError('MCP response exceeds image size limit')
                message = json.loads(body)
            if not message or 'error' in message or message.get('id') != self.counter:
                raise RuntimeError('MCP tool request failed')
            return message['result']

    def check_tool(self, name):
        tools = self.rpc('tools/list').get('tools', [])
        tool = next((t for t in tools if t['name'] == name), None)
        expected = {'image_base64', 'mime_type', 'instructions'}
        if not tool or not expected.issubset(tool.get('inputSchema', {}).get('properties', {})):
            raise ValueError('The selected MCP tool must accept image_base64, mime_type, and instructions')
        return tool


class NoopRetouchProvider:
    def process(self, image, source_path):
        return image


class MCPRetouchProvider:
    def __init__(self, config):
        self.config = config

    def process(self, image, source_path):
        buffer = io.BytesIO()
        image.convert('RGB').save(buffer, 'JPEG', quality=95)
        with MCPConnection() as connection:
            result = connection.rpc('tools/call', {'name': self.config['tool'], 'arguments': {
                'image_base64': base64.b64encode(buffer.getvalue()).decode(), 'mime_type': 'image/jpeg',
                'instructions': self.config['instructions']}})
        if result.get('isError'):
            raise RuntimeError('MCP editing failed; no image was approved')
        block = next((b for b in result.get('content', []) if b.get('type') == 'image'), None)
        if not block:
            raise RuntimeError('The MCP editor must return an image content block')
        raw = base64.b64decode(block['data'], validate=True)
        with Image.open(io.BytesIO(raw)) as output:
            output.load()
            return output.convert('RGB')


def get_retouch_provider():
    config = settings()
    return MCPRetouchProvider(config) if config.get('enabled') else NoopRetouchProvider()


def apply_retouch(image, source_path):
    if not Path(source_path).is_file():
        raise FileNotFoundError('Retouch source file missing')
    return get_retouch_provider().process(image, source_path)
