import unittest

import fx2trt_oss.tracer.acc_tracer.acc_ops as acc_ops
import tensorrt as trt
import torch
import torch.nn as nn
from parameterized import param, parameterized
from torch.testing._internal.common_fx2trt import AccTestCase
from torch.testing._internal.common_utils import run_tests


class TestPadConverter(AccTestCase):
    @parameterized.expand(
        [
            ("1d", (1, 2)),
            ("2d", (2, 0, 0, 1)),
        ]
    )
    def test_pad(self, _, pad):
        class Pad(nn.Module):
            def forward(self, x):
                return torch.nn.functional.pad(x, pad)

        inputs = [torch.randn(1, 2, 3, 4)]
        self.run_test(
            Pad(),
            inputs,
            expected_ops={acc_ops.pad},
        )

    @parameterized.expand(
        [
            param("value", pad=(2, 0, 0, 1), value=1),
        ]
    )
    def test_pad_fail(self, _, pad, mode="constant", value=0):
        class Pad(nn.Module):
            def forward(self, x):
                return torch.nn.functional.pad(x, pad, mode, value)

        inputs = [torch.randn(1, 2, 3, 4)]
        self.run_test_with_assert_error(
            Pad(),
            inputs,
            expect_error=RuntimeError,
        )

    @parameterized.expand(
        [
            ("3d", (2, 2, 3, 1, 2, 2)),
        ]
    )
    @unittest.skipIf(
        trt.__version__ < "8.2",
        "Padding 3d only supported in TensorRT 8.2 and later",
    )
    def test_pad_3d(self, _, pad):
        class Pad(nn.Module):
            def forward(self, x):
                return torch.nn.functional.pad(x, pad)

        inputs = [torch.randn(1, 2, 3, 4)]
        self.run_test(
            Pad(),
            inputs,
            expected_ops={acc_ops.pad},
        )


if __name__ == "__main__":
    run_tests()
