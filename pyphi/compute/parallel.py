# -*- coding: utf-8 -*-
# compute/parallel.py

"""
Utilities for parallel computation.
"""

import functools
import logging
import math
import multiprocessing
import sys
import threading
from itertools import chain, islice
from typing import Iterable, List

import ray
from more_itertools import chunked, flatten as iflatten
from tblib import Traceback
from tqdm.auto import tqdm

from .. import config
from ..conf import fallback

log = logging.getLogger(__name__)

# Protect reference to builtin map
_map_builtin = map


def get_num_processes():
    """Return the number of processes to use in parallel."""
    cpu_count = multiprocessing.cpu_count()

    if config.NUMBER_OF_CORES == 0:
        raise ValueError("Invalid NUMBER_OF_CORES; value may not be 0.")

    if config.NUMBER_OF_CORES > cpu_count:
        log.info(
            "Requesting %s cores; only %s available", config.NUMBER_OF_CORES, cpu_count
        )
        return cpu_count

    if config.NUMBER_OF_CORES < 0:
        num = cpu_count + config.NUMBER_OF_CORES + 1
        if num <= 0:
            raise ValueError(
                "Invalid NUMBER_OF_CORES; negative value is too negative: "
                f"requesting {num} cores, {cpu_count} available."
            )

        return num

    return config.NUMBER_OF_CORES


class ExceptionWrapper:
    """A picklable wrapper suitable for passing exception tracebacks through
    instances of ``multiprocessing.Queue``.

    Args:
        exception (Exception): The exception to wrap.
    """

    def __init__(self, exception):  # coverage: disable
        self.exception = exception
        _, _, tb = sys.exc_info()
        self.tb = Traceback(tb)

    def reraise(self):
        """Re-raise the exception."""
        raise self.exception.with_traceback(self.tb.as_traceback())


POISON_PILL = None
Q_MAX_SIZE = multiprocessing.synchronize.SEM_VALUE_MAX


