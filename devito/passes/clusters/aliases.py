from collections import OrderedDict

from cached_property import cached_property
from sympy import Indexed
import numpy as np

from devito.ir import (ROUNDABLE, DataSpace, IterationInstance, Interval,
                       IntervalGroup, LabeledVector, detect_accesses, build_intervals)
from devito.logger import perf_adv
from devito.passes.clusters.utils import cluster_pass, make_is_time_invariant
from devito.symbolics import (compare_ops, estimate_cost, q_leaf, q_sum_of_product,
                              q_terminalop, retrieve_indexed, yreplace)
from devito.tools import MinMax
from devito.types import Array, Eq, IncrDimension, Scalar

__all__ = ['cire']


MIN_COST_ALIAS = 10
"""
Minimum operation count of an aliasing expression to be lifted into
a vector temporary.
"""

MIN_COST_ALIAS_INV = 50
"""
Minimum operation count of a time-invariant aliasing expression to be
lifted into a vector temporary. Time-invariant aliases are lifted outside
of the time-marching loop, thus they will require vector temporaries as big
as the entire grid.
"""


@cluster_pass
def cire(cluster, template, platform, mode):
    """
    Cross-iteration redundancies elimination.

    Parameters
    ----------
    cluster : Cluster
        Input Cluster, subject of the optimization pass.
    template : callable
        Build symbols to store the redundant expressions.
    platform : Platform
        The underlying platform. Used to optimize the shape of the introduced
        tensor symbols.
    mode : str
        The optimization mode. Accepted: ['all', 'invariants', 'sops'].
        * 'invariants' is for sub-expressions that are invariant w.r.t. one or
          more Dimensions.
        * 'sops' stands for sums-of-products, that is redundancies are searched
          across all expressions in sum-of-product form.
        * 'all' is the union of 'invariants' and 'sops'.

    Examples
    --------
    1) 'invariants'. Below is an expensive sub-expression invariant w.r.t. `t`

    t0 = (cos(a[x,y,z])*sin(b[x,y,z]))*c[t,x,y,z]

    becomes

    t1[x,y,z] = cos(a[x,y,z])*sin(b[x,y,z])
    t0 = t1[x,y,z]*c[t,x,y,z]

    2) 'sops'. Below are redundant sub-expressions in sum-of-product form (in this
    case, the sum degenerates to a single product).

    t0 = 2.0*a[x,y,z]*b[x,y,z]
    t1 = 3.0*a[x,y,z+1]*b[x,y,z+1]

    becomes

    t2[x,y,z] = a[x,y,z]*b[x,y,z]
    t0 = 2.0*t2[x,y,z]
    t1 = 3.0*t2[x,y,z+1]
    """
    assert mode in ['invariants', 'sops', 'all']

    # Extract potentially aliasing expressions
    exprs = extract(cluster, template, mode)

    # Search aliasing expressions
    aliases = collect(exprs)

    # Rule out aliasing expressions with a bad flops/memory trade-off
    candidates, others = choose(exprs, aliases)

    if not candidates:
        # Do not waste time
        return cluster

    # Create Aliases and assign them to Clusters
    clusters, subs = process(cluster, candidates, aliases, template, platform)

    # Rebuild `cluster` so as to use the newly created Aliases
    rebuilt = rebuild(cluster, others, aliases, subs)

    return clusters + [rebuilt]


def extract(cluster, template, mode):
    make = lambda: Scalar(name=template(), dtype=cluster.dtype).indexify()

    if mode in ['invariants', 'all']:
        rule = make_is_time_invariant(cluster.exprs)
        costmodel = lambda e: estimate_cost(e, True) >= MIN_COST_ALIAS_INV

        exprs, _ = yreplace(cluster.exprs, make, rule, costmodel, eager=True)

    if mode in ['sops', 'all']:
        # Rule out symbols inducing Dimension-independent data dependences
        exclude = {i.source.indexed for i in cluster.scope.d_flow.independent()}

        rule = lambda e: q_sum_of_product(e) and not e.free_symbols & exclude
        costmodel = lambda e: not (q_leaf(e) or q_terminalop(e))

        exprs, _ = yreplace(cluster.exprs, make, rule, costmodel)

    return exprs


