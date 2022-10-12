import collections
import dataclasses
import functools
import itertools
import logging
import os
import pprint
import textwrap
from typing import Dict
from typing import List
from typing import Optional
from typing import Set
from typing import Union

import numpy as np
import sympy
import torch

from . import config
from . import dependencies
from . import ir
from .codegen.triton_template import should_use_template
from .codegen.triton_template import template_can_fuse
from .codegen.triton_template import template_codegen
from .dependencies import MemoryDep
from .dependencies import StarDep
from .sizevars import SimplifyIndexing
from .utils import cache_on_self
from .utils import cmp
from .utils import dynamo_utils
from .virtualized import V

log = logging.getLogger(__name__)


def pformat(obj):
    if isinstance(obj, set):
        # pformat has trouble with sets of sympy exprs
        obj = sorted(obj, key=str)
    result = pprint.pformat(obj, indent=4)
    if "\n" in result:
        return f"\n{textwrap.indent(result, ' '*4)}"
    return result


class OutputNode:
    def __init__(self, dep):
        self.unmet_dependencies = {dep}
        self.inverse_users = []

    def is_reduction(self):
        return False

    def get_alias_names(self):
        return ()

    def get_name(self):
        return "OUTPUT"

    __repr__ = get_name


class BaseSchedulerNode:
    def __init__(self, scheduler: "Scheduler", node: ir.Buffer):
        self.scheduler: "Scheduler" = scheduler
        self.node: ir.Buffer = node
        self.users: Optional[List[NodeUser]] = None
        self.inverse_users: List[BaseSchedulerNode] = []
        self.set_read_writes(node.get_read_writes())
        self.recursive_predecessors: Optional[Set[str]] = None
        self.min_order: Optional[int] = None
        self.max_order: Optional[int] = None
        self.last_usage: Set[str] = None  # buffers that won't be used after this kernel
        self.written = False

    def __repr__(self):
        return f"{type(self).__name__}(name={self.get_name()!r})"

    def debug_str(self):
        """Longer form printout for trace logs"""
        name = self.get_name()
        lines = [
            f"{name}: {type(self).__name__}({type(self.node).__name__})",
            f"{name}.writes = {pformat(self.read_writes.writes)}",
            f"{name}.unmet_dependencies = {pformat(self.unmet_dependencies)}",
            f"{name}.met_dependencies = {pformat(self.read_writes.reads - self.unmet_dependencies)}",
        ]
        try:
            lines += [
                self.debug_str_extra(),
            ]
        except Exception:
            log.warning("Ignoring error in debug_str()", exc_info=True)
        return "\n".join(lines).rstrip()

    def debug_str_extra(self):
        return ""

    def log_details(self):
        log.info(
            "%s: unmet_dependencies = %s, writes = %s",
            self,
            self.unmet_dependencies,
            self.read_writes.writes,
        )

    def update_mutated_names(self, renames: Dict[str, str]):
        self.set_read_writes(self.read_writes.rename(renames))

    def add_mutation_dep(self, name):
        self.set_read_writes(self.read_writes.with_read(name))

    def set_users(self, users: List["NodeUser"]):
        # deduplicate
        result: Dict[int, NodeUser] = {}
        for use in users:
            if id(use.node) in result:
                result[id(use.node)] = NodeUser(
                    use.node, result[id(use.node)].can_inplace and use.can_inplace
                )
            else:
                result[id(use.node)] = use
        self.users = list(result.values())

    def get_aliases(self):
        return self.node.get_alias_names()

    def get_mutations(self):
        return self.node.get_mutation_names()

    def set_read_writes(self, rw: dependencies.ReadWrites):
        self.read_writes: dependencies.ReadWrites = rw
        self.unmet_dependencies = self.read_writes.reads
        self.prune_deps()

    def used_buffer_names(self) -> Set[str]:
        return {
            dep.name
            for dep in itertools.chain(self.read_writes.reads, self.read_writes.writes)
        }

    def prune_deps(self):
        self.unmet_dependencies = {
            dep
            for dep in self.unmet_dependencies
            if dep.name not in self.scheduler.available_buffer_names
        }

    def get_name(self) -> str:
        return self.node.get_name()

    def get_first_name(self) -> str:
        return self.get_name()

    def get_names(self) -> Set[str]:
        return set([self.get_name()])

    def get_nodes(self) -> List["BaseSchedulerNode"]:
        return [self]

    def get_device(self):
        return self.node.get_device()

    def is_reduction(self):
        return False

    def is_template(self):
        return False

    def is_extern(self):
        return False

    def can_inplace(self, read_dep: dependencies.MemoryDep):
        return False

    def allocate(self):
        if self.node.should_allocate() or should_use_template(self.node):
            # if self.node should allocate or
            # if self.node is generated by TritonKernelTemplates
            # because Triton kernel could not allocate tensor itself
            V.graph.wrapper_code.codegen_allocation(self.node)

    def can_free(self):
        for use in self.users:
            if isinstance(use.node, OutputNode):
                return False
        return True

    def codegen_originating_info(self, buffer, only_once=True):
        if not config.comment_origin:
            return

        if only_once and self.written:
            return
        origins = self.node.origins
        out_lines = []

        for o in origins:
            if o.op == "output":
                # These are boring and samey
                continue

            out_lines.append("")
            # TODO(voz): Should the pragma be constant somewhere?
            out_lines.append("#pragma CMT ORIGIN:")
            out_lines.append(f"#pragma CMT {o.op} {o.target}")
            if "stack_trace" in o.meta:
                stack_trace = f"{o.meta['stack_trace']}"
                stack_trace_last_line = stack_trace.split("|")[-1]
                out_lines.append(
                    "#pragma CMT "
                    + stack_trace_last_line.replace("{", "{{")
                    .replace("}", "}}")
                    .replace("\n", "\\")
                )
                out_lines.append("#pragma CMT END ORIGIN")
                out_lines.append("")

        if len(out_lines) == 0:
            return

        # TODO(voz): Ostensibly, we should not need this. But there are cases where C++ codegen does
        # not use BracesBuffer, so we have no good indicator of a C++ buffer atm.
        buffer.writelines(out_lines)
        self.written = True


