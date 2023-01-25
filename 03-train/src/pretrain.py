from transformers import DataCollatorForLanguageModeling
from transformers import TrainingArguments
from transformers import GPT2TokenizerFast
from transformers import GPT2LMHeadModel
from sagemaker.s3 import S3Downloader
from sagemaker.session import Session
from transformers import AutoConfig
from sagemaker.s3 import S3Uploader
from transformers import pipeline 
from datasets import load_dataset
from datasets import DatasetDict
from transformers import Trainer
from pathlib import Path
import pandas as pd
import transformers
import sagemaker
import datasets
import argparse
import logging
import random
import shutil
import torch
import boto3
import json
import time
import math
import sys
import os


# Setup logging
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.getLevelName('INFO'), 
                    handlers=[logging.StreamHandler(sys.stdout)], 
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# Log versions of dependencies
logger.info(f'[Using Transformers: {transformers.__version__}]')
logger.info(f'[Using SageMaker: {sagemaker.__version__}]')
logger.info(f'[Using Datasets: {datasets.__version__}]')
logger.info(f'[Using Torch: {torch.__version__}]')
logger.info(f'[Using Boto3: {boto3.__version__}]')
logger.info(f'[Using Pandas: {pd.__version__}]')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    logger.info('Parsing command line arguments')
    parser.add_argument('--input_dir', type=str, default=os.environ['SM_INPUT_DIR'])
    parser.add_argument('--model_dir', type=str, default=os.environ['SM_MODEL_DIR'])
    parser.add_argument('--train', type=str, default=os.environ['SM_CHANNEL_TRAIN'])
    parser.add_argument('--current_host', type=str, default=os.environ['SM_CURRENT_HOST'])
    parser.add_argument('--master_host', type=str, default=os.environ['SMDATAPARALLEL_SERVER_ADDR'])
    
    # [IMPORTANT] Hyperparameters sent by the client (Studio notebook with the driver code to launch training) 
    # are passed as command-line arguments to the training script
    parser.add_argument('--s3_bucket', type=str)
    parser.add_argument('--max_len', type=int)
    parser.add_argument('--num_train_epochs', type=int)
    parser.add_argument('--per_device_train_batch_size', type=int)
    parser.add_argument('--region', type=str)
    
    args, _ = parser.parse_known_args()
    current_host = args.current_host
    master_host = args.master_host
    
    logger.info(f'Current host = {current_host}')
    logger.info(f'Master host = {master_host}')
    
    S3_BUCKET = args.s3_bucket
    MAX_LENGTH = args.max_len
    TRAIN_EPOCHS = args.num_train_epochs
    BATCH_SIZE = args.per_device_train_batch_size
    REGION = args.region 
    SAVE_STEPS = 10000
    SAVE_TOTAL_LIMIT = 2
    
    LOCAL_DATA_DIR = '/tmp/cache/data/processed'
    LOCAL_MODEL_DIR = '/tmp/cache/model/custom'
    
    # Setup SageMaker Session for S3Downloader and S3Uploader 
    boto_session = boto3.session.Session(region_name=REGION)
    sm_session = sagemaker.Session(boto_session=boto_session)
    
    
    def download(s3_path: str, ebs_path: str, session: Session) -> None:
        try:
            if not os.path.exists(ebs_path):
                os.makedirs(ebs_path, exist_ok=True)
            S3Downloader.download(s3_path, ebs_path, sagemaker_session=session)
        except FileExistsError:  # to avoid race condition between GPUs
            logger.info('Ignoring FileExistsError to avoid I/O race conditions.')
        except FileNotFoundError:
            logger.info('Ignoring FileNotFoundError to avoid I/O race conditions.')
        
        
    def upload(ebs_path: str, s3_path: str, session: Session) -> None:
        S3Uploader.upload(ebs_path, s3_path, sagemaker_session=session)
        
    
    # Download saved custom vocabulary file from S3 to local input path of the training cluster
    logger.info(f'Downloading custom vocabulary from [{S3_BUCKET}/data/vocab/] to [{args.input_dir}/vocab/]')
    path = os.path.join(f'{args.input_dir}', 'vocab')
    download(f's3://{S3_BUCKET}/data/vocab/', path, sm_session)
         
    # Download preprocessed datasets from S3 to local EBS volume (cache dir)
    logger.info(f'Downloading preprocessed datasets from [{S3_BUCKET}/data/processed/] to [{LOCAL_DATA_DIR}/]')
    download(f's3://{S3_BUCKET}/data/processed/', f'{LOCAL_DATA_DIR}/', sm_session)
    
    # Re-create GPT2 BPE tokenizer 
    logger.info(f'Re-creating GPT tokenizer using custom vocabulary from [{args.input_dir}/vocab/]')
    tokenizer = GPT2TokenizerFast.from_pretrained(path, pad_token='<|endoftext|>')
    tokenizer.model_max_length = MAX_LENGTH
    tokenizer.pad_token = tokenizer.eos_token
    logger.info(f'Tokenizer: {tokenizer}')
    
    config = AutoConfig.from_pretrained('gpt2', 
                                        vocab_size=tokenizer.vocab_size, 
                                        n_ctx=MAX_LENGTH, 
                                        bos_token_id=tokenizer.bos_token_id, 
                                        eos_token_id=tokenizer.eos_token_id)
    # Load GPT2 model head
    logger.info('Loading GPT2 model')
    clm = GPT2LMHeadModel(config)
    model_size = sum(t.numel() for t in clm.parameters())
    logger.info(f'GPT2 size: {model_size/1000**2:.1f}M parameters')
    
    # Read dataset 
    tokenized_datasets = datasets.load_from_disk(LOCAL_DATA_DIR)
    logger.info(f'Tokenized datasets: {tokenized_datasets}')
    
    # Create the DataCollator
    data_collator = DataCollatorForLanguageModeling(tokenizer, mlm=False)
    
    # Train GPT2 CLM from scratch (further pre-training for domain adaptation)
    logger.info('Further pre-training GPT2 model for domain adaptation')
    args = TrainingArguments(output_dir='/tmp/checkpoints', 
                             overwrite_output_dir=True, 
                             optim='adamw_torch',
                             per_device_train_batch_size=BATCH_SIZE, 
                             evaluation_strategy='epoch', 
                             num_train_epochs=TRAIN_EPOCHS, 
                             weight_decay=0.1, 
                             warmup_steps=1_000, 
                             lr_scheduler_type='cosine', 
                             learning_rate=5e-4,
                             save_steps=SAVE_STEPS, 
                             save_total_limit=SAVE_TOTAL_LIMIT)
    
    trainer = Trainer(model=clm, 
                      tokenizer=tokenizer, 
                      args=args, 
                      data_collator=data_collator, 
                      train_dataset=tokenized_datasets['train'], 
                      eval_dataset=tokenized_datasets['validation'])
    
    # Evaluate trained model for perplexity
    eval_results = trainer.evaluate()
    logger.info(f"Perplexity before training: {math.exp(eval_results['eval_loss']):.2f}")
    
    trainer.train()
    
    eval_results = trainer.evaluate()
    logger.info(f"Perplexity after training: {math.exp(eval_results['eval_loss']):.2f}")
    
    
    if current_host == master_host:
        
        if not os.path.exists(LOCAL_MODEL_DIR):
            os.makedirs(LOCAL_MODEL_DIR, exist_ok=True)
            
        # Save trained model to local model directory
        logger.info(f'Saving trained CLM to [/tmp/cache/model/custom/]')
        trainer.save_model(LOCAL_MODEL_DIR)
        
        if os.path.exists(f'{LOCAL_MODEL_DIR}/pytorch_model.bin') and os.path.exists(f'{LOCAL_MODEL_DIR}/config.json'):
            # Copy trained model from local directory of the training cluster to S3 
            logger.info(f'Copying saved model from local to [s3://{S3_BUCKET}/model/custom/]')
            upload(f'{LOCAL_MODEL_DIR}/', f's3://{S3_BUCKET}/model/custom/', sm_session)

            # Copy vocab.txt to local model directory - this is needed to re-create the trained MLM
            logger.info('Copying custom vocabulary to local model artifacts location to faciliate model evaluation')
            shutil.copyfile(f'{path}/vocab.txt', f'{LOCAL_MODEL_DIR}/vocab.txt')

            # Copy vocab.txt to saved model artifacts location in S3
            logger.info(f'Copying custom vocabulary from [{path}/vocab.txt] to [s3://{S3_BUCKET}/model/custom/] for future stages of ML pipeline')
            upload(f'{path}/', f's3://{S3_BUCKET}/model/custom/', sm_session)
