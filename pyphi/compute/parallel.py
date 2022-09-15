# compute/parallel.py

"""
Utilities for parallel computation.
"""

import functools
import logging
import multiprocessing
from itertools import cycle
from textwrap import indent
from typing import Any, Callable, Iterable, List, Optional

import ray
from more_itertools import chunked_even, flatten
from tqdm.auto import tqdm

from .. import config
from ..conf import fallback
from ..utils import try_len
from .progress import ProgressBar, throttled_update, wait_then_finish
from .tree import get_constraints

log = logging.getLogger(__name__)


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
    """An object that is not equal to anything and is falsey."""

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


def get(
    items,
    remote=False,
    shortcircuit_value=_NoShortCircuit(),
    shortcircuit_callback=cancel_all,
    shortcircuit_callback_args=None,
):
    """Get (potentially) remote results.

    Optionally return early if a particular value is found.
    """
    shortcircuit_callback_args = fallback(shortcircuit_callback_args, items)
    if remote:
        items = as_completed(items)
    return shortcircuit(
        items,
        shortcircuit_value=shortcircuit_value,
        shortcircuit_callback=shortcircuit_callback,
        shortcircuit_callback_args=shortcircuit_callback_args,
    )


def backpressure(func, *argslist, inflight_limit=1000, **kwargs):
    # https://docs.ray.io/en/latest/ray-core/tasks/patterns/limit-tasks.html
    result_refs = []
    for i, args in enumerate(zip(*argslist)):
        if len(result_refs) > inflight_limit:
            num_ready = i - inflight_limit
            ray.wait(result_refs, num_returns=num_ready)
        result_refs.append(func.remote(*args, **kwargs))
    return result_refs


def _flatten(items, branch=False):
    if branch:
        items = flatten(items)
    return list(items)


def _map_sequential(func, *arglists, **kwargs):
    for args in zip(*arglists):
        yield func(*args, **kwargs)


def _reduce(results, reduce_func, reduce_kwargs, branch):
    if reduce_func is _flatten:
        return reduce_func(results, branch=branch)
    return reduce_func(results, **reduce_kwargs)


def _map_reduce_tree(
    iterables,
    map_func,
    reduce_func,
    constraints,
    tree,
    chunksize,
    shortcircuit_value,
    shortcircuit_callback,
    shortcircuit_callback_args,
    inflight_limit,
    map_kwargs,
    reduce_kwargs,
    progress_bar,
    _level=1,
):
    """Recursive map-reduce using a tree structure.

    Useful when the reduction function is expensive or when reducing in one
    chunk is otherwise problematic.
    """
    total = fallback(try_len(*iterables), float("inf"))
    branch = _level < tree.depth and constraints.sequential_threshold < total
    if branch:
        chunksize = max(chunksize, constraints.sequential_threshold)
        chunked_iterables = zip(
            *(chunked_even(iterable, chunksize) for iterable in iterables)
        )
        # Reduce chunksize by branch factor, down to sequential threshold
        chunksize = chunksize // constraints.branch_factor
        # Submit tasks with backpressure
        results = backpressure(
            _remote_map_reduce_tree,
            chunked_iterables,
            cycle([map_func]),
            cycle([reduce_func]),
            cycle([constraints]),
            cycle([tree]),
            cycle([chunksize]),
            cycle([shortcircuit_value]),
            cycle([shortcircuit_callback]),
            cycle([shortcircuit_callback_args]),
            cycle([inflight_limit]),
            cycle([map_kwargs]),
            cycle([reduce_kwargs]),
            cycle([progress_bar]),
            inflight_limit=inflight_limit,
            _level=_level + 1,
        )
    else:
        results = _map_sequential(
            map_func,
            *iterables,
            **map_kwargs,
        )
    if progress_bar and _level == 1:
        # We're on root node: block on the progress bar before blocking on results.
        wait_then_finish.remote(progress_bar, results)
        progress_bar.print_until_done()
    # Get (potentially remote) results.
    results = get(
        results,
        remote=branch,
        shortcircuit_value=shortcircuit_value,
        shortcircuit_callback=shortcircuit_callback,
        shortcircuit_callback_args=shortcircuit_callback_args,
    )
    if progress_bar and _level > 1:
        # We're on a child node: update the progress bar.
        results = throttled_update(progress_bar, results)
    return _reduce(results, reduce_func, reduce_kwargs, branch)


_remote_map_reduce_tree = ray.remote(_map_reduce_tree)


