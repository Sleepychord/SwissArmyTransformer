import math
import copy
import os
import warnings
import re
import sys
from transformers.utils import logging
logger = logging.get_logger(__name__)

import torch
import torch.nn as nn
from transformers import GenerationMixin
from SwissArmyTransformer import AutoModel
from typing import Optional, Tuple, Union, List, Callable, Dict, Any
from transformers.generation.utils import LogitsProcessorList, StoppingCriteriaList, GenerationConfig, ModelOutput
from transformers.modeling_outputs import (
    CausalLMOutputWithPast,
)
from transformers.generation.logits_process import LogitsProcessor
class InvalidScoreLogitsProcessor(LogitsProcessor):
    def __call__(self, input_ids: torch.LongTensor, scores: torch.FloatTensor) -> torch.FloatTensor:
        if torch.isnan(scores).any() or torch.isinf(scores).any():
            scores.zero_()
            scores[..., 20005] = 5e4
        return scores

from transformers import AutoConfig

class ChatModel(nn.Module, GenerationMixin):
    def __init__(self, args, model=None):
        super().__init__()
        self.position_encoding_2d = True
        self.config = AutoConfig.from_pretrained('THUDM/chatglm-6b', trust_remote_code=True)
        self.generation_config = GenerationConfig.from_model_config(self.config)
        if model is None:
            self.model, args = AutoModel.from_pretrained(args, "chatglm-6b")
        else:
            self.model = model
        self.device = self.model.parameters().__next__().device
        self.main_input_name = 'input_ids'
    
    @classmethod
    def from_pretrained(cls, args, name, base_cls=None, *, home_path=None, url=None, prefix='', **kwargs):
        if base_cls is None:
            model, args = AutoModel.from_pretrained(args, name, home_path=home_path, url=url, prefix=prefix, **kwargs)
        else:
            model, args = base_cls.from_pretrained(args, name, home_path=home_path, url=url, prefix=prefix, **kwargs)
        return cls(args, model), args
    
    def can_generate(self):
        return True

    def _update_model_kwargs_for_generation(
        self,
        outputs: ModelOutput,
        model_kwargs: Dict[str, Any],
        is_encoder_decoder: bool = False,
        standardize_cache_format: bool = False,
    ) -> Dict[str, Any]:
        # update past_key_values
        model_kwargs["past_key_values"] = self._extract_past_from_model_output(
            outputs, standardize_cache_format=standardize_cache_format
        )

        # update attention mask
        if "attention_mask" in model_kwargs:
            attention_mask = model_kwargs["attention_mask"]
            if attention_mask is not None and attention_mask.dtype == torch.bool:
                attention_mask = torch.cat(
                    [attention_mask, attention_mask.new_ones((*attention_mask.shape[:3], 1))], dim=3)
                new_attention_mask = attention_mask[:, :, -1:].clone()
                new_attention_mask[..., -1] = False
                model_kwargs["attention_mask"] = torch.cat(
                    [attention_mask, new_attention_mask], dim=2
                )

        # update position ids
        if "position_ids" in model_kwargs:
            position_ids = model_kwargs["position_ids"]
            new_position_id = position_ids[..., -1:].clone()
            new_position_id[:, 1, :] += 1
            model_kwargs["position_ids"] = torch.cat(
                [position_ids, new_position_id], dim=-1
            )

        return model_kwargs

    def prepare_inputs_for_generation(
            self,
            input_ids: torch.LongTensor,
            past: Optional[torch.Tensor] = None,
            past_key_values: Optional[torch.Tensor] = None,
            attention_mask: Optional[torch.Tensor] = None,
            **kwargs
    ) -> dict:
        if past is None:
            past = past_key_values
        return {
            "input_ids": input_ids,
            "past_key_values": past
        }

    def forward(
            self,
            input_ids: Optional[torch.Tensor] = None,
            position_ids: Optional[torch.Tensor] = None,
            attention_mask: Optional[torch.Tensor] = None,
            past_key_values: Optional[Tuple[torch.FloatTensor]] = None,
            **kw_args
    ):
        outputs = self.model(
            input_ids=input_ids,
            position_ids=position_ids,
            attention_mask=attention_mask,
            past_key_values=past_key_values,
        )

        lm_logits = outputs[0]
        past_key_values = [x['past_key_values'] for x in outputs[1:]]

        return CausalLMOutputWithPast(
            logits=lm_logits,
            past_key_values=past_key_values
        )

    @staticmethod
    def _reorder_cache(
            past: Tuple[Tuple[torch.Tensor, torch.Tensor], ...], beam_idx: torch.LongTensor
    ) -> Tuple[Tuple[torch.Tensor, torch.Tensor], ...]:
        """
        This function is used to re-order the `past_key_values` cache if [`~PreTrainedModel.beam_search`] or
        [`~PreTrainedModel.beam_sample`] is called. This is required to match `past_key_values` with the correct
        beam_idx at every generation step.
        Output shares the same memory storage as `past`.
        """
        return tuple(
            (
                layer_past[0].index_select(1, beam_idx.to(layer_past[0].device)),
                layer_past[1].index_select(1, beam_idx.to(layer_past[1].device)),
            )
            for layer_past in past
        )

    def process_response(self, response):
        response = response.strip()
        response = response.replace("[[训练时间]]", "2023年")
        punkts = [
            [",", "，"],
            ["!", "！"],
            [":", "："],
            [";", "；"],
            ["\?", "？"],
        ]
        for item in punkts:
            response = re.sub(r"([\u4e00-\u9fff])%s" % item[0], r"\1%s" % item[1], response)
            response = re.sub(r"%s([\u4e00-\u9fff])" % item[0], r"%s\1" % item[1], response)
        return response

    @torch.no_grad()
    def chat(self, tokenizer, query: str, history: List[Tuple[str, str]] = None, max_length: int = 2048, num_beams=1,
             do_sample=True, top_p=0.7, temperature=0.95, logits_processor=None, **kwargs):
        if history is None:
            history = []
        if logits_processor is None:
            logits_processor = LogitsProcessorList()
        logits_processor.append(InvalidScoreLogitsProcessor())
        gen_kwargs = {"max_length": max_length, "num_beams": num_beams, "do_sample": do_sample, "top_p": top_p,
                      "temperature": temperature, "logits_processor": logits_processor, **kwargs}
        if not history:
            prompt = query
        else:
            prompt = ""
            for i, (old_query, response) in enumerate(history):
                prompt += "[Round {}]\n问：{}\n答：{}\n".format(i, old_query, response)
            prompt += "[Round {}]\n问：{}\n答：".format(len(history), query)
        input_ids = tokenizer([prompt], return_tensors="pt")
        input_ids = input_ids.to(self.device)
        outputs = self.generate(**input_ids, **gen_kwargs)
        outputs = outputs.tolist()[0][len(input_ids["input_ids"][0]):]
        response = tokenizer.decode(outputs)
        response = self.process_response(response)
        history = history + [(query, response)]
        return response, history
