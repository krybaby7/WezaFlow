"""Opt-in Windows owned local editor, with no pipeline/default-profile changes."""
from dataclasses import dataclass, field, asdict
import hashlib
import json
import math
import multiprocessing
import os
from pathlib import Path
import threading
import time
import uuid
import re
from services.asr.windows_job import WorkerJob
from services.llm.local_worker_process import child_main, MAX_RESPONSE_BYTES, MAX_REQUEST_CHARS


@dataclass(frozen=True)
class LocalEditorConfig:
    manifest_path: str
    startup_timeout: float = 120
    request_timeout: float = 15
    idle_timeout: float = 60
    reap_timeout: float = 5
    threads: int = 2
    context_tokens: int = 4096
    output_tokens: int = 512
    manifest_sha256: str = field(init=False)

    @property
    def identity(self):
        return hashlib.sha256(json.dumps(asdict(self),sort_keys=True).encode()).hexdigest()

    def __post_init__(self):
        path = Path(self.manifest_path)
        if not path.is_absolute() or path.drive.startswith('\\\\') or not path.is_file() or path.stat().st_size > 65536:
            raise ValueError('local_manifest_invalid')
        for name,cap in (('startup_timeout',600),('request_timeout',120),('idle_timeout',3600),('reap_timeout',30)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (float,int)) or not math.isfinite(value) or not 0 < value <= cap:
                raise ValueError('local_deadline_invalid')
        if any(type(getattr(self,key)) is not int for key in ('threads','context_tokens','output_tokens')) or self.threads != 2 or self.context_tokens != 4096 or self.output_tokens != 512:
            raise ValueError('local_resource_config_invalid')
        object.__setattr__(self, 'manifest_path', str(path.resolve()))
        with path.open('rb') as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError('local_manifest_invalid')
        object.__setattr__(self, 'manifest_sha256', hashlib.sha256(raw).hexdigest())


class _Backend:
    def __init__(self, service, config):
        self.service, self.config = service, config

    def edit(self, *, system_prompt, user_payload):
        return self.service._edit(self.config, system_prompt, user_payload)