def collect(exprs):
    """
    Find groups of aliasing expressions.

    An expression A aliases an expression B if both A and B perform the same
    arithmetic operations over the same input operands, with the possibility for
    Indexeds to access locations at a fixed constant offset in each Dimension.

    For example, consider the following expressions:

        * a[i+1] + b[i+1]
        * a[i+1] + b[j+1]
        * a[i] + c[i]
        * a[i+2] - b[i+2]
        * a[i+2] + b[i]
        * a[i-1] + b[i-1]

    The following alias to `a[i] + b[i]`:

        * a[i+1] + b[i+1] : same operands and operations, distance along i: 1
        * a[i-1] + b[i-1] : same operands and operations, distance along i: -1

    Whereas the following do not:

        * a[i+1] + b[j+1] : because at least one index differs
        * a[i] + c[i] : because at least one of the operands differs
        * a[i+2] - b[i+2] : because at least one operation differs
        * a[i+2] + b[i] : because the distances along ``i`` differ (+2 and +0)
    """
    # Find the potential aliases
    candidates = []
    for expr in exprs:
        candidate = analyze(expr)
        if candidate is not None:
            candidates.append(candidate)

    # Create groups of aliasing expressions
    mapper = OrderedDict()
    unseen = list(candidates)
    while unseen:
        c = unseen.pop(0)
        group = [c]
        for u in list(unseen):
            if c.dimensions != u.dimensions:
                continue

            # Is the arithmetic structure of `c` and `u` equivalent ?
            if not compare_ops(c.expr, u.expr):
                continue

            # Is `c` translated w.r.t. `u` ?
            # IOW, are their offsets pairwise translated ? For example:
            # c := A[i,j] + A[i,j+1]     -> Toffsets = {i: [0, 0], j: [0, 1]}
            # u := A[i+1,j] + A[i+1,j+1] -> Toffsets = {i: [1, 1], j: [0, 1]}
            # Then `c` is translated w.r.t. `u` with distance `{i: 1, j: 0}`
            if any(len(set(i-j)) != 1 for (_, i), (_, j) in zip(c.Toffsets, u.Toffsets)):
                continue

            group.append(u)
            unseen.remove(u)

        mapper.setdefault(c.dimensions, GroupList()).append(Group(group))

    # For simplicity, focus on one set of Dimensions at a time
    # Also there basically never is more than one in typical use cases
    try:
        dimensions, groups = mapper.popitem()
    except KeyError:
        return Aliases()

    # The iteration Intervals for these groups
    intervals = [Interval(d, 0, v) for d, v in groups.mds.items()]

    aliases = Aliases(intervals)

    for group in groups:
        shift = group.calculate_shift(groups.mds)

        if not all(v != np.inf for v in shift.values()):
            perf_adv("Increase halo size for more aggressive FLOPs reduction")
            continue

        # Construct the offsets of the Basis Alias
        offsets = []
        for i in group.Toffsets:
            offsets.append(LabeledVector([(d, min(v) - shift[d]) for d, v in i]))

        # Construct the alias
        c = group[0]
        subs = {i: i.function[[x + v.fromlabel(x, 0) for x in b]]
                for i, b, v in zip(c.indexeds, c.bases, offsets)}
        alias = c.expr.xreplace(subs)

        # The aliases expressions
        aliaseds = [i.expr for i in group]

        # Determine the distance of each aliasing expression from the Basis Alias
        distances = []
        for i in group:
            assert len(offsets) == len(i.offsets)
            distance = [o.distance(v) for o, v in zip(i.offsets, offsets)]
            distance = [(d, set(v)) for d, v in LabeledVector.transpose(*distance)]

            distance_mapper = OrderedDict(distance)
            distance = []
            for d, v in distance_mapper.items():
                # The distance of each Indexed from the Basis Alias must be
                # uniform across all Indexeds
                if len(v) != 1:
                    raise ValueError

                # The distance along the ShiftedDimensions must be identical
                # to that of their parent
                if isinstance(d, ShiftedDimension):
                    if v != distance_mapper.get(d.parent, v):
                        raise ValueError
                    continue

                distance.append((d, v))

            distances.append(LabeledVector([(d, v.pop()) for d, v in distance]))

        aliases.add(alias, aliaseds, distances)
        from IPython import embed; embed()

    return aliases


