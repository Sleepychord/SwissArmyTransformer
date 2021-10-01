# -*- encoding: utf-8 -*-
'''
@File    :   gpt2_modeling.py
@Time    :   2021/10/02 00:37:22
@Author  :   Ming Ding 
@Contact :   dm18@mail.tsinghua.edu.cn
'''

# here put the import lib
import os
import sys
import math
import random
import torch

from .base_model import BaseModel
from mpu.transformer import standard_attention, split_tensor_along_last_dim

class CachedAutoregressiveModel(BaseModel):
    def __init__(self, args, transformer=None):
        super().__init__(args, transformer=transformer)
        self.log_attention_weights = None
        
    def attention_forward(self, hidden_states, mask, *other_tensors, layer_id=None):
        attn_module = self.transformer.layers[layer_id].attention
        mem = other_tensors[layer_id] if len(other_tensors) > 0 else None
        
        mixed_raw_layer = attn_module.query_key_value(hidden_states)
        (mixed_query_layer,
            mixed_key_layer,
            mixed_value_layer) = split_tensor_along_last_dim(mixed_raw_layer, 3)
        
        if mem is not None: # the first time, mem is None
            memk, memv = split_tensor_along_last_dim(mem, 2)
            mixed_key_layer = torch.cat((memk, mixed_key_layer), dim=1)
            mixed_value_layer = torch.cat((memv, mixed_value_layer), dim=1)

        # same as training
        query_layer = self._transpose_for_scores(mixed_query_layer)
        key_layer = self._transpose_for_scores(mixed_key_layer)
        value_layer = self._transpose_for_scores(mixed_value_layer)
        context_layer = standard_attention(query_layer, key_layer, value_layer, mask, dropout_fn=None, log_attention_weights=self.log_attention_weights)
        context_layer = context_layer.permute(0, 2, 1, 3).contiguous()
        new_context_layer_shape = context_layer.size()[:-2] + (self.hidden_size_per_partition,)
        context_layer = context_layer.view(*new_context_layer_shape)
        output = self.dense(context_layer)
        
        # new mem this layer
        new_mem = mixed_raw_layer.detach()[..., -(mixed_raw_layer.shape[-1] // 3 * 2):].contiguous()
            
        return output, new_mem
