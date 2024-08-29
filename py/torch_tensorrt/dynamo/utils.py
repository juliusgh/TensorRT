from __future__ import annotations

import logging
from dataclasses import fields, replace
from enum import Enum
from typing import Any, Callable, Dict, Optional, Sequence, Union

import numpy as np
import tensorrt as trt
import torch
from torch_tensorrt._Device import Device
from torch_tensorrt._enums import dtype
from torch_tensorrt._Input import Input
from torch_tensorrt.dynamo import _defaults
from torch_tensorrt.dynamo._settings import CompilationSettings

from packaging import version

from .types import TRTDataType

logger = logging.getLogger(__name__)

COSINE_THRESHOLD = 0.99
DYNAMIC_DIM = -1
RTOL = 5e-3
ATOL = 5e-3


class Frameworks(Enum):
    NUMPY = "numpy"
    TORCH = "torch"
    TRT = "trt"


DataTypeEquivalence: Dict[
    TRTDataType, Dict[Frameworks, Union[TRTDataType, np.dtype, torch.dtype]]
] = {
    trt.int8: {
        Frameworks.NUMPY: np.int8,
        Frameworks.TORCH: torch.int8,
        Frameworks.TRT: trt.int8,
    },
    trt.int32: {
        Frameworks.NUMPY: np.int32,
        Frameworks.TORCH: torch.int32,
        Frameworks.TRT: trt.int32,
    },
    trt.int64: {
        Frameworks.NUMPY: np.int64,
        Frameworks.TORCH: torch.int64,
        Frameworks.TRT: trt.int64,
    },
    trt.float16: {
        Frameworks.NUMPY: np.float16,
        Frameworks.TORCH: torch.float16,
        Frameworks.TRT: trt.float16,
    },
    trt.float32: {
        Frameworks.NUMPY: np.float32,
        Frameworks.TORCH: torch.float32,
        Frameworks.TRT: trt.float32,
    },
    trt.bool: {
        Frameworks.NUMPY: bool,
        Frameworks.TORCH: torch.bool,
        Frameworks.TRT: trt.bool,
    },
}

if trt.__version__ >= "7.0":
    DataTypeEquivalence[trt.bool] = {
        Frameworks.NUMPY: np.bool_,
        Frameworks.TORCH: torch.bool,
        Frameworks.TRT: trt.bool,
    }


def use_python_runtime_parser(use_python_runtime: Optional[bool] = None) -> bool:
    """Parses a user-provided input argument regarding Python runtime

    Automatically handles cases where the user has not specified a runtime (None)

    Returns True if the Python runtime should be used, False if the C++ runtime should be used
    """
    using_python_runtime = use_python_runtime
    reason = ""

    # Runtime was manually specified by the user
    if using_python_runtime is not None:
        reason = "as requested by user"
    # Runtime was not manually specified by the user, automatically detect runtime
    else:
        try:
            from torch_tensorrt.dynamo.runtime import TorchTensorRTModule  # noqa: F401

            using_python_runtime = False
            reason = "since C++ dependency was detected as present"
        except ImportError:
            using_python_runtime = True
            reason = "since import failed, C++ dependency not installed"

    logger.info(
        f"Using {'Python-only' if using_python_runtime else 'Default'} Torch-TRT Runtime ({reason})"
    )

    return using_python_runtime


def cosine_similarity(gt_tensor: torch.Tensor, pred_tensor: torch.Tensor) -> float:
    gt_tensor = gt_tensor.flatten().to(torch.float32)
    pred_tensor = pred_tensor.flatten().to(torch.float32)
    if torch.sum(gt_tensor) == 0.0 or torch.sum(pred_tensor) == 0.0:
        if torch.allclose(gt_tensor, pred_tensor, atol=1e-4, rtol=1e-4, equal_nan=True):
            return 1.0
    res_t = torch.nn.functional.cosine_similarity(
        gt_tensor, pred_tensor, dim=0, eps=1e-6
    )
    res: float = res_t.cpu().detach().item()

    return res


