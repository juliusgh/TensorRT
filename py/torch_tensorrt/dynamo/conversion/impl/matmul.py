from typing import Optional

import torch
from torch.fx.node import Target
from torch_tensorrt import _enums
from torch_tensorrt.dynamo._SourceIR import SourceIR
from torch_tensorrt.dynamo.conversion._ConversionContext import ConversionContext
from torch_tensorrt.dynamo.conversion.converter_utils import get_trt_tensor
from torch_tensorrt.fx.converters.converter_utils import broadcast, set_layer_name
from torch_tensorrt.fx.types import TRTTensor

import tensorrt as trt


def matrix_multiply(
    ctx: ConversionContext,
    target: Target,
    source_ir: Optional[SourceIR],
    name: str,
    input: TRTTensor,
    other: TRTTensor,
    input_matrix_op: trt.MatrixOperation = trt.MatrixOperation.NONE,
    other_matrix_op: trt.MatrixOperation = trt.MatrixOperation.NONE,
) -> TRTTensor:
    if not isinstance(input, trt.ITensor):
        input = get_trt_tensor(ctx, input, f"{name}_input")
    if not isinstance(other, trt.ITensor):
        other = get_trt_tensor(
            ctx,
            other,
            f"{name}_other",
            dtype=_enums.dtype._from(input.dtype).to(torch.dtype),
        )

    preset_diff = 0

    if len(input.shape) == 1:
        preset_diff -= 1
        input_matrix_op = trt.MatrixOperation.VECTOR

    if len(other.shape) == 1:
        preset_diff += 1
        other_matrix_op = trt.MatrixOperation.VECTOR

    input, other = broadcast(
        ctx.net, input, other, f"{name}_input", f"{name}_other", preset_diff
    )
    layer = ctx.net.add_matrix_multiply(input, input_matrix_op, other, other_matrix_op)
    set_layer_name(layer, target, name, source_ir)
    return layer.get_output(0)