def choose(exprs, aliases):
    # TODO: Generalize `is_time_invariant` -- no need to have it specific for time
    is_time_invariant = make_is_time_invariant(exprs)
    time_invariants = {e.rhs: is_time_invariant(e) for e in exprs}

    processed = []
    candidates = OrderedDict()
    for e in exprs:
        # Cost check (to keep the memory footprint under control)
        naliases = len(aliases.get(e.rhs))
        cost = estimate_cost(e, True)*naliases

        test0 = lambda: cost >= MIN_COST_ALIAS and naliases > 1
        test1 = lambda: cost >= MIN_COST_ALIAS_INV and time_invariants[e.rhs]
        if test0() or test1():
            candidates[e.rhs] = e.lhs
        else:
            processed.append(e)

    return candidates, processed


def process(cluster, candidates, aliases, template, platform):
    # The write-to region, as an IntervalGroup
    writeto = IntervalGroup(aliases.intervals, relations=cluster.ispace.relations)

    # Optimization: only retain those Interval along which some redundancies
    # have been detected
    dep_inducing = [i for i in writeto if any(i.offsets)]
    if dep_inducing:
        index = writeto.index(dep_inducing[0])
        writeto = writeto[index:]

    clusters = []
    subs = {}
    for origin, (aliaseds, distances) in aliases.items():
        if all(i not in candidates for i in aliaseds):
            continue

        # The memory scope of the Array
        # TODO: this has required refinements for a long time
        if len([i for i in writeto if i.dim.is_Incr]) >= 1:
            scope = 'stack'
        else:
            scope = 'heap'

        # Create a temporary to store `alias`
        array = Array(name=template(), dimensions=writeto.dimensions,
                      halo=[(abs(i.lower), abs(i.upper)) for i in writeto],
                      dtype=cluster.dtype, scope=scope)

        # The access Dimensions may differ from `writeto.dimensions`. This may
        # happen e.g. if ShiftedDimensions are introduced (`a[x,y]` -> `a[xs,y]`)
        adims = [aliases.index_mapper.get(d, d) for d in writeto.dimensions]

        # The expression computing `alias`
        indices = [d - (0 if writeto[d].is_Null else writeto[d].lower) for d in adims]
        expression = Eq(array[indices], origin.xreplace(subs))

        # Create the substitution rules so that we can use the newly created
        # temporary in place of the aliasing expressions
        for aliased, distance in zip(aliaseds, distances):
            assert len(adims) == len(writeto)
            assert all(i.dim in distance.labels for i in writeto)

            indices = [d - i.lower + distance[i.dim] for d, i in zip(adims, writeto)]
            subs[aliased] = array[indices]

            if aliased in candidates:
                subs[candidates[aliased]] = array[indices]
            else:
                # Perhaps part of a composite alias ?
                pass

        # Construct the `alias` IterationSpace
        ispace = cluster.ispace.add(writeto).augment(aliases.index_mapper)

        # Optimization: if possible, the innermost IterationInterval is
        # rounded up to a multiple of the vector length
        try:
            it = ispace.itintervals[-1]
            if ROUNDABLE in cluster.properties[it.dim]:
                vl = platform.simd_items_per_reg(cluster.dtype)
                ispace = ispace.add(Interval(it.dim, 0, it.interval.size % vl))
        except (TypeError, KeyError):
            pass

        # Construct the `alias` DataSpace
        accesses = detect_accesses(expression)
        parts = {k: IntervalGroup(build_intervals(v)).add(ispace.intervals).relaxed
                 for k, v in accesses.items() if k}
        dspace = DataSpace(cluster.dspace.intervals, parts)

        # Finally create a new Cluster for `alias`
        clusters.append(cluster.rebuild(exprs=expression, ispace=ispace, dspace=dspace))

    return clusters, subs