def input_is_dynamic(inputs: Sequence[Union[Input, torch.Tensor]]) -> bool:
    """
    Return true if the provided inputs are `torch_tensorrt.Input` objects and have dynamic shapes.
    """
    return not any(isinstance(input, torch.Tensor) for input in inputs) and any(
        input.shape_mode == Input._ShapeMode.DYNAMIC for input in inputs
    )


def get_torch_inputs(
    inputs: Sequence[Input] | Dict[Any, Any],
    device: Union[Device, torch.device, str],
    mode: str = "",
) -> Sequence[torch.tensor] | Dict[Any, Any]:
    """
    Return the torch_tensor from the Input object. If mode is set, this implies
    user is using dynamic shaped inputs and return the corresponding input based
    on the mode requested.
    """
    device = to_torch_device(device)
    if mode:
        if isinstance(inputs, dict):
            result = {}
            for k, v in inputs.items():
                if isinstance(v, (list, tuple, dict)):
                    result[k] = get_torch_inputs(v, device)
                else:
                    result[k] = v.example_tensor(mode).to(device)
            return result
        else:
            return [
                input.example_tensor(mode).to(device)
                for input in inputs
                if isinstance(input, Input)
            ]

    if isinstance(inputs, dict):
        result = {}
        for k, v in inputs.items():
            if isinstance(v, (list, tuple, dict)):
                result[k] = get_torch_inputs(v, device)
            else:
                result[k] = v.torch_tensor.to(device)
        return result
    else:
        return [
            input.torch_tensor.to(device) if isinstance(input, Input) else input
            for input in inputs
        ]


def set_log_level(parent_logger: Any, level: Any) -> None:
    """
    Sets the log level to the user provided level.
    This is used to set debug logging at a global level
    at entry points of tracing, dynamo and torch_compile compilation.
    """
    if parent_logger:
        parent_logger.setLevel(level)


def prepare_inputs(
    inputs: Input | torch.Tensor | Sequence[Any] | Dict[Any, Any],
    disable_memory_format_check: bool = False,
) -> Any:
    if isinstance(inputs, Input):
        return inputs

    elif isinstance(inputs, torch.Tensor):
        return Input.from_tensor(
            inputs, disable_memory_format_check=disable_memory_format_check
        )

    elif isinstance(inputs, (list, tuple)):
        torchtrt_input_list = []
        for input_obj in inputs:
            torchtrt_input = prepare_inputs(
                input_obj, disable_memory_format_check=disable_memory_format_check
            )
            torchtrt_input_list.append(torchtrt_input)

        return (
            torchtrt_input_list
            if isinstance(inputs, list)
            else tuple(torchtrt_input_list)
        )

    elif isinstance(inputs, dict):
        torchtrt_inputs_dict: Dict[Any, Any] = dict()

        for key, input_obj in inputs.items():
            torchtrt_input = prepare_inputs(
                input_obj, disable_memory_format_check=disable_memory_format_check
            )
            torchtrt_inputs_dict[key] = torchtrt_input

        return torchtrt_inputs_dict

    else:
        raise ValueError(
            f"Invalid input type {type(inputs)} encountered in the dynamo_compile input parsing. "
            + "Allowed input types: {torch_tensorrt.Input, torch.Tensor, list, tuple, dict}"
        )


