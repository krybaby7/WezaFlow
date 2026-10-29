"""Engine-owned, bounded, offline ASR process supervision.

idle_timeout_seconds=0 releases the worker after each completed request.
There is no queue: overlapping callers receive worker_busy.
"""
from __future__ import annotations
import base64
import hashlib
import json
import math
import multiprocessing
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from services.pipeline.models import AsrResult
from services.asr.local_artifacts import file_hash, validate_snapshot
from services.asr.worker_process import worker_main, encode, receive
from services.asr.windows_job import WorkerJob
from services.spawn_connection import child_connection

class AsrWorkerError(RuntimeError):
    """Sanitized machine-readable supervisor failure."""

@dataclass(frozen=True)
class AsrWorkerConfig:
    snapshot: str
    device: str
    compute_type: str
    cpu_threads: int
    runtime_manifest: str | None = None
    startup_timeout_seconds: float = 90
    decode_timeout_seconds: float = 60
    idle_timeout_seconds: float = 60
    reap_timeout_seconds: float = 5

    def __post_init__(self):
        if not isinstance(self.snapshot,str):
            raise ValueError('worker_snapshot_invalid')
        validate_snapshot(self.snapshot)
        if self.device not in ('cpu','cuda') or self.compute_type not in ('default','auto','int8','int8_float32','int8_float16','int8_bfloat16','int16','float16','bfloat16','float32'):
            raise ValueError('worker_config_invalid')
        if type(self.cpu_threads) is not int or not 1 <= self.cpu_threads <= 256:
            raise ValueError('worker_threads_invalid')
        for name in ('startup_timeout_seconds','decode_timeout_seconds','idle_timeout_seconds','reap_timeout_seconds'):
            value = getattr(self,name)
            if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value > {'startup_timeout_seconds':600,'decode_timeout_seconds':600,'idle_timeout_seconds':3600,'reap_timeout_seconds':30}[name] or value < 0 or (value == 0 and name != 'idle_timeout_seconds'):
                raise ValueError('worker_timeout_invalid')
        if self.runtime_manifest is not None and (not isinstance(self.runtime_manifest,str) or not Path(self.runtime_manifest).is_absolute() or not Path(self.runtime_manifest).is_file() or not 0 < Path(self.runtime_manifest).stat().st_size <= 1024*1024):
            raise ValueError('worker_runtime_invalid')

class WorkerTranscriber:
    def __init__(self, service, config):
        self._service, self.config = service, config
    def transcribe(self, audio, language=None, initial_prompt=None):
        # The pipeline's WAV/VAD adapters produce ordinary sample lists. Bound
        # their size and scalar shape before allocating a contiguous buffer.
        if isinstance(audio,(list,tuple)):
            from numbers import Real
            if not 0 < len(audio) <= 4800000 or any(not isinstance(sample,Real) for sample in audio):
                raise ValueError('worker_audio_invalid')
            try:
                import numpy as np
                audio = np.asarray(audio,dtype=np.float32)
            except (ValueError,TypeError,OverflowError):
                raise ValueError('worker_audio_invalid') from None
        return self._service._transcribe(self.config,audio,language,initial_prompt)

