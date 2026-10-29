"""Launched by the Windows job crash-containment test."""
import sys
import time
import numpy as np
from services.asr.worker import AsrWorkerConfig, AsrWorkerService
from tests.fixtures.asr_worker_fixture import fixture_worker

if __name__ == '__main__':
    service = AsrWorkerService(_worker_target=fixture_worker)
    result = service.transcriber(AsrWorkerConfig(sys.argv[1],'cpu','int8',1)).transcribe(np.zeros(100,dtype=np.float32))
    print(result.text,flush=True)
    time.sleep(120)
