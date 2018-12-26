import time
import warnings
import itertools
from distutils.version import LooseVersion
from collections import OrderedDict
from .proto.attr_value_pb2 import AttrValue
from .proto.graph_pb2 import GraphDef
from .proto.node_def_pb2 import NodeDef
from .proto.step_stats_pb2 import RunMetadata, StepStats, DeviceStepStats, NodeExecStats, AllocatorMemoryUsed
from .proto.tensor_shape_pb2 import TensorShapeProto
from .proto.versions_pb2 import VersionDef
from .proto_graph import Node_proto

methods_OP = ['attributeNames', 'hasMultipleOutputs', 'hasUses', 'inputs',
              'kind', 'outputs', 'outputsSize', 'scopeName']
methods_IO = ['node', 'offset', 'uniqueName']  # 'unique' <int> , 'type' <Tensor<class 'torch._C.Type'>>


class Node_base(object):
    def __init__(self, uniqueName, inputs, scope=None, tensorSize=None):
        self.uniqueName = uniqueName
        self.inputs = inputs
        self.tensorSize = tensorSize

        if scope is not None:
            self.scope = scope
        # if tensorSize is not None:

    def __repr__(self):
        repr = []
        repr.append(str(type(self)))
        for m in dir(self):
            if '__' not in m:
                repr.append(m + ': ' + str(getattr(self, m)) + str(type(getattr(self, m))))
        return '\n'.join(repr) + '\n\n'


class Node_dummy(Node_base):
    def __init__(self, uniqueName, inputs, scope, tensorSize=None):
        super(Node_dummy, self).__init__(uniqueName, inputs, scope, tensorSize)


class Node_py(object):
    def __init__(self, Node_cpp, valid_mothods):
        self.valid_mothods = valid_mothods[:]
        self.inputs = []
        for m in self.valid_mothods:
            if m == 'inputs' or m == 'outputs':
                list_of_node = list(getattr(Node_cpp, m)())
                io_uniqueName_list = []
                io_tensorSize_list = []
                for n in list_of_node:
                    io_uniqueName_list.append(n.uniqueName())
                    if n.type().kind() in ['DynamicType', 'ListType']:  # segfault
                        io_tensorSize_list.append(None)
                    else:
                        io_tensorSize_list.append(n.type().sizes())

                setattr(self, m, io_uniqueName_list)
                setattr(self, m + 'TensorSize', io_tensorSize_list)

            else:
                setattr(self, m, getattr(Node_cpp, m)())


class Node_py_IO(Node_py):
    def __init__(self, Node_cpp, input_or_output=None):
        super(Node_py_IO, self).__init__(Node_cpp, methods_IO)
        self.tensorSize = Node_cpp.type().sizes()
        if input_or_output is not None:
            self.input_or_output = input_or_output


class Node_py_OP(Node_py):
    def __init__(self, Node_cpp):
        super(Node_py_OP, self).__init__(Node_cpp, methods_OP)


class Graph_py(object):
    def __init__(self):
        self.nodes_OP = []
        self.nodes_IO = OrderedDict()
        self.uniqueNameToScopedName = {}

    def append(self, x):
        if type(x) == Node_py_IO:
            # if hasattr(x, 'input_or_output'):
            #     if x.input_or_output == 'output':
            self.nodes_IO[x.uniqueName] = x
        if type(x) == Node_py_OP:
            self.nodes_OP.append(x)
            for node_output, outputSize in zip(x.outputs, x.outputsTensorSize):
                self.nodes_IO[node_output] = Node_dummy(node_output, x.inputs, x.scopeName, outputSize)

    def printall(self):
        print('all nodes')
        for node in self.nodes_OP:
            print(node)
        for key in self.nodes_IO:
            print(self.nodes_IO[key])

    def populate_namespace_from_OP_to_IO(self):
        for node in self.nodes_OP:
            for input_node_id in node.inputs:
                self.uniqueNameToScopedName[input_node_id] = node.scopeName + '/' + input_node_id

        for key, node in self.nodes_IO.items():
            if type(node) == Node_dummy:
                self.uniqueNameToScopedName[key] = node.scope + '/' + node.uniqueName

        # replace name
        print(self.uniqueNameToScopedName)
        for key, node in self.nodes_IO.items():
            self.nodes_IO[key].inputs = [self.uniqueNameToScopedName[node_input_id] for node_input_id in node.inputs]
            if node.uniqueName in self.uniqueNameToScopedName:
                self.nodes_IO[key].uniqueName = self.uniqueNameToScopedName[node.uniqueName]

    def to_proto(self):
        nodes = []
        for v in self.nodes_IO.values():
            nodes.append(Node_proto(v.uniqueName, input=v.inputs, outputsize=v.tensorSize))

        return nodes