class AsrWorkerService:
    def __init__(self, *, _worker_target=worker_main, _connection_factory=None):
        self._target = _worker_target
        self._connection_factory = _connection_factory
        self._lock = threading.RLock()
        self._operation = threading.Lock()
        self._reaping = threading.Lock()
        self._process = self._connection = self._io = self._job = self._timer = None
        self._identity = self._generation = None
        self._status = 'STOPPED'
        self._closed = False
        self._error = None
        self._completion = 0
        self._provenance = None

    def transcriber(self, config):
        if not isinstance(config,AsrWorkerConfig):
            raise ValueError('worker_config_invalid')
        return WorkerTranscriber(self,config)

    def snapshot(self):
        with self._lock:
            if self._process is not None and self._process.pid is not None and self._status == 'READY' and not self._process.is_alive():
                self._status = 'UNHEALTHY'
                self._error = 'worker_exited'
            return {'provenance':json.loads(json.dumps(self._provenance)),'state':self._status,'busy':self._status in ('STARTING','DECODING','REAPING'),'last_error':self._error,'status':self._status,'pid':None if self._process is None else self._process.pid,'generation':self._generation,'config_identity':self._identity,'error':self._error,'owns_process':self._process is not None,'owns_io':self._io is not None,'closed':self._closed}

    def _exchange(self, message, timeout):
        done = threading.Event()
        outcome = []
        with self._lock:
            connection = self._connection
            if self._closed:
                raise AsrWorkerError('worker_closed')
            def exchange():
                try:
                    connection.send_bytes(encode(message))
                    outcome.append(receive(connection))
                except BaseException:
                    outcome.append(None)
                finally:
                    done.set()
            thread = threading.Thread(target=exchange,name='asr-worker-io',daemon=True)
            self._io = thread
            try:
                thread.start()
            except Exception:
                self._io = None
                raise AsrWorkerError('worker_io_start_failed') from None
        if not done.wait(timeout):
            raise AsrWorkerError('worker_timeout')
        thread.join()
        with self._lock:
            if self._io is thread:
                self._io = None
        reply = outcome[0]
        if not isinstance(reply,dict) or reply.get('generation') != message['generation'] or reply.get('identity') != message['identity']:
            raise AsrWorkerError('worker_protocol_error')
        if reply.get('op') == 'error':
            expected = 'startup_failed' if message['op'] == 'start' else 'decode_failed'
            if reply.get('code') != expected or (message['op'] == 'decode' and reply.get('request') != message.get('request')):
                raise AsrWorkerError('worker_protocol_error')
            raise AsrWorkerError('worker_'+expected)
        return reply

    def _reap(self, timeout):
        deadline = time.monotonic()+timeout
        if not self._reaping.acquire(timeout=max(0,timeout)):
            return False
        try:
            with self._lock:
                process, connection, io, job = self._process,self._connection,self._io,self._job
                self._status = 'REAPING'
            if process is not None and process.pid is not None:
                try:
                    if process.is_alive():
                        process.terminate()
                    process.join(max(0,deadline-time.monotonic()))
                    if process.is_alive():
                        process.kill()
                        process.join(max(0,deadline-time.monotonic()))
                except Exception:
                    pass
            # Closing the job is also a kill guarantee if normal process termination failed.
            if job is not None:
                try:
                    job.close()
                except Exception:
                    return False
            if io is not None and io is not threading.current_thread():
                io.join(max(0,deadline-time.monotonic()))
            if (process is not None and process.pid is not None and process.is_alive()) or (io is not None and io.is_alive()):
                with self._lock:
                    self._error = 'worker_cleanup_incomplete'
                return False
            if connection is not None:
                connection.close()
            with self._lock:
                if process is not None:
                    process.close()
                self._process = self._connection = self._io = self._job = None
                self._identity = self._generation = None
                self._status = 'CLOSED' if self._closed else 'STOPPED'
            return True
        finally:
            self._reaping.release()

    def close(self, timeout_seconds=5):
        if isinstance(timeout_seconds,bool) or not isinstance(timeout_seconds,(int,float)) or not math.isfinite(timeout_seconds) or timeout_seconds < 0:
            raise ValueError('worker_timeout_invalid')
        deadline = time.monotonic()+timeout_seconds
        with self._lock:
            self._closed = True
            timer = self._timer
            if timer is not None:
                timer.cancel()
        result = self._reap(max(0,deadline-time.monotonic()))
        if timer is not None and timer is not threading.current_thread():
            timer.join(max(0,deadline-time.monotonic()))
            result = result and not timer.is_alive()
        return result

    def _cancel_idle(self):
        with self._lock:
            timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()
            timer.join()

    def _schedule_idle(self, config):
        with self._lock:
            if self._closed or self._process is None:
                return
            generation = self._generation
            completion = self._completion
            def release():
                while not self._operation.acquire(False):
                    if timer.finished.wait(.01):
                        return
                try:
                    with self._lock:
                        if self._generation != generation or self._completion != completion or self._status != 'READY':
                            return
                    self._reap(config.reap_timeout_seconds)
                finally:
                    self._operation.release()
            timer = threading.Timer(config.idle_timeout_seconds,release)
            self._timer = timer
            timer.daemon = True
            timer.start()

    def _start(self, config, identity, runtime_hash):
        deadline = time.monotonic()+config.startup_timeout_seconds
        with self._lock:
            if self._closed:
                raise AsrWorkerError('worker_closed')
            parent, child = (self._connection_factory or multiprocessing.get_context('spawn').Pipe)()
            generation = uuid.uuid4().hex
            process = multiprocessing.get_context('spawn').Process(target=self._target,args=(child_connection(child),config,identity,generation,runtime_hash))
            self._connection,self._process,self._identity,self._generation = parent,process,identity,generation
            self._status = 'STARTING'
            self._provenance = None
            try:
                self._job = WorkerJob()
                process.start()
                child.close()
                self._job.assign(process)
            except BaseException:
                child.close()
                raise AsrWorkerError('worker_start_failed') from None
        reply = self._exchange({'op':'start','generation':generation,'identity':identity},max(0,deadline-time.monotonic()))
        if reply.get('op') != 'ready':
            raise AsrWorkerError('worker_protocol_error')
        with self._lock:
            if self._closed:
                raise AsrWorkerError('worker_closed')
            self._provenance = _provenance(reply.get('provenance'))
            self._status = 'READY'

    def _transcribe(self, config, audio, language, initial_prompt):
        if not self._operation.acquire(False):
            raise AsrWorkerError('worker_busy')
        try:
            with self._lock:
                if self._closed:
                    raise AsrWorkerError('worker_closed')
            self._cancel_idle()
            import numpy as np
            if not isinstance(audio,np.ndarray) or audio.dtype != np.float32 or audio.ndim != 1 or not 0 < audio.size <= 4800000 or not np.isfinite(audio).all():
                raise ValueError('worker_audio_invalid')
            for value, bound, allow_empty in ((language,64,False),(initial_prompt,2000,True)):
                if value is not None and (not isinstance(value,str) or (not value and not allow_empty) or len(value)>bound or '\x00' in value):
                    raise ValueError('worker_text_option_invalid')
            try:
                if config.runtime_manifest and not 0 < Path(config.runtime_manifest).stat().st_size <= 1024*1024:
                    raise ValueError('worker_runtime_invalid')
                runtime_hash = file_hash(config.runtime_manifest) if config.runtime_manifest else None
            except Exception:
                raise AsrWorkerError('worker_runtime_invalid') from None
            identity = hashlib.sha256(json.dumps([asdict(config),runtime_hash],sort_keys=True).encode()).hexdigest()
            if self._process is not None and self._identity != identity:
                if not self._reap(config.reap_timeout_seconds):
                    raise AsrWorkerError('worker_cleanup_incomplete')
            try:
                if self._process is None:
                    self._start(config,identity,runtime_hash)
                with self._lock:
                    if self._status != 'READY' or self._closed:
                        raise AsrWorkerError('worker_unavailable')
                    self._status = 'DECODING'
                request = uuid.uuid4().hex
                reply = self._exchange({'op':'decode','generation':self._generation,'identity':identity,'request':request,'audio':base64.b64encode(audio.astype('<f4',copy=False).tobytes()).decode('ascii'),'language':language,'initial_prompt':initial_prompt},config.decode_timeout_seconds)
                if reply.get('op') != 'result' or reply.get('request') != request:
                    raise AsrWorkerError('worker_protocol_error')
                result = _result(reply.get('result'))
                with self._lock:
                    if self._closed:
                        raise AsrWorkerError('worker_closed')
                    self._status = 'READY'
                    self._completion += 1
                    self._error = None
                if config.idle_timeout_seconds == 0:
                    self._reap(config.reap_timeout_seconds)
                return result
            except BaseException as exc:
                with self._lock:
                    self._error = str(exc) if isinstance(exc,AsrWorkerError) else 'worker_failed'
                self._reap(config.reap_timeout_seconds)
                raise AsrWorkerError(self._error) from None
        finally:
            # Install the sole timer while still owning the operation. A timer
            # checks the operation and completion generation before unloading.
            if config.idle_timeout_seconds > 0:
                self._schedule_idle(config)
            self._operation.release()

