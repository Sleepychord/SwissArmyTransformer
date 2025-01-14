import os
import torch
import argparse
from SwissArmyTransformer import get_args

args = get_args()

from chat_model import ChatModel

model = ChatModel(args)
from transformers import AutoTokenizer
tokenizer = AutoTokenizer.from_pretrained("THUDM/chatglm-6b", trust_remote_code=True)
model = model.eval()
response, history = model.chat(tokenizer, "你好", history=[])
print(response)
response, history = model.chat(tokenizer, "晚上睡不着应该怎么办", history=history)
print(response)

breakpoint()