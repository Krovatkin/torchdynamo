#!/usr/bin/env pytest
import builtins
import contextlib
import dataclasses
import importlib
import unittest
from unittest.mock import patch

import torch
from torch import fx

from torchdynamo.testing import same
from torchinductor import config
from torchinductor.compile_fx import compile_fx

HAS_CUDA = False
if torch.cuda.is_available():
    try:
        importlib.import_module("triton")
        HAS_CUDA = True
    except ImportError:
        pass


class TestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._stack = contextlib.ExitStack()
        cls._stack.enter_context(patch.object(config, "debug", True))
        cls._stack.enter_context(patch.object(config.cpp, "min_chunk_size", 1))

    @classmethod
    def tearDownClass(cls):
        cls._stack.close()


@dataclasses.dataclass
class InputGen:
    n: int
    device: str

    def dense(self):
        return torch.randn((self.n, self.n), device=self.device)

    def transposed(self):
        return self.dense().transpose(0, 1)

    def strided(self):
        return torch.randn((self.n * 2, self.n * 3), device=self.device)[
            self.n :, self.n :: 2
        ]

    def broadcast1(self):
        return torch.randn((self.n,), device=self.device)

    def broadcast2(self):
        return torch.randn((1, self.n, 1), device=self.device)

    def broadcast3(self):
        return torch.randn((1,), device=self.device)

    def double(self):
        return torch.randn((self.n, self.n), device=self.device, dtype=torch.double)

    def int(self):
        return torch.arange(self.n, device=self.device, dtype=torch.int32)


def check_model(self: TestCase, model, example_inputs):
    if isinstance(model, torch.fx.Graph):
        model = torch.fx.GraphModule({}, model)
    elif not isinstance(model, torch.fx.GraphModule):
        model = fx.symbolic_trace(model)
    compiled_fn = compile_fx(model, example_inputs)
    actual = compiled_fn(*example_inputs)
    correct = model(*example_inputs)
    self.assertTrue(same(actual, correct))


def check_model_cuda(self: TestCase, model, example_inputs):
    if hasattr(model, "to"):
        model = model.to("cuda")
    example_inputs = tuple(x.to("cuda") for x in example_inputs)
    check_model(self, model, example_inputs)


class SweepInputs2:
    input_gen_types1 = [
        "dense",
        "transposed",
        "strided",
        "broadcast1",
        "broadcast2",
        "broadcast3",
        "double",
        "int",
    ]
    input_gen_types2 = input_gen_types1
    gen = None

    @staticmethod
    def kernel(a, b):
        return (a + b,)

    @classmethod
    def gen_template(cls, name1, name2):
        def test(self):
            check_model(
                self,
                cls.kernel,
                (
                    getattr(cls.gen, name1)(),
                    getattr(cls.gen, name2)(),
                ),
            )

        test.__name__ = f"test_{cls.gen.device}_{name1}_{name2}"
        setattr(cls, test.__name__, test)

    @classmethod
    def populate(cls):
        for name1 in cls.input_gen_types1:
            for name2 in cls.input_gen_types2:
                cls.gen_template(name1, name2)


class SweepInputsCpuTest(SweepInputs2, TestCase):
    gen = InputGen(10, "cpu")


SweepInputsCpuTest.populate()


