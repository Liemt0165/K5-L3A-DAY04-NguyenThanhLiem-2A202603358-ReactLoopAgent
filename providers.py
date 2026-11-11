"""Các provider cùng một interface. Không chứa agent loop."""
import json
import os
from pathlib import Path
import urllib.error
import urllib.request
import uuid


def _load_env_file():
    """Tự động tải các biến từ .env nếu chưa có trong os.environ."""
    env_file = Path(__file__).resolve().parent / '.env'
    if env_file.is_file():
        try:
            with open(env_file, encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    if line.startswith('export '):
                        line = line[7:].strip()
                    if '=' in line:
                        k, v = line.split('=', 1)
                        k = k.strip()
                        v = v.strip().strip('\'"')
                        if k and k not in os.environ:
                            os.environ[k] = v
        except Exception:
            pass


_load_env_file()

CONFIG = {
    'openai': ('OPENAI_API_KEY', 'https://api.openai.com/v1/chat/completions'),
    'gemini': ('GEMINI_API_KEY', 'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions'),
    'openrouter': ('OPENROUTER_API_KEY', 'https://openrouter.ai/api/v1/chat/completions'),
    'opencode-go': ('OPENCODE_GO_API_KEY', 'https://opencode.ai/inference/go/openai/v1/chat/completions'),
    'anthropic': ('ANTHROPIC_API_KEY', 'https://api.anthropic.com/v1/messages'),
    'deepinfra': ('DEEPINFRA_API_KEY', 'https://api.deepinfra.com/v1/openai/chat/completions'),
    'deepinfrac': ('DEEPINFRA_API_KEY', 'https://api.deepinfra.com/v1/openai/chat/completions'),
    'groq': ('GROQ_API_KEY', 'https://api.groq.com/openai/v1/chat/completions'),
}


def post_json(url, headers, payload):
    user_agent = os.getenv('STATUS_USER_AGENT', 'StatusAgentLab/1.0 (educational classroom)')
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json', 'User-Agent': user_agent, **headers})
    try:
        with urllib.request.urlopen(req, timeout=180) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        # Do not print response bodies: they may contain account or request data.
        raise RuntimeError(f'Provider trả HTTP {exc.code}; kiểm tra model, key và quota.') from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f'Không kết nối được provider: {exc.reason}') from exc


class Provider:
    """complete(messages, schemas) -> {message, calls, finish, usage}.

    messages: system/user/assistant/tool theo format Chat Completions. message trả về
    cần được append nguyên vẹn; tool result có role, tool_call_id, content (JSON string).
    """
    def __init__(self, name, model):
        if name not in CONFIG:
            raise ValueError(f'Provider không hợp lệ: {name}')
        if not model:
            raise ValueError('Cần đặt MODEL là model có hỗ trợ tool calling.')
        self.name, self.model = name, model
        # One Provider instance represents one conversation/run.
        self.session_id = str(uuid.uuid4()) if name == 'opencode-go' else None
        key_name, self.url = CONFIG[name]
        if self.name == 'openai' and os.getenv('OPENAI_BASE_URL'):
            base = os.getenv('OPENAI_BASE_URL').rstrip('/')
            self.url = base if base.endswith('/chat/completions') else f'{base}/chat/completions'
        self.key = os.getenv(key_name)
        if not self.key:
            raise ValueError(f'Thiếu {key_name}; xem README.md.')

    def complete(self, messages, schemas):
        if self.name == 'anthropic':
            return self._anthropic(messages, schemas)
        payload = {'model': self.model, 'messages': messages}
        if schemas:
            payload['tools'] = schemas
        headers = {'Authorization': f'Bearer {self.key}'}
        if self.name == 'opencode-go':
            headers.update({'User-Agent': 'StatusAgentLab/1.0', 'x-opencode-session': self.session_id})
        data = post_json(self.url, headers, payload)
        choice = data['choices'][0]
        message = choice['message']
        tool_calls = message.get('tool_calls') or []
        calls = [{'id': c['id'], 'name': c['function']['name'], 'arguments': c['function']['arguments']} for c in tool_calls]
        return {'message': message, 'calls': calls, 'finish': choice.get('finish_reason'), 'usage': data.get('usage', {})}

    def _anthropic(self, messages, schemas):
        system = '\n'.join(m['content'] for m in messages if m['role'] == 'system')
        native = []
        for m in messages:
            if m['role'] == 'system':
                continue
            if m['role'] == 'assistant':
                blocks = []
                if m.get('content'):
                    blocks.append({'type': 'text', 'text': m['content']})
                for call in m.get('tool_calls', []):
                    blocks.append({'type': 'tool_use', 'id': call['id'], 'name': call['function']['name'], 'input': json.loads(call['function']['arguments'])})
                native.append({'role': 'assistant', 'content': blocks})
            elif m['role'] == 'tool':
                block = {'type': 'tool_result', 'tool_use_id': m['tool_call_id'], 'content': m['content']}
                if native and native[-1]['role'] == 'user' and isinstance(native[-1]['content'], list):
                    native[-1]['content'].append(block)
                else:
                    native.append({'role': 'user', 'content': [block]})
            else:
                native.append({'role': 'user', 'content': m['content']})
        tools = [{'name': x['function']['name'], 'description': x['function']['description'], 'input_schema': x['function']['parameters']} for x in schemas]
        payload = {'model': self.model, 'max_tokens': 1024, 'system': system, 'messages': native}
        if tools:
            payload['tools'] = tools
        data = post_json(self.url, {'x-api-key': self.key, 'anthropic-version': '2023-06-01'}, payload)
        blocks = data['content']
        calls = [{'id': b['id'], 'name': b['name'], 'arguments': json.dumps(b['input'], ensure_ascii=False)} for b in blocks if b['type'] == 'tool_use']
        text = '\n'.join(b['text'] for b in blocks if b['type'] == 'text')
        message = {'role': 'assistant', 'content': text or None}
        if calls:
            message['tool_calls'] = [{'id': c['id'], 'type': 'function', 'function': {'name': c['name'], 'arguments': c['arguments']}} for c in calls]
        reason = {'end_turn': 'stop', 'tool_use': 'tool_calls', 'max_tokens': 'length'}.get(data.get('stop_reason'), data.get('stop_reason'))
        return {'message': message, 'calls': calls, 'finish': reason, 'usage': data.get('usage', {})}
