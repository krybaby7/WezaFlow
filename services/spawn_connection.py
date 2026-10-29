"""Transfer Windows pipe ownership before the spawned child can be cancelled."""
import os


def child_connection(connection):
    if os.name == 'nt':
        from multiprocessing.connection import PipeConnection
        if isinstance(connection, PipeConnection):
            return _ChildPipe(connection)
    return connection


class _ChildPipe:
    def __init__(self, connection):
        self.connection = connection

    def __reduce__(self):
        import _winapi
        from multiprocessing.context import get_spawning_popen
        from multiprocessing.reduction import DupHandle
        connection = self.connection
        access = ((_winapi.FILE_GENERIC_READ if connection.readable else 0) |
                  (_winapi.FILE_GENERIC_WRITE if connection.writable else 0))
        # CPython's default reducer leaves a duplicate in the parent until the
        # child unpickles it. Early termination strands that handle and prevents
        # pipe EOF. Duplicate directly into the child so Windows always reaps it.
        handle = DupHandle(connection.fileno(), access, pid=get_spawning_popen().pid)
        return _rebuild_child_pipe, (handle, connection.readable, connection.writable)


def _rebuild_child_pipe(handle, readable, writable):
    from multiprocessing.connection import PipeConnection
    return PipeConnection(handle.detach(), readable, writable)