class MapReduce:
    """An engine for doing heavy computations over an iterable.

    This is similar to ``multiprocessing.Pool``, but allows computations to
    shortcircuit, and supports both parallel and sequential computations.

    Args:
        iterable (Iterable): A collection of objects to perform a computation
            over.
        *context: Any additional data necessary to complete the computation.

    Any subclass of ``MapReduce`` must implement three methods::

        - ``empty_result``,
        - ``compute``, (map), and
        - ``process_result`` (reduce).

    The engine includes a builtin ``tqdm`` progress bar; this can be disabled
    by setting ``pyphi.config.PROGRESS_BARS`` to ``False``.

    Parallel operations start a daemon thread which handles log messages sent
    from worker processes.

    Subprocesses spawned by ``MapReduce`` cannot spawn more subprocesses; be
    aware of this when composing nested computations. This is not an issue in
    practice because it is typically most efficient to only parallelize the top
    level computation.
    """

    # Description for the tqdm progress bar
    description = ""

    def __init__(self, iterable, *context):
        self.iterable = iterable
        self.context = context
        self.done = False
        self.progress = self.init_progress_bar()

        # Attributes used by parallel computations
        self.task_queue = None
        self.result_queue = None
        self.log_queue = None
        self.log_thread = None
        self.processes = None
        self.num_processes = None
        self.tasks = None
        self.complete = None

    def empty_result(self, *context):
        """Return the default result with which to begin the computation."""
        raise NotImplementedError

    @staticmethod
    def compute(obj, *context):
        """Map over a single object from ``self.iterable``."""
        raise NotImplementedError

    def process_result(self, new_result, old_result):
        """Reduce handler.

        Every time a new result is generated by ``compute``, this method is
        called with the result and the previous (accumulated) result. This
        method compares or collates these two values, returning the new result.

        Setting ``self.done`` to ``True`` in this method will abort the
        remainder of the computation, returning this final result.
        """
        raise NotImplementedError

    #: Is this process a subprocess in a parallel computation?
    _forked = False

    # TODO: pass size of iterable alongside?
    def init_progress_bar(self):
        """Initialize and return a progress bar."""
        # Forked worker processes can't show progress bars.
        disable = MapReduce._forked or not config.PROGRESS_BARS

        # Don't materialize iterable unless we have to: huge iterables
        # (e.g. of `KCuts`) eat memory.
        if disable:
            total = None
        else:
            self.iterable = list(self.iterable)
            total = len(self.iterable)

        return tqdm(total=total, disable=disable, leave=False, desc=self.description)

    @staticmethod  # coverage: disable
    def worker(compute, task_queue, result_queue, log_queue, complete, *context):
        """A worker process, run by ``multiprocessing.Process``."""
        try:
            MapReduce._forked = True
            log.debug("Worker process starting...")

            configure_worker_logging(log_queue)

            for obj in iter(task_queue.get, POISON_PILL):
                if complete.is_set():
                    log.debug("Worker received signal - exiting early")
                    break

                log.debug("Worker got %s", obj)
                result_queue.put(compute(obj, *context))
                log.debug("Worker finished %s", obj)

            result_queue.put(POISON_PILL)
            log.debug("Worker process exiting")

        except Exception as e:  # pylint: disable=broad-except
            result_queue.put(ExceptionWrapper(e))

    def start_parallel(self):
        """Initialize all queues and start the worker processes and the log
        thread.
        """
        self.num_processes = get_num_processes()

        self.task_queue = multiprocessing.Queue(maxsize=Q_MAX_SIZE)
        self.result_queue = multiprocessing.Queue()
        self.log_queue = multiprocessing.Queue()

        # Used to signal worker processes when a result is found that allows
        # the computation to terminate early.
        self.complete = multiprocessing.Event()

        args = (
            self.compute,
            self.task_queue,
            self.result_queue,
            self.log_queue,
            self.complete,
        ) + self.context
        self.processes = [
            multiprocessing.Process(target=self.worker, args=args, daemon=True)
            for i in range(self.num_processes)
        ]

        for process in self.processes:
            process.start()

        self.log_thread = LogThread(self.log_queue)
        self.log_thread.start()

        self.initialize_tasks()

    def initialize_tasks(self):
        """Load the input queue to capacity.

        Overfilling causes a deadlock when `queue.put` blocks when
        full, so further tasks are enqueued as results are returned.
        """
        # Add a poison pill to shutdown each process.
        self.tasks = chain(self.iterable, [POISON_PILL] * self.num_processes)
        for task in islice(self.tasks, Q_MAX_SIZE):
            log.debug("Putting %s on queue", task)
            self.task_queue.put(task)

    def maybe_put_task(self):
        """Enqueue the next task, if there are any waiting."""
        try:
            task = next(self.tasks)
        except StopIteration:
            pass
        else:
            log.debug("Putting %s on queue", task)
            self.task_queue.put(task)

    def run_parallel(self):
        """Perform the computation in parallel, reading results from the output
        queue and passing them to ``process_result``.
        """
        try:
            self.start_parallel()

            result = self.empty_result(*self.context)

            while self.num_processes > 0:
                r = self.result_queue.get()
                self.maybe_put_task()

                if r is POISON_PILL:
                    self.num_processes -= 1

                elif isinstance(r, ExceptionWrapper):
                    r.reraise()

                else:
                    result = self.process_result(r, result)
                    self.progress.update(1)

                    # Did `process_result` decide to terminate early?
                    if self.done:
                        self.complete.set()

            self.finish_parallel()
        finally:
            log.debug("Removing progress bar")
            self.progress.close()

        return result

    def finish_parallel(self):
        """Orderly shutdown of workers."""
        for process in self.processes:
            process.join()

        # Shutdown the log thread
        log.debug("Joining log thread")
        self.log_queue.put(POISON_PILL)
        self.log_thread.join()
        self.log_queue.close()

        # Close all queues
        log.debug("Closing queues")
        self.task_queue.close()
        self.result_queue.close()

    def run_sequential(self):
        """Perform the computation sequentially, only holding two computed
        objects in memory at a time.
        """
        try:
            result = self.empty_result(*self.context)

            for obj in self.iterable:
                r = self.compute(obj, *self.context)
                result = self.process_result(r, result)
                self.progress.update(1)

                # Short-circuited?
                if self.done:
                    break
        except Exception as e:
            raise e
        finally:
            self.progress.close()

        return result

    def run(self, parallel=True):
        """Perform the computation.

        Keyword Args:
            parallel (boolean): If True, run the computation in parallel.
                Otherwise, operate sequentially.
        """
        if parallel:
            return self.run_parallel()
        return self.run_sequential()