def parse_complex_tensor_structs(
    inputs: Input | torch.Tensor | Sequence[Any] | Dict[Any, Any],
    attribute_to_extract: str,
    apply_fn: Callable[[Any], Any] = lambda x: x,
) -> Any:
    """Parses complex structures of Tensors and returns a mirrored structure
    Extracts key attributes of each singular element, while reconstructing the struct
    Optionally applies a function to each attribute before returning
    """
    if isinstance(inputs, (torch.Tensor, Input)):
        return apply_fn(getattr(inputs, attribute_to_extract, None))
    elif isinstance(inputs, (int, float, bool)):
        # inputs is a python scalar value
        inputs_torch = torch.tensor(inputs)
        return apply_fn(getattr(inputs_torch, attribute_to_extract, None))

    elif isinstance(inputs, (list, tuple)):
        torchtrt_input_list = []
        for input_obj in inputs:
            torchtrt_input = parse_complex_tensor_structs(
                input_obj, attribute_to_extract, apply_fn
            )
            torchtrt_input_list.append(torchtrt_input)

        return (
            torchtrt_input_list
            if isinstance(inputs, list)
            else tuple(torchtrt_input_list)
        )

    elif isinstance(inputs, dict):
        torchtrt_inputs_dict: Dict[Any, Any] = dict()

        for key, input_obj in inputs.items():
            torchtrt_input = parse_complex_tensor_structs(
                input_obj, attribute_to_extract, apply_fn
            )
            torchtrt_inputs_dict[key] = torchtrt_input

        return torchtrt_inputs_dict

    else:
        raise ValueError(
            f"Invalid input type {type(inputs)} encountered during Dynamo input parsing. "
            + "Allowed input types: {torch_tensorrt.Input, torch.Tensor, list, tuple, dict}"
        )


def to_torch_device(device: Optional[Union[Device, torch.device, str]]) -> torch.device:
    """Cast a device-type to torch.device

    Returns the corresponding torch.device
    """
    if isinstance(device, Device):
        return device.to(torch.device)

    elif isinstance(device, torch.device):
        return device

    elif device is None:
        return torch.device(torch.cuda.current_device())

    else:
        return torch.device(device)


def to_torch_tensorrt_device(
    device: Optional[Union[Device, torch.device, str]]
) -> Device:
    """Cast a device-type to torch_tensorrt.Device

    Returns the corresponding torch_tensorrt.Device
    """
    return Device._from(device)


def parse_dynamo_kwargs(kwargs: Any) -> CompilationSettings:
    """Parses the kwargs field of a Dynamo backend

    Args:
        kwargs: Keyword arguments dictionary provided to the backend
    Returns:
        CompilationSettings object with relevant kwargs
    """

    # Initialize an empty CompilationSettings object
    settings = CompilationSettings()

    # If the user specifies keyword args, overwrite those fields in settings
    # Validate all specified kwargs to ensure they are true fields of the dataclass
    #
    # Note: kwargs provided by torch.compile are wrapped in the "options" key
    if kwargs:
        if "options" in kwargs and len(kwargs) == 1:
            kwargs = kwargs["options"]

        valid_attrs = {attr.name for attr in fields(settings)}
        valid_kwargs = {k: v for k, v in kwargs.items() if k in valid_attrs}
        settings = replace(settings, **valid_kwargs)

    # TODO: Remove once Dynamo precisions refactoring is complete
    if "enabled_precisions" in kwargs:
        enabled_precisions = {dtype._from(e) for e in kwargs["enabled_precisions"]}

        if len(enabled_precisions) == 0:
            logger.info(
                f"No precision specified, defaulting to {_defaults.ENABLED_PRECISION}"
            )
            enabled_precisions = _defaults.ENABLED_PRECISIONS

        settings.enabled_precisions = enabled_precisions

    # Parse input runtime specification
    settings.use_python_runtime = use_python_runtime_parser(settings.use_python_runtime)

    # Ensure device is a torch_tensorrt Device
    settings.device = to_torch_tensorrt_device(settings.device)

    # Check and update device settings
    if "device" not in kwargs:
        logger.info(
            f"Device not specified, using Torch default current device - cuda:{settings.device.gpu_id}. "
            "If this is incorrect, please specify an input device, via the device keyword."
        )

    # Ignore and warn about require_full_compilation flag
    if settings.require_full_compilation:
        logger.warning(
            "Detected require_full_compilation=True for a torch.compile run. "
            "This option has no effect in torch.compile."
        )
        settings.require_full_compilation = False

    # If cache_built_engines and reuse_cached_engines are True but custom_engine_cache is not provided,
    # then create a default disk engine cache
    if kwargs.get("cache_built_engines") or kwargs.get("reuse_cached_engines"):
        if settings.custom_engine_cache is None:
            from torch_tensorrt.dynamo._engine_caching import DiskEngineCache

            engine_cache_dir = kwargs.get(
                "engine_cache_dir", _defaults.ENGINE_CACHE_DIR
            )
            engine_cache_size = kwargs.get(
                "engine_cache_size", _defaults.ENGINE_CACHE_SIZE
            )
            settings.custom_engine_cache = DiskEngineCache(
                engine_cache_dir, engine_cache_size
            )

    logger.info("Compilation Settings: %s\n", settings)

    return settings


