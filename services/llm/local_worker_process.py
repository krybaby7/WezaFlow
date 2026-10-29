"""Owned child: verifies pinned artifacts, then serves bounded local requests."""
import json
import os
import secrets
import socket
import subprocess
import time
import urllib.request
from services.llm.local_artifacts import verify_manifest, _unique

MAX_RESPONSE_BYTES = 1024 * 1024
MAX_REQUEST_CHARS = 120000
NEUTRAL_SYSTEM = 'Return only the JSON object {"edited_text":"Ready."}.'
NEUTRAL_USER = '{"raw":"Ready.","context":null,"vocabulary":[],"protected_literals":[]}'


class NativeRuntime:
    def __init__(self, config):
        self.config = config
        self.alias = 'wezza-' + secrets.token_hex(16)
        self.key = secrets.token_hex(32)
        self.process = None
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def http(self, path, payload=None):
        request = urllib.request.Request(self.url + path, data=None if payload is None else json.dumps(payload).encode(), headers={'Authorization': 'Bearer '+self.key, 'Content-Type': 'application/json'})
        timeout = self.config.request_timeout
        if getattr(self, '_startup_deadline', None) is not None:
            timeout = max(.001, self._startup_deadline-time.monotonic())
        with self.opener.open(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError('local_response_oversize')
        return json.loads(raw, object_pairs_hook=_unique)

    def start(self):
        started = time.monotonic()
        self._startup_deadline = started+self.config.startup_timeout
        artifacts = verify_manifest(self.config.manifest_path)
        if artifacts['manifest_sha256'] != self.config.manifest_sha256:
            raise ValueError('local_manifest_changed')
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0))
            port = reservation.getsockname()[1]
        self.url = f'http://127.0.0.1:{port}'
        args = [artifacts['server']['absolute_path'], '--model', artifacts['model']['absolute_path'], '--chat-template-file', artifacts['template']['absolute_path'], '--host', '127.0.0.1', '--port', str(port), '--alias', self.alias, '--no-webui', '--parallel', '1', '--threads', str(self.config.threads), '--threads-batch', str(self.config.threads), '--ctx-size', str(self.config.context_tokens), '--no-context-shift', '--poll', '0', '--poll-batch', '0', '--jinja', '--chat-template-kwargs', '{"enable_thinking":false}', '--reasoning', 'off', '--n-predict', str(self.config.output_tokens), '--gpu-layers', '99', '--flash-attn', 'on']
        env = {key: value for key, value in os.environ.items() if not key.upper().startswith('LLAMA_')}
        env['LLAMA_API_KEY'] = self.key
        self.process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, creationflags=subprocess.CREATE_NO_WINDOW)
        while True:
            if self.process.poll() is not None:
                raise ValueError('local_native_exited')
            try:
                models = self.http('/v1/models')['data']
                if len(models) != 1 or models[0]['id'] != self.alias:
                    raise ValueError('local_model_identity')
                break
            except (OSError, urllib.error.URLError):
                time.sleep(.03)
        loaded = time.monotonic() - started
        warm = time.monotonic()
        if json.loads(self.edit(NEUTRAL_SYSTEM, NEUTRAL_USER), object_pairs_hook=_unique) != {'edited_text':'Ready.'}:
            raise ValueError('local_warmup_invalid')
        self._startup_deadline = None
        return {'manifest_sha256': artifacts['manifest_sha256'], 'source': artifacts['source'], 'revision': artifacts['revision'], 'model_sha256': artifacts['model']['sha256'], 'template_sha256': artifacts['template']['sha256'], 'runtime_sha256': [artifacts['server']['sha256'], *[item['sha256'] for item in artifacts['runtime_files']]], 'load_seconds': loaded, 'warmup_seconds': time.monotonic()-warm}

    def edit(self, system, user):
        if not isinstance(system, str) or not isinstance(user, str) or len(system)+len(user) > MAX_REQUEST_CHARS:
            raise ValueError('local_request_invalid')
        messages = [{'role':'system','content':system}, {'role':'user','content':user}]
        rendered = self.http('/apply-template', {'messages':messages, 'add_generation_prompt':True, 'chat_template_kwargs':{'enable_thinking':False}})['prompt']
        if not isinstance(rendered, str) or len(rendered) > MAX_REQUEST_CHARS * 2 or not rendered.rstrip().endswith('<think>\n\n</think>'):
            raise ValueError('local_thinking_prefix_invalid')
        tokens = self.http('/tokenize', {'content':rendered, 'add_special':False})['tokens']
        if not isinstance(tokens, list) or len(tokens)+self.config.output_tokens+32 > self.config.context_tokens:
            raise ValueError('local_context_overflow')
        reply = self.http('/v1/chat/completions', {'model':self.alias,'messages':messages,'chat_template_kwargs':{'enable_thinking':False},'temperature':0,'top_k':1,'top_p':1,'min_p':0,'presence_penalty':0,'frequency_penalty':0,'repeat_penalty':1,'max_tokens':self.config.output_tokens,'stream':False,'response_format':{'type':'json_schema','json_schema':{'name':'edited_text','strict':True,'schema':{'type':'object','properties':{'edited_text':{'type':'string'}},'required':['edited_text'],'additionalProperties':False}}}})
        if reply.get('model') != self.alias or len(reply.get('choices', [])) != 1:
            raise ValueError('local_reply_invalid')
        choice = reply['choices'][0]
        message = choice['message']
        if choice.get('finish_reason') != 'stop' or message.get('role') != 'assistant' or not isinstance(message.get('content'), str) or any(message.get(key) for key in ('reasoning', 'reasoning_content', 'tool_calls', 'function_call')):
            raise ValueError('local_reply_invalid')
        return message['content']


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('local_redirect_rejected')


def child_main(connection, gate, config, generation):
    runtime = NativeRuntime(config)
    try:
        if not gate.wait(config.startup_timeout):
            return
        provenance = runtime.start()
        connection.send_bytes(json.dumps({'generation':generation,'kind':'ready','provenance':provenance}).encode())
        while True:
            request = json.loads(connection.recv_bytes(MAX_RESPONSE_BYTES))
            if request['generation'] != generation:
                raise ValueError('local_generation_invalid')
            content = runtime.edit(request['system'], request['user'])
            connection.send_bytes(json.dumps({'generation':generation,'id':request['id'],'kind':'result','content':content}).encode())
    except BaseException:
        # Parent treats EOF/error as poison and terminates the entire job.
        pass
    finally:
        connection.close()
        if runtime.process is not None:
            try:
                runtime.process.terminate()
                runtime.process.wait(timeout=config.reap_timeout)
            except Exception:
                runtime.process.kill()
