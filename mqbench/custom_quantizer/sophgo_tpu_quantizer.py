import operator
import torch
from torch.fx import GraphModule
import torch.nn.intrinsic as nni
import mqbench.nn.intrinsic.qat as qnniqat
import mqbench.nn.intrinsic as qnni
from mqbench.utils.registry import register_model_quantizer
from mqbench.prepare_by_platform import BackendType
from mqbench.custom_quantizer import ModelQuantizer
import torch.nn as nn
import mqbench.nn.qat as qnnqat


@register_model_quantizer(BackendType.Sophgo_TPU)
class SophgoTpuQuantizer(ModelQuantizer):
    """There is only INT8 calculations in the model.
    We quantize the input tensors and output tensors of all layers,
    except those in _passed_func_type and _passed_module_type.
    For example add + relu pattern, there is no need to insert fake
    quantize node between them.
    """

    def __init__(self, extra_quantizer_dict, extra_fuse_dict):
        super().__init__(extra_quantizer_dict, extra_fuse_dict)
        self.additional_qat_module_mapping = {
            # Intrinsic modules:
            nni.ConvBn2d: qnniqat.ConvBn2d_sophgo,
            nni.ConvBnReLU2d: qnniqat.ConvBnReLU2d_sophgo,
            nn.Conv2d: qnnqat.Conv2d_sophgo,
            nni.ConvReLU2d: qnniqat.ConvReLU2d_sophgo,
            nni.LinearReLU: qnniqat.LinearReLU_sophgo,
            nn.Linear: qnniqat.Linear_sophgo,
            qnni.LinearBn1d: qnniqat.LinearBn1d_sophgo,
            qnni.ConvTransposeBnReLU2d:qnniqat.ConvTransposeBnReLU2d_sophgo,
            qnni.ConvTransposeReLU2d:qnniqat.ConvTransposeReLU2d_sophgo,
            qnni.ConvTransposeBn2d:qnniqat.ConvTransposeBn2d_sophgo,
        }

    @property
    def module_type_to_quant_input(self) -> tuple:
        return super().module_type_to_quant_input + self._layers_need_scale_form_input_fake_quantizer

    @property
    def function_type_to_quant_input(self) -> tuple:
        return super().function_type_to_quant_input + [
            torch.cat
        ]

    @property
    def _passed_func_type(self):
        return (
            torch.nn.functional.relu, 
            torch.nn.functional.relu6,
            torch.flatten
        )

    @property
    def _passed_module_type(self):
        return (
            torch.nn.ReLU,
            torch.nn.ReLU6
        )

    @property
    def _layers_need_scale_form_input_fake_quantizer(self):
        return (
            qnniqat.ConvBnReLU2d_sophgo, #todo:add transposeConv support
            qnniqat.ConvBn2d_sophgo, 
            qnniqat.ConvReLU2d_sophgo, 
            qnnqat.Conv2d_sophgo,
            qnniqat.LinearReLU_sophgo,
            qnniqat.Linear_sophgo,
        )

    def prepare(self, model: GraphModule, qconfig):
        model = super().prepare(model, qconfig)
        model = self._set_fake_quantizer_to_next_weight_layer(model)
        return model

    def _find_act_quants(self, model: GraphModule) -> list:
        nodes = list(model.graph.nodes)
        modules = dict(model.named_modules())
        node_need_to_quantize_output = super()._find_act_quants(model)
        for node in nodes:
            if (node.op == "call_module" and node.target in self.exclude_module_name) or \
                ((node.op == 'call_function' or node.op == 'call_method') and
                 node.target in self.exclude_function_type) or \
                    node.name in self.exclude_node_name:
                continue
            if (node.op == "call_module" and isinstance(modules[node.target], self.module_type_to_quant_input)) or \
                ((node.op == 'call_function' or node.op == 'call_method') and
                    node.target in self.function_type_to_quant_input):
                for next_node in node.users:
                    if not ((next_node.op == 'call_function' and next_node.target in self._passed_func_type) or
                            (next_node.op == 'call_module' and isinstance(modules[next_node.target], self._passed_module_type))):
                        node_need_to_quantize_output.append(node)
                    else:
                        node_need_to_quantize_output.append(next_node)
        return node_need_to_quantize_output

    def _set_fake_quantizer_to_next_weight_layer(self, model: GraphModule):
        nodes = list(model.graph.nodes)
        modules = dict(model.named_modules())
        for node in nodes:
            if node.target in modules and "_post_act_fake_quantizer" in node.target:
                fake_quantizer = getattr(model, node.target)
                for user in node.users:
                    if (user.op == "call_module" and isinstance(modules[user.target], self._layers_need_scale_form_input_fake_quantizer)):
                        setattr(modules[user.target], "input_fake_quantizer", fake_quantizer)
                        print('wlog:', user.target,'\'type is:', type(modules[user.target]), "add input_fake_quantizer")