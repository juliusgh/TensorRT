# type: ignore

import logging
import time
import unittest
from typing import Callable, List, Optional, Set, Tuple

import torch
from torch.testing._internal.common_utils import TestCase
from torch_tensorrt import Input
from torch_tensorrt.dynamo._settings import CompilationSettings

# Use interpreter, input spec, and test case from fx_ts_compat to test Dynamo Converter Registry
from torch_tensorrt.dynamo.conversion import TRTInterpreter
from torch_tensorrt.dynamo.lowering import apply_lowering_passes
from torch_tensorrt.dynamo.runtime import PythonTorchTensorRTModule

_LOGGER: logging.Logger = logging.getLogger(__name__)


def fetch_attr(mod, target):
    """
    Fetch an attribute from the ``Module`` hierarchy of ``mod.module``.

    Args:
        target (str): The fully-qualfiied name of the attribute to fetch

    Return:
        Any: The value of the attribute.
    """
    target_atoms = target.split(".")
    attr_itr = mod
    for i, atom in enumerate(target_atoms):
        if not hasattr(attr_itr, atom):
            raise RuntimeError(
                f"Node referenced nonexistent target {'.'.join(target_atoms[:i])}"
            )
        attr_itr = getattr(attr_itr, atom)
    return attr_itr


@unittest.skipIf(not torch.cuda.is_available(), "Skip because CUDA is not available")
class TRTTestCase(TestCase):
    def setUp(self):
        super().setUp()
        torch.manual_seed(3)

    def run_test(
        self,
        mod,
        inputs,
        interpreter,
        rtol,
        atol,
        check_dtype=True,
    ):
        with torch.no_grad():
            cuda_inputs = []
            for i in inputs:
                cuda_inputs.append(i.cuda())

            mod.eval()
            start = time.perf_counter()
            interpreter_result = interpreter.run()
            sec = time.perf_counter() - start
            _LOGGER.info(f"Interpreter run time(s): {sec}")
            trt_mod = PythonTorchTensorRTModule(
                interpreter_result.engine,
                interpreter_result.input_names,
                interpreter_result.output_names,
            )

            ref_outputs = mod(*inputs)

            torch.cuda.synchronize()
            start_event = torch.cuda.Event(enable_timing=True)
            end_event = torch.cuda.Event(enable_timing=True)
            start_event.record()
            outputs = trt_mod(*cuda_inputs)
            end_event.record()
            torch.cuda.synchronize()
            _LOGGER.info(
                f"TRT run time(s)= {(start_event.elapsed_time(end_event) * 1.0e-3)}"
            )

            if type(outputs) not in (list, tuple):
                outputs = [outputs]
            if type(ref_outputs) not in (
                list,
                tuple,
                torch.return_types.max,
                torch.return_types.min,
            ):
                ref_outputs = [ref_outputs]
            for out, ref in zip(outputs, ref_outputs):
                if not isinstance(ref, torch.Tensor):
                    ref = torch.tensor([ref])
                ref = ref.cpu()  # to_dtype test has cases with gpu output
                if ref.dtype == torch.int64:
                    ref = ref.int()  # convert torch.max's index output tensor to int32
                torch.testing.assert_close(
                    out.cpu(),
                    ref,
                    rtol=rtol,
                    atol=atol,
                    equal_nan=True,
                    check_dtype=check_dtype,
                )

    def run_test_custom_compare_results(
        self,
        mod,
        inputs,
        expected_ops,
        interpreter,
        comparators: List[Tuple[Callable, List]],
        fp16_mode=False,
    ):
        """
        Runs the test and compares the result using the provided comparators.
        The size of comparators must be equal to the number of outputs from 'mod'.

        mod          - a model to run.
        inputs       - a list of the model inputs.
        expected ops - a list of ops that should be verified.
        interpreter  - used for converting the model to TRT.
        comparators  - a list of (func, args) pairs corresponding to each of
                       the module outputs. usage: func(x, y, *args)

        """
        with torch.no_grad():
            cuda_inputs = []
            for i in inputs:
                cuda_inputs.append(i.cuda())

            mod.eval()
            if len(expected_ops):
                self.assert_has_op(mod, expected_ops)

            interpreter_result = interpreter.run(
                precision=torch.half if fp16_mode else torch.float
            )
            trt_mod = PythonTorchTensorRTModule(
                interpreter_result.engine,
                interpreter_result.input_names,
                interpreter_result.output_names,
            )
            res_trt = trt_mod(*cuda_inputs).cpu()
            res_cpu = mod(*inputs)
            assert len(res_trt) == len(res_cpu)
            assert len(res_cpu) == len(comparators)
            for output_trt, output_cpu, comparator in zip(
                res_trt, res_cpu, comparators
            ):
                comp_func = comparator[0]
                args = comparator[1]
                self.assertTrue(comp_func(output_trt, output_cpu, *args))

    def run_test_with_error(self, mod, inputs, interpreter, expect_error):
        with self.assertRaises(expect_error):
            with torch.no_grad():
                cuda_inputs = []
                for i in inputs:
                    cuda_inputs.append(i.cuda())

                mod.eval()
                interpreter.run(precision=torch.float)

    def assert_has_op(self, mod, ops):
        ops_in_mod = set()

        for node in mod.graph.nodes:
            if node.op == "call_module":
                ops_in_mod.add(type(fetch_attr(mod, node.target)))
            elif node.op in {"call_function", "call_method"}:
                ops_in_mod.add(node.target)

        self.assertTrue(
            ops_in_mod >= ops, f"expected ops {ops}, actuall ops {ops_in_mod}"
        )

    def assert_unexpected_op(self, mod, ops):
        for node in mod.graph.nodes:
            if node.op == "call_module":
                if type(fetch_attr(mod, node.target)) in ops:
                    return False
            elif node.op in {"call_function", "call_method"}:
                if node.target in ops:
                    return False
        return True


