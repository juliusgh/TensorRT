from typing import Any

import torch
from torch_tensorrt._version import __version__


def dump_build_info() -> None:
    """Prints build information about the torch_tensorrt distribution to stdout"""
    print(get_build_info())


def get_build_info() -> str:
    """Returns a string containing the build information of torch_tensorrt distribution

    Returns:
        str: String containing the build information for torch_tensorrt distribution
    """
    try:
        from torch_tensorrt import _C

        core_build_info = _C.get_build_info()
    except:
        core_build_info = ""
        print("Unable to get _C build info, _C extensions unavailable")

    build_info = str(
        "Torch-TensorRT Version: "
        + str(__version__)
        + "\n"
        + "Using PyTorch Version: "
        + str(torch.__version__)
        + "\n"
        + core_build_info
    )
    return build_info


def set_device(gpu_id: int) -> None:
    try:
        from torch_tensorrt import _C

        _C.set_device(gpu_id)
    except:
        print("Unable to set_device, _C extensions unavailable")


def sanitized_torch_version() -> Any:
    return (
        torch.__version__
        if ".nv" not in torch.__version__
        else torch.__version__.split(".nv")[0]
    )