# one argument: 'hasAttribute', 'hasAttributes',
def parse_2(graph, args=None, omit_useless_nodes=True):
    import torch
    n_inputs = len(args)  # not sure...

    scope = {}
    nodes_py = Graph_py()
    for i, node in enumerate(graph.inputs()):
        if omit_useless_nodes:
            if len(node.uses()) == 0:  # number of user of the node (= number of outputs/ fanout)
                continue

        if i < n_inputs:
            nodes_py.append(Node_py_IO(node, 'input'))
        else:
            nodes_py.append(Node_py_IO(node))  # parameter

    for node in graph.nodes():
        nodes_py.append(Node_py_OP(node))

    for node in graph.outputs():  # must place last.
        # nodes_py.append(Node_py_IO(node, 'output'))
        Node_py_IO(node, 'output')
    nodes_py.populate_namespace_from_OP_to_IO()
    return nodes_py.to_proto()


def parse(graph):
    import torch
    scope = {}
    for n in graph.nodes():
        if n.kind() == 'prim::Undefined':
            for outputnode in iter(n.outputs()):
                scope[outputnode.uniqueName()] = 'Undefined'
            continue
        inputs = [i.uniqueName() for i in n.inputs()]
        for i in range(0, len(inputs)):
            if inputs[i] not in scope.keys():
                scope[inputs[i]] = n.scopeName()

        scopename = n.scopeName()
        if not scopename:
            print('{} has empty scope name. FIXME!'.format(n))
            scopename = 'unknownScope'

        for outputnode in iter(n.outputs()):
            uname = outputnode.uniqueName()
            scope[uname] = scopename

    if LooseVersion(torch.__version__) >= LooseVersion("0.4"):
        scope['0'] = 'input'
    else:
        scope['1'] = 'input'

    nodes = []

    for count, n in enumerate(graph.outputs()):
        uname = 'output' + str(count)
        scope[uname] = 'output'
        nodes.append({'name': uname, 'op': 'output', 'inputs': [
                     n.uniqueName()], 'attr': 'output'})
        Node_proto(uname, 'output', n.uniqueName())
    for n in graph.nodes():
        try:
            attrs = str({k: n[k] for k in n.attributeNames()})
        except RuntimeError as e:
            attrs = str(n).strip()
            warnings.warn(
                "Error getting attributes of node {}, error is {}".format(attrs, e))
        # singlequote will be escaped by tensorboard
        attrs = attrs.replace("'", ' ')
        for outputnode in iter(n.outputs()):
            inputs = [i.uniqueName() for i in n.inputs()]
            uname = outputnode.uniqueName()
            if outputnode.type().kind() == 'TensorType':
                outputsize = outputnode.type().sizes()
                nodes.append({'name': uname,
                              'op': n.kind(),
                              'inputs': inputs,
                              'attr': attrs,
                              'outputsize': outputsize})
            else:
                nodes.append({'name': uname, 'op': n.kind(), 'inputs': inputs, 'attr': attrs})
            Node_proto(uname, n.kind(), inputs)

    for n in graph.inputs():
        uname = n.uniqueName()
        if uname not in scope.keys():
            scope[uname] = 'unknown'
        outputsize = n.type().sizes()
        nodes.append({'name': uname,
                      'op': 'Parameter',
                      'inputs': [],
                      'attr': str(n.type()),
                      'outputsize': outputsize})
        Node_proto(uname, 'output', [])

    mapping = {}
    for n in nodes:
        if scope[n['name']] != '':
            mapping[n['name']] = scope[n['name']] + '/' + \
                n['op'].replace('onnx::', '') + '_' + n['name']
        else:
            mapping[n['name']] = n['op'].replace('onnx::', '') + '_' + n['name']
    for n in nodes:
        n['name'] = mapping[n['name']]
        for i, s in enumerate(n['inputs']):
            n['inputs'][i] = mapping[s]
    return nodes


