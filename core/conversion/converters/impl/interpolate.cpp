#include "torch/torch.h"
#include "core/util/prelude.h"
#include "core/conversion/converters/converters.h"

#include <csignal>

namespace trtorch {
namespace core {
namespace conversion {
namespace converters {
namespace impl {
namespace {

auto interpolate_registrations = RegisterNodeConversionPatterns()
    .pattern({
        "aten::upsample_nearest1d(Tensor self, int[1] output_size, float? scales=None) -> (Tensor)",
        [](ConversionCtx* ctx, const torch::jit::Node*n, args& args) -> bool {
            auto in = args[0].ITensor();
            auto in_shape = util::toVec(in->getDimensions());

            // remove padding that TensorRt adds automatically
            if (in_shape.size() >= 4) {
                // remove first dimension
                in_shape.erase(in_shape.begin());

                auto shuffle = ctx->net->addShuffle(*in);
                shuffle->setReshapeDimensions(util::toDims(in_shape));
                shuffle->setName( (util::node_info(n) + " [Reshape to " + util::toStr(util::toDims(in_shape)) + "]").c_str() );
                in = shuffle->getOutput(0);
            }
                
            // Case 1: user uses output size and not scales
            if (!args[1].IValue()->isNone() && args[2].IValue()->isNone()) {
                auto out_size = util::toVec(util::toDims(args[1].unwrapToIntList()));

                TRTORCH_ASSERT(out_size.size() == 1, "aten::upsample_nearest1d input Tensor and output size dimension mismatch");
                
                auto out_shape = in_shape;
                std::copy(out_size.begin(), out_size.end(), out_shape.begin() + (in_shape.size() - out_size.size()));

                auto resize_layer = ctx->net->addResize(*in);
                TRTORCH_CHECK(resize_layer, "Unable to create interpolation (resizing) layer from node" << *n);

                resize_layer->setOutputDimensions(util::toDims(out_shape));
                resize_layer->setResizeMode(nvinfer1::ResizeMode::kNEAREST);
                resize_layer->setName(util::node_info(n).c_str());

                // auto out_tensor = resize_layer->getOutput(0);
                // out_shape.erase(out_shape.begin());
                // auto shuffle = ctx->net->addShuffle(*out_tensor);
                // shuffle->setReshapeDimensions(util::toDims(out_shape));
                // shuffle->setName( (util::node_info(n) + " [Reshape to " + util::toStr(util::toDims(out_shape)) + "]").c_str() );
                // auto layer_output = ctx->AssociateValueAndTensor(n->outputs()[0], shuffle->getOutput(0));
                // LOG_DEBUG("Output tensor shape: " << layer_output->getDimensions());
                
                // std::cout << "PRINTING STUFF AT THE END!" << std::endl;
                // auto final = util::toVec(shuffle->getOutput(0)->getDimensions());
                // for (auto iter = final.begin(); iter != final.end(); iter++) {
                //     std::cout << *iter << std::endl;
                // }

                //std::raise(SIGABRT);
                
                auto layer_output = ctx->AssociateValueAndTensor(n->outputs()[0], resize_layer->getOutput(0));
                LOG_DEBUG("Output tensor shape: " << layer_output->getDimensions());

                // std::cout << "PRINTING STUFF AT THE END!" << std::endl;
                // auto final = util::toVec(resize_layer->getOutput(0)->getDimensions());
                // for (auto iter = final.begin(); iter != final.end(); iter++) {
                //     std::cout << *iter << std::endl;
                // }
            } else {
                LOG_DEBUG("scale factor parameter not supported yet.");
            }

            return true;
        }
    }).pattern({
        "aten::upsample_nearest2d(Tensor self, int[2] output_size, float? scales_h=None, float? scales_w=None) -> (Tensor)",
        [](ConversionCtx* ctx, const torch::jit::Node* n, args& args) -> bool {
            auto in = args[0].ITensor();
            auto in_shape = util::toVec(in->getDimensions());

            // Case 1: user uses output_size and not scales_h, scales_w
            if (!args[1].IValue()->isNone() && args[2].IValue()->isNone() && args[3].IValue()->isNone()){
                auto out_size = util::toVec(util::toDims(args[1].unwrapToIntList()));

                TRTORCH_ASSERT(out_size.size() == 2, "aten::upsample_nearest2d input Tensor and output size dimension mismatch");
                
                auto out_shape = in_shape;
                std::copy(out_size.begin(), out_size.end(), out_shape.begin() + (in_shape.size() - out_size.size()));
                
                auto resize_layer = ctx->net->addResize(*in);
                TRTORCH_CHECK(resize_layer, "Unable to create interpolation (resizing) layer from node" << *n);

                resize_layer->setOutputDimensions(util::toDims(out_shape));
                resize_layer->setResizeMode(nvinfer1::ResizeMode::kNEAREST);
                resize_layer->setName(util::node_info(n).c_str());

                auto layer_output = ctx->AssociateValueAndTensor(n->outputs()[0], resize_layer->getOutput(0));
                LOG_DEBUG("Output tensor shape: " << layer_output->getDimensions());
            } else {
                LOG_DEBUG("scale factor parameters not supported yet.");
            }

            return true;
        }
    }).pattern({
        "aten::upsample_nearest3d(Tensor self, int[3] output_size, float? scales_d=None, float? scales_h=None, float? scales_w=None) -> (Tensor)",
        [](ConversionCtx* ctx, const torch::jit::Node*n, args& args) -> bool {
            auto in = args[0].ITensor();
            auto in_shape = util::toVec(in->getDimensions());

            // Case 1: user uses output size and not scales_d, scales_h, scales_w
            if (!args[1].IValue()->isNone() && args[2].IValue()->isNone() && args[3].IValue()->isNone() && args[4].IValue()->isNone()) {
                auto out_size = util::toVec(util::toDims(args[1].unwrapToIntList()));

                TRTORCH_ASSERT(out_size.size() == 3, "aten::upsample_nearest3d input Tensor and output size dimension mismatch");
                
                auto out_shape = in_shape;
                std::copy(out_size.begin(), out_size.end(), out_shape.begin() + (in_shape.size() - out_size.size()));
                
                auto resize_layer = ctx->net->addResize(*in);
                TRTORCH_CHECK(resize_layer, "Unable to create interpolation (resizing) layer from node" << *n);

                resize_layer->setOutputDimensions(util::toDims(out_shape));
                resize_layer->setResizeMode(nvinfer1::ResizeMode::kNEAREST);
                resize_layer->setName(util::node_info(n).c_str());

                auto layer_output = ctx->AssociateValueAndTensor(n->outputs()[0], resize_layer->getOutput(0));
                LOG_DEBUG("Output tensor shape: " << layer_output->getDimensions());
            } else {
                LOG_DEBUG("scale factor parameters not supported yet.");
            }

            return true;
        }
    });


} // namespace
} // namespace impl
} // namespace converters
} // namespace conversion
} // namespace core
} // namespace trtorch
