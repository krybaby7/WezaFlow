"""Importable real-spawn fault fixtures; never import a native backend."""
import os
import struct
import time
from services.asr.worker_process import receive, encode

def fixture_worker(connection, config, identity, generation, runtime_hash):
    from pathlib import Path
    mode_path = Path(config.snapshot)/'fixture-mode'
    mode = mode_path.read_text() if mode_path.exists() else 'normal'
    receive(connection)
    if mode == 'startup_error':
        connection.send_bytes(encode({'op':'error','generation':generation,'identity':identity,'code':'startup_failed'}))
        return
    if mode == 'hung_start':
        time.sleep(120)
    if mode == 'die_start':
        os._exit(2)
    ready = {'op':'ready','generation':generation,'identity':identity}
    if mode == 'provenance':
        from services.asr.local_artifacts import REQUIRED_FILES, file_hash
        root = Path(config.snapshot)
        ready['provenance'] = {'snapshot':{'path':str(root),'files':{name:{'sha256':file_hash(root/name),'bytes':(root/name).stat().st_size} for name in REQUIRED_FILES}},'runtime':{'manifest':None,'sha256':None},'startup':{'load_ms':1,'warmup_ms':2,'total_ms':3}}
    connection.send_bytes(encode(ready))
    if mode == 'hung_read':
        time.sleep(120)
    while True:
        request = receive(connection)
        (Path(config.snapshot)/'fixture-decode').write_text('decode')
        if mode in ('decode_error','unsafe_error'):
            connection.send_bytes(encode({'op':'error','generation':generation,'identity':identity,'request':request['request'],'code':'decode_failed' if mode == 'decode_error' else 'unsafe user transcript'}))
            return
        if mode == 'hung_decode':
            time.sleep(120)
        if mode == 'die_decode':
            os._exit(3)
        if mode == 'malformed':
            connection.send_bytes(b'{')
            return
        if mode == 'partial':
            if hasattr(connection,'_send'):
                connection._send(struct.pack('!i',1000)+b'{')
            elif os.name == 'nt':
                import _winapi
                _winapi.WriteFile(connection.fileno(),struct.pack('!i',1000)+b'{',overlapped=False)
            else:
                os.write(connection.fileno(),struct.pack('!i',1000)+b'{')
            time.sleep(120)
        if mode == 'oversized':
            connection.send_bytes(b'x'*(31*1024*1024))
            return
        result = {'text':str(os.getpid()),'language':'en','duration_ms':1,'inference_ms':1,'queue_ms':0,'audio_duration_ms':1}
        connection.send_bytes(encode({'op':'result','generation':generation if mode != 'mismatch' else 'bad','identity':identity,'request':request['request'],'result':result}))


def stream_connections():
    import socket
    from multiprocessing.connection import Connection
    first, second = socket.socketpair()
    return Connection(first.detach()), Connection(second.detach())