class ExternKernelSchedulerNode(BaseSchedulerNode):
    def debug_str_extra(self):
        return f"{self.get_name()}.node.kernel = {getattr(self.node, 'kernel', None)}"

    def is_extern(self):
        return True


class TemplateSchedulerNode(BaseSchedulerNode):
    def __init__(self, scheduler: "Scheduler", node: ir.ExternKernel, group_fn):
        super().__init__(scheduler, node)
        (self._sizes, self._stride) = node.get_group_stride()
        self.group = (node.get_device(), group_fn(self._sizes))
        self.set_read_writes(node.get_read_writes())
        self.update_dep_type()

    def is_template(self):
        return True

    def update_dep_type(self):
        assert len(self.read_writes.writes) == 1
        write = self.read_writes.writes.pop()
        if isinstance(write, StarDep):
            name = write.name
            canonicalized_index, canonicalized_size = self.node.canonicalize()
            new_dep = MemoryDep(name, canonicalized_index, canonicalized_size)
            self.read_writes.writes.add(new_dep)
        else:
            self.read_writes.writes.add(write)

    def get_ranges(self):
        return self._sizes


class NopKernelSchedulerNode(BaseSchedulerNode):
    pass


class SchedulerNode(BaseSchedulerNode):
    def __init__(self, scheduler: "Scheduler", node: ir.ComputedBuffer, group_fn):
        super().__init__(scheduler, node)
        (
            self._sizes,
            self._body,
        ) = node.simplify_and_reorder()

        self.group = (node.get_device(), group_fn(self._sizes))

        self.set_read_writes(
            dependencies.extract_read_writes(self._body, *self._sizes, normalize=True)
        )
        if self.is_reduction():
            # reduction has last (reduced) dim in its sizes, and some
            # downstream dependencies get confused by it
            self.read_writes.writes = self.read_writes.writes | {
                w.strip_last_size() for w in self.read_writes.writes
            }
            # reduction not on the last dim swaps the sizes, and downstream
            # dependencies expect unswapped
            # TODO swapping sizes doesn't work, leads to
            # File "/scratch/ngimel/work/repos/torchdynamo/torchinductor/sizevars.py", line 130, in guard_equals
            # if len(right.free_symbols) < len(left.free_symbols):
            # AttributeError: 'int' object has no attribute 'free_symbols'
            # even though memory dep looks correct
            # self.read_writes.writes = self.read_writes.writes | {
            #     w.maybe_swap_sizes() for w in self.read_writes.writes
            # }

    def debug_str_extra(self):
        name = self.get_name()
        lines = [
            f"{name}.group.device = {self.group[0]}",
            f"{name}.group.iteration = {self.group[1]}",
            f"{name}.sizes = {self._sizes}",
        ]
        if self.get_aliases():
            lines.append(f"{name}.aliases = {pformat(self.get_aliases())}")
        if self.get_mutations():
            lines.append(f"{name}.mutations = {pformat(self.get_mutations())}")
        if isinstance(self._body, ir.LoopBody):
            lines.append(f"class {name}_loop_body:")
            lines.append(textwrap.indent(self._body.debug_str(), "    "))
        return "\n".join(lines)

    def get_ranges(self):
        return self._sizes

    def is_reduction(self):
        return bool(self.node.data.get_reduction_type())

    def allocate(self):
        if (
            not self.node.should_allocate()
            or self.node.get_alias_names()
            or self.node.get_mutation_names()
        ):
            return super().allocate()

        if config.inplace_buffers:
            assert False, "https://github.com/pytorch/torchdynamo/issues/823"
            """
            for read in self.read_writes.reads:
                input_node: BaseSchedulerNode = self.scheduler.name_to_node.get(
                    read.name
                )
                if input_node and V.graph.wrapper_code.can_reuse(input_node):
                    remaining_uses = [
                        x
                        for x in input_node.users
                        if x.node.get_name()
                        not in self.scheduler.available_buffer_names
                    ]
                    if (
                        len(remaining_uses) == 1
                        and remaining_uses[0].can_inplace
                        and remaining_uses[0].node is self
                    ):
                        V.graph.wrapper_code.codegen_inplace_reuse(
                            input_node.node, self.node
                        )
                        V.kernel.args.make_inplace(
                            input_node.get_name(), self.get_name()
                        )
                        return
            """
        super().allocate()

    def run(self, *index_vars):
        self.mark_run()
        self.codegen(index_vars)

    def mark_run(self):
        self.allocate()

    def codegen(self, index_vars):
        sizes = self._sizes
        assert sum(map(len, sizes)) == sum(map(len, index_vars))
        var_ranges = dict(
            zip(
                itertools.chain.from_iterable(index_vars),
                itertools.chain.from_iterable(sizes),
            )
        )
        try:
            with V.set_ops_handler(
                SimplifyIndexing(V.get_ops_handler(), var_ranges)
            ), V.kernel.set_current_node(self):
                self._body(*index_vars)
        except Exception:
            log.fatal("Error in codegen for %s", self.node)
            raise

    def pointwise_read_writes(self):
        """
        Get the memory dependencies in the non-reduction axis.
        """
        sizes, reduction_sizes = self._sizes

        def fn(index):
            return self._body(index, [sympy.Integer(0) for _ in reduction_sizes])

        return dependencies.extract_read_writes(fn, sizes)

    def can_inplace(self, read_dep: dependencies.MemoryDep):
        if self.get_aliases():
            return False
        if len(self.read_writes.writes) == 1 and hasattr(read_dep, "index"):
            write_dep = next(iter(self.read_writes.writes))
            return read_dep.index == write_dep.index and read_dep.size == write_dep.size
        return False