# TODO: maintain a single log thread?
class LogThread(threading.Thread):
    """Thread which handles log records sent from ``MapReduce`` processes.

    It listens to an instance of ``multiprocessing.Queue``, rewriting log
    messages to the PyPhi log handler.
    """

    def __init__(self, q):
        self.q = q
        super().__init__()
        self.daemon = True

    def run(self):
        log.debug("Log thread started")
        while True:
            record = self.q.get()
            if record is POISON_PILL:
                break
            logger = logging.getLogger(record.name)
            logger.handle(record)
        log.debug("Log thread exiting")


def configure_worker_logging(queue):  # coverage: disable
    """Configure a worker process to log all messages to ``queue``."""
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "handlers": {
                "queue": {
                    "class": "logging.handlers.QueueHandler",
                    "queue": queue,
                },
            },
            "root": {"level": "DEBUG", "handlers": ["queue"]},
        }
    )


def init(*args, **kwargs):
    """Initialize Ray if not already initialized."""
    if not ray.is_initialized():
        return ray.init(
            *args,
            **{
                "num_cpus": get_num_processes(),
                **config.RAY_CONFIG,
                **kwargs,
            },
        )


class _NoShortCircuit:
    """An object that is not equal to anything and is falsy."""

    def __eq__(self, other):
        return False

    def __bool__(self):
        return False


def shortcircuit(
    items,
    shortcircuit_value=_NoShortCircuit(),
    shortcircuit_callback=None,
    shortcircuit_callback_args=None,
):
    """Yield from an iterable, stopping early if a certain value is found."""
    for result in items:
        yield result
        if result == shortcircuit_value:
            if shortcircuit_callback is not None:
                if shortcircuit_callback_args is None:
                    shortcircuit_callback_args = items
                shortcircuit_callback(shortcircuit_callback_args)
            return


def as_completed(object_refs: List[ray.ObjectRef], num_returns: int = 1):
    """Yield remote results in order of completion."""
    unfinished = object_refs
    while unfinished:
        finished, unfinished = ray.wait(unfinished, num_returns=num_returns)
        yield from ray.get(finished)


@functools.wraps(ray.cancel)
def cancel_all(object_refs: Iterable, *args, **kwargs):
    try:
        for ref in object_refs:
            ray.cancel(ref, *args, **kwargs)
        # TODO remove the following when ray.cancel is less noisy; see
        # https://github.com/ray-project/ray/issues/24658
        object_refs, _ = ray.wait(object_refs, num_returns=len(object_refs))
        for ref in object_refs:
            try:
                ray.get(ref)
            except (ray.exceptions.RayTaskError, ray.exceptions.TaskCancelledError):
                pass
    except TypeError:
        # Do nothing if the object_refs are not actually ObjectRefs
        pass
    return object_refs


def _try_len(iterable):
    try:
        return len(iterable)
    except TypeError:
        return None


def _try_lens(*iterables):
    """Return the minimum length of iterables, or ``None`` if none has a length."""
    return min(
        filter(lambda l: l is not None, _map_builtin(_try_len, iterables)), default=None
    )