def rebuild(cluster, others, aliases, subs):
    # Rebuild the non-aliasing expressions
    exprs = [e.xreplace(subs) for e in others]

    # Add any new ShiftedDimension to the IterationSpace
    ispace = cluster.ispace.augment(aliases.index_mapper)

    # Rebuild the DataSpace to include the new symbols
    accesses = detect_accesses(exprs)
    parts = {k: IntervalGroup(build_intervals(v)).relaxed
             for k, v in accesses.items() if k}
    dspace = DataSpace(cluster.dspace.intervals, parts)

    return cluster.rebuild(exprs=exprs, ispace=ispace, dspace=dspace)


def analyze(expr):
    """
    Determine whether ``expr`` is a potential Alias and collect relevant metadata.

    A necessary condition is that all Indexeds in ``expr`` are affine in the
    access Dimensions so that the access offsets (or "strides") can be derived.
    For example, given the following Indexeds: ::

        A[i, j, k], B[i, j+2, k+3], C[i-1, j+4]

    All of the access functions are affine in ``i, j, k``, and the offsets are: ::

        (0, 0, 0), (0, 2, 3), (-1, 4)
    """
    # No way if writing to a tensor or an increment
    if expr.lhs.is_Indexed or expr.is_Increment:
        return

    indexeds = retrieve_indexed(expr.rhs)
    if not indexeds:
        return

    bases = []
    offsets = []
    for i in indexeds:
        ii = IterationInstance(i)

        # There must not be irregular accesses, otherwise we won't be able to
        # calculate the offsets
        if ii.is_irregular:
            return

        # Since `ii` is regular (and therefore affine), it is guaranteed that `ai`
        # below won't be None
        base = []
        offset = []
        for e, ai in zip(ii, ii.aindices):
            if e.is_Number:
                base.append(e)
            else:
                base.append(ai)
                offset.append((ai, e - ai))
        bases.append(tuple(base))
        offsets.append(LabeledVector(offset))

    return Candidate(expr.rhs, indexeds, bases, offsets)


def calculate_COM(group):
    """
    Determine a centre of mass (COM) for a group of definitely aliasing expressions,
    which is a set of bases spanning all iteration vectors.

    Return the COM as well as the vector distance of each aliasing expression from
    the COM.
    """
    # Find the COM
    COM = []
    for ofs in zip(*[i.offsets for i in group]):
        Tofs = LabeledVector.transpose(*ofs)
        entries = []
        for k, v in Tofs:
            try:
                entries.append((k, int(np.mean(v, dtype=int))))
            except TypeError:
                # At least an element in `v` has symbolic components. Even though
                # `analyze` guarantees that no accesses can be irregular, a symbol
                # might still be present as long as it's constant (i.e., known to
                # be never written to). For example: `A[t, x_m + 2, y, z]`
                # At this point, the only chance we have is that the symbolic entry
                # is identical across all elements in `v`
                if len(set(v)) == 1:
                    entries.append((k, v[0]))
                else:
                    raise ValueError
        COM.append(LabeledVector(entries))

    # Calculate the distance from the COM
    distances = []
    for i in group:
        assert len(COM) == len(i.offsets)
        distance = [o.distance(c) for o, c in zip(i.offsets, COM)]
        distance = [(l, set(i)) for l, i in LabeledVector.transpose(*distance)]

        if any(len(i) != 1 for l, i in distance):
            raise ValueError

        mapper = OrderedDict(distance)
        distance = []
        for d, v in mapper.items():
            # The distance of each Indexed from the COM must be uniform across
            # all Indexeds
            if len(v) != 1:
                raise ValueError

            # The distance along ShiftedDimensions must be identical to that
            # of their parent
            if isinstance(d, ShiftedDimension):
                if v != mapper.get(d.parent, v):
                    raise ValueError
                continue

            distance.append((d, v))

        distances.append(LabeledVector([(d, v.pop()) for d, v in distance]))

    return COM, distances


