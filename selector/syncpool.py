"""In-process stand-in for concurrent.futures.ProcessPoolExecutor.

sqlglot's optimizer and executor tests submit work to spawn-based process
pools. Running that work in-process keeps it visible to coverage (map build)
and avoids paying for fresh interpreters (local runs). Arguments and results
round-trip through pickle, as they would between processes, so shared mutable
state behaves like it does with a real pool.
"""
import concurrent.futures as cf
import pickle


ALLOWED = [None]  # set of submission indices to run for the current test; None = all
LAZY = [False]    # run each task only when its result is first asked for (tests that read results in order)


class _LazyFuture(cf.Future):
    def __init__(self, payload):
        super().__init__()
        self._payload = payload

    def _run(self):
        if self.done() or self._payload is None:
            return
        payload, self._payload = self._payload, None
        try:
            fn_, args_, kwargs_ = pickle.loads(payload)
            res = fn_(*args_, **kwargs_)
            self.set_result(pickle.loads(pickle.dumps(res)))
        except BaseException as e:  # noqa: BLE001 - mirror pool behaviour
            self.set_exception(e)

    def result(self, timeout=None):
        self._run()
        return super().result(timeout)

    def exception(self, timeout=None):
        self._run()
        return super().exception(timeout)


class SyncExecutor(cf.Executor):
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kwargs):
        self._shutdown = False
        self._n = 0
        if initializer is not None:
            initializer(*initargs)

    def submit(self, fn, /, *args, **kwargs):
        fut = cf.Future()
        idx = getattr(self, "_n", 0)
        self._n = idx + 1
        allowed = ALLOWED[0]
        if allowed is not None and idx not in allowed:
            fut.set_result(None)  # executor tests skip queries whose result is None
            return fut
        if LAZY[0]:
            return _LazyFuture(pickle.dumps((fn, args, kwargs)))
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