class MapReduce:
    """Map-reduce engine.

    Parallelized using Ray remote functions.
    """

    def __init__(
        self,
        map_func: Callable,
        iterable: Iterable,
        *iterables,
        reduce_func: Optional[Callable] = None,
        reduce_kwargs: Optional[dict] = None,
        parallel: bool = True,
        total: Optional[int] = None,
        chunksize: Optional[int] = None,
        sequential_threshold: int = 1,
        max_depth: Optional[int] = None,
        max_size: Optional[int] = None,
        max_leaves: Optional[int] = None,
        branch_factor: int = 2,
        shortcircuit_value: Any = _NoShortCircuit(),
        shortcircuit_callback: Callable = cancel_all,
        shortcircuit_callback_args: Any = None,
        inflight_limit: int = 1000,
        progress: Optional[bool] = None,
        desc: Optional[str] = None,
        **kwargs,
    ):
        """
        Specifying tree size: order of precedence:
            chunksize, max_depth, max_size, max_leaves
        """
        self.map_func = map_func
        self.iterables = (iterable,) + iterables
        self.reduce_func = fallback(reduce_func, _flatten)
        self.reduce_kwargs = fallback(reduce_kwargs, dict())
        self.parallel = parallel
        self.total = fallback(try_len(*self.iterables), total)
        self.shortcircuit_value = shortcircuit_value
        self.shortcircuit_callback = shortcircuit_callback
        self.shortcircuit_callback_args = shortcircuit_callback_args
        self.inflight_limit = inflight_limit
        self.progress = progress
        self.desc = desc
        self.map_kwargs = kwargs

        if self.parallel:
            self.constraints = get_constraints(
                total=self.total,
                chunksize=chunksize,
                sequential_threshold=sequential_threshold,
                max_depth=max_depth,
                max_size=max_size,
                max_leaves=max_leaves,
                branch_factor=branch_factor,
            )
            # Get the tree specifications
            self.tree = self.constraints.simulate()
            # Get the chunksize
            self.chunksize = self.constraints.get_initial_chunksize()

        self.progress_bar = None
        # Store errors
        self.error = None
        # Flag indicating whether computation is finished
        self.done = False
        # Finished result
        self.result = None

    def _repr_attrs(self):
        attrs = [
                "map_func",
                "map_kwargs",
                "iterables",
                "reduce_func",
                "reduce_kwargs",
                "parallel",
                "total",
                "shortcircuit_value",
                "shortcircuit_callback",
                "shortcircuit_callback_args",
                "inflight_limit",
                "progress",
                "desc",
            ]
        if self.parallel:
            attrs += ["constraints", "tree"]
        return attrs

    def __repr__(self):
        data = [
            f"{attr}={getattr(self, attr)}"
            for attr in self._repr_attrs()
        ]
        return "\n".join(
            [f"{self.__class__.__name__}(", indent("\n".join(data), "  "), ")"]
        )

    def _run_parallel(self):
        """Perform the computation in parallel."""
        # Ensure ray is initialized with args from config
        init()
        if self.progress:
            # Set up remote progress bar actor
            self.progress_bar = ProgressBar(self.total, desc=self.desc)

            # Insert a hook into the shortcircuit callback to finish the
            # progress bar
            user_callback = self.shortcircuit_callback
            def _shortcircuit_callback(*args, **kwargs):
                self.progress_bar.actor.finish.remote(interrupted=True)
                user_callback(*args, **kwargs)

            self.shortcircuit_callback = _shortcircuit_callback
        try:
            self.result = _map_reduce_tree(
                self.iterables,
                self.map_func,
                self.reduce_func,
                self.constraints,
                self.tree,
                self.chunksize,
                self.shortcircuit_value,
                self.shortcircuit_callback,
                self.shortcircuit_callback_args,
                self.inflight_limit,
                self.map_kwargs,
                self.reduce_kwargs,
                self.progress_bar,
            )
            self.done = True
            return self.result
        except Exception as e:
            self.error = e
            raise e
        finally:
            # Clean up progress bar actor
            # TODO this should be 'exit_actor', but the method doesn't seem to
            # have been injected
            if self.progress:
                self.progress_bar.actor.__ray_terminate__.remote()

    def _run_sequential(self):
        """Perform the computation serially."""
        try:
            results = _map_sequential(self.map_func, *self.iterables, **self.map_kwargs)
            if self.progress:
                results = tqdm(results, total=self.total, desc=self.desc)
            results = get(
                results,
                remote=False,
                shortcircuit_value=self.shortcircuit_value,
                shortcircuit_callback=self.shortcircuit_callback,
                shortcircuit_callback_args=self.shortcircuit_callback_args
            )
            self.result = _reduce(results, self.reduce_func, self.reduce_kwargs, branch=False)
            self.done = True
            return self.result
        except Exception as e:
            self.error = e
            raise e

    def run(self):
        """Perform the computation."""
        if self.done:
            return self.result
        if self.parallel and self.tree.depth > 1:
            return self._run_parallel()
        return self._run_sequential()
