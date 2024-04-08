import fnmatch
import math
import os
import time
import shutil
import gc
import numpy as np
import argparse
import itertools
import zipfile
import torch
import torch.utils.checkpoint
import matplotlib.pyplot as plt
from peft import LoraConfig, get_peft_model
from diffusers.optimization import get_scheduler
from tqdm import tqdm

from trainer.dataset_and_utils import (
    PreprocessedDataset, 
    plot_torch_hist, 
    plot_grad_norms,
    plot_curve,
    plot_loss, 
    plot_token_stds,
    plot_lrs,
    pick_best_gpu_id,
    zipdir
)
from trainer.utils.seed import seed_everything
from trainer.utils.lora import (
    save_lora,
    TokenEmbeddingsHandler
)

from trainer.utils.dtype import dtype_map
from trainer.config import TrainingConfig
from trainer.models import print_trainable_parameters, load_models
from trainer.loss import *
from trainer.utils.snr import compute_snr
from trainer.utils.training_info import get_avg_lr
from trainer.utils.inference import render_images, get_conditioning_signals
from trainer.utils.config_modification import post_process_args
from preprocess import preprocess

from typing import Union, Iterable, List, Dict, Tuple, Optional, cast
from torch import Tensor, inf

def main(
    config: TrainingConfig,
):  
    config = post_process_args(config)
    seed_everything(config.seed)
    gpu_id = pick_best_gpu_id()
    config.device = f'cuda:{gpu_id}'

    input_dir, n_imgs, trigger_text, segmentation_prompt, captions = preprocess(
        config,
        working_directory=config.output_dir,
        concept_mode=config.concept_mode,
        input_zip_path=config.lora_training_urls,
        caption_text=config.caption_prefix,
        mask_target_prompts=config.mask_target_prompts,
        target_size=config.resolution,
        crop_based_on_salience=config.crop_based_on_salience,
        use_face_detection_instead=config.use_face_detection_instead,
        temp=config.clipseg_temperature,
        left_right_flip_augmentation=config.left_right_flip_augmentation,
        augment_imgs_up_to_n = config.augment_imgs_up_to_n,
        caption_model = config.caption_model,
        seed = config.seed,
    )

    # Update the training attributes with some info from the pre-processing:
    config.training_attributes["n_training_imgs"] = n_imgs
    config.training_attributes["trigger_text"] = trigger_text
    config.training_attributes["segmentation_prompt"] = segmentation_prompt
    config.training_attributes["captions"] = captions

    if config.allow_tf32:
        torch.backends.cuda.matmul.allow_tf32 = True

    print("Using seed", config.seed)
    torch.manual_seed(config.seed)
    weight_dtype = dtype_map[config.weight_type]

    (   
        pipe,
        tokenizer_one,
        tokenizer_two,
        noise_scheduler,
        text_encoder_one,
        text_encoder_two,
        vae,
        unet,
    ) = load_models(config.pretrained_model, config.device, weight_dtype, keep_vae_float32=0)

    # Initialize new tokens for training.
    embedding_handler = TokenEmbeddingsHandler(
        text_encoders = [text_encoder_one, text_encoder_two], 
        tokenizers = [tokenizer_one, tokenizer_two]
    )

    '''
    initialize 2 new tokens in the embeddings with random initialization
    '''
    embedding_handler.initialize_new_tokens(
        inserting_toks=config.inserting_list_tokens, 
        #starting_toks = ["style", "object"],
        starting_toks=None, 
        seed=config.seed
    )

    unet.requires_grad_(False)
    vae.requires_grad_(False)
    text_encoders = embedding_handler.text_encoders
    text_encoder_parameters = []
    for text_encoder in text_encoders:
        if text_encoder is not  None:
            text_encoder.train()
            text_encoder.requires_grad_(False)
            for name, param in text_encoder.named_parameters():
                if "token_embedding" in name:
                    param.requires_grad = True
                    text_encoder_parameters.append(param)
                    print(f"Added {name} with shape {param.shape} to the trainable parameters")
                else:
                    param.requires_grad = False

    # Optimizer creation
    ti_prod_opt = False

    params_to_optimize_ti = [
        {
            "params": text_encoder_parameters,
            "lr": config.ti_lr if (not ti_prod_opt) else 1.0,
            "weight_decay": config.ti_weight_decay,
        },
    ]

    if ti_prod_opt:
        import prodigyopt
        optimizer_ti = prodigyopt.Prodigy(
                            params_to_optimize_ti,
                            d_coef = 1.0,
                            lr=1.0,
                            decouple=True,
                            use_bias_correction=True,
                            safeguard_warmup=True,
                            weight_decay=config.ti_weight_decay,
                            betas=(0.9, 0.99),
                            #growth_rate=5.0,  # this slows down the lr_rampup
                        )
    else:
        optimizer_ti = torch.optim.AdamW(
            params_to_optimize_ti,
            weight_decay=config.ti_weight_decay,
        )
        

    unet_param_to_optimize = []
    unet_lora_params_to_optimize = []

    if not config.is_lora:
        WHITELIST_PATTERNS = [
            # "*.attn*.weight",
            # "*ff*.weight",
            "*"
        ]
        BLACKLIST_PATTERNS = ["*.norm*.weight", "*time*"]
        for name, param in unet.named_parameters():
            if any(
                fnmatch.fnmatch(name, pattern) for pattern in WHITELIST_PATTERNS
            ) and not any(
                fnmatch.fnmatch(name, pattern) for pattern in BLACKLIST_PATTERNS
            ):
                param.requires_grad_(True)
                unet_param_to_optimize.append(name)
                print(f"Training: {name}")
            else:
                param.requires_grad_(False)

    else:
        # Do lora-training instead.
        # https://huggingface.co/docs/peft/main/en/developer_guides/lora#rank-stabilized-lora
        unet_lora_config = LoraConfig(
            r=config.lora_rank,
            lora_alpha=config.lora_rank * config.lora_alpha_multiplier,
            init_lora_weights="gaussian",
            target_modules=["to_k", "to_q", "to_v", "to_out.0"],
            #use_rslora=True,
            use_dora=config.use_dora,
        )
        #unet.add_adapter(unet_lora_config)
        unet = get_peft_model(unet, unet_lora_config)
        pipe.unet = unet
        print_trainable_parameters(unet, name = 'unet')
        unet_lora_parameters = list(filter(lambda p: p.requires_grad, unet.parameters()))

        unet_lora_params_to_optimize = [
            {
                "params": unet_lora_parameters,
                "weight_decay": config.lora_weight_decay if not config.use_dora else 0.0,
            },
        ]

    optimizer_type = "prodigy" # hardcode for now

    if optimizer_type != "prodigy":
        optimizer_lora_unet = torch.optim.AdamW(unet_lora_params_to_optimize, lr = 1e-4)
    else:
        import prodigyopt
        # Note: the specific settings of Prodigy seem to matter A LOT
        optimizer_lora_unet = prodigyopt.Prodigy(
                        unet_lora_params_to_optimize,
                        d_coef = config.prodigy_d_coef,
                        lr=1.0,
                        decouple=True,
                        use_bias_correction=True,
                        safeguard_warmup=True,
                        weight_decay=config.lora_weight_decay if not config.use_dora else 0.0,
                        betas=(0.9, 0.99),
                        #growth_rate=1.025,  # this slows down the lr_rampup
                        growth_rate=1.04,  # this slows down the lr_rampup
                    )
        
    train_dataset = PreprocessedDataset(
        os.path.join(input_dir, "captions.csv"),
        pipe,
        tokenizer_one,
        tokenizer_two,
        vae.float(),
        size = config.resolution,
        do_cache=config.do_cache,
        substitute_caption_map=config.token_dict,
        aspect_ratio_bucketing=config.aspect_ratio_bucketing,
        train_batch_size=config.train_batch_size
    )
    # offload the vae to cpu:
    vae = vae.to('cpu')
    gc.collect()
    torch.cuda.empty_cache()

    print(f"# PTI : Loaded dataset, do_cache: {config.do_cache}")
    train_dataloader = torch.utils.data.DataLoader(
        train_dataset,
        batch_size=config.train_batch_size,
        shuffle=True,
        num_workers=config.dataloader_num_workers,
    )

    num_update_steps_per_epoch = math.ceil(
        len(train_dataloader) / config.gradient_accumulation_steps
    )
    if config.max_train_steps is None:
        config.max_train_steps = config.num_train_epochs * num_update_steps_per_epoch

    num_update_steps_per_epoch = math.ceil(
        len(train_dataloader) / config.gradient_accumulation_steps
    )
    config.num_train_epochs = math.ceil(config.max_train_steps / num_update_steps_per_epoch)
    total_batch_size = config.train_batch_size * config.gradient_accumulation_steps

    if config.verbose:
        print(f"--- Num samples = {len(train_dataset)}")
        print(f"--- Num batches each epoch = {len(train_dataloader)}")
        print(f"--- Num Epochs = {config.num_train_epochs}")
        print(f"--- Instantaneous batch size per device = {config.train_batch_size}")
        print(f"--- Total batch_size (distributed + accumulation) = {total_batch_size}")
        print(f"--- Gradient Accumulation steps = {config.gradient_accumulation_steps}")
        print(f"--- Total optimization steps = {config.max_train_steps}\n")

    global_step = 0
    last_save_step = 0

    progress_bar = tqdm(range(global_step, config.max_train_steps), position=0, leave=True)
    checkpoint_dir = os.path.join(str(config.output_dir), "checkpoints")
    if os.path.exists(checkpoint_dir):
        shutil.rmtree(checkpoint_dir)
    os.makedirs(f"{checkpoint_dir}")

    # Experimental TODO: warmup the token embeddings using CLIP-similarity optimization
    #embedding_handler.pre_optimize_token_embeddings(train_dataset)
    if config.debug:
        embedding_handler.visualize_random_token_embeddings(config.output_dir,
            token_list = ['face', 'man', 'butterfly', 'chess', 'fly', 'the', ' ', '.'])

    # Data tracking inits:
    start_time, images_done = time.time(), 0
    ti_lrs, lora_lrs, prompt_embeds_norms = [], [], {'main':[], 'reg':[]}
    losses = {'img_loss': [], 'tot_loss': []}
    grad_norms, token_stds = {'unet': []}, {}
    for i in range(len(text_encoders)):
        grad_norms[f'text_encoder_{i}'] = []
        token_stds[f'text_encoder_{i}'] = {j: [] for j in range(config.n_tokens)}

    #######################################################################################################

    for epoch in range(config.num_train_epochs):
        if config.aspect_ratio_bucketing:
            train_dataset.bucket_manager.start_epoch()
        progress_bar.set_description(f"# PTI :step: {global_step}, epoch: {epoch}")

        for step, batch in enumerate(train_dataloader):
            progress_bar.update(1)
            if config.hard_pivot:
                if epoch >= config.num_train_epochs // 2:
                    if optimizer_ti is not None:
                        # remove text encoder parameters from the optimizer
                        optimizer_ti.param_groups = None
                        # remove the optimizer state corresponding to text_encoder_parameters
                        for param in text_encoder_parameters:
                            if param in optimizer_ti.state:
                                del optimizer_ti.state[param]
                        optimizer_ti = None

            elif not ti_prod_opt: # Update learning rates gradually:
                finegrained_epoch = epoch + step / len(train_dataloader)
                completion_f = finegrained_epoch / config.num_train_epochs
                # param_groups[1] goes from ti_lr to 0.0 over the course of training
                optimizer_ti.param_groups[0]['lr'] = config.ti_lr * (1 - completion_f) ** 2.0

            # warmup the token embedding lr:
            if (not ti_prod_opt) and config.token_embedding_lr_warmup_steps > 0:
                warmup_f = min(global_step / config.token_embedding_lr_warmup_steps, 1.0)
                optimizer_ti.param_groups[0]['lr'] *= warmup_f

            if not config.aspect_ratio_bucketing:
                token_indices, vae_latent, mask = batch
            else:
                token_indices, vae_latent, mask = train_dataset.get_aspect_ratio_bucketed_batch()
                    
            prompt_embeds, pooled_prompt_embeds, add_time_ids = get_conditioning_signals(
                config, pipe, token_indices, text_encoders
            )
            
            # Sample noise that we'll add to the latents:
            vae_latent = vae_latent.to(weight_dtype)
            noise = torch.randn_like(vae_latent)

            if config.noise_offset > 0.0:
                # https://www.crosslabs.org//blog/diffusion-with-offset-noise
                noise += config.noise_offset * torch.randn(
                    (noise.shape[0], noise.shape[1], 1, 1), device=noise.device)

            bsz = vae_latent.shape[0]
            timesteps = torch.randint(
                0,
                noise_scheduler.config.num_train_timesteps,
                (bsz,),
                device=vae_latent.device,
            ).long()

            noisy_latent = noise_scheduler.add_noise(vae_latent, noise, timesteps)
            noise_sigma = 0.0
            if noise_sigma > 0.0: # experimental: apply random noise to the conditioning vectors as a form of regularization
                prompt_embeds[0,1:-2,:] += torch.randn_like(prompt_embeds[0,2:-2,:]) * noise_sigma

            # Predict the noise residual
            model_pred = unet(
                noisy_latent,
                timesteps,
                encoder_hidden_states=prompt_embeds,
                timestep_cond=None,
                added_cond_kwargs={"text_embeds": pooled_prompt_embeds, "time_ids": add_time_ids},
                return_dict=False,
            )[0]
            
            # Get the unet prediction target depending on the prediction type:
            if noise_scheduler.config.prediction_type == "epsilon":
                target = noise
            elif noise_scheduler.config.prediction_type == "v_prediction":
                print(f"Using velocity prediction!")
                target = noise_scheduler.get_velocity(noisy_latent, noise, timesteps)
            else:
                raise ValueError(f"Unknown prediction type {noise_scheduler.config.prediction_type}")

            # Make sure we're computing the loss at full precision:
            #target, model_pred, mask = target.float(), model_pred.float(), mask.float()

            # Compute the loss:
            loss = (model_pred - target).pow(2) * mask

            if config.snr_gamma is None or config.snr_gamma == 0.0:
                # modulate loss by the inverse of the mask's mean value
                mean_mask_values = mask.mean(dim=list(range(1, len(loss.shape))))
                mean_mask_values = mean_mask_values / mean_mask_values.mean()
                loss = loss.mean(dim=list(range(1, len(loss.shape)))) / mean_mask_values
                loss = loss.mean()

            else:
                # Compute loss-weights as per Section 3.4 of https://arxiv.org/abs/2303.09556.
                # Since we predict the noise instead of x_0, the original formulation is slightly changed.
                # This is discussed in Section 4.2 of the same paper.
                snr = compute_snr(noise_scheduler, timesteps)
                base_weight = (
                    torch.stack([snr, config.snr_gamma * torch.ones_like(timesteps)], dim=1).min(dim=1)[0] / snr
                )
                if noise_scheduler.config.prediction_type == "v_prediction":
                    # Velocity objective needs to be floored to an SNR weight of one.
                    mse_loss_weights = base_weight + 1
                else:
                    # Epsilon and sample both use the same loss weights.
                    mse_loss_weights = base_weight

                mse_loss_weights = mse_loss_weights / mse_loss_weights.mean()
                loss = loss.mean(dim=list(range(1, len(loss.shape)))) * mse_loss_weights

                # modulate loss by the inverse of the mask's mean value
                mean_mask_values = mask.mean(dim=list(range(1, len(loss.shape))))
                mean_mask_values = mean_mask_values / mean_mask_values.mean()
                loss = loss.mean(dim=list(range(1, len(loss.shape)))) / mean_mask_values
                loss = loss.mean()

            if config.l1_penalty > 0.0 and not config.use_dora:
                # Compute normalized L1 norm (mean of abs sum) of all lora parameters:
                l1_norm = sum(p.abs().sum() for p in unet_lora_parameters) / sum(p.numel() for p in unet_lora_parameters)
                loss +=  config.l1_penalty * l1_norm

            # Some custom regularization terms: # TODO test how much these actually help!!
            loss, prompt_embeds_norms = conditioning_norm_regularization_loss(loss, config, prompt_embeds_norms, prompt_embeds)
            loss, prompt_embeds_norms = tok_conditioning_norm_regularization_loss(loss, config, prompt_embeds_norms, pipe, embedding_handler)

            losses['tot_loss'].append(loss.item())
            loss = loss / config.gradient_accumulation_steps
            loss.backward()

            last_batch = (step + 1 == len(train_dataloader))
            if (step + 1) % config.gradient_accumulation_steps == 0 or last_batch:
                optimizer_lora_unet.step()

                if optimizer_ti is not None:
                    # zero out the gradients of the non-trained text-encoder embeddings
                    for embedding_tensor in text_encoder_parameters:
                        embedding_tensor.grad.data[:-config.n_tokens, : ] *= 0.

                    if config.debug:
                        # Track the average gradient norms:
                        grad_norms['unet'].append(compute_grad_norm(itertools.chain(unet.parameters())).item())
                        for i, text_encoder in enumerate(text_encoders):
                            if text_encoder is not None:
                                text_encoder_norm = compute_grad_norm(itertools.chain(text_encoder.parameters())).item()
                                grad_norms[f'text_encoder_{i}'].append(text_encoder_norm)
                    
                    # Clip the gradients to stabilize training:
                    if config.clip_grad_norm > 0.0:
                        # Filter parameters with gradients for the UNet model
                        unet_params_with_grad = [p for p in unet.parameters() if p.grad is not None]
                        torch.nn.utils.clip_grad_norm_(unet_params_with_grad, clip_grad_norm)

                        # Filter parameters with gradients for each text encoder
                        for text_encoder in text_encoders:
                            if text_encoder is not None:
                                text_encoder_params_with_grad = [p for p in text_encoder.parameters() if p.grad is not None]
                                torch.nn.utils.clip_grad_norm_(text_encoder_params_with_grad, clip_grad_norm)

                    optimizer_ti.step()

                    # after every optimizer step, we do some manual intervention of the embeddings to regularize them:
                    # embedding_handler.retract_embeddings()
                    embedding_handler.fix_embedding_std(config.off_ratio_power)

                    optimizer_ti.zero_grad()
                optimizer_lora_unet.zero_grad()

            #############################################################################################################

            # Track the token embedding stds:
            trainable_embeddings, _ = embedding_handler.get_trainable_embeddings()
            for idx in range(len(text_encoders)):
                if text_encoders[idx] is not None:
                    embedding_stds = torch.stack(trainable_embeddings[f'txt_encoder_{idx}']).detach().float().std(dim=1)
                    for std_i, std in enumerate(embedding_stds):
                        token_stds[f'text_encoder_{idx}'][std_i].append(embedding_stds[std_i].item())

            
            # Track the learning rates for final plotting:
            lora_lrs.append(get_avg_lr(optimizer_lora_unet))
            try:
                ti_lrs.append(optimizer_ti.param_groups[0]['lr'])
            except:
                ti_lrs.append(0.0)

            # Print some statistics:
            if config.debug and (global_step % config.checkpointing_steps == 0) and global_step > 0:
                output_save_dir = f"{checkpoint_dir}/checkpoint-{global_step}"
                os.makedirs(output_save_dir, exist_ok=True)
                config.save_as_json(
                    os.path.join(output_save_dir, "training_args.json")
                )
                save_lora(
                    output_dir=output_save_dir, 
                    global_step=global_step, 
                    unet=unet, 
                    embedding_handler=embedding_handler, 
                    token_dict=config.token_dict, 
                    seed=config.seed, 
                    is_lora=config.is_lora, 
                    unet_lora_parameters=unet_lora_parameters,
                    unet_param_to_optimize=unet_param_to_optimize,
                    name=name
                )
                last_save_step = global_step

                token_embeddings, trainable_tokens = embedding_handler.get_trainable_embeddings()
                for idx, text_encoder in enumerate(text_encoders):
                    if text_encoder is None:
                        continue
                    n = len(token_embeddings[f'txt_encoder_{idx}'])
                    for i in range(n):
                        token = trainable_tokens[f'txt_encoder_{idx}'][i]
                        # Strip any backslashes from the token name:
                        token = token.replace("/", "_")
                        embedding = token_embeddings[f'txt_encoder_{idx}'][i]
                        plot_torch_hist(embedding, global_step, os.path.join(config.output_dir, 'ti_embeddings') , f"enc_{idx}_tokid_{i}: {token}", min_val=-0.05, max_val=0.05, ymax_f = 0.05, color = 'red')

                embedding_handler.print_token_info()
                plot_torch_hist(unet_lora_parameters, global_step, config.output_dir, "lora_weights", min_val=-0.4, max_val=0.4, ymax_f = 0.08)
                plot_loss(losses, save_path=f'{config.output_dir}/losses.png')
                plot_token_stds(token_stds, save_path=f'{config.output_dir}/token_stds.png')
                plot_grad_norms(grad_norms, save_path=f'{config.output_dir}/grad_norms.png')
                plot_lrs(lora_lrs, ti_lrs, save_path=f'{config.output_dir}/learning_rates.png')
                plot_curve(prompt_embeds_norms, 'steps', 'norm', 'prompt_embed norms', save_path=f'{config.output_dir}/prompt_embeds_norms.png')
                validation_prompts = render_images(pipe, config.validation_img_size, output_save_dir, global_step, 
                    config.seed, 
                    config.is_lora, 
                    config.pretrained_model, 
                    config.sample_imgs_lora_scale,
                    n_imgs = config.n_sample_imgs, 
                    verbose=config.verbose, 
                    trigger_text=trigger_text, 
                    device = config.device
                    )
                
                gc.collect()
                torch.cuda.empty_cache()
            
            images_done += config.train_batch_size
            global_step += 1

            if global_step % 100 == 0:
                print(f" ---- avg training fps: {images_done / (time.time() - start_time):.2f}", end="\r")

            if global_step % (config.max_train_steps//20) == 0:
                progress = (global_step / config.max_train_steps) + 0.05
                yield np.min((progress, 1.0))

    # final_save
    if (global_step - last_save_step) > 51:
        output_save_dir = f"{checkpoint_dir}/checkpoint-{global_step}"
    else:
        output_save_dir = f"{checkpoint_dir}/checkpoint-{last_save_step}"

    if config.debug:
        plot_loss(losses, save_path=f'{config.output_dir}/losses.png')
        plot_token_stds(token_stds, save_path=f'{config.output_dir}/token_stds.png')
        plot_lrs(lora_lrs, ti_lrs, save_path=f'{config.output_dir}/learning_rates.png')
        plot_torch_hist(unet_lora_parameters, global_step, config.output_dir, "lora_weights", min_val=-0.4, max_val=0.4, ymax_f = 0.08)

    if not os.path.exists(output_save_dir):
        os.makedirs(output_save_dir, exist_ok=True)
        config.save_as_json(
            os.path.join(output_save_dir, "training_args.json")
        )
        save_lora(
            output_dir=output_save_dir, 
            global_step=global_step, 
            unet=unet, 
            embedding_handler=embedding_handler, 
            token_dict=config.token_dict, 
            seed=config.seed, 
            is_lora=config.is_lora, 
            unet_lora_parameters=unet_lora_parameters,
            unet_param_to_optimize=unet_param_to_optimize,
            name=name
        )
        validation_prompts = render_images(pipe, config.validation_img_size, output_save_dir, global_step, 
            config.seed, 
            config.is_lora, 
            config.pretrained_model, 
            config.sample_imgs_lora_scale,
            n_imgs = config.n_sample_imgs, 
            n_steps = 30, 
            verbose=config.verbose, 
            trigger_text=trigger_text, 
            device = config.device
            )
    else:
        print(f"Skipping final save, {output_save_dir} already exists")

    del unet
    del vae
    del text_encoder_one
    del text_encoder_two
    del tokenizer_one
    del tokenizer_two
    del embedding_handler
    del pipe
    gc.collect()
    torch.cuda.empty_cache()

    config.save_as_json(
            os.path.join(output_save_dir, "training_args.json")
        )

    if config.debug:
        parent_dir = os.path.dirname(os.path.abspath(__file__))
        # Create a zipfile of all the *.py files in the directory
        zip_file_path = os.path.join(config.output_dir, 'source_code.zip')
        with zipfile.ZipFile(zip_file_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            zipdir(parent_dir, zipf)

    return output_save_dir, validation_prompts



if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Train a concept')
    parser.add_argument("-c", '--config-filename', type=str, help='Input string to be processed')
    args = parser.parse_args()

    config = TrainingConfig.from_json(
        file_path=args.config_filename
    )
    for progress in main(config=config):
        print(f"Progress: {(100*progress):.2f}%", end="\r")

    print("Training done :)")