def graph(model, args, verbose=False, omit_useless_nodes=True):
    import torch
    from torch.onnx.utils import OperatorExportTypes

    def _optimize_graph(graph, operator_export_type):
        # torch._C._jit_pass_remove_inplace_ops(graph)
        # we record now record some ops like ones/zeros
        # into a trace where we previously recorded constants
        # use constant prop to maintain our current level of onnx support
        # without implementing symbolics for all of them
        torch._C._jit_pass_constant_propagation(graph)
        torch.onnx.utils._split_tensor_list_constants(graph, graph)
        # run dce to eliminate dead parts of the graph that might have been
        # left behind by things like symbolic_override
        torch._C._jit_pass_dce(graph)
        torch._C._jit_pass_lint(graph)

        # torch._C._jit_pass_canonicalize_ops(graph)
        torch._C._jit_pass_lint(graph)

        torch._C._jit_pass_peephole(graph, True)
        torch._C._jit_pass_lint(graph)

        # onnx only supports tensors, but 1 / 2 = 0.5 and tensor(1) / tensor(2) = 0
        torch._C._jit_pass_prepare_division_for_onnx(graph)
        # onnx only supports tensors, so we turn all out number types into tensors
        torch._C._jit_pass_erase_number_types(graph)
        # onnx does not support tuples, so try to remove them
        torch._C._jit_pass_lower_all_tuples(graph)
        torch._C._jit_pass_peephole(graph, True)
        torch._C._jit_pass_lint(graph)

        if operator_export_type != OperatorExportTypes.RAW:
            graph = torch._C._jit_pass_onnx(graph, operator_export_type)
            torch._C._jit_pass_lint(graph)
            # torch._C._jit_pass_onnx_peephole(graph)
            torch._C._jit_pass_lint(graph)
        torch._C._jit_pass_dce(graph)
        torch._C._jit_pass_lint(graph)
        torch._C._jit_pass_fixup_onnx_loops(graph)
        torch._C._jit_pass_lint(graph)
        graph = torch._C._jit_pass_canonicalize(graph)
        torch._C._jit_pass_lint(graph)
        return graph

    def _optimize_trace(trace, operator_export_type):
        from torch.onnx import utils
        trace.set_graph(_optimize_graph(trace.graph(), operator_export_type))

    with torch.onnx.set_training(model, False):
        try:
            trace, _ = torch.jit.get_trace_graph(model, args)
        except RuntimeError:
            print('Error occurs, No graph saved')
            _ = model(args)  # don't catch, just print the error message
            print("Checking if it's onnx problem...")
            try:
                import tempfile
                torch.onnx.export(
                    model, args, tempfile.TemporaryFile(), verbose=True)
            except RuntimeError:
                print("Your model fails onnx too, please report to onnx team")
            return GraphDef(versions=VersionDef(producer=22))
    assert LooseVersion(torch.__version__) >= LooseVersion("1.0.0")
    _optimize_trace(trace, torch.onnx.utils.OperatorExportTypes.ONNX)

    graph = trace.graph()
    if verbose:
        print(graph)
    list_of_nodes = parse_2(graph, args, omit_useless_nodes)
    # list_of_nodes = parse(graph)
    nodes = []
    node_stats = []
    stepstats = RunMetadata(step_stats=StepStats(dev_stats=[DeviceStepStats(device="/device:CPU:0",
                                                                            node_stats=node_stats)]))
    return GraphDef(node=list_of_nodes, versions=VersionDef(producer=22)), stepstats

    for node in list_of_nodes:
        if 'outputsize' in node.keys():
            shapeproto = TensorShapeProto(
                dim=[TensorShapeProto.Dim(size=d) for d in node['outputsize']])
            nodes.append(
                NodeDef(name=node['name'], op=node['op'], input=node['inputs'],
                        attr={'lanpa': AttrValue(s=node['attr'].encode(encoding='utf_8')),
                              '_output_shapes': AttrValue(list=AttrValue.ListValue(shape=[shapeproto]))}))
            # FIXME: fill with profile data
            node_stats.append(NodeExecStats(node_name=node['name'],
                                            all_start_micros=int(
                                                time.time() * 1e7),
                                            all_end_rel_micros=42,
                                            memory=[AllocatorMemoryUsed(allocator_name="cpu",
                                                                        total_bytes=19950829,
                                                                        peak_bytes=19950829,
                                                                        live_bytes=19950829)]))
        else:
            nodes.append(
                NodeDef(name=node['name'], op=node['op'], input=node['inputs'],
                        attr={'lanpa': AttrValue(s=node['attr'].encode(encoding='utf_8'))}))

    stepstats = RunMetadata(step_stats=StepStats(dev_stats=[DeviceStepStats(device="/device:CPU:0",
                                                                            node_stats=node_stats)]))
    return GraphDef(node=nodes, versions=VersionDef(producer=22)), stepstats