def req_torch_version(min_torch_version: str = "2.dev") -> Callable[..., Any]:
    """
    Create a decorator which verifies the Torch version installed
    against a specified version range

    Args:
        min_torch_version (str): The minimum required Torch version
        for the decorated function to work properly

    Returns:
        A decorator which raises a descriptive error message if
        an unsupported Torch version is used
    """

    def nested_decorator(f: Callable[..., Any]) -> Callable[..., Any]:
        def function_wrapper(*args: Any, **kwargs: Any) -> Any:
            # Parse minimum and current Torch versions
            min_version = version.parse(min_torch_version)
            current_version = version.parse(torch.__version__)

            if current_version < min_version:
                raise AssertionError(
                    f"Expected Torch version {min_torch_version} or greater, "
                    + f"when calling {f}. Detected version {torch.__version__}"
                )
            else:
                return f(*args, **kwargs)

        return function_wrapper

    return nested_decorator


def check_module_output(
    new_module: torch.fx.GraphModule,
    refitted_module: torch.fx.GraphModule,
    arg_inputs: Any,
    kwarg_inputs: Any = None,
) -> bool:
    old_outputs, new_outputs = refitted_module(*arg_inputs), new_module(
        *arg_inputs, **kwarg_inputs
    )
    if type(old_outputs) != type(new_outputs):
        logger.warning("The output types are different. Output check is skipped.")
        return True
    return check_output_equal(old_outputs, new_outputs)


def check_output_equal(
    output1: Any,
    output2: Any,
    rtol: float = RTOL,
    atol: float = ATOL,
) -> bool:

    if type(output1) != type(output2):
        logger.warning(
            "The output types are different. Check_output_equal will always return false."
        )
        return False

    if isinstance(output1, torch.Tensor):
        if output1.shape != output2.shape:
            return False
        return torch.allclose(output1, output2, rtol, atol)  # type: ignore

    elif isinstance(output1, (tuple, list)):
        if len(output1) != len(output2):
            return False
        for a, b in zip(output1, output2):
            if not check_output_equal(a, b):
                return False
            return True

    elif isinstance(output1, dict):
        if output1.keys() != output2.keys():
            return False
        for a, b in zip(output1.values(), output2.values()):
            if not check_output_equal(a, b):
                return False
        return True

    logger.warning(
        "The output type is not supported to be checked. Check_output_equal will always return false."
    )
    return False


def get_flat_args_with_check(
    exported_program: torch.export.ExportedProgram,
    args: list[Any],
    kwargs: dict[str, Any],
) -> tuple[Any, Any]:
    """Flatten args, kwargs using pytree, then, check specs.

    Args:
        args: List[Any] original args passed to __call__
        kwargs: Dict[str, Any] original kwargs passed to __call

    Returns:
        A tuple of (flat_args, received_spec)
        flat_args is flattend args / kwargs
        received_spec is the pytree spec produced while flattening the
        tuple (args, kwargs)
    """
    import torch.utils._pytree as pytree
    from torch.export._tree_utils import reorder_kwargs

    in_spec = exported_program.call_spec.in_spec
    if in_spec is not None:
        kwargs = reorder_kwargs(kwargs, in_spec)
    flat_args_with_path, received_spec = pytree.tree_flatten_with_path((args, kwargs))
    flat_args = tuple(x[1] for x in flat_args_with_path)
    return flat_args, received_spec
