# -*- coding: utf-8 -*-
# big_phi.py

import operator
import pickle
from collections import UserDict, defaultdict
from dataclasses import dataclass
from itertools import product

import networkx as nx
import ray
import scipy
from importlib_metadata import functools
from toolz.itertoolz import partition_all, unique
from tqdm.auto import tqdm

from pyphi import utils
from pyphi.cache import cache
from pyphi.models import cmp
from pyphi.models.cuts import CompleteSystemPartition, SystemPartition
from pyphi.partition import system_partition_types
from pyphi.subsystem import Subsystem

from . import config
from .combinatorics import maximal_independent_sets
from .compute.parallel import as_completed, init
from .direction import Direction
from .models.subsystem import CauseEffectStructure, FlatCauseEffectStructure
from .relations import ConcreteRelations, Relations
from .utils import extremum_with_short_circuit

# TODO
# - cache relations, compute as needed for each nonconflicting CES


def is_affected_by_partition(distinction, partition):
    """Return whether the distinctions is affected by the partition."""
    # TODO(4.0) standardize logic for complete partition vs other partition
    if isinstance(partition, CompleteSystemPartition):
        return True
    coming_from = set(partition.from_nodes) & set(distinction.mechanism)
    going_to = set(partition.to_nodes) & set(distinction.purview(partition.direction))
    return coming_from and going_to


def unaffected_distinctions(ces, partition):
    """Return the CES composed of distinctions that are not affected by the given partition."""
    # Special case for empty CES
    if isinstance(partition, CompleteSystemPartition):
        return CauseEffectStructure([], subsystem=ces.subsystem)
    return CauseEffectStructure(
        [
            distinction
            for distinction in ces
            if not is_affected_by_partition(distinction, partition)
        ],
        subsystem=ces.subsystem,
    )


def sia_partitions(node_indices, node_labels=None):
    """Yield all system partitions."""
    # TODO(4.0) consolidate 3.0 and 4.0 cuts
    scheme = config.SYSTEM_PARTITION_TYPE
    valid = ["TEMPORAL_DIRECTED_BI", "TEMPORAL_DIRECTED_BI_CUT_ONE"]
    if scheme not in valid:
        raise ValueError(
            "IIT 4.0 calculations must use one of the following system"
            f"partition schemes: {valid}; got {scheme}"
        )
    return system_partition_types[config.SYSTEM_PARTITION_TYPE](
        node_indices, node_labels=node_labels
    )


@cache(cache={}, maxmem=None)
def _f(n, k):
    return (2 ** (2 ** (n - k + 1))) - (1 + 2 ** (n - k + 1))


@cache(cache={}, maxmem=None)
def number_of_possible_relations_of_order(n, k):
    """Return the number of possible relations with overlap of size k."""
    # Alireza's generalization of Will's theorem
    return scipy.special.comb(n, k) * sum(
        ((-1) ** i * scipy.special.comb(n - k, i) * _f(n, k + i))
        for i in range(n - k + 1)
    )


@cache(cache={}, maxmem=None)
def number_of_possible_relations(n):
    """Return the total of possible relations of all orders."""
    return sum(number_of_possible_relations_of_order(n, k) for k in range(1, n + 1))


@cache(cache={}, maxmem=None)
def optimum_sum_small_phi_relations(n):
    """Return the 'best possible' sum of small phi for relations."""
    # \sum_{k=1}^{n} (size of purview) * (number of relations with that purview size)
    return sum(k * number_of_possible_relations_of_order(n, k) for k in range(1, n + 1))


@cache(cache={}, maxmem=None)
def optimum_sum_small_phi_distinctions_one_direction(n):
    """Return the 'best possible' sum of small phi for distinctions in one direction"""
    # \sum_{k=1}^{n} k(n choose k)
    return (2 / n) * (2 ** n)


@cache(cache={}, maxmem=None)
def optimum_sum_small_phi(n):
    """Return the 'best possible' sum of small phi for the system."""
    # Double distinction term for cause & effect sides
    distinction_term = 2 * optimum_sum_small_phi_distinctions_one_direction(n)
    relation_term = optimum_sum_small_phi_relations(n)
    return distinction_term + relation_term


def _requires_relations(func):
    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
        # Get relations from Ray if they're remote
        if isinstance(self.relations, ray.ObjectRef):
            self.relations = ray.get(self.relations)
        # Filter relations if flag is set
        if self.requires_filter:
            self.filter_relations()
        return func(self, *args, **kwargs)

    return wrapper


