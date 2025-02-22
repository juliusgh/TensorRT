from __future__ import annotations

import logging
from typing import Any, List, Optional, Sequence

import tensorrt as trt
import torch
from torch.fx.experimental.proxy_tensor import unset_fake_temporarily
from torch_tensorrt._Device import Device
from torch_tensorrt._enums import dtype
from torch_tensorrt._features import ENABLED_FEATURES
from torch_tensorrt._Input import Input
from torch_tensorrt.dynamo._settings import CompilationSettings
from torch_tensorrt.dynamo.conversion._TRTInterpreter import (
    TRTInterpreter,
    TRTInterpreterResult,
)
from torch_tensorrt.dynamo.runtime import PythonTorchTensorRTModule, TorchTensorRTModule
from torch_tensorrt.dynamo.utils import get_torch_inputs

logger = logging.getLogger(__name__)


def infer_module_output_dtypes(
    module: torch.fx.GraphModule,
    inputs: Sequence[Input],
    device: Device,
    kwarg_inputs: Optional[dict[str, Any]] = None,
    truncate_double: bool = False,
) -> List[dtype]:
    """
    inputs can be either arg_inputs or flattened input list. If it is flattened list, kwarg_inputs
    should be None, as it is already included in the flattened input.
    """
    with unset_fake_temporarily():
        torch_inputs = get_torch_inputs(inputs, device)
        if kwarg_inputs is None:
            kwarg_inputs = {}
        torch_kwarg_inputs = get_torch_inputs(kwarg_inputs, device)
        module = module.to(device.to(torch.device))
        module_outputs = module(*torch_inputs, **torch_kwarg_inputs)
        if not isinstance(module_outputs, (list, tuple)):
            module_outputs = [module_outputs]

    # Int64 outputs can sometimes be generated from within other operators
    # such as aten.sum - such outputs can be truncated
    output_dtypes = []
    for output in module_outputs:
        output_ = output
        # We don't need to check if output is nested here because the input module will be flattened
        if not isinstance(output, torch.Tensor):
            if isinstance(output, str):
                raise ValueError(
                    f"Received an output type {type(output)} that's not in the acceptable datatypes (https://pytorch.org/docs/stable/tensor_attributes.html#torch.dtype)"
                )
            else:
                output_ = torch.tensor(output)

        if truncate_double and output_.dtype == dtype.float64:
            output_dtypes.append(dtype.float32)
        else:
            output_dtypes.append(dtype._from(output_.dtype))

    return output_dtypes


def interpret_module_to_result(
    module: torch.fx.GraphModule,
    inputs: Sequence[Input],
    settings: CompilationSettings = CompilationSettings(),
    arg_inputs: Optional[Sequence[Input]] = None,
    kwarg_inputs: Optional[dict[str, Any]] = None,
) -> TRTInterpreterResult:
    """Interpret an FX module to a TRTInterpreterResult
    Args:
        module: FX GraphModule to interpret
        inputs: Sequence of FLATTENED Tensors representing inputs to the module. It should include both
                arg_inputs and kwarg_inputs, if applicable.
        arg_inputs: Sequence of Tensors representing inputs to the module.
        kwarg_inputs: A dictionary of Tensors representing inputs to the module.
        settings: Compilation settings
    Returns:
        TRTInterpreterResult
    """
    if arg_inputs is not None:
        output_dtypes = infer_module_output_dtypes(
            module,
            arg_inputs,
            settings.device,
            kwarg_inputs=kwarg_inputs,
            truncate_double=settings.truncate_double,
        )
    else:
        # args and kwargs are combined and flattened to one list
        output_dtypes = infer_module_output_dtypes(
            module,
            inputs,
            settings.device,
            truncate_double=settings.truncate_double,
        )

    interpreter = TRTInterpreter(
        module,
        inputs,
        logger_level=(trt.Logger.VERBOSE if settings.debug else trt.Logger.WARNING),
        output_dtypes=output_dtypes,
        compilation_settings=settings,
    )
    interpreter_result = interpreter.run()
    return interpreter_result


def convert_module(
    module: torch.fx.GraphModule,
    inputs: Sequence[Input],
    settings: CompilationSettings = CompilationSettings(),
    name: str = "",
) -> PythonTorchTensorRTModule | TorchTensorRTModule:
    """Convert an FX module to a TRT module
    Args:
        module: FX GraphModule to convert
        inputs: Sequence of Tensors representing inputs to the module
        settings: Compilation settings
        name: TRT engine name
    Returns:
        PythonTorchTensorRTModule or TorchTensorRTModule
    """
    interpreter_result = interpret_module_to_result(module, inputs, settings)
    # Test fast refit:
    from torch_tensorrt.dynamo._refit import _refit_single_trt_engine_with_gm
    from torch_tensorrt.logging import TRT_LOGGER

    weight_name_map: Any = None
    # Do the test refit with cached map if make_refitable is enabled
    if settings.make_refitable:
        runtime = trt.Runtime(TRT_LOGGER)
        refit_test_engine = runtime.deserialize_cuda_engine(
            interpreter_result.serialized_engine
        )
        try:
            _refit_single_trt_engine_with_gm(
                new_gm=module,
                old_engine=refit_test_engine,
                input_list=inputs,
                settings=settings,
                weight_name_map=interpreter_result.weight_name_map,
            )
            weight_name_map = interpreter_result.weight_name_map
        except AssertionError:
            logger.warning("Fast refit test failed. Removing the weight map caching.")

        del refit_test_engine
        torch.cuda.empty_cache()

    rt_cls = PythonTorchTensorRTModule

    if ENABLED_FEATURES.torch_tensorrt_runtime and not settings.use_python_runtime:

        from torch_tensorrt.dynamo.runtime import TorchTensorRTModule

        rt_cls = TorchTensorRTModule

    elif (
        not ENABLED_FEATURES.torch_tensorrt_runtime and not settings.use_python_runtime
    ):

        logger.info(
            "Since Torch-TensorRT runtime is not available, using Python Runtime, some features may not be available"
        )

    return rt_cls(
        serialized_engine=interpreter_result.serialized_engine,
        input_binding_names=list(interpreter_result.input_names),
        output_binding_names=list(interpreter_result.output_names),
        name=name,
        settings=settings,
        weight_name_map=weight_name_map,
    )