def _result(value):
    if not isinstance(value,dict) or set(value) != {'text','language','duration_ms','inference_ms','queue_ms','audio_duration_ms'}:
        raise AsrWorkerError('worker_result_invalid')
    if not isinstance(value['text'],str) or len(value['text']) > 1000000 or (value['language'] is not None and (not isinstance(value['language'],str) or len(value['language'])>64)):
        raise AsrWorkerError('worker_result_invalid')
    for key in ('duration_ms','inference_ms','queue_ms','audio_duration_ms'):
        if value[key] is None and key == 'audio_duration_ms':
            continue
        if type(value[key]) is not int or not 0 <= value[key] <= 2147483647:
            raise AsrWorkerError('worker_result_invalid')
    return AsrResult(**value)


def _provenance(value):
    """Only bounded model/runtime facts may enter diagnostics, never child text."""
    if value is None:
        return None
    from services.asr.local_artifacts import REQUIRED_FILES
    try:
        if set(value) != {'snapshot','runtime','startup'} or set(value['snapshot']) != {'path','files'} or set(value['runtime']) != {'manifest','sha256'} or set(value['startup']) != {'load_ms','warmup_ms','total_ms'}:
            raise ValueError()
        snapshot = value['snapshot']
        if not isinstance(snapshot['path'],str) or len(snapshot['path'])>32768 or set(snapshot['files']) != set(REQUIRED_FILES):
            raise ValueError()
        for artifact in snapshot['files'].values():
            if set(artifact) != {'sha256','bytes'} or type(artifact['bytes']) is not int or artifact['bytes'] <= 0:
                raise ValueError()
            if not _digest(artifact['sha256']):
                raise ValueError()
        runtime = value['runtime']
        if runtime['manifest'] is not None and (not isinstance(runtime['manifest'],str) or len(runtime['manifest'])>32768 or not _digest(runtime['sha256'])):
            raise ValueError()
        if runtime['manifest'] is None and runtime['sha256'] is not None:
            raise ValueError()
        if any(type(item) is not int or not 0 <= item <= 600000 for item in value['startup'].values()):
            raise ValueError()
    except (TypeError,KeyError,ValueError,AttributeError):
        raise AsrWorkerError('worker_provenance_invalid') from None
    # Return a detached JSON-safe copy: callers cannot mutate retained ownership facts.
    return json.loads(json.dumps(value))

def _digest(value):
    return isinstance(value,str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)