class PhiStructure(cmp.Orderable):
    def __init__(self, distinctions, relations, requires_filter=False):
        if not isinstance(distinctions, CauseEffectStructure):
            raise ValueError(
                f"distinctions must be a CauseEffectStructure, got {type(distinctions)}"
            )
        if not distinctions.subsystem:
            raise ValueError("CauseEffectStructure must have the `subsystem` attribute")
        if isinstance(distinctions, FlatCauseEffectStructure):
            distinctions = distinctions.unflatten()
        if not isinstance(relations, (Relations, ray.ObjectRef)):
            raise ValueError(
                f"relations must be a Relations object; got {type(relations)}"
            )
        self.requires_filter = requires_filter
        self.distinctions = distinctions
        self.relations = relations
        self._system_intrinsic_information = None
        self._sum_phi_distinctions = None
        self._selectivity = None
        # TODO improve this
        self._substrate_size = len(distinctions.subsystem)

    def order_by(self):
        return self.system_intrinsic_information()

    def __eq__(self, other):
        return cmp.general_eq(
            self,
            other,
            [
                "distinctions",
                "relations",
            ],
        )

    def __hash__(self):
        return hash((self.distinctions, self.relations))

    def __getstate__(self):
        dct = self.__dict__
        if isinstance(self.relations, ConcreteRelations):
            distinctions = FlatCauseEffectStructure(self.distinctions)
            dct["relations"] = self.relations.to_indirect_json(distinctions)
        return dct

    def __setstate__(self, state):
        try:
            distinctions = state["distinctions"]
            distinctions = FlatCauseEffectStructure(distinctions)
            state["relations"] = ConcreteRelations.from_indirect_json(
                distinctions, state["relations"]
            )
        except:
            # Assume relations can be unpickled by default
            pass
        finally:
            self.__dict__ = state

    to_json = __getstate__

    def to_pickle(self, path):
        with open(path, mode="wb") as f:
            pickle.dump(self, f)
        return path

    @classmethod
    def read_pickle(cls, path):
        with open(path, mode="rb") as f:
            return pickle.load(f)

    @classmethod
    def from_json(cls, data):
        instance = cls(data["distinctions"], data["relations"])
        instance.__dict__.update(data)
        return instance

    def filter_relations(self):
        """Update relations so that only those supported by distinctions remain.

        Modifies the relations on this object in-place.
        """
        self.relations = self.relations.supported_by(self.distinctions)
        self.requires_filter = False

    def sum_phi_distinctions(self):
        if self._sum_phi_distinctions is None:
            self._sum_phi_distinctions = sum(self.distinctions.phis)
        return self._sum_phi_distinctions

    @_requires_relations
    def sum_phi_relations(self):
        return self.relations.sum_phi()

    def selectivity(self):
        if self._selectivity is None:
            self._selectivity = (
                self.sum_phi_distinctions() + self.sum_phi_relations()
            ) / optimum_sum_small_phi(self._substrate_size)
        return self._selectivity

    @_requires_relations
    def realize(self):
        """Instantiate lazy properties."""
        # Currently this is just a hook to force _requires_relations to do its
        # work. Also very Zen.
        return self

    def partition(self, partition):
        """Return a PartitionedPhiStructure with the given partition."""
        return PartitionedPhiStructure(
            self, partition, requires_filter=self.requires_filter
        )

    def system_intrinsic_information(self):
        """Return the system intrinsic information.

        This is the phi of the system with respect to the complete partition.
        """
        if self._system_intrinsic_information is None:
            self._system_intrinsic_information = self.partition(
                CompleteSystemPartition()
            ).phi()
        return self._system_intrinsic_information