def get(
    object_refs,
    parallel=False,
    shortcircuit_value=_NoShortCircuit(),
    shortcircuit_callback=cancel_all,
    shortcircuit_callback_args=None,
    progress=None,
    desc=None,
    total=None,
):
    """Get (potentially) remote results.

    Optionally return early if a particular value is found.
    """
    if shortcircuit_callback_args is None:
        shortcircuit_callback_args = object_refs

    if parallel:
        completed = as_completed(list(object_refs))
    else:
        completed = object_refs

    if fallback(progress, config.PROGRESS_BARS):
        completed = tqdm(completed, total=(total or _try_lens(object_refs)), desc=desc)

    return list(
        shortcircuit(
            completed,
            shortcircuit_value=shortcircuit_value,
            shortcircuit_callback=shortcircuit_callback,
            shortcircuit_callback_args=shortcircuit_callback_args,
        )
    )


def backpressure(func, *arglists, inflight_limit=1000, **kwargs):
    # https://docs.ray.io/en/latest/ray-core/tasks/patterns/limit-tasks.html
    result_refs = []
    for i, args in enumerate(zip(*arglists)):
        if len(result_refs) > inflight_limit:
            num_ready = i - inflight_limit
            ray.wait(result_refs, num_returns=num_ready)
        result_refs.append(func.remote(*args, **kwargs))
    return result_refs


def _map_sequential(func, *arglists, **kwargs):
    for args in zip(*arglists):
        yield func(*args, **kwargs)


def flatten(*args, **kwargs):
    return list(iflatten(*args, **kwargs))


def map(
    func,
    *arglists,
    chunksize=1,
    sequential_threshold=1,
    max_size=None,
    max_depth=None,
    branch_factor=2,
    shortcircuit_value=_NoShortCircuit(),
    shortcircuit_callback=cancel_all,
    inflight_limit=1000,
    parallel=True,
    progress=None,
    desc="",
    total=None,
    **kwargs,
):  # pylint: disable=redefined-builtin
    """Map a function to some arguments.

    Optionally return early if a particular value is found.
    """
    if not arglists:
        raise ValueError("no arguments")
    if parallel:
        return map_reduce(
            func,
            None,
            *arglists,
            chunksize=chunksize,
            sequential_threshold=sequential_threshold,
            max_size=max_size,
            max_depth=max_depth,
            branch_factor=branch_factor,
            inflight_limit=inflight_limit,
            shortcircuit_value=shortcircuit_value,
            shortcircuit_callback=shortcircuit_callback,
            parallel=True,
            **kwargs,
        )
    else:
        results = _map_sequential(func, *arglists, **kwargs)
    return get(
        results,
        parallel=parallel,
        shortcircuit_value=shortcircuit_value,
        shortcircuit_callback=shortcircuit_callback,
        progress=progress,
        total=(total or _try_lens(*arglists)),
        desc=desc,
    )


def tree_size(branch_factor, depth):
    """Return the number of nodes in a tree."""
    if branch_factor < 1:
        return depth
    return sum(branch_factor ** l for l in range(depth))


def get_depth(max_size, branch_factor):
    """Return depth required to not exceed given max tree size."""
    if max_size is None or math.isinf(max_size):
        return float("inf")
    depth = 1
    while tree_size(branch_factor, depth + 1) < max_size:
        depth += 1
    return depth


def _map_reduce_sequential(
    map_func,
    reduce_func,
    *arglists,
    reduce_kwargs=None,
    **kwargs,
):
    return reduce_func(
        map(map_func, *arglists, parallel=False, **kwargs),
        **(reduce_kwargs or dict()),
    )


def _enforce_positive_integer(i):
    return min(max(int(i), 1), sys.maxsize)


