# Owner(s): ["module: higher order operators"]
# flake8: noqa: B950

import unittest

import torch
import torch._dynamo
import torch._functorch
import torch._inductor
import torch._inductor.decomposition
from functorch.compile import aot_function, nop
from torch._dynamo.testing import AotEagerAndRecordGraphs, normalize_gm
from torch._higher_order_ops.invoke_subgraph import wrap_with_invoke_subgraph
from torch.testing._internal.common_utils import (
    run_tests,
    skipIfTorchDynamo,
    TEST_WITH_CROSSREF,
    TestCase,
)
from torch.utils.checkpoint import (
    CheckpointPolicy,
    create_selective_checkpoint_contexts,
)


def _get_custom_policy(no_recompute_list=None, must_recompute_list=None):
    def _custom_policy(ctx, func, *args, **kwargs):
        if no_recompute_list is not None and func in no_recompute_list:
            return CheckpointPolicy.MUST_SAVE
        if must_recompute_list is not None and func in must_recompute_list:
            return CheckpointPolicy.MUST_RECOMPUTE
        else:
            return CheckpointPolicy.PREFER_RECOMPUTE

    return _custom_policy


@skipIfTorchDynamo("Not a torch._dynamo test")
class TestInvokeSubgraph(TestCase):
    def test_simple(self):
        def gn(x, y):
            return torch.mul(x, y)

        def fn(x, y):
            return wrap_with_invoke_subgraph(gn)(x, y)

        x = torch.randn(8, requires_grad=True)
        y = torch.randn(8, requires_grad=True)
        ref = gn(x, y)

        x_clone = x.clone().detach().requires_grad_(True)
        y_clone = y.clone().detach().requires_grad_(True)
        res = fn(x_clone, y_clone)

        # Run backward
        ref.sum().backward()
        res.sum().backward()

        self.assertEqual(ref, res)
        self.assertEqual(x.grad, x_clone.grad)
        self.assertEqual(y.grad, y_clone.grad)

    def test_aot_function(self):
        def gn(x, y):
            return torch.mul(x, y)

        def fn(x, y):
            return wrap_with_invoke_subgraph(gn)(x, y)

        x = torch.randn(8, requires_grad=True)
        y = torch.randn(8, requires_grad=True)
        ref = gn(x, y)

        x_clone = x.clone().detach().requires_grad_(True)
        y_clone = y.clone().detach().requires_grad_(True)
        aot_fn = aot_function(fn, nop)
        res = aot_fn(x_clone, y_clone)

        # Run backward
        ref.sum().backward()
        res.sum().backward()

        self.assertEqual(ref, res)
        self.assertEqual(x.grad, x_clone.grad)
        self.assertEqual(y.grad, y_clone.grad)

    def test_multiple(self):
        n_layers = 2

        @wrap_with_invoke_subgraph
        def cos(x):
            return torch.cos(x)

        @wrap_with_invoke_subgraph
        def sin(x):
            return torch.sin(x)

        def fn(x):
            a = cos(x)
            b = sin(a)
            return cos(b)

        x = torch.randn(8, requires_grad=True)
        ref = fn(x)
        aot_fn = aot_function(fn, nop)
        res = aot_fn(x)

        self.assertEqual(ref, res)