class PartitionedPhiStructure(PhiStructure):
    def __init__(self, unpartitioned_phi_structure, partition, requires_filter=False):
        # We need to realize the underlying PhiStructure in case
        # distinctions/relations are generators which may later become exhausted
        self.unpartitioned_phi_structure = unpartitioned_phi_structure.realize()
        super().__init__(
            self.unpartitioned_phi_structure.distinctions,
            self.unpartitioned_phi_structure.relations,
            # Relations should have been filtered when `.realize()` was called
            requires_filter=False,
        )
        self.partition = partition
        # Lift values from unpartitioned PhiStructure
        for attr in [
            "_system_intrinsic_information",
            "_substrate_size",
            "_sum_phi_distinctions",
            "_selectivity",
        ]:
            setattr(
                self,
                attr,
                getattr(self.unpartitioned_phi_structure, attr),
            )
        self._partitioned_distinctions = None
        self._partitioned_relations = None
        self._sum_phi_partitioned_distinctions = None
        self._sum_phi_partitioned_relations = None
        self._informativeness = None

    def order_by(self):
        return self.phi()

    def __eq__(self, other):
        return super().__eq__(other) and cmp.general_eq(
            self,
            other,
            [
                "phi",
                "partition",
                "partitioned_distinctions",
                "partitioned_relations",
            ],
        )

    def __hash__(self):
        return hash((super().__hash__(), self.partition))

    def __bool__(self):
        """A |SystemIrreducibilityAnalysis| is ``True`` if it has |big_phi > 0|."""
        return not utils.eq(self.phi(), 0)

    def partitioned_distinctions(self):
        if self._partitioned_distinctions is None:
            self._partitioned_distinctions = unaffected_distinctions(
                self.distinctions, self.partition
            )
        return self._partitioned_distinctions

    @_requires_relations
    def partitioned_relations(self):
        if self._partitioned_relations is None:
            self._partitioned_relations = self.relations.supported_by(
                self.partitioned_distinctions()
            )
        return self._partitioned_relations

    def sum_phi_partitioned_distinctions(self):
        if self._sum_phi_partitioned_distinctions is None:
            self._sum_phi_partitioned_distinctions = sum(
                self.partitioned_distinctions().phis
            )
        return self._sum_phi_partitioned_distinctions

    def sum_phi_partitioned_relations(self):
        if self._sum_phi_partitioned_relations is None:
            self._sum_phi_partitioned_relations = self.partitioned_relations().sum_phi()
            # Remove reference to the (heavy and rather redundant) lists of
            # partitioned distinctions & relations under the assumption we won't
            # need them again, since most PartitionedPhiStructures will be used
            # only once, during SIA calculation
            self._partitioned_distinctions = None
            self._partitioned_relations = None
        return self._sum_phi_partitioned_relations

    # TODO use only a single pass through the distinctions / relations?
    def informativeness(self):
        if self._informativeness is None:
            distinction_term = (
                self.sum_phi_distinctions() - self.sum_phi_partitioned_distinctions()
            )
            relation_term = (
                self.sum_phi_relations() - self.sum_phi_partitioned_relations()
            )
            self._informativeness = distinction_term + relation_term
        return self._informativeness

    def phi(self):
        return self.selectivity() * self.informativeness()

    def to_json(self):
        return {
            "unpartitioned_phi_structure": self.unpartitioned_phi_structure,
            "partition": self.partition,
        }


def selectivity(phi_structure):
    """Return the selectivity of the PhiStructure."""
    return phi_structure.selectivity()


def informativeness(partitioned_phi_structure):
    """Return the informativeness of the PartitionedPhiStructure."""
    return partitioned_phi_structure.informativeness()


def phi(partitioned_phi_structure):
    """Return the phi of the PartitionedPhiStructure."""
    return partitioned_phi_structure.phi()


# TODO add rich methods, comparisons, etc.
@dataclass
class SystemIrreducibilityAnalysis(cmp.Orderable):
    subsystem: Subsystem
    phi_structure: PhiStructure
    partitioned_phi_structure: PartitionedPhiStructure
    partition: SystemPartition
    selectivity: float
    informativeness: float
    phi: float

    _sia_attributes = ["phi", "phi_structure", "partitioned_phi_structure", "subsystem"]

    def order_by(self):
        return [self.phi, len(self.subsystem), self.subsystem.node_indices]

    def __eq__(self, other):
        return cmp.general_eq(self, other, self._sia_attributes)

    def __bool__(self):
        """A |SystemIrreducibilityAnalysis| is ``True`` if it has |big_phi > 0|."""
        return not utils.eq(self.phi, 0)

    def __hash__(self):
        return hash(
            (
                self.phi,
                self.ces,
                self.partitioned_ces,
                self.subsystem,
                self.partitioned_subsystem,
            )
        )


def evaluate_partition(subsystem, phi_structure, partition):
    partitioned_phi_structure = phi_structure.partition(partition)
    return SystemIrreducibilityAnalysis(
        subsystem=subsystem,
        phi_structure=phi_structure,
        partitioned_phi_structure=partitioned_phi_structure,
        partition=partitioned_phi_structure.partition,
        selectivity=partitioned_phi_structure.selectivity(),
        informativeness=partitioned_phi_structure.informativeness(),
        phi=partitioned_phi_structure.phi(),
    )