def _map_reduce_tree(
    map_func,
    reduce_func,
    *arglists,
    chunksize=None,
    sequential_threshold=1,
    max_size=None,
    max_depth=None,
    branch_factor=2,
    shortcircuit_value=_NoShortCircuit(),
    shortcircuit_callback=cancel_all,
    inflight_limit=1000,
    reduce_kwargs=None,
    parallel=True,
    progress=None,
    desc=None,
    __root__=True,
    __level__=0,
    **kwargs,
):
    """Recursive map-reduce using a tree structure.

    Useful when the reduction function is expensive or when reducing in one
    chunk is otherwise problematic.

    Chunksize parameter takes precedence over branch_factor.
    """
    if not arglists:
        raise ValueError("no arguments provided")

    n = _try_lens(*arglists)

    if reduce_kwargs is None:
        reduce_kwargs = dict()

    # Must be at least 1 to avoid infinite branching
    sequential_threshold = _enforce_positive_integer(sequential_threshold)

    if chunksize is not None:
        chunksize = _enforce_positive_integer(chunksize)

    if __root__ and max_depth is None:
        max_depth = get_depth(max_size, branch_factor)

    if parallel:
        if n is None:
            if not chunksize:
                raise ValueError("mapping to generator(s); must provide chunksize")
            n = float("inf")
    else:
        # Don't branch; process sequentially
        max_depth = 0

    # Branch
    if __level__ < max_depth and sequential_threshold < n and branch_factor > 1:
        if chunksize is not None:
            _chunksize = max(chunksize, sequential_threshold)
            # Reduce chunksize by branch factor, down to sequential threshold
            chunksize = chunksize // (branch_factor ** (__level__ + 1))
        else:
            # Add 1 to odd sizes to avoid single-element partitions
            # NOTE: Adding 1 to even sizes will result in infinite branching
            assert math.isfinite(n)
            _chunksize = max(1, (n // branch_factor) + n % 2)

        chunked_argslists = zip(*(chunked(args, _chunksize) for args in arglists))

        # Submit tasks with backpressure
        # TODO refactor?
        result_refs = []
        for i, _arglists in enumerate(chunked_argslists):
            if len(result_refs) > inflight_limit:
                num_ready = i - inflight_limit
                ray.wait(result_refs, num_returns=num_ready)
            result_refs.append(
                _remote_map_reduce_tree.remote(
                    map_func,
                    reduce_func,
                    *_arglists,
                    branch_factor=branch_factor,
                    max_size=max_size,
                    max_depth=max_depth,
                    sequential_threshold=sequential_threshold,
                    chunksize=chunksize,
                    shortcircuit_value=shortcircuit_value,
                    shortcircuit_callback=shortcircuit_callback,
                    inflight_limit=inflight_limit,
                    reduce_kwargs=reduce_kwargs,
                    progress=progress,
                    desc=desc,
                    __root__=False,
                    __level__=__level__ + 1,
                    **kwargs,
                )
            )

        if not reduce_func:
            reduce_func = flatten
        return reduce_func(
            get(
                result_refs,
                shortcircuit_value=shortcircuit_value,
                shortcircuit_callback=shortcircuit_callback,
                parallel=True,
                progress=False,
            ),
            **reduce_kwargs,
        )

    # Leaf
    results = list(
        map(
            map_func,
            *arglists,
            parallel=False,
            shortcircuit_value=shortcircuit_value,
            shortcircuit_callback=shortcircuit_callback,
            progress=progress,
            desc=desc,
            total=n,
            **kwargs,
        )
    )
    if not reduce_func:
        return results
    return reduce_func(
        results,
        **reduce_kwargs,
    )


# Stay on driver for first call in case we're given a generator
_remote_map_reduce_tree = ray.remote(_map_reduce_tree)


@functools.wraps(_map_reduce_tree)
def map_reduce(
    *args,
    parallel=True,
    **kwargs,
):
    if parallel:
        return _map_reduce_tree(
            *args,
            **kwargs,
        )
    return _map_reduce_sequential(*args, **kwargs)
