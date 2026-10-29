"""Spawn entry point. No native imports occur before the engine startup gate."""
import base64
import json
import os
import time
from dataclasses import asdict

MAX_FRAME = 30 * 1024 * 1024

def encode(message):
    data = json.dumps(message,ensure_ascii=True,allow_nan=False,separators=(',', ':')).encode('utf-8')
    if len(data) > MAX_FRAME:
        raise ValueError('worker_frame_oversized')
    return data

def receive(connection):
    value = json.loads(connection.recv_bytes(MAX_FRAME))
    if not isinstance(value, dict):
        raise ValueError('worker_protocol_invalid')
    return value

def worker_main(connection, config, identity, generation, runtime_hash):
    stage = 'startup_failed'
    request_id = None
    started = time.perf_counter()
    try:
        if not connection.poll(config.startup_timeout_seconds):
            return
        gate = receive(connection)
        if gate != {'op':'start','generation':generation,'identity':identity}:
            return
        from services.asr.local_artifacts import validate_snapshot, configure_runtime, REQUIRED_FILES, file_hash
        root = validate_snapshot(config.snapshot)
        files = {name:{'sha256':file_hash(root/name),'bytes':(root/name).stat().st_size} for name in REQUIRED_FILES}
        runtime_handles = configure_runtime(config.runtime_manifest, runtime_hash)
        import numpy as np
        from services.asr.transcriber import Transcriber, FasterWhisperBackend
        transcriber = Transcriber(backend=FasterWhisperBackend(config.snapshot, device=config.device, compute_type=config.compute_type, cpu_threads=config.cpu_threads, local_files_only=True))
        loading = time.perf_counter()
        _ = transcriber.backend.model
        warming = time.perf_counter()
        transcriber.transcribe(np.zeros(16000,dtype=np.float32), language='en')
        ready = time.perf_counter()
        provenance = {'snapshot':{'path':str(root),'files':files},'runtime':{'manifest':config.runtime_manifest,'sha256':runtime_hash},'startup':{'load_ms':int((warming-loading)*1000),'warmup_ms':int((ready-warming)*1000),'total_ms':int((ready-started)*1000)}}
        connection.send_bytes(encode({'op':'ready','generation':generation,'identity':identity,'provenance':provenance}))
        stage = 'decode_failed'
        while True:
            request_id = None
            request = receive(connection)
            request_id = request.get('request')
            if request.get('generation') != generation or request.get('identity') != identity or request.get('op') != 'decode':
                return
            audio = np.frombuffer(base64.b64decode(request['audio'],validate=True),dtype='<f4').copy()
            if audio.ndim != 1 or not 0 < audio.size <= 4800000 or not np.isfinite(audio).all():
                return
            result = transcriber.transcribe(audio, language=request['language'],initial_prompt=request['initial_prompt'])
            connection.send_bytes(encode({'op':'result','generation':generation,'identity':identity,'request':request['request'],'result':asdict(result)}))
    except BaseException:
        # Any backend/protocol failure poisons this process. Never expose input or native error text.
        try:
            connection.send_bytes(encode({'op':'error','generation':generation,'identity':identity,'request':request_id,'code':stage}))
        except BaseException:
            pass
    finally:
        connection.close()