class FusedSchedulerNode(BaseSchedulerNode):
    """
    This is a "fake" scheduler node that represents a group of scheduler nodes
    that are meant to be fused together. The way it does this is by maintaining
    its unmet dependencies as the union of its constituent nodes.
    """

    @classmethod
    def fuse(cls, node1: BaseSchedulerNode, node2: BaseSchedulerNode):
        assert node1.scheduler is node2.scheduler
        return cls(node1.scheduler, node1.get_nodes() + node2.get_nodes())

    def __init__(self, scheduler: "Scheduler", snodes: List[SchedulerNode]):
        # NB: No need to call super().__init__() because we don't need to re-use any of its logic.
        self.snodes = snodes
        self.scheduler = scheduler
        self.node = None  # type: ignore[assignment]
        self.users = None
        self.inverse_users = []
        self.group = max(snodes, key=lambda x: int(x.is_reduction())).group
        self.recursive_predecessors = functools.reduce(
            set.union, [x.recursive_predecessors for x in snodes]
        )
        self.set_read_writes(
            functools.reduce(
                dependencies.ReadWrites.merge, [x.read_writes for x in snodes]
            )
        )
        names = set(self.get_names())
        self.unmet_dependencies = {
            dep
            for dep in functools.reduce(
                set.union, [x.unmet_dependencies for x in snodes]
            )
            if dep.name not in names
        } - self.read_writes.writes
        self.min_order = min([x.min_order for x in self.snodes])
        self.max_order = max([x.max_order for x in self.snodes])

    @cache_on_self
    def get_name(self) -> str:
        return "_".join([x.get_name() for x in self.snodes])

    def get_first_name(self) -> str:
        return self.snodes[0].get_name()

    @cache_on_self
    def get_names(self) -> Set[str]:
        return functools.reduce(set.union, [x.get_names() for x in self.snodes])

    def debug_str_extra(self):
        return (
            f"{self.get_name()}.snodes = {pformat([x.get_name() for x in self.snodes])}"
        )

    @cache_on_self
    def used_buffer_names(self) -> Set[str]:
        return functools.reduce(set.union, [x.used_buffer_names() for x in self.snodes])

    def get_nodes(self) -> List[BaseSchedulerNode]:
        return self.snodes

    def __repr__(self):
        return f"{type(self).__name__}(nodes={self.get_name()})"

    @cache_on_self
    def is_reduction(self):
        return any(x.is_reduction() for x in self.snodes)

    @cache_on_self
    def is_template(self):
        return any(x.is_template() for x in self.snodes)

    def get_device(self):
        return self.group[0]

    # None of these need to be implemented, as a FusedSchedulerNode is just an
    # abstraction for scheduling purposes
    def update_mutated_names(self, renames: Dict[str, str]):
        raise NotImplementedError

    def add_mutation_dep(self, name):
        raise NotImplementedError

    def set_users(self, users: List["NodeUser"]):
        raise NotImplementedError

    def get_aliases(self):
        raise NotImplementedError

    def get_mutations(self):
        raise NotImplementedError

    def can_inplace(self, read_dep: dependencies.MemoryDep):
        raise NotImplementedError

    def allocate(self):
        raise NotImplementedError

    def can_free(self):
        raise NotImplementedError


