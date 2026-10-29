"""Windows crash containment. Only the engine owns the kill-on-close job handle."""
import os

class WorkerJob:
    def __init__(self):
        self.handle = None
        if os.name != 'nt':
            return  # POSIX cleanup works; abrupt-parent containment is Windows-only.
        import ctypes
        from ctypes import wintypes
        class BASIC(ctypes.Structure):
            _fields_ = [('PerProcessUserTimeLimit',ctypes.c_int64),('PerJobUserTimeLimit',ctypes.c_int64),('LimitFlags',wintypes.DWORD),('MinimumWorkingSetSize',ctypes.c_size_t),('MaximumWorkingSetSize',ctypes.c_size_t),('ActiveProcessLimit',wintypes.DWORD),('Affinity',ctypes.c_size_t),('PriorityClass',wintypes.DWORD),('SchedulingClass',wintypes.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(n,ctypes.c_uint64) for n in ('ReadOperationCount','WriteOperationCount','OtherOperationCount','ReadTransferCount','WriteTransferCount','OtherTransferCount')]
        class EXTENDED(ctypes.Structure):
            _fields_ = [('BasicLimitInformation',BASIC),('IoInfo',IO),('ProcessMemoryLimit',ctypes.c_size_t),('JobMemoryLimit',ctypes.c_size_t),('PeakProcessMemoryUsed',ctypes.c_size_t),('PeakJobMemoryUsed',ctypes.c_size_t)]
        kernel = ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p,wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD]
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE,wintypes.HANDLE]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel = kernel
        handle = kernel.CreateJobObjectW(None,None)
        if not handle:
            raise RuntimeError('worker_job_create_failed')
        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = 0x2000
        if not kernel.SetInformationJobObject(handle,9,ctypes.byref(info),ctypes.sizeof(info)):
            kernel.CloseHandle(handle)
            raise RuntimeError('worker_job_configure_failed')
        self.handle = handle

    def assign(self, process):
        if self.handle is not None and not self.kernel.AssignProcessToJobObject(self.handle, int(process.sentinel)):
            raise RuntimeError('worker_job_assign_failed')

    def close(self):
        if self.handle is not None:
            if not self.kernel.CloseHandle(self.handle):
                raise RuntimeError('worker_job_close_failed')
            self.handle = None