def has_nonspecified_elements(subsystem, distinctions):
    """Return whether any elements are not specified by a purview in both
    directions."""
    elements = set(subsystem.node_indices)
    specified = {direction: set() for direction in Direction.both()}
    for distinction in distinctions:
        for direction in Direction.both():
            specified[direction].update(set(distinction.purview(direction)))
    return any(elements - _specified for _specified in specified.values())


def has_no_spanning_specification(subsystem, distinctions):
    """Return whether the system can be separated into disconnected components.

    Here disconnected means that there is no "spanning specification"; some
    subset of elements only specifies themselves and is not specified by any
    other subset.
    """
    # TODO
    return False


REDUCIBILITY_CHECKS = [
    has_nonspecified_elements,
    has_no_spanning_specification,
]


class CompositionalState(UserDict):
    """A mapping from purviews to states."""


def is_congruent(distinction, state):
    """Return whether (any of) the (tied) specified state(s) is the given one."""
    return any(state == tuple(specified) for specified in distinction.specified_state)


def filter_ces(ces, direction, compositional_state):
    """Return only the distinctions consistent with the given compositional state."""
    for distinction in ces:
        try:
            if distinction.direction == direction and is_congruent(
                distinction,
                compositional_state[distinction.purview],
            ):
                yield distinction
        except KeyError:
            pass


def conflict_graph(distinctions):
    """Return a graph where nodes are distinctions and edges are conflicts."""
    # Map mechanisms to distinctions for fast retreival
    mechanism_to_distinction = dict()
    # Map purviews to mechanisms that specify them, on both cause and effect sides
    purview_to_mechanisms = {
        direction: defaultdict(list) for direction in Direction.both()
    }
    # Populate mappings
    for distinction in distinctions:
        mechanism_to_distinction[distinction.mechanism] = distinction
        for direction in Direction.both():
            purview_to_mechanisms[direction][distinction.purview(direction)].append(
                distinction.mechanism
            )
    # Construct graph where nodes are distinctions and edges are conflicts
    G = nx.Graph()
    for direction, mapping in purview_to_mechanisms.items():
        for mechanisms in mapping.values():
            # Conflicting distinctions on one side form a clique
            G.update(nx.complete_graph(mechanisms))
    return G, mechanism_to_distinction


def all_nonconflicting_distinction_sets(distinctions):
    """Return all maximal non-conflicting sets of distinctions."""
    # Nonconflicting sets are maximal independent sets of the conflict graph.
    # NOTE: The maximality criterion here depends on the property of big phi
    # that it is monotonic increasing with the number of distinctions. If this
    # changes, this function should be changed to yield all independent sets,
    # not just the maximal ones.
    graph, mechanism_to_distinction = conflict_graph(distinctions)
    for maximal_independent_set in maximal_independent_sets(graph):
        yield CauseEffectStructure(
            # Though distinctions are hashable, the hash function is relatively
            # expensive (since repertoires are hashed), so we work with
            # mechanisms instead
            map(mechanism_to_distinction.get, maximal_independent_set),
            subsystem=distinctions.subsystem,
        )


def evaluate_partitions(subsystem, phi_structure, partitions):
    return extremum_with_short_circuit(
        (
            evaluate_partition(subsystem, phi_structure, partition)
            for partition in partitions
        ),
        cmp=operator.lt,
        initial=float("inf"),
        shortcircuit_value=0,
    )


_evaluate_partitions = ray.remote(evaluate_partitions)


def _null_sia(subsystem, phi_structure):
    if not subsystem.cut.is_null:
        raise ValueError("subsystem must have no partition")
    partitioned_phi_structure = phi_structure.partition(subsystem.cut)
    return SystemIrreducibilityAnalysis(
        subsystem=subsystem,
        phi_structure=phi_structure,
        partitioned_phi_structure=partitioned_phi_structure,
        partition=partitioned_phi_structure.partition,
        selectivity=None,
        informativeness=None,
        phi=0.0,
    )


def is_trivially_reducible(subsystem, phi_structure):
    # TODO(4.0) realize phi structure here if anything requires relations?
    return any(
        check(subsystem, phi_structure.distinctions) for check in REDUCIBILITY_CHECKS
    )


# TODO configure
DEFAULT_PARTITION_CHUNKSIZE = 500
DEFAULT_PHI_STRUCTURE_CHUNKSIZE = 50