def pick_loop_order(stride_lengths, sizes, priority_idx=[]):
    """
    A heuristic to decide loop iteration orders.  This has not been well
    tuned and may be something we should autotune.
    """

    @functools.cmp_to_key
    def index_cmp(a, b):
        if sizes[a] == 1 or sizes[b] == 1:
            # 1-sizes don't matter, just move them to the end
            return cmp(sizes[a] == 1, sizes[b] == 1)

        a_first = np.logical_or(
            stride_lengths[:, b] == 0, stride_lengths[:, a] < stride_lengths[:, b]
        ).all()
        b_first = np.logical_or(
            stride_lengths[:, a] == 0, stride_lengths[:, a] > stride_lengths[:, b]
        ).all()

        if a_first and not b_first:
            return -1
        if b_first and not a_first:
            return 1

        # otherwise contiguous
        return cmp(b, a)

    order = list(reversed(range(stride_lengths.shape[1])))
    if len(priority_idx) > 0:
        # if we have priority node, only use that node's order
        stride_lengths = stride_lengths[priority_idx]
    if config.pick_loop_orders:
        order.sort(key=index_cmp)
    return order


@dataclasses.dataclass
class NodeUser:
    node: BaseSchedulerNode
    can_inplace: bool = False

    def get_name(self):
        return self.node.get_name()


