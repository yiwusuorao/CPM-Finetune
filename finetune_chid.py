# coding=utf-8
# Copyright (c) 2019, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Sample Generate GPT2"""

import os
import random
import numpy as np
import torch
import torch.nn.functional as F
import argparse
import time
import json
from arguments import get_args
from utils import Timers
from pretrain_gpt2 import initialize_distributed
from pretrain_gpt2 import set_random_seed
from pretrain_gpt2 import get_train_val_test_data
from pretrain_gpt2 import get_masks_and_position_ids
from utils import load_checkpoint
# from data_utils import make_tokenizer
from data_utils.tokenization_gpt2 import GPT2Tokenizer
from configure_data import configure_data
import mpu
import deepspeed

from tqdm import tqdm
from fp16 import FP16_Module
from model import GPT2Model
from model import DistributedDataParallel as DDP
from utils import print_rank_0
from data.samplers import DistributedBatchSampler
# from sklearn.metrics import accuracy_score

from torch.utils.data import TensorDataset

from pretrain_gpt2 import *

class CHIDDataset(torch.utils.data.Dataset):
    def __init__(self, args, data_path, split, tokenizer, ratio=1):
        self.split = split
        self.tokenizer = tokenizer
        self.ratio = ratio
        self.args = args
        self.world_size = args.world_size

        with open(data_path, "r") as f:
            data = json.load(f)
            # train: {"contents": ["谈到巴萨目前的成就", ...], "sids": [0, 1, 2, 3, ...], "labels": []}
            # dev: {"contents": ["中国青年报：篮协改革切莫因噎废食", ...], "sids": [0, 0, ..., 1, 1, ...], "labels": [5, 1, 4, 3, ...]}

        self.pad_id = tokenizer.encoder['<pad>']
        self.eod_token = tokenizer.encoder['<eod>']
        args.eod_token = tokenizer.encoder['<eod>']
    
        self.seq, self.sizes, self.truth_labels = self.process(data)

        self.max_size = max(self.sizes)

    def process(self, data):
        contents = data["contents"]
        sids = data["sids"]
        truth_labels = data["labels"]
        cids = data["cids"]
        sizes = []
        seq = []
        for content, sid, cid in zip(tqdm(contents[:int(self.ratio * len(contents))], desc="Processing"), sids, cids):
            input_ids = self.tokenizer.encode(content)
            input_ids = input_ids + [self.eod_token]
            length = len(input_ids) - 1
            sizes.append(length)
            seq.append({
                "sid": sid,
                "cid": cid,
                "input_ids": input_ids[:-1],
                "loss_mask": [1.0] * length,
                "labels": input_ids[1:]
            })

        print(max(sizes))
        print(sum([int(x > 256) for x in sizes]))
        print(sum([int(x > 384) for x in sizes]))
        print(sum([int(x > 512) for x in sizes]))

        return seq, sizes, truth_labels

    def __len__(self):
        return len(self.sizes)

    def __getitem__(self, idx):
        return self.seq[idx], self.sizes[idx]

    def collate(self, samples):
        bs = len(samples)
        seq = [s[0] for s in samples]
        sizes = [s[1] for s in samples]
        # max_size = max(sizes)
        max_size = self.max_size

        attn_mask, pos_ids = build_attn_mask_pos_ids(self.args, bs, max_size)

        batch_seq = {
            "input_ids": torch.ones(bs, max_size).long() * self.pad_id,
            "attention_mask": attn_mask,
            "position_ids":pos_ids
        }

        no_model_seq = {
            "sids": torch.zeros(bs).long(),
            "cids": torch.zeros(bs).long(),
            "loss_mask": torch.zeros(bs, max_size).float(),
            "labels": torch.ones(bs, max_size).long() * self.pad_id,
        }

        for i, samp in enumerate(seq):
            batch_seq["input_ids"][i, :len(samp["input_ids"])] = torch.tensor(samp["input_ids"])
            no_model_seq["loss_mask"][i, :len(samp["loss_mask"])] = torch.tensor(samp["loss_mask"])
            no_model_seq["labels"][i, :len(samp["labels"])] = torch.tensor(samp["labels"])
            no_model_seq["sids"][i] = torch.tensor(samp["sid"])
            no_model_seq["cids"][i] = torch.tensor(samp["cid"])

        return batch_seq, no_model_seq

def build_attn_mask_pos_ids(args, batch_size, max_size):
    attn_mask = torch.tril(torch.ones((max_size, max_size))).unsqueeze(0)

    position_ids = torch.arange(max_size, dtype=torch.long).unsqueeze(0).repeat(batch_size, 1)

    if args.fp16:
        attn_mask = attn_mask.half()

    return attn_mask, position_ids

