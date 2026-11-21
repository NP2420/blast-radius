"""In-process stand-in for concurrent.futures.ProcessPoolExecutor.

sqlglot's optimizer and executor tests submit work to spawn-based process
pools. Running that work in-process keeps it visible to coverage (map build)
and avoids paying for fresh interpreters (local runs). Arguments and results
round-trip through pickle, as they would between processes, so shared mutable
state behaves like it does with a real pool.
"""
import concurrent.futures as cf
import pickle


class SyncExecutor(cf.Executor):
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kwargs):
        self._shutdown = False
        if initializer is not None:
            initializer(*initargs)

    def submit(self, fn, /, *args, **kwargs):
        fut = cf.Future()
        try:
            fn_, args_, kwargs_ = pickle.loads(pickle.dumps((fn, args, kwargs)))
            res = fn_(*args_, **kwargs_)
            fut.set_result(pickle.loads(pickle.dumps(res)))
        except BaseException as e:  # noqa: BLE001 - mirror pool behaviour
            fut.set_exception(e)
        return fut

    def shutdown(self, wait=True, *, cancel_futures=False):
        self._shutdown = True


def install() -> None:
    cf.ProcessPoolExecutor = SyncExecutor
    try:
        import concurrent.futures.process as cfp
        cfp.ProcessPoolExecutor = SyncExecutor
    except Exception:
        pass