@skipIfTorchDynamo("Not a torch._dynamo test")
class TestInvokeSubgraphCompile(TestCase):
    def count_unique_get_attr_nodes(self, gm, args, expected):
        subgraph_attr_names = set()
        for node in gm.graph.nodes:
            if node.op == "get_attr":
                subgraph_attr_names.add(node.target)
        self.assertEqual(len(subgraph_attr_names), expected)

    def test_simple(self):
        @wrap_with_invoke_subgraph
        def gn(x, y):
            return torch.mul(x, y)

        def fn(x, y):
            return gn(x, y)

        x = torch.randn(8, requires_grad=True)
        y = torch.randn(8, requires_grad=True)
        ref = gn(x, y)

        x_clone = x.clone().detach().requires_grad_(True)
        y_clone = y.clone().detach().requires_grad_(True)
        res = torch.compile(fn, backend="inductor", fullgraph=True)(x_clone, y_clone)

        # Run backward
        ref.sum().backward()
        res.sum().backward()

        self.assertEqual(ref, res)
        self.assertEqual(x.grad, x_clone.grad)
        self.assertEqual(y.grad, y_clone.grad)

    @unittest.skip("FunctionCtx ops is not cacheable right now")
    def test_differing_strides_for_grad_outs(self):
        class CustomOp(torch.autograd.Function):
            @staticmethod
            def forward(ctx, x):
                return torch.sin(x)

            @staticmethod
            def backward(ctx, grad_out):
                a = grad_out.view(12, 5)
                return torch.cos(torch.reshape(a, (3, 4, 5)))

        @wrap_with_invoke_subgraph
        def gn(x):
            return CustomOp.apply(x)

        def fn(x):
            a = gn(x)
            # Force stride changes so that backward view causes a failure if
            # contiguous not called.
            b = torch.permute(a, (0, 2, 1))
            return b

        x = torch.randn(3, 4, 5, requires_grad=True)
        ref = torch.permute(gn(x), (0, 2, 1))

        x_clone = x.clone().detach().requires_grad_(True)
        opt_fn = torch.compile(fn, backend="aot_eager")
        res = opt_fn(x_clone)

        # Run backward
        ref.sum().backward()
        res.sum().backward()

        self.assertEqual(ref, res)
        self.assertEqual(x.grad, x_clone.grad)

    def test_diamond(self):
        @wrap_with_invoke_subgraph
        def gn(x):
            return torch.sin(x)

        def fn(x):
            a = gn(x)
            b = gn(x)
            return torch.sin(a) + torch.cos(b)

        x = torch.randn(8, requires_grad=True)
        ref = fn(x)

        x_clone = x.clone().detach().requires_grad_(True)
        res = torch.compile(fn, fullgraph=True)(x_clone)

        # Run backward
        ref.sum().backward()
        res.sum().backward()

        self.assertEqual(ref, res)
        self.assertEqual(x.grad, x_clone.grad)

    def test_dropout(self):
        @wrap_with_invoke_subgraph
        def gn(x):
            return torch.nn.functional.dropout(torch.sin(x), p=0.5)

        @wrap_with_invoke_subgraph
        def hn(x):
            return torch.sin(x)

        def fn(x):
            return gn(x) + hn(x)

        x = torch.randn(8, requires_grad=True)
        # Difficult to check the results here because we random does not match
        # between eager and Triton.
        res = torch.compile(fn, backend="inductor", fullgraph=True)(x)

    def test_dedupe(self):
        @wrap_with_invoke_subgraph
        def gn(x, y):
            return torch.mul(x, y)

        def fn(x, y):
            a = gn(x, y)
            return gn(a, y)

        x = torch.randn(8, requires_grad=True)
        y = torch.randn(8, requires_grad=True)
        ref = fn(x, y)

        x_clone = x.clone().detach().requires_grad_(True)
        y_clone = y.clone().detach().requires_grad_(True)
        backend = AotEagerAndRecordGraphs()
        res = torch.compile(fn, backend=backend, fullgraph=True)(x_clone, y_clone)

        # Run backward
        ref.sum().backward()
        res.sum().backward()

        self.assertEqual(ref, res)
        self.assertEqual(x.grad, x_clone.grad)
        self.assertEqual(y.grad, y_clone.grad)

        # Check that the Dynamo and AOT graphs have just one subgraph module
        self.assertEqual(len(backend.graphs), 1)
        self.assertEqual(len(backend.fw_graphs), 1)
        self.assertEqual(len(backend.bw_graphs), 1)
        self.count_unique_get_attr_nodes(backend.graphs[0], [], 1)
        self.count_unique_get_attr_nodes(backend.fw_graphs[0], [], 1)
        self.count_unique_get_attr_nodes(backend.bw_graphs[0], [], 1)

        if not TEST_WITH_CROSSREF:
            self.assertExpectedInline(
                normalize_gm(backend.graphs[0].print_readable(print_output=False)),
                """\
class GraphModule(torch.nn.Module):
    def forward(self, L_x_: "f32[8]", L_y_: "f32[8]"):
        l_x_ = L_x_
        l_y_ = L_y_

        invoke_subgraph_0 = self.invoke_subgraph_0
        invoke_subgraph = torch.ops.higher_order.invoke_subgraph(invoke_subgraph_0, 'invoke_subgraph_0', (l_x_, l_y_));  invoke_subgraph_0 = l_x_ = None
        a: "f32[8]" = invoke_subgraph[0];  invoke_subgraph = None

        invoke_subgraph_1 = self.invoke_subgraph_0
        invoke_subgraph_2 = torch.ops.higher_order.invoke_subgraph(invoke_subgraph_1, 'invoke_subgraph_0', (a, l_y_));  invoke_subgraph_1 = a = l_y_ = None
        getitem_1: "f32[8]" = invoke_subgraph_2[0];  invoke_subgraph_2 = None
        return (getitem_1,)

    class invoke_subgraph_0(torch.nn.Module):
        def forward(self, l_x_: "f32[8]", l_y_: "f32[8]"):
            mul: "f32[8]" = torch.mul(l_x_, l_y_);  l_x_ = l_y_ = None
            return (mul,)
""",
            )

        self.assertExpectedInline(
            normalize_gm(backend.fw_graphs[0].print_readable(print_output=False)),
            """\
class GraphModule(torch.nn.Module):
    def forward(self, primals_1: "f32[8]", primals_2: "f32[8]"):
        ___forward_invoke_subgraph_0_post_graph = self.___forward_invoke_subgraph_0_post_graph

        invoke_subgraph = torch.ops.higher_order.invoke_subgraph(___forward_invoke_subgraph_0_post_graph, '___forward_invoke_subgraph_0_post_graph', (primals_1, primals_2));  ___forward_invoke_subgraph_0_post_graph = primals_1 = None
        getitem: "f32[8]" = invoke_subgraph[1]
        getitem_1: "f32[8]" = invoke_subgraph[2]
        getitem_2: "f32[8]" = invoke_subgraph[0];  invoke_subgraph = None

        ___forward_invoke_subgraph_0_post_graph_1 = self.___forward_invoke_subgraph_0_post_graph

        invoke_subgraph_1 = torch.ops.higher_order.invoke_subgraph(___forward_invoke_subgraph_0_post_graph_1, '___forward_invoke_subgraph_0_post_graph', (getitem_2, primals_2));  ___forward_invoke_subgraph_0_post_graph_1 = getitem_2 = primals_2 = None
        getitem_3: "f32[8]" = invoke_subgraph_1[1]
        getitem_4: "f32[8]" = invoke_subgraph_1[2]
        getitem_5: "f32[8]" = invoke_subgraph_1[0];  invoke_subgraph_1 = None
        return (getitem_5, getitem, getitem_1, getitem_3, getitem_4)

    class ___forward_invoke_subgraph_0_post_graph(torch.nn.Module):
        def forward(self, primals_0: "f32[8]", primals_1: "f32[8]"):
            mul: "f32[8]" = torch.ops.aten.mul.Tensor(primals_0, primals_1)
            return (mul, primals_0, primals_1)
""",
        )

    def test_nonlocal_update(self):
        counter = 2

        @wrap_with_invoke_subgraph
        def gn(x, y):
            nonlocal counter
            return (torch.mul(x, y) * counter,)

        def fn(x, y):
            nonlocal counter
            counter = 2
            a = gn(x, y)[0]
            counter = 3
            return gn(a, y)[0]

        x = torch.randn(8, requires_grad=True)
        y = torch.randn(8, requires_grad=True)
        ref = fn(x, y)

        x_clone = x.clone().detach().requires_grad_(True)
        y_clone = y.clone().detach().requires_grad_(True)
        res = torch.compile(fn, backend="inductor", fullgraph=True)(x_clone, y_clone)

        # Run backward
        ref.sum().backward()
        res.sum().backward()

        self.assertEqual(ref, res)
        self.assertEqual(x.grad, x_clone.grad)
        self.assertEqual(y.grad, y_clone.grad)

        torch._dynamo.reset()
        backend = AotEagerAndRecordGraphs()
        torch.compile(fn, backend=backend, fullgraph=True)(x_clone, y_clone)

        if not TEST_WITH_CROSSREF:
            self.assertExpectedInline(
                normalize_gm(backend.graphs[0].print_readable(print_output=False)),
                """\
class GraphModule(torch.nn.Module):
    def forward(self, L_x_: "f32[8]", L_y_: "f32[8]"):
        l_x_ = L_x_
        l_y_ = L_y_

        invoke_subgraph_0 = self.invoke_subgraph_0
        invoke_subgraph = torch.ops.higher_order.invoke_subgraph(invoke_subgraph_0, 'invoke_subgraph_0', (l_x_, l_y_));  invoke_subgraph_0 = l_x_ = None
        a: "f32[8]" = invoke_subgraph[0];  invoke_subgraph = None

        invoke_subgraph_1 = self.invoke_subgraph_1
        invoke_subgraph_2 = torch.ops.higher_order.invoke_subgraph(invoke_subgraph_1, 'invoke_subgraph_1', (a, l_y_));  invoke_subgraph_1 = a = l_y_ = None
        getitem_1: "f32[8]" = invoke_subgraph_2[0];  invoke_subgraph_2 = None
        return (getitem_1,)

    class invoke_subgraph_0(torch.nn.Module):
        def forward(self, l_x_: "f32[8]", l_y_: "f32[8]"):
            mul: "f32[8]" = torch.mul(l_x_, l_y_);  l_x_ = l_y_ = None
            child: "f32[8]" = mul * 2;  mul = None
            return (child,)

    class invoke_subgraph_1(torch.nn.Module):
        def forward(self, a: "f32[8]", l_y_: "f32[8]"):
            mul: "f32[8]" = torch.mul(a, l_y_);  a = l_y_ = None
            child: "f32[8]" = mul * 3;  mul = None
            return (child,)
""",
            )

    def test_normalize_gm(self):
        @wrap_with_invoke_subgraph
        def gn(x, y):
            # Different graph give different names to intermediate nodes
            for _ in range(5):
                x = x * y
            return x

        def fn(x, y):
            for _ in range(5):
                x = gn(x, y)
            return x

        backend = AotEagerAndRecordGraphs()
        opt_fn = torch.compile(fn, backend=backend, fullgraph=True)

        x = torch.randn(8, requires_grad=True)
        y = torch.randn(8, requires_grad=True)

        opt_fn(x, y)

        if not TEST_WITH_CROSSREF:
            self.assertExpectedInline(
                normalize_gm(backend.graphs[0].print_readable(print_output=False)),
                """\
class GraphModule(torch.nn.Module):
    def forward(self, L_x_: "f32[8]", L_y_: "f32[8]"):
        l_x_ = L_x_
        l_y_ = L_y_

        invoke_subgraph_0 = self.invoke_subgraph_0
        invoke_subgraph = torch.ops.higher_order.invoke_subgraph(invoke_subgraph_0, 'invoke_subgraph_0', (l_x_, l_y_));  invoke_subgraph_0 = l_x_ = None
        x: "f32[8]" = invoke_subgraph[0];  invoke_subgraph = None
        invoke_subgraph_1 = self.invoke_subgraph_0
        invoke_subgraph_2 = torch.ops.higher_order.invoke_subgraph(invoke_subgraph_1, 'invoke_subgraph_0', (x, l_y_));  invoke_subgraph_1 = x = None
        x_1: "f32[8]" = invoke_subgraph_2[0];  invoke_subgraph_2 = None
        invoke_subgraph_3 = self.invoke_subgraph_0
        invoke_subgraph_4 = torch.ops.higher_order.invoke_subgraph(invoke_subgraph_3, 'invoke_subgraph_0', (x_1, l_y_));  invoke_subgraph_3 = x_1 = None
        x_2: "f32[8]" = invoke_subgraph_4[0];  invoke_subgraph_4 = None
        invoke_subgraph_5 = self.invoke_subgraph_0
        invoke_subgraph_6 = torch.ops.higher_order.invoke_subgraph(invoke_subgraph_5, 'invoke_subgraph_0', (x_2, l_y_));  invoke_subgraph_5 = x_2 = None
        x_3: "f32[8]" = invoke_subgraph_6[0];  invoke_subgraph_6 = None
        invoke_subgraph_7 = self.invoke_subgraph_0
        invoke_subgraph_8 = torch.ops.higher_order.invoke_subgraph(invoke_subgraph_7, 'invoke_subgraph_0', (x_3, l_y_));  invoke_subgraph_7 = x_3 = l_y_ = None
        x_4: "f32[8]" = invoke_subgraph_8[0];  invoke_subgraph_8 = None
        return (x_4,)

    class invoke_subgraph_0(torch.nn.Module):
        def forward(self, l_x_: "f32[8]", l_y_: "f32[8]"):
            x: "f32[8]" = l_x_ * l_y_;  l_x_ = None
            x_1: "f32[8]" = x * l_y_;  x = None
            x_2: "f32[8]" = x_1 * l_y_;  x_1 = None
            x_3: "f32[8]" = x_2 * l_y_;  x_2 = None
            x_4: "f32[8]" = x_3 * l_y_;  x_3 = l_y_ = None
            return (x_4,)
""",
            )

    def test_input_mutation(self):
        @wrap_with_invoke_subgraph
        def gn(x, y):
            x.add_(1)
            return torch.mul(x, y)

        def fn(x, y):
            return gn(x, y)

        x = torch.randn(8, requires_grad=False)
        y = torch.randn(8, requires_grad=False)

        opt_fn = torch.compile(fn, backend="inductor", fullgraph=True)
        with self.assertRaisesRegex(
            torch._dynamo.exc.Unsupported, "NYI: invoke_subgraph with aliasing"
        ):
            opt_fn(x, y)

    def test_simple_module(self):
        mod = torch.nn.Linear(8, 8)

        @wrap_with_invoke_subgraph
        def gn(x):
            return mod(x)

        def fn(x):
            return gn(x)

        opt_fn = torch.compile(fn, backend="inductor", fullgraph=True)
        x = torch.randn(8, 8, requires_grad=True)

        ref = mod(x)
        res = opt_fn(x)
        self.assertEqual(ref, res)

    def test_fail_with_direct_invoke_subgraph(self):
        from torch._higher_order_ops import invoke_subgraph

        def gn(x):
            return torch.sin(x)

        def fn(x):
            return invoke_subgraph(gn, None, (x,))

        opt_fn = torch.compile(fn, backend="eager", fullgraph=True)
        x = torch.randn(8, 8, requires_grad=True)

        with self.assertRaisesRegex(
            torch._dynamo.exc.Unsupported, "Directly using invoke_subgraph is not"
        ):
            opt_fn(x)

    def test_input_aliasing(self):
        @wrap_with_invoke_subgraph
        def gn(x, y):
            return (x, torch.mul(x, y))

        def fn(x, y):
            outs = gn(x, y)
            return outs[0] * outs[1]

        x = torch.randn(8, requires_grad=False)
        y = torch.randn(8, requires_grad=False)

        opt_fn = torch.compile(fn, backend="inductor", fullgraph=True)
        with self.assertRaisesRegex(
            torch._dynamo.exc.Unsupported, "NYI: invoke_subgraph with aliasing"
        ):
            opt_fn(x, y)

    def test_kwargs_only(self):
        @wrap_with_invoke_subgraph
        def gn(x, *, y):
            return x * y

        x = torch.randn(8, requires_grad=False)
        y = torch.randn(8, requires_grad=False)

        def fn(x, y):
            return gn(x, y=y)

        ref = fn(x, y)
        opt_fn = torch.compile(fn, backend="inductor", fullgraph=True)
        res = opt_fn(x, y)
        self.assertEqual(ref, res)

    def test_module_method(self):
        class Mod(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.linear = torch.nn.Linear(8, 8)

            @wrap_with_invoke_subgraph
            def helper(self, x):
                return self.linear(x)

            def forward(self, x):
                return x + self.helper(x) * self.helper(x) + x

        mod = Mod()
        backend = AotEagerAndRecordGraphs()
        opt_mod = torch.compile(mod, backend=backend, fullgraph=True)

        x = torch.randn(8, 8, requires_grad=True)

        ref = mod(x)
        res = opt_mod(x)
        self.assertEqual(ref, res)

        if not TEST_WITH_CROSSREF:
            self.assertExpectedInline(
                normalize_gm(backend.graphs[0].print_readable(print_output=False)),
                """\
class GraphModule(torch.nn.Module):
    def forward(self, L_x_: "f32[8, 8]", L_self_modules_linear_parameters_weight_: "f32[8, 8]", L_self_modules_linear_parameters_bias_: "f32[8]"):
        l_x_ = L_x_
        l_self_modules_linear_parameters_weight_ = L_self_modules_linear_parameters_weight_
        l_self_modules_linear_parameters_bias_ = L_self_modules_linear_parameters_bias_

        invoke_subgraph_0 = self.invoke_subgraph_0
        invoke_subgraph = torch.ops.higher_order.invoke_subgraph(invoke_subgraph_0, 'invoke_subgraph_0', (l_x_, l_self_modules_linear_parameters_weight_, l_self_modules_linear_parameters_bias_));  invoke_subgraph_0 = None
        getitem: "f32[8, 8]" = invoke_subgraph[0];  invoke_subgraph = None
        invoke_subgraph_1 = self.invoke_subgraph_0
        invoke_subgraph_2 = torch.ops.higher_order.invoke_subgraph(invoke_subgraph_1, 'invoke_subgraph_0', (l_x_, l_self_modules_linear_parameters_weight_, l_self_modules_linear_parameters_bias_));  invoke_subgraph_1 = l_self_modules_linear_parameters_weight_ = l_self_modules_linear_parameters_bias_ = None
        getitem_1: "f32[8, 8]" = invoke_subgraph_2[0];  invoke_subgraph_2 = None

        mul: "f32[8, 8]" = getitem * getitem_1;  getitem = getitem_1 = None
        add: "f32[8, 8]" = l_x_ + mul;  mul = None
        add_1: "f32[8, 8]" = add + l_x_;  add = l_x_ = None
        return (add_1,)

    class invoke_subgraph_0(torch.nn.Module):
        def forward(self, l_x_: "f32[8, 8]", l_self_modules_linear_parameters_weight_: "f32[8, 8]", l_self_modules_linear_parameters_bias_: "f32[8]"):
            linear: "f32[8, 8]" = torch._C._nn.linear(l_x_, l_self_modules_linear_parameters_weight_, l_self_modules_linear_parameters_bias_);  l_x_ = l_self_modules_linear_parameters_weight_ = l_self_modules_linear_parameters_bias_ = None
            return (linear,)
""",
            )

    def test_module(self):
        class SubMod(torch.nn.Module):
            def __init__(self):
                super().__init__()

            def forward(self, x):
                return torch.sin(x)

        class Mod(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.submod = wrap_with_invoke_subgraph(SubMod())

            def forward(self, x):
                return x + self.submod(x) * self.submod(x) + x

        mod = Mod()
        backend = AotEagerAndRecordGraphs()
        opt_mod = torch.compile(mod, backend=backend, fullgraph=True)

        x = torch.randn(8, 8, requires_grad=True)

        ref = mod(x)
        res = opt_mod(x)
        self.assertEqual(ref, res)

        if not TEST_WITH_CROSSREF:
            self.assertExpectedInline(
                normalize_gm(backend.graphs[0].print_readable(print_output=False)),
                """\
class GraphModule(torch.nn.Module):
    def forward(self, L_x_: "f32[8, 8]"):
        l_x_ = L_x_

        invoke_subgraph_0 = self.invoke_subgraph_0
        invoke_subgraph = torch.ops.higher_order.invoke_subgraph(invoke_subgraph_0, 'invoke_subgraph_0', (l_x_,));  invoke_subgraph_0 = None
        getitem: "f32[8, 8]" = invoke_subgraph[0];  invoke_subgraph = None
        invoke_subgraph_1 = self.invoke_subgraph_0
        invoke_subgraph_2 = torch.ops.higher_order.invoke_subgraph(invoke_subgraph_1, 'invoke_subgraph_0', (l_x_,));  invoke_subgraph_1 = None
        getitem_1: "f32[8, 8]" = invoke_subgraph_2[0];  invoke_subgraph_2 = None

        mul: "f32[8, 8]" = getitem * getitem_1;  getitem = getitem_1 = None
        add: "f32[8, 8]" = l_x_ + mul;  mul = None
        add_1: "f32[8, 8]" = add + l_x_;  add = l_x_ = None
        return (add_1,)

    class invoke_subgraph_0(torch.nn.Module):
        def forward(self, l_x_: "f32[8, 8]"):
            sin: "f32[8, 8]" = torch.sin(l_x_);  l_x_ = None
            return (sin,)
""",
            )

    def test_ac(self):
        def fn1(x):
            return torch.cos(x)

        @wrap_with_invoke_subgraph
        def fn1_checkpoint(x):
            return torch.utils.checkpoint.checkpoint(fn1, x, use_reentrant=False)

        def fn2(x):
            return torch.sin(x)

        @wrap_with_invoke_subgraph
        def fn2_checkpoint(x):
            return torch.utils.checkpoint.checkpoint(fn2, x, use_reentrant=False)

        def fn(x):
            return (
                fn1_checkpoint(x)
                # repeat the same fn1_checkpoint to see that we dedupe
                + fn1_checkpoint(x)
                # Check that a new fn2_checkpoint goes through a different HOP
                + fn2_checkpoint(x)
            )

        x = torch.randn(8, requires_grad=True)
        ref = fn(x)

        x_clone = x.clone().detach().requires_grad_(True)
        backend = AotEagerAndRecordGraphs()
        res = torch.compile(fn, backend=backend, fullgraph=True)(x_clone)

        # Run backward
        ref.sum().backward()
        res.sum().backward()

        self.assertEqual(ref, res)
        self.assertEqual(x.grad, x_clone.grad)

        # Check that the Dynamo and AOT graphs have just one subgraph module
        self.assertEqual(len(backend.graphs), 1)
        self.assertEqual(len(backend.fw_graphs), 1)
        self.assertEqual(len(backend.bw_graphs), 1)
        self.count_unique_get_attr_nodes(backend.graphs[0], [], 2)
        self.count_unique_get_attr_nodes(backend.fw_graphs[0], [], 2)
        self.count_unique_get_attr_nodes(backend.bw_graphs[0], [], 2)

        res = torch.compile(fn, backend="inductor", fullgraph=True)(x_clone)
        self.assertEqual(ref, res)

    def test_selective_ac(self):
        def context_fn_must_recompute_mm():
            must_recompute_list = [
                torch.ops.aten.mm.default,
            ]
            return create_selective_checkpoint_contexts(
                _get_custom_policy(
                    must_recompute_list=must_recompute_list,
                ),
            )

        def gn(x):
            return torch.sigmoid(torch.matmul(x, x))

        @wrap_with_invoke_subgraph
        def checkpoint(x):
            return torch.utils.checkpoint.checkpoint(
                gn,
                x,
                use_reentrant=False,
                context_fn=context_fn_must_recompute_mm,
            )

        def fn(x):
            return checkpoint(x)

        x = torch.randn(8, requires_grad=True)
        ref = fn(x)

        x_clone = x.clone().detach().requires_grad_(True)
        backend = AotEagerAndRecordGraphs()
        res = torch.compile(fn, backend=backend, fullgraph=True)(x_clone)
        self.assertEqual(ref, res)


if __name__ == "__main__":
    run_tests()
