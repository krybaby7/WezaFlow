"""Abrupt-owner fixture for actual Windows kill-on-close containment tests."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import json
import time
from unittest.mock import patch
from tests.test_local_editor_worker import fixture_child, gated_wait_child
from services.llm.local_worker import LocalEditorConfig, LocalEditorService


if __name__ == '__main__':
    if '--unassigned' in sys.argv:
        import multiprocessing
        context = multiprocessing.get_context('spawn')
        connection, child = context.Pipe()
        gate = context.Event()
        config = LocalEditorConfig(sys.argv[1],startup_timeout=.5)
        process = context.Process(target=gated_wait_child,args=(child,gate,config,1))
        process.start()
        child.close()
        connection.recv_bytes()
        print(json.dumps({'pid':process.pid}),flush=True)
        time.sleep(60)
        raise SystemExit
    service = LocalEditorService()
    with patch('services.llm.local_worker.child_main', fixture_child):
        service.backend(LocalEditorConfig(sys.argv[1], idle_timeout=60)).edit(system_prompt='policy', user_payload='hello')
        print(json.dumps(service.snapshot()), flush=True)
        time.sleep(60)