def load_data(data_path, data_type, tokenizer, ratio=1):
    args = get_args()
    batch_size = args.batch_size
    args.batch_size = 1


    # Data parallel arguments.
    world_size = mpu.get_data_parallel_world_size()
    rank = mpu.get_data_parallel_rank()
    args.batch_size = batch_size
    global_batch_size = args.batch_size * world_size
    num_workers = args.num_workers

    # Dataset
    filename = os.path.join(data_path, data_type+'.json')
    dataset = CHIDDataset(args, filename, data_type, tokenizer, ratio=ratio)
    
    # Use a random sampler with distributed batch sampler.
    if data_type == 'train':
        sampler = torch.utils.data.RandomSampler(dataset)
    else:
        sampler = torch.utils.data.SequentialSampler(dataset)
    batch_sampler = DistributedBatchSampler(sampler=sampler,
                                            batch_size=global_batch_size,
                                            drop_last=True,
                                            rank=rank,
                                            world_size=world_size)
    
    # Torch dataloader.
    return torch.utils.data.DataLoader(dataset,
                                       batch_sampler=batch_sampler,
                                       num_workers=num_workers,
                                       pin_memory=True,
                                       collate_fn=dataset.collate), dataset

def main():
    """Main training program."""

    print('Generate Samples')

    # Disable CuDNN.
    torch.backends.cudnn.enabled = False

    # Timer.
    timers = Timers()

    # Arguments.
    args = get_args()

    # Pytorch distributed.
    initialize_distributed(args)

    # Random seeds for reproducability.
    set_random_seed(args.seed)

    # get the tokenizer
    tokenizer = GPT2Tokenizer(os.path.join(args.tokenizer_path, 'vocab.json'), os.path.join(args.tokenizer_path, 'merges.txt'), os.path.join(args.tokenizer_path, 'chinese_vocab.model'))

    # load data
    train_dataloader, _ = load_data('/data/gyx/chid/preprocessed', 'train', tokenizer, 1)
    dev_dataloader, dev_dataset = load_data('/data/gyx/chid/preprocessed', 'dev', tokenizer, 1)

    args.train_iters = len(train_dataloader)

    # Model, optimizer, and learning rate.
    # TODO: maybe need to reinitialize optimizer
    model, optimizer, lr_scheduler = setup_model_and_optimizer(args)

    epoch = 3
    device = torch.cuda.current_device()
    for e in range(epoch):
        model.train()
        for batch, no_model_batch in train_dataloader:
            for k in batch:
                batch[k] = batch[k].to(device)
            for k in no_model_batch:
                no_model_batch[k] = no_model_batch[k].to(device)

            output = model(**batch)
            losses = mpu.vocab_parallel_cross_entropy(output.contiguous().float(), no_model_batch["labels"])
            loss_mask = no_model_batch["loss_mask"].view(-1)
            loss = torch.sum(losses.view(-1) * loss_mask) / loss_mask.sum()
            
            model.backward(loss)
            model.step()

            torch.distributed.all_reduce(loss.data)
            loss.data = loss.data / args.world_size

            if torch.distributed.get_rank() == 0:
                print("train lm loss: {}".format(loss.item()))

        model.eval()
        all_sids = []
        all_cids = []
        all_losses = []
        with torch.no_grad():
            for batch, no_model_batch in dev_dataloader:
                for k in batch:
                    batch[k] = batch[k].to(device)
                for k in no_model_batch:
                    no_model_batch[k] = no_model_seq[k].to(device)
                
                output = model(**batch)
                losses = mpu.vocab_parallel_cross_entropy(output.contiguous().float(), no_model_batch["labels"])
                loss_mask = no_model_batch["loss_mask"]
                loss = torch.sum(losses * loss_mask, dim=-1) / loss_mask.sum(dim=-1)

                loss_tensor_list = [torch.zeros_like(loss).to(device) for _ in range(args.world_size)]
                torch.distributed.all_gather(loss_tensor_list, loss.data)
                all_losses.extend(loss_tensor_list)

                sids = no_model_batch["sids"]
                sid_tensor_list = [torch.zeros_like(sids) for _ in range(args.world_size)]
                torch.distributed.all_gather(sid_tensor_list, sids.data)
                all_sids.extend(sid_tensor_list)

                cids = no_model_batch["cids"]
                cid_tensor_list = [torch.zeros_like(cids) for _ in range(args.world_size)]
                torch.distributed.all_gather(cid_tensor_list, cids.data)
                all_cids.extend(cid_tensor_list)

        if torch.distributed.get_rank() == 0:
            all_losses = torch.stack(all_losses).cpu().detach().numpy()
            all_sids = torch.stack(all_sids).cpu().detach().numpy()
            all_cids = torch.stack(all_cids).cpu().detach().numpy()

            truth_labels = dev_dataset.truth_labels
            preds = [[] for _ in truth_labels]

            for sid, cid, loss in zip(all_sids, all_cids, all_losses):
                preds[sid].append((cid, loss))

            preds = [max(p, key=lambda x: x[1])[0] for p in preds]

            print(sum([int(p == l) for p, l in zip(preds, truth_labels)]) / len(truth_labels))

if __name__ == "__main__":
    # args = get_args()

    # # Pytorch distributed.
    # initialize_distributed(args)

    # # Random seeds for reproducability.
    # set_random_seed(args.seed)

    # # get the tokenizer
    # tokenizer = GPT2Tokenizer(os.path.join(args.tokenizer_path, 'vocab.json'), os.path.join(args.tokenizer_path, 'merges.txt'), os.path.join(args.tokenizer_path, 'chinese_vocab.model'))

    # train_dataloader = load_data('/data/gyx/chid/preprocessed', 'train', tokenizer, ratio=0.01)

    # for batch in train_dataloader:
    #     print(batch)
    #     exit(0)

    main()