# TODO document args
def evaluate_phi_structure(
    subsystem,
    phi_structure,
    check_trivial_reducibility=True,
    chunksize=DEFAULT_PARTITION_CHUNKSIZE,
):
    """Analyze the irreducibility of a PhiStructure."""
    # Realize the PhiStructure before distributing tasks
    phi_structure.realize()

    if check_trivial_reducibility and is_trivially_reducible(subsystem, phi_structure):
        return _null_sia(subsystem, phi_structure)

    tasks = [
        _evaluate_partitions.remote(
            subsystem,
            phi_structure,
            partitions,
        )
        for partitions in partition_all(
            chunksize,
            sia_partitions(subsystem.cut_indices, subsystem.cut_node_labels),
        )
    ]
    return extremum_with_short_circuit(
        as_completed(tasks),
        cmp=operator.lt,
        initial=float("inf"),
        shortcircuit_value=0,
        shortcircuit_callback=lambda: [ray.cancel(task) for task in tasks],
    )


def evaluate_phi_structures(
    subsystem,
    phi_structures,
    **kwargs,
):
    return max(
        evaluate_phi_structure(subsystem, phi_structure, **kwargs)
        for phi_structure in phi_structures
    )


_evaluate_phi_structures = ray.remote(evaluate_phi_structures)


def max_system_intrinsic_information(phi_structures):
    return max(
        phi_structures,
        key=lambda phi_structure: phi_structure.system_intrinsic_information(),
    )


_max_system_intrinsic_information = ray.remote(max_system_intrinsic_information)


# TODO refactor into a pattern
def find_maximal_compositional_state(
    phi_structures,
    chunksize=DEFAULT_PHI_STRUCTURE_CHUNKSIZE,
    progress=True,
):
    print("Finding maximal compositional state")
    tasks = [
        _max_system_intrinsic_information.remote(chunk)
        for chunk in tqdm(
            partition_all(chunksize, phi_structures), desc="Submitting tasks"
        )
    ]
    print("Done submitting tasks")
    results = as_completed(tasks)
    if progress:
        results = tqdm(results, total=len(tasks))
    return max_system_intrinsic_information(results)


# TODO allow choosing whether you provide precomputed distinctions
# (sometimes faster to compute as you go if many distinctions are killed by conflicts)
# TODO document args
def sia(
    subsystem,
    all_distinctions,
    all_relations,
    phi_structures=None,
    check_trivial_reducibility=True,
    chunksize=DEFAULT_PHI_STRUCTURE_CHUNKSIZE,
    partition_chunksize=DEFAULT_PARTITION_CHUNKSIZE,
    filter_relations=False,
    progress=True,
):
    """Analyze the irreducibility of a system."""
    if isinstance(all_distinctions, FlatCauseEffectStructure):
        all_distinctions = all_distinctions.unflatten()
    # First check that the entire set of distinctions/relations is not trivially reducible
    # (since then all subsets must be)
    full_phi_structure = PhiStructure(all_distinctions, all_relations)
    if check_trivial_reducibility and is_trivially_reducible(
        subsystem, full_phi_structure
    ):
        print("Returning trivially-reducible SIA")
        return _null_sia(subsystem, full_phi_structure)

    # Assume that phi structures passed by the user don't need to have their
    # relations filtered
    if phi_structures is None:
        filter_relations = True
        # Broadcast relations to workers
        all_relations = ray.put(all_relations)
        print("Done putting relations")
        phi_structures = (
            PhiStructure(
                distinctions,
                all_relations,
                requires_filter=filter_relations,
            )
            for distinctions in all_nonconflicting_distinction_sets(all_distinctions)
        )

    if config.IIT_VERSION == "maximal-state-first":
        maximal_compositional_state = find_maximal_compositional_state(
            phi_structures,
            chunksize=chunksize,
            progress=progress,
        )
        print("Evaluating maximal compositional state")
        return evaluate_phi_structure(
            subsystem,
            maximal_compositional_state,
            check_trivial_reducibility=check_trivial_reducibility,
            chunksize=partition_chunksize,
        )
    else:
        # Broadcast subsystem object to workers
        subsystem = ray.put(subsystem)
        print("Done putting subsystem")

        print("Evaluating all compositional states")
        tasks = [
            _evaluate_phi_structures.remote(
                subsystem,
                chunk,
                check_trivial_reducibility=check_trivial_reducibility,
                chunksize=partition_chunksize,
            )
            for chunk in tqdm(
                partition_all(chunksize, phi_structures), desc="Submitting tasks"
            )
        ]
        print("Done submitting tasks")
        results = as_completed(tasks)
        if progress:
            results = tqdm(results, total=len(tasks))
        return max(results)
