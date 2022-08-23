import os
import os
import pickle

import fire
import redis
import torch
import torch.distributed as dist
from torch.utils.data import DataLoader
from tqdm.auto import tqdm
from transformers import (
    Seq2SeqTrainer,
    Seq2SeqTrainingArguments,
    T5ForConditionalGeneration,
    PreTrainedTokenizerFast,
    DataCollatorForSeq2Seq,
    TrainerCallback,
    TrainerState,
    TrainerControl,
)
from sklearn.model_selection import KFold

from .args import get_config
from .collators import DistributedSamplerForEval
from .processors import KLUE_PROCESSORS, Text2TextDataset, VincaProcessor


class Metrics(TrainerCallback):
    def __init__(self, processor, r: redis.Redis, test_data, data_collator, max_length=512):
        self.metrics = []
        self.processor = processor
        self.test_data = test_data
        self.data_collator = data_collator
        self.max_length = max_length
        self.r = r

    @torch.no_grad()
    def on_evaluate(self, args: Seq2SeqTrainingArguments, state: TrainerState, control: TrainerControl, model: T5ForConditionalGeneration, **kwargs):
        model.eval()
        all_scores, all_logits = [], []
        eval_dataloader = DataLoader(
            self.test_data,
            batch_size=args.per_device_eval_batch_size,
            sampler=DistributedSamplerForEval(self.test_data),
            collate_fn=self.data_collator
        )
        print("Start evaluation")
        for data in tqdm(eval_dataloader, desc="Eval.."):
            output = model.generate(
                input_ids=data['input_ids'].cuda(),
                attention_mask=data['attention_mask'].cuda(),
                num_beams=args.generation_num_beams,
                max_length=args.generation_max_length,
                early_stopping=True,
                output_scores=True,
                return_dict_in_generate=True,
            )
            logits = output.sequences
            if self.processor.task == 'mrc' or self.processor.task == 'vinca':
                all_scores += output.sequences_scores.tolist()
            assert logits.shape[0] == data['input_ids'].shape[0]
            logits = logits.detach().cpu().tolist()
            all_logits += logits

        self.r.set(f"{dist.get_rank()}", pickle.dumps([all_logits, all_scores]))
        dist.barrier()
        all_scores, all_logits = [], []
        for i in range(dist.get_world_size()):
            logits, scores = pickle.loads(self.r.get(f"{i}"))
            all_logits += logits
            all_scores += scores
        if len(all_scores) == 0:
            all_scores = None
        self.metrics.append(self.processor.compute_metrics(all_logits, self.test_data.entries, output_scores=all_scores))
        print(self.metrics[-1])
        model.train()


def train(model="./models/t5-kr-small-bbpe", task='ynat', max_length=1300):
    print(f"Start training \'{task}\' task")
    os.environ["CUDA_VISIBLE_DEVICES"] = "0"
    model_name_or_path = model
    output_dir = './models/klue_t5'
    local_rank = int(os.getenv("LOCAL_RANK", "-1"))

    r = redis.Redis(
        host='localhost',
        port=6379)

    tokenizer = PreTrainedTokenizerFast.from_pretrained(model_name_or_path)

    if task == "vinca":
        processor = VincaProcessor(tokenizer)
    else:
        processor = KLUE_PROCESSORS[task](tokenizer)

    # TODO: 학습 진행 후 test set score 저장
    kf = KFold(n_splits=10)
    i = 0
    processed_data = processor.process('train')
    for train_index, dev_index in kf.split(processed_data):
        args = Seq2SeqTrainingArguments(
            output_dir=f'{output_dir}/{i}',
            local_rank=local_rank,
            **get_config(task)
        )

        model = T5ForConditionalGeneration.from_pretrained(model_name_or_path)

        train_data = Text2TextDataset(processed_data[train_index], max_length=max_length)
        dev_data = Text2TextDataset(processed_data[dev_index], max_length=max_length)
        data_collator = DataCollatorForSeq2Seq(tokenizer, model, padding=True)

        metrics = Metrics(
            processor,
            r=r,
            test_data=dev_data,
            data_collator=data_collator,
            max_length=max_length
        )
        trainer = Seq2SeqTrainer(
            model=model,
            args=args,
            data_collator=data_collator,
            train_dataset=train_data,
            eval_dataset=dev_data,
            tokenizer=tokenizer,
            callbacks=[metrics],
        )

        trainer.train()
        trainer.save_model(output_dir=f'{output_dir}/{i}')

        best_metric = None
        for metric in metrics.metrics:
            if best_metric is None:
                best_metric = metric
            elif any(metric[k] >= best_metric[k] for k in best_metric.keys()):
                best_metric = metric

        if local_rank == 0:
            with open(f'{output_dir}/{i}/result_{task}.txt', 'wt') as f:
                for k, v in best_metric.items():
                    f.write(f"{k}={v}\n")
        i += 1


if __name__ == '__main__':
    fire.Fire(train)