class ShiftedDimension(IncrDimension):

    def __new__(cls, d, name):
        return super().__new__(cls, d, 0, d.symbolic_size - 1, step=1, name=name)


class Candidate(object):

    def __init__(self, expr, indexeds, bases, offsets):
        self.expr = expr
        self.indexeds = indexeds
        self.bases = bases
        self.offsets = offsets

    def __repr__(self):
        return "Candidate(expr=%s)" % self.expr

    @cached_property
    def Toffsets(self):
        return LabeledVector.transpose(*self.offsets)

    @cached_property
    def dimensions(self):
        return frozenset(i for i, _ in self.Toffsets)


class Group(tuple):

    def __repr__(self):
        return "Group(%s)" % ", ".join([str(i) for i in self])

    @cached_property
    def Toffsets(self):
        return [LabeledVector.transpose(*i) for i in zip(*[i.offsets for i in self])]

    @cached_property
    def mlis(self):
        """
        MLIs - Maximum definitely-Legal Increment along each Dimensions.
        """
        ret = {}
        for c in self:
            for i, ofs in zip(c.indexeds, c.offsets):
                f = i.function
                for d in ofs.labels:
                    k = (set(d._defines) & set(f.dimensions)).pop()
                    ret[d] = min(ret.get(d, np.inf), sum(f._size_halo[k]) - ofs[d])
        return ret

    @cached_property
    def mlds(self):
        """
        MLDs - Maximum definitely-Legal Decrement along each Dimensions.
        """
        ret = {}
        for c in self:
            for i, ofs in zip(c.indexeds, c.offsets):
                f = i.function
                for d in ofs.labels:
                    ret[d] = min(ret.get(d, np.inf), ofs[d])
        return ret

    def calculate_shift(self, mds):
        ret = {}
        for i in self.Toffsets:
            for d, v in i:
                n_extra_iters = mds[d] - self.mlis[d]
                if n_extra_iters <= 0:
                    # All good
                    ret[d] = ret.get(d, 0)
                elif n_extra_iters <= self.mlds[d]:
                    # Definitely need to shift
                    ret[d] = max(ret.get(d, 0), n_extra_iters)
                else:
                    ret[d] = np.inf

        return ret


class GroupList(list):

    def __repr__(self):
        return "GroupList(%s)" % ", ".join([str(i) for i in self])

    @cached_property
    def mds(self):
        """
        MDs - Maximum Distance between two points across any two aliasing expressions.
        """
        ret = {}
        for group in self:
            for i in group.Toffsets:
                ret.update({d: max(ret.get(d, 0), max(v) - min(v)) for d, v in i})
        return ret


class Aliases(OrderedDict):

    def __init__(self, intervals=None):
        super(Aliases, self).__init__()

        self.intervals = intervals or []
        self.index_mapper = {}

    @cached_property
    def dimensions(self):
        return tuple(i.dim for i in self.intervals)

    def add(self, alias, aliaseds, distances):
        assert len(aliaseds) == len(distances)

        self[alias] = (aliaseds, distances)

        # Update the index_mapper
        for d in self.dimensions:
            if d in self.index_mapper:
                continue
            elif isinstance(d, ShiftedDimension):
                self.index_mapper[d.parent] = d
            elif d.is_Incr:
                # IncrDimensions must be substituted with ShiftedDimensions
                # such that we don't go out-of-array-bounds at runtime
                self.index_mapper[d] = ShiftedDimension(d, "%ss" % d.name)

    def get(self, key):
        ret = super(Aliases, self).get(key)
        if ret is not None:
            assert len(ret) == 2
            return ret[0]
        for aliaseds, _ in self.values():
            if key in aliaseds:
                return aliaseds
        return []