class LocalEditorService:
    def __init__(self):
        self._lock = threading.RLock()
        self._operation = threading.Lock()
        self._reap_lock = threading.Lock()
        self._process = self._job = self._connection = self._io = self._timer = None
        self._config = None
        self._generation = 0
        self._activity = 0
        self._closed = False
        self._state = 'STOPPED'
        self._error = None
        self._provenance = None

    def backend(self, config):
        if not isinstance(config, LocalEditorConfig):
            raise ValueError('local_config_invalid')
        return _Backend(self, config)

    def snapshot(self):
        with self._lock:
            if self._state == 'READY' and self._process is not None and not self._process.is_alive():
                self._state = 'UNHEALTHY'
                self._error = 'local_worker_exited'
            return json.loads(json.dumps({'state':self._state,'last_error':self._error,'generation':self._generation,'pid':None if self._process is None else self._process.pid,'owns_process':self._process is not None,'owns_io':self._io is not None,'closed':self._closed,'provenance':self._provenance,'config_identity':None if self._config is None else self._config.identity}))

    @staticmethod
    def _valid_provenance(value, config):
        expected = {'manifest_sha256','model_sha256','template_sha256','runtime_sha256','source','revision','load_seconds','warmup_seconds'}
        if not isinstance(value,dict) or set(value) != expected:
            return False
        if value['manifest_sha256'] != config.manifest_sha256:
            return False
        if not isinstance(value['runtime_sha256'],list) or not 1 <= len(value['runtime_sha256']) <= 129:
            return False
        if any(not isinstance(item,str) or re.fullmatch('[0-9a-f]{64}',item) is None for item in [value['manifest_sha256'],value['model_sha256'],value['template_sha256'],*value['runtime_sha256']]):
            return False
        if any(not isinstance(value[key],str) or not 0 < len(value[key]) <= 512 for key in ('source','revision')):
            return False
        return all(type(value[key]) in (float,int) and math.isfinite(value[key]) and 0 <= value[key] <= 3600 for key in ('load_seconds','warmup_seconds'))

    def _exchange(self, message, timeout):
        deadline = time.monotonic()+timeout
        remaining = lambda: max(0,deadline-time.monotonic())
        done = threading.Event()
        outcome = []
        if not self._lock.acquire(timeout=remaining()):
            raise RuntimeError('local_operation_timeout')
        try:
            if self._closed or self._connection is None:
                raise RuntimeError('local_service_closed')
            connection = self._connection
            def exchange():
                try:
                    if message is not None:
                        connection.send_bytes(json.dumps(message).encode())
                    outcome.append(json.loads(connection.recv_bytes(MAX_RESPONSE_BYTES)))
                except BaseException:
                    outcome.append(None)
                finally:
                    done.set()
            io = threading.Thread(target=exchange, daemon=True, name='local-editor-io')
            self._io = io
            io.start()
        finally:
            self._lock.release()
        if not done.wait(remaining()):
            raise RuntimeError('local_operation_timeout')
        io.join(remaining())
        if io.is_alive() or not self._lock.acquire(timeout=remaining()):
            raise RuntimeError('local_operation_timeout')
        try:
            if self._io is io:
                self._io = None
        finally:
            self._lock.release()
        if not outcome or not isinstance(outcome[0], dict):
            raise RuntimeError('local_protocol_invalid')
        return outcome[0]

    def _reap(self, timeout):
        # The caller's budget includes serialization and state-lock contention.
        deadline = time.monotonic()+timeout
        remaining = lambda: max(0,deadline-time.monotonic())
        if not self._reap_lock.acquire(timeout=remaining()):
            return False
        try:
            if not self._lock.acquire(timeout=remaining()):
                return False
            try:
                self._state = 'REAPING'
                if self._timer is not None:
                    self._timer.cancel()
                    self._timer = None
                process, job, connection, io = self._process, self._job, self._connection, self._io
            finally:
                self._lock.release()
            try:
                if job is not None:
                    job.terminate()
                # A gated wrapper may not have been assigned when assignment fails.
                if process is not None and process.is_alive():
                    process.terminate()
                if connection is not None:
                    connection.close()
                if process is not None and process.pid is not None:
                    process.join(remaining())
                    if process.is_alive():
                        return False
                if job is not None and not job.wait_empty(remaining()):
                    return False
                if io is not None:
                    io.join(remaining())
                    if io.is_alive():
                        return False
            except Exception:
                return False
            # Retain all ownership references if any proof or finalization cannot
            # finish in this call. A later close/replacement can retry cleanup.
            if not self._lock.acquire(timeout=remaining()):
                return False
            try:
                if self._process is not process or self._job is not job:
                    return False
                if self._io is not None and self._io is not io:
                    return False
                if job is not None:
                    job.close()
                if process is not None:
                    process.close()
                self._process = self._connection = self._job = self._io = self._config = None
                self._state = 'STOPPED'
                return True
            except Exception:
                self._error = 'local_reap_incomplete'
                return False
            finally:
                self._lock.release()
        finally:
            self._reap_lock.release()

    def _start(self, config):
        deadline = time.monotonic()+config.startup_timeout
        if os.name != 'nt':
            raise RuntimeError('local_tree_guarantee_unsupported')
        with self._lock:
            if self._closed:
                raise RuntimeError('local_service_closed')
            self._generation += 1
            self._state = 'STARTING'
            self._provenance = None
            self._config = config
            context = multiprocessing.get_context('spawn')
            parent, child = context.Pipe(duplex=True)
            gate = context.Event()
            self._connection = parent
            self._job = WorkerJob()
            self._process = context.Process(target=child_main, args=(child,gate,config,self._generation), daemon=True)
            self._process.start()
            child.close()
            self._job.assign(self._process)
            if self._closed:
                raise RuntimeError('local_service_closed')
            gate.set()
        reply = self._exchange(None, max(.001,deadline-time.monotonic()))
        if reply.get('kind') != 'ready' or reply.get('generation') != self._generation or not self._valid_provenance(reply.get('provenance'),config):
            raise RuntimeError('local_startup_invalid')
        with self._lock:
            if self._closed:
                raise RuntimeError('local_service_closed')
            self._provenance = reply['provenance']
            self._state = 'READY'

    def _idle(self, generation, activity):
        if not self._operation.acquire(False):
            return
        try:
            with self._lock:
                if self._generation != generation or self._activity != activity or self._state != 'READY':
                    return
                timeout = self._config.reap_timeout
            self._reap(timeout)
        finally:
            self._operation.release()

    def _edit(self, config, system, user):
        if not isinstance(system,str) or not isinstance(user,str) or len(system)+len(user) > MAX_REQUEST_CHARS:
            raise ValueError('local_request_invalid')
        if not self._operation.acquire(False):
            raise RuntimeError('local_worker_busy')
        try:
            with self._lock:
                if self._closed:
                    raise RuntimeError('local_service_closed')
                self._activity += 1
                with open(config.manifest_path,'rb') as stream:
                    current_manifest = stream.read(65537)
                if len(current_manifest)>65536 or hashlib.sha256(current_manifest).hexdigest() != config.manifest_sha256:
                    raise RuntimeError('local_manifest_changed')
                if self._timer is not None:
                    self._timer.cancel()
                    self._timer = None
                replace = self._process is not None and (self._config != config or self._state != 'READY' or not self._process.is_alive())
            if replace and not self._reap(config.reap_timeout):
                raise RuntimeError('local_reap_incomplete')
            if self._process is None:
                self._start(config)
            with self._lock:
                self._state = 'EDITING'
                generation = self._generation
            request_id = uuid.uuid4().hex
            reply = self._exchange({'generation':generation,'id':request_id,'system':system,'user':user},config.request_timeout)
            if reply.get('generation') != generation or reply.get('id') != request_id or reply.get('kind') != 'result' or not isinstance(reply.get('content'),str):
                raise RuntimeError('local_protocol_invalid')
            with self._lock:
                if self._closed:
                    raise RuntimeError('local_service_closed')
                self._state = 'READY'
                self._error = None
                self._timer = threading.Timer(config.idle_timeout, self._idle, args=(generation,self._activity))
                self._timer.daemon = True
                self._timer.start()
            return reply['content']
        except Exception as exc:
            with self._lock:
                self._error = str(exc) if str(exc) in {'local_worker_busy','local_operation_timeout','local_service_closed','local_reap_incomplete','local_tree_guarantee_unsupported'} else 'local_worker_failed'
            self._reap(config.reap_timeout)
            raise RuntimeError(self._error) from None
        finally:
            self._operation.release()

    def close(self, timeout_seconds=None):
        if timeout_seconds is not None and (type(timeout_seconds) not in (float,int) or not math.isfinite(timeout_seconds) or not 0 <= timeout_seconds <= 30):
            raise ValueError('local_deadline_invalid')
        # Publish cancellation without waiting for a lock held by startup work.
        # Every path that creates I/O or opens the spawn gate rechecks this flag.
        self._closed = True
        config = self._config
        timeout = timeout_seconds if timeout_seconds is not None else (config.reap_timeout if config else 5)
        return self._reap(timeout)