class Scheduler:
    @dynamo_utils.dynamo_timed
    def __init__(self, nodes):
        super(Scheduler, self).__init__()
        self.backends = {}

        self.nodes = []
        self.available_buffer_names = {
            *V.graph.graph_inputs.keys(),
            *V.graph.constants.keys(),
        }
        for node in nodes:
            assert (
                node.origins is not None
            ), "All nodes passed to scheduling must have an origin"
            if node.is_no_op():
                self.nodes.append(NopKernelSchedulerNode(self, node))
            elif isinstance(node, ir.ComputedBuffer):
                group_fn = self.get_backend(node.get_device()).group_fn
                self.nodes.append(SchedulerNode(self, node, group_fn))
            elif isinstance(node, ir.ExternKernel) and should_use_template(node):
                group_fn = self.get_backend(node.get_device()).group_fn
                self.nodes.append(TemplateSchedulerNode(self, node, group_fn))
            elif isinstance(node, ir.ExternKernel):
                self.nodes.append(ExternKernelSchedulerNode(self, node))
            else:
                assert False, node
        # some new constants could have been created above
        self.available_buffer_names.update(V.graph.constants.keys())
        for node in self.nodes:
            node.prune_deps()

        self.name_to_node = {node.get_name(): node for node in self.nodes}
        self.name_to_fused_node = None  # set in fuse_nods()

        # we handle mutation by renaming modified versions of the same
        # buffer in the dependency graph to prevent cycles.
        # mutation_renames: tracks the current name for a given buffer
        #                   (changed once per mutation)
        self.mutation_real_name = {}
        # mutation_real_name: maps back to the original name for codegen
        self.mutation_renames = {}

        self.compute_dependencies()
        self.topological_sort_schedule()
        self.compute_predecessors()
        self.dead_node_elimination()

        V.debug.ir_pre_fusion(self.nodes)
        self.num_orig_nodes = len(self.nodes)
        self.name_to_fused_node = {n.get_name(): n for n in self.nodes}
        self.fuse_nodes()
        self.compute_last_usage()
        V.debug.ir_post_fusion(self.nodes)
        V.debug.graph_diagram(self.nodes)
        self.debug_draw_graph()

        # used during codegen:
        self.current_device = None
        self.buffer_names_to_free = set()
        self.buffer_names_no_longer_needed = set()

    def debug_draw_graph(self):
        """Generate an image of the graph for debugging"""
        if os.environ.get("INDUCTOR_WRITE_SCHEDULER_GRAPH", None) == "1":
            from .debug import draw_buffers

            draw_buffers(self.nodes, print_graph=True)

    def debug_print_nodes(self, label):
        if log.isEnabledFor(logging.INFO):
            log.info("%s:", label)
            for node in self.nodes:
                node.log_details()

    def compute_dependencies(self):
        """
        Create dependency edges between nodes, handling aliasing and
        mutation properly.
        """
        name_to_users = collections.defaultdict(list)

        # handle aliasing by using python aliasing in name_to_users
        # if foo aliases bar then we will make name_to_users["foo"] point
        # to the same python list as name_to_users["bar"]
        for node1 in self.nodes:
            node1_name = node1.get_name()
            for node2_name in node1.get_aliases():
                if node1_name in name_to_users and node2_name in name_to_users:
                    # merge the two
                    list1 = name_to_users[node1_name]
                    list2 = name_to_users[node2_name]
                    combined = list1 + list2
                    for key in name_to_users.keys():
                        if name_to_users[key] is list1 or name_to_users[key] is list2:
                            name_to_users[key] = combined
                elif node1_name in name_to_users:
                    name_to_users[node2_name] = name_to_users[node1_name]
                else:
                    name_to_users[node1_name] = name_to_users[node2_name]

        def rename(n):
            if n in self.mutation_renames:
                return rename(self.mutation_renames[n])
            return n

        def dep_closure(node_name):
            reachable_names = {node_name}
            node = self.name_to_node[node_name]
            write_dep = list(node.read_writes.writes)[0]
            for read_dep in node.read_writes.reads:
                if (
                    read_dep.name in self.name_to_node
                    and read_dep.index == write_dep.index
                    and read_dep.size == write_dep.size
                ):
                    reachable_names.update(dep_closure(read_dep.name))
            return reachable_names

        def add_user(used_by_name, user_node, can_inplace=False):
            name_to_users[rename(used_by_name)].append(NodeUser(user_node, can_inplace))

        for node in self.nodes:
            # a node will mutate either 0 or 1 buffers
            for alt_name in node.get_mutations():
                alt_name = rename(alt_name)
                # this node must run after the prior writer
                add_user(alt_name, node)
                node.add_mutation_dep(alt_name)
                for other_node in name_to_users[alt_name]:
                    # this node must run after all prior readers
                    other_name = rename(other_node.get_name())
                    known_dep_node_names = dep_closure(node.get_name())
                    if other_name not in known_dep_node_names:
                        # If this node alreay directly or indirectly depends on other_node,
                        # we don't need to insert an extra StarDep.
                        node.add_mutation_dep(other_name)
                        add_user(other_name, node)

            # add normal non-mutation dependencies
            for read in node.read_writes.reads:
                add_user(read.name, node, node.can_inplace(read))

            node.update_mutated_names(self.mutation_renames)

            # update our renaming scheme for the next iteration
            for alt_name in node.get_mutations():
                self.mutation_renames[rename(alt_name)] = node.get_name()
                self.mutation_renames[alt_name] = node.get_name()
                self.mutation_real_name[node.get_name()] = self.mutation_real_name.get(
                    alt_name, alt_name
                )

        # make sure outputs aren't dead-code-eliminated
        for node_name in V.graph.get_output_names():
            add_user(node_name, OutputNode(StarDep(node_name)))

        # make sure input mutation isn't dead-code-eliminated
        for name in self.mutation_renames:
            if name in V.graph.graph_inputs:
                add_user(name, OutputNode(StarDep(name)))
                V.graph.mutated_inputs.add(name)

        # copy users information onto the nodes
        for node in self.nodes:
            node.set_users(name_to_users[node.get_name()])

        # populate inverse_users
        for node in self.nodes:
            for user in node.users:
                user.node.inverse_users.append(node)

    def dead_node_elimination(self):
        """
        Remove any nodes without users
        """
        updated_nodes = []
        for node in self.nodes:
            if node.users:
                updated_nodes.append(node)
            else:
                # dead code
                log.debug("removed dead node: %s", node.get_name())
                V.graph.removed_buffers.add(node.get_name())
        self.nodes = updated_nodes

    def topological_sort_schedule(self):
        """
        Ensure self.nodes is in topologically sorted order
        """
        seen = set()
        name_to_node = dict()
        result = []

        def visit(n):
            if n not in seen:
                seen.add(n)
                for dep in sorted(n.unmet_dependencies, key=lambda d: d.name):
                    visit(name_to_node[dep.name])
                result.append(n)

        for node in self.nodes:
            for name in node.get_names():
                name_to_node[name] = node
        for node in self.nodes:
            visit(node)
        self.nodes = result

    def compute_predecessors(self):
        """
        Populate each node.recursive_predecessors
        """
        # note self.nodes is topologically sorted
        name_to_predecessors = {}
        for node in self.nodes:
            recursive_predecessors = set()
            for dep in node.unmet_dependencies:
                recursive_predecessors.add(dep.name)
                recursive_predecessors |= name_to_predecessors[dep.name]
            name_to_predecessors[node.get_name()] = recursive_predecessors
            node.recursive_predecessors = recursive_predecessors

        for order, node in enumerate(self.nodes):
            node.min_order = order
            node.max_order = order

    def fuse_nodes(self):
        """
        Mutates self.nodes to combine nodes into FusedSchedulerNodes.
        """
        for _ in range(10):
            old_len = len(self.nodes)
            self.fuse_nodes_once()
            if len(self.nodes) == old_len:
                break

    def fuse_nodes_once(self):
        """
        Mutates self.nodes to combine nodes into FusedSchedulerNodes.

        This relies on two key functions to control the logic:
            - self.can_fuses(): checks if a fusion is legal
            - self.score_fusion(): assigns priority to a given fusion
        """
        fused_nodes = set(self.nodes)
        for node1, node2 in self.get_possible_fusions():
            node1 = self.name_to_fused_node[node1.get_first_name()]
            node2 = self.name_to_fused_node[node2.get_first_name()]
            if self.can_fuse(node1, node2) and not self.will_fusion_create_cycle(
                node1, node2
            ):
                node3 = FusedSchedulerNode.fuse(node1, node2)
                fused_nodes.remove(node1)
                fused_nodes.remove(node2)
                fused_nodes.add(node3)
                self.name_to_fused_node.update(
                    {n.get_name(): node3 for n in node3.get_nodes()}
                )
        self.nodes = sorted(fused_nodes, key=lambda x: x.min_order)
        self.topological_sort_schedule()

    def get_possible_fusions(self):
        """
        Helper to find all legal fusion opportunities, sorted by self.score_fusion()
        """
        possible_fusions = []
        seen = set()

        def check_all_pairs(nodes):
            for node1_index, node1 in enumerate(nodes):
                for node2 in nodes[node1_index + 1 :]:
                    key = (node1, node2)
                    if key in seen:
                        continue
                    seen.add(key)

                    if self.can_fuse(node1, node2):
                        possible_fusions.append(key)
                    elif node2.is_template() and self.can_fuse(node2, node1):
                        # epilogue fusions are order dependent
                        possible_fusions.append((node2, node1))

        buffer_names_grouping = collections.defaultdict(list)
        for node in self.nodes:
            for buf in node.used_buffer_names():
                buffer_names_grouping[buf].append(node)
        for node_grouping in buffer_names_grouping.values():
            check_all_pairs(node_grouping)

        if config.aggressive_fusion:
            group_grouping = collections.defaultdict(list)
            for node in self.nodes:
                group = getattr(node, "group", None)
                if group:
                    group_grouping[group].append(node)
            for node_grouping in group_grouping.values():
                check_all_pairs(node_grouping)

        return sorted(possible_fusions, key=self.score_fusion_key, reverse=True)

    def will_fusion_create_cycle(self, node1, node2):
        """Finds whether there's a path from src to dst caused indirectly by fusion"""

        def check(node):
            if isinstance(node, FusedSchedulerNode) and node not in visited:
                visited.add(node)
                return bool(combined_names & node.recursive_predecessors) or any(
                    check(self.name_to_fused_node[n])
                    for n in node.recursive_predecessors - combined_predecessors
                )
            return False

        visited = set()
        combined_names = node1.get_names() | node2.get_names()
        combined_predecessors = (
            node1.recursive_predecessors | node2.recursive_predecessors
        ) - combined_names
        return any(check(self.name_to_fused_node[n]) for n in combined_predecessors)

    def can_fuse(self, node1: BaseSchedulerNode, node2: BaseSchedulerNode):
        """
        Determine if it is possible to combine node1 and node2 into a
        single fused node.
        """
        if node1 is node2:
            return False
        if (
            isinstance(node1, (ExternKernelSchedulerNode, NopKernelSchedulerNode))
            and not node1.is_template()
        ):
            return False
        if (
            isinstance(node2, (ExternKernelSchedulerNode, NopKernelSchedulerNode))
            and not node2.is_template()
        ):
            return False
        if node2.get_names() & node1.recursive_predecessors:
            return False  # node2 must go before node1
        if node2.is_template():
            return False  # only epilogues

        device = node1.get_device()
        if device != node2.get_device():
            return False  # wrong device

        no_shared_data = self.score_fusion_memory(node1, node2) == 0
        if no_shared_data and (
            not config.aggressive_fusion or node1.is_reduction() or node2.is_reduction()
        ):
            return False  # heuristic not needed for correctness

        if len(node1.get_nodes()) + len(node2.get_nodes()) > config.max_fusion_size:
            return False  # heuristic not needed for correctness

        if node1.get_names() & node2.recursive_predecessors:
            # node2 depends on node1 outputs
            if not self.can_fuse_vertical(node1, node2):
                return False
            if node1.is_template():
                return template_can_fuse(node1, node2)
            return self.get_backend(device).can_fuse_vertical(node1, node2)
        else:  # nodes don't depend on each other, but may have common reads
            if node1.is_template():
                return False
            return self.get_backend(device).can_fuse_horizontal(node1, node2)

    def can_fuse_vertical(self, node1, node2):
        """
        Check if it is legal to fuse a consumer (node2) into a producer (node1).

        We can fuse them if all the reads of node2 either match
        corresponding writes in node1, or are written by nodes that can
        be scheduled before the fusion of node1 and node2.
        """
        node1_names = node1.get_names()
        remaining_deps = {
            dep.name for dep in node2.unmet_dependencies - node1.read_writes.writes
        }
        if remaining_deps & node1_names:
            # MemoryDeps didn't match and read different locations of the same buffer.
            # Examples here include:
            #   - MemoryDep("foo", x) != MemoryDep("foo", x + 1)
            #   - MemoryDep("foo", x) != StarDep("foo")
            return False
        for name in remaining_deps:
            if node1_names & self.name_to_fused_node[name].recursive_predecessors:
                return False
        return True

    def score_fusion(self, node1: BaseSchedulerNode, node2: BaseSchedulerNode):
        """
        Assign a score (higher comes first) to the fusion of node1
        and node2.  When different fusions conflict with each other,
        this is the way we decide what order to run them in.

        Our current score is based on:
        - Estimate of the saved memory operations
        - Fusions closer together in original order
        """
        memory_score = self.score_fusion_memory(node1, node2)
        proximity_score = -max(
            abs(node1.min_order - node2.max_order),
            abs(node2.min_order - node1.max_order),
        )
        return (
            node1.is_reduction() == node2.is_reduction() and memory_score > 0,
            memory_score,
            proximity_score,
        )

    def score_fusion_memory(self, node1, node2):
        """
        The first term in our fusion score that estimates number of saved memory operations.
        """
        common_memory_deps = (node1.read_writes.reads | node1.read_writes.writes) & (
            node2.read_writes.reads | node2.read_writes.writes
        )
        return sum(dep.numel_hint() for dep in common_memory_deps)

    def score_fusion_key(self, nodes):
        """
        Shim for list.sort(key=...)
        """
        node1, node2 = nodes
        return self.score_fusion(node1, node2)

    def compute_last_usage(self):
        """
        Populate node.last_usage
        """

        future_used_buffers = set()
        for node_name in V.graph.get_output_names():
            future_used_buffers.add(node_name)

        for node in reversed(self.nodes):
            used_buffers = node.used_buffer_names()
            used_buffers = {self.mutation_real_name.get(k, k) for k in used_buffers}
            node.last_usage = used_buffers - future_used_buffers
            future_used_buffers.update(used_buffers)

    def free_buffers(self):
        """Free any buffers that are no longer needed"""
        for name in sorted(self.buffer_names_to_free - V.graph.removed_buffers):
            if name in self.name_to_node:
                node = self.name_to_node[name]
                if node.can_free():
                    V.graph.wrapper_code.codegen_free(node.node)
        self.buffer_names_to_free.clear()

    def remove_kernel_local_buffers(self):
        """
        Any buffers that are both created and have a last use in the
        same kernel can be removed.
        """
        for name in V.kernel.store_buffer_names & self.buffer_names_no_longer_needed:
            if (
                name not in V.kernel.must_keep_buffers
                and name not in V.kernel.args.input_buffers
                and name not in self.mutation_renames
                and name not in self.mutation_real_name
            ):
                self.remove_buffer(name)

    def remove_buffer(self, name):
        # Assign a special value instead of deleting the entry
        # because we still rely on output_buffers's length to
        # generate unique arg name.
        log.debug("remove_buffer(%r)", name)
        V.kernel.args.output_buffers[name] = "REMOVED"
        V.graph.removed_buffers.add(name)

    def flush(self):
        for backend in self.backends.values():
            backend.flush()
        self.free_buffers()

    def codegen_extern_call(self, scheduler_node: ExternKernelSchedulerNode):
        assert isinstance(scheduler_node, ExternKernelSchedulerNode)
        scheduler_node.allocate()
        node = scheduler_node.node
        node.codegen(V.graph.wrapper_code)
        self.free_buffers()

    def codegen_template_call(
        self, scheduler_node: Union[FusedSchedulerNode, TemplateSchedulerNode]
    ):
        node, *epilogue = scheduler_node.get_nodes()
        node.allocate()
        template_codegen(self, node, epilogue)
        self.free_buffers()

    def create_backend(self, device: torch.device):
        assert (
            device.type != "cuda" or device.index is not None
        ), f"{device} should have been normalized in lowering"
        V.graph.device_types.add(device.type)
        if device.type == "cpu":
            from .codegen.cpp import CppScheduling

            return CppScheduling(self)
        else:
            from .codegen.triton import TritonScheduling

            return TritonScheduling(self)

    def get_backend(self, device: torch.device):
        if device not in self.backends:
            self.backends[device] = self.create_backend(device)
        return self.backends[device]

    @dynamo_utils.dynamo_timed
    def codegen(self):
        for node in self.nodes:
            self.buffer_names_no_longer_needed.update(node.last_usage)

            if not isinstance(node, NopKernelSchedulerNode):
                device = node.get_device()
                if (
                    device != self.current_device
                    or node.is_extern()
                    or node.is_template()
                ):
                    self.flush()
                    self.current_device = device

            self.buffer_names_to_free.update(node.last_usage)

            if node.is_template():
                self.codegen_template_call(node)
            elif node.is_extern():
                self.codegen_extern_call(node)
            elif isinstance(node, (FusedSchedulerNode, SchedulerNode)):
                self.get_backend(device).codegen_nodes(node.get_nodes())
            else:
                assert isinstance(node, NopKernelSchedulerNode)
                node.allocate()

        self.flush()
