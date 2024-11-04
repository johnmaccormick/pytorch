# Owner(s): ["module: dynamo"]

import torch
import torch._dynamo.test_case
import torch._dynamo.testing


def my_custom_function(x):
    return x + 1


class RunDiffGuardTests(torch._dynamo.test_case.TestCase):
    def test_bool_recompile(self):
        def fn(x, y, c):
            if c:
                return x * y
            else:
                return x + y

        opt_fn = torch.compile(fn, backend="inductor")
        x = 2 * torch.ones(4)
        y = 3 * torch.ones(4)

        ref1 = opt_fn(x, y, True)
        ref2 = opt_fn(x, y, False)

        with torch.compiler.skip_guard_eval_unsafe():
            res2 = opt_fn(x, y, False)
            res1 = opt_fn(x, y, True)

        self.assertEqual(ref1, res1)
        self.assertEqual(ref2, res2)

    def test_tensor_recompile(self):
        def fn(x, y):
            return x * y

        opt_fn = torch.compile(fn, backend="eager")
        x = torch.randn(4, dtype=torch.float32)
        y = torch.randn(4, dtype=torch.float32)

        ref1 = opt_fn(x, y)

        x64 = torch.randn(4, dtype=torch.float64)
        y64 = torch.randn(4, dtype=torch.float64)
        ref2 = opt_fn(x64, y64)

        with torch.compiler.skip_guard_eval_unsafe():
            res1 = opt_fn(x, y)
            res2 = opt_fn(x64, y64)

        self.assertEqual(ref1, res1)
        self.assertEqual(ref2, res2)

    def test_post_recompile(self):
        class Foo:
            a = 4
            b = 5

        foo = Foo()

        def fn(x):
            return x + foo.a + foo.b

        cnts = torch._dynamo.testing.CompileCounter()
        opt_fn = torch.compile(fn, backend=cnts)

        x = torch.randn(4)
        ref = fn(x)
        res = opt_fn(x)
        self.assertEqual(ref, res)
        self.assertEqual(cnts.frame_count, 1)

        foo.a = 11
        ref = fn(x)
        res = opt_fn(x)
        self.assertEqual(ref, res)
        self.assertEqual(cnts.frame_count, 2)

        with torch.compiler.skip_guard_eval_unsafe():
            # Set it back to original value
            foo.a = 4
            ref = fn(x)
            res = opt_fn(x)
            self.assertEqual(ref, res)

            foo.a = 11
            ref = fn(x)
            res = opt_fn(x)
            self.assertEqual(ref, res)

        # Check that we are back to original behavior
        foo.b = 8
        ref = fn(x)
        res = opt_fn(x)
        self.assertEqual(ref, res)
        self.assertEqual(cnts.frame_count, 3)

    def test_fail_on_tensor_shape_change(self):
        def fn(dt):
            return dt["x"] + 1

        x = torch.randn(4)
        dt = {}
        dt["x"] = x
        opt_fn = torch.compile(fn, backend="eager")
        opt_fn(dt)

        with self.assertRaisesRegex(
            RuntimeError, "Could not find a compiled graph for skip_guard_eval_unsafe"
        ):
            with torch.compiler.skip_guard_eval_unsafe():
                x = torch.randn(4, 4)
                dt["x"] = x
                opt_fn(dt)


if __name__ == "__main__":
    from torch._dynamo.test_case import run_tests

    run_tests()