class DispatchTestCase(TRTTestCase):
    def generate_graph(
        self,
        mod: torch.nn.Module,
        original_inputs: List[torch.Tensor],
        use_dynamo_tracer: bool,
        enable_passes: bool,
    ):
        if use_dynamo_tracer:
            fx_module = torch._dynamo.export(
                mod,
                *original_inputs,
                aten_graph=True,
                assume_static_by_default=True,
                tracing_mode="real",
            ).graph_module
        else:
            fx_module = torch.fx.symbolic_trace(mod)
        if enable_passes:
            fx_module = apply_lowering_passes(fx_module, original_inputs)
        _LOGGER.info(f"FX graph= {fx_module.graph}")
        return fx_module

    def run_test(
        self,
        mod,
        inputs,
        rtol=1e-03,
        atol=1e-03,
        precision=torch.float,
        check_dtype=True,
        output_dtypes=None,
        use_dynamo_tracer=False,
        enable_passes=False,
    ):
        mod.eval()
        mod = self.generate_graph(
            mod,
            inputs,
            use_dynamo_tracer=use_dynamo_tracer,
            enable_passes=enable_passes,
        )

        # Previous instance of the interpreter auto-casted 64-bit inputs
        # We replicate this behavior here
        compilation_settings = CompilationSettings(
            precision=precision, truncate_long_and_double=True
        )

        interp = TRTInterpreter(
            mod,
            Input.from_tensors(inputs),
            output_dtypes=output_dtypes,
            compilation_settings=compilation_settings,
        )
        super().run_test(
            mod,
            inputs,
            interp,
            rtol,
            atol,
            check_dtype,
        )

    def run_test_with_dynamic_shape(
        self,
        mod,
        input_specs,
        rtol=1e-03,
        atol=1e-03,
        output_dtypes=None,
        use_dynamo_tracer=False,
        enable_passes=False,
    ):
        mod.eval()
        inputs = [spec.example_tensor("opt_shape") for spec in input_specs]
        mod = self.generate_graph(
            mod,
            inputs,
            use_dynamo_tracer=use_dynamo_tracer,
            enable_passes=enable_passes,
        )

        # Previous instance of the interpreter auto-casted 64-bit inputs
        # We replicate this behavior here
        compilation_settings = CompilationSettings(truncate_long_and_double=True)

        interp = TRTInterpreter(
            mod,
            input_specs,
            output_dtypes=output_dtypes,
            compilation_settings=compilation_settings,
        )
        # Since the lowering is based on optimal shape. We need to test with
        # different shape(for ex. max shape) for testing dynamic shape
        inputs_max = [spec.example_tensor("max_shape") for spec in input_specs]
        super().run_test(mod, inputs_max, interp, rtol, atol)