class CommonTemplate:
    @classmethod
    def install(my_cls, other_cls, suffix):
        for name, value in my_cls.__dict__.items():
            if name.startswith("test_"):
                setattr(other_cls, f"{name}_{suffix}", value)

    def test_add_const_int(self):
        def fn(a):
            return (a + 1,)

        self.common(fn, (torch.randn(32),))

    def test_add_const_float(self):
        def fn(a):
            return (a + 1.5,)

        self.common(fn, (torch.randn(32),))

    def test_abs(self):
        def fn(a):
            return (a / (torch.abs(a) + 1),)

        self.common(fn, (torch.randn(17),))

    def test_max_min(self):
        def fn(a, b):
            return (torch.maximum(a, b), torch.minimum(a, b))

        self.common(fn, (torch.randn(8), torch.randn(8)))

    def test_horizonal_fusion1(self):
        def fn(a, b, c):
            return (a + b, a - c, b * c)

        self.common(
            fn, (torch.randn(8, 16, 16), torch.randn(8, 16, 16), torch.randn(1, 16, 1))
        )

    def test_horizonal_fusion2(self):
        def fn(a, b, c):
            return a + 1, b + 2, c + 3

        self.common(fn, (torch.randn(8, 16, 8), torch.randn(8, 16), torch.randn(16, 8)))

    def test_sum1(self):
        def fn(a, b):
            return ((a + b).sum(-1),)

        self.common(fn, (torch.randn(8, 8), torch.randn(8, 8)))

    def test_sum2(self):
        def fn(a, b):
            return ((a + b).sum([1, 2]), (a + b).sum(-1))

        self.common(fn, (torch.randn(8, 9, 3, 21), torch.randn(8, 9, 3, 21)))

    def test_sum3(self):
        def fn(a, b):
            r1 = a + b
            r2 = r1.sum(-1)
            r3 = torch.squeeze(b) + 10
            return (r1, r2, r3)

        self.common(fn, (torch.randn(10, 10), torch.randn(1, 10)))

    def test_sum4(self):
        def fn(a):
            b = a + 1
            c = b.sum(-1)
            d = c + 3
            e = d.sum(-1)
            f = e + 5
            return (f, e, d, c, b)

        self.common(fn, (torch.randn(1, 16, 8, 8),))

    def test_sum5(self):
        def fn(a):
            b = a + 1
            c = b.sum(-1)
            d = c + 3
            e = d.sum(-1)
            f = e + 5
            return (f,)

        self.common(fn, (torch.randn(1, 17, 8, 9),))

    def test_min_max_reduction(self):
        def fn(a, b):
            return ((a + b).max(), (a + b).min())

        self.common(fn, (torch.randn(8, 8), torch.randn(8, 8)))

    def test_clamp(self):
        def fn(a, b):
            return (a.clamp(-0.1, 0.1), b.clamp(0), torch.clamp(a + b, max=0))

        self.common(fn, (torch.randn(8, 8), torch.randn(8, 8)))

    def test_arange(self):
        # fx can't capture arange
        g = fx.Graph()
        x = g.placeholder("x")
        device = g.call_function(builtins.getattr, (x, "device"))
        rng1 = g.call_function(
            torch.arange,
            (8 * 8,),
            {"dtype": torch.float32, "device": device},
        )
        rng1 = g.call_method("view", (rng1, 8, 8))
        rng2 = g.call_function(torch.arange, (10, 18), {"device": device})
        r1 = g.call_function(torch.mul, (x, rng1))
        r2 = g.call_function(torch.add, (r1, rng2))
        g.output((r1, r2))

        self.common(g, (torch.randn(8, 8),))

    def test_views1(self):
        def fn1(x, y):
            return (x.view(size2) + y,)

        def fn2(x, y):
            return ((x + 1).view(size2) + y,)

        views = [
            ([5 * 7], [5, 7]),
            ([2 * 3 * 4 * 5 * 6 * 7], [2, 3, 4, 5, 6, 7]),
            ([2 * 3, 4, 5, 6 * 7], [2, 3, 4, 5, 6, 7]),
            ([10 * 5, 20], [10, 5, 20]),
            ([1, 10, 1], [10]),
            ([10, 1, 10, 1, 10], [10, 100]),
            ([2, 2, 2, 2], [4, 4]),
        ]
        for size1, size2 in views:
            self.common(fn1, (torch.randn(size1), torch.randn(size2)))
            self.common(fn2, (torch.randn(size1), torch.randn(size2)))

        for size2, size1 in views:
            self.common(fn1, (torch.randn(size1), torch.randn(size2)))
            self.common(fn2, (torch.randn(size1), torch.randn(size2)))

    def test_views2(self):
        def fn1(x):
            return (x.view(size2) + 1,)

        def fn2(x):
            return ((x * 2).view(size2) + 1,)

        for size1, size2 in [
            ([2, 2, 2, 2], [4, -1]),
            ([10, 1, 10, 1, 10], [-1, 100]),
            ([10 * 5, 20], [10, -1, 20]),
        ]:
            self.common(fn1, (torch.randn(size1),))
            self.common(fn2, (torch.randn(size1),))


class CpuTests(TestCase):
    common = check_model


CommonTemplate.install(CpuTests, "cpu")

if HAS_CUDA:

    class SweepInputsCudaTest(SweepInputs2, TestCase):
        gen = InputGen(10, "cuda")

    SweepInputsCudaTest.populate()

    class GpuTests(TestCase):
        common = check_model_cuda

        def test_simplify_dims(self):
            def fn(a):
                return (a + 1,)

            self.common(
                fn, (torch.randn(2, 3, 10, 5, 6, device="cuda")[:, :, 2::2, :, :],)
            )

    CommonTemplate.install(GpuTests, "cuda")
