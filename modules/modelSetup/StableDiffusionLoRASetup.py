from typing import Iterable

import torch
from torch.nn import Parameter

from modules.model.StableDiffusionModel import StableDiffusionModel, StableDiffusionModelEmbedding
from modules.modelSetup.BaseStableDiffusionSetup import BaseStableDiffusionSetup
from modules.modelSetup.mixin.ModelSetupClipEmbeddingMixin import ModelSetupClipEmbeddingMixin
from modules.module.LoRAModule import LoRAModuleWrapper
from modules.util import create
from modules.util.TrainProgress import TrainProgress
from modules.util.config.TrainConfig import TrainConfig


class StableDiffusionLoRASetup(
    BaseStableDiffusionSetup,
    ModelSetupClipEmbeddingMixin,
):
    def __init__(
            self,
            train_device: torch.device,
            temp_device: torch.device,
            debug_mode: bool,
    ):
        super(StableDiffusionLoRASetup, self).__init__(
            train_device=train_device,
            temp_device=temp_device,
            debug_mode=debug_mode,
        )

    def create_parameters(
            self,
            model: StableDiffusionModel,
            config: TrainConfig,
    ) -> Iterable[Parameter]:
        params = list()

        if config.text_encoder.train:
            params += list(model.text_encoder_lora.parameters())

        if config.train_embedding:
            params += list(model.text_encoder.get_input_embeddings().parameters())

        if config.unet.train:
            params += list(model.unet_lora.parameters())

        return params

    def create_parameters_for_optimizer(
            self,
            model: StableDiffusionModel,
            config: TrainConfig,
    ) -> Iterable[Parameter] | list[dict]:
        param_groups = list()

        if config.text_encoder.train:
            param_groups.append(
                self.create_param_groups(config, model.text_encoder_lora.parameters(), config.text_encoder.learning_rate)
            )

        if args.train_embedding:
            param_groups.append(
                self.create_param_groups(
                    args,
                    model.text_encoder.get_input_embeddings().parameters(),
                    args.embedding_learning_rate,
                )
            )

        if config.unet.train:
            param_groups.append(
                self.create_param_groups(config, model.unet_lora.parameters(), config.unet.learning_rate)
            )

        return param_groups

    def setup_model(
            self,
            model: StableDiffusionModel,
            config: TrainConfig,
    ):
        if model.text_encoder_lora is None:
            model.text_encoder_lora = LoRAModuleWrapper(
                model.text_encoder, config.lora_rank, "lora_te", config.lora_alpha
            )

        if model.unet_lora is None:
            model.unet_lora = LoRAModuleWrapper(
                model.unet, config.lora_rank, "lora_unet", config.lora_alpha, ["attentions"]
            )

        model.text_encoder_lora.set_dropout(config.dropout_probability)
        model.unet_lora.set_dropout(config.dropout_probability)

        model.text_encoder.requires_grad_(False)
        model.unet.requires_grad_(False)
        model.vae.requires_grad_(False)

        if model.text_encoder_lora is not None:
            train_text_encoder = config.text_encoder.train and \
                                 not self.stop_text_encoder_training_elapsed(config, model.train_progress)
            model.text_encoder_lora.requires_grad_(train_text_encoder)

        if model.unet_lora is not None:
            train_unet = config.unet.train and \
                                 not self.stop_unet_training_elapsed(config, model.train_progress)
            model.unet_lora.requires_grad_(train_unet)

        train_embedding = args.train_embedding and (model.train_progress.epoch < args.train_embedding_epochs)
        if train_embedding:
            model.text_encoder.get_input_embeddings().requires_grad_(True)
            model.text_encoder.get_input_embeddings().to(dtype=args.embedding_weight_dtype.torch_dtype())

        train_unet = args.train_unet and (model.train_progress.epoch < args.train_unet_epochs)
        model.unet_lora.requires_grad_(train_unet)

        model.text_encoder_lora.to(dtype=config.lora_weight_dtype.torch_dtype())
        model.unet_lora.to(dtype=config.lora_weight_dtype.torch_dtype())

        model.text_encoder_lora.hook_to_module()
        model.unet_lora.hook_to_module()

        if len(model.embeddings) == 0:
            vector = self._create_new_embedding(
                model.tokenizer,
                model.text_encoder,
                args.initial_embedding_text,
                args.token_count,
            )

            model.embeddings = [StableDiffusionModelEmbedding(vector, 'embedding')]

        original_token_embeds, untrainable_token_ids = self._add_embeddings_to_clip(
            model.tokenizer,
            model.text_encoder,
            [(model.embeddings[0].text_encoder_vector, model.embeddings[0].text_tokens, True)],
        )
        model.all_text_encoder_original_token_embeds = original_token_embeds
        model.text_encoder_untrainable_token_embeds_mask = untrainable_token_ids

        if config.rescale_noise_scheduler_to_zero_terminal_snr:
            model.rescale_noise_scheduler_to_zero_terminal_snr()
            model.force_v_prediction()

        model.optimizer = create.create_optimizer(
            self.create_parameters_for_optimizer(model, config), model.optimizer_state_dict, config
        )
        del model.optimizer_state_dict

        model.ema = create.create_ema(
            self.create_parameters(model, config), model.ema_state_dict, config
        )
        del model.ema_state_dict

        self.setup_optimizations(model, config)

    def setup_train_device(
            self,
            model: StableDiffusionModel,
            config: TrainConfig,
    ):
        vae_on_train_device = self.debug_mode or config.align_prop
        text_encoder_on_train_device = \
            config.text_encoder.train \
            or config.train_embedding \
            or config.align_prop \
            or not config.latent_caching

        model.text_encoder_to(self.train_device if text_encoder_on_train_device else self.temp_device)
        model.vae_to(self.train_device if vae_on_train_device else self.temp_device)
        model.unet_to(self.train_device)
        model.depth_estimator_to(self.temp_device)

        if config.text_encoder.train:
            model.text_encoder.train()
        else:
            model.text_encoder.eval()

        model.vae.eval()

        if config.unet.train:
            model.unet.train()
        else:
            model.unet.eval()

    def after_optimizer_step(
            self,
            model: StableDiffusionModel,
            config: TrainConfig,
            train_progress: TrainProgress
    ):
        if model.text_encoder_lora is not None:
            train_text_encoder = config.text_encoder.train and \
                                 not self.stop_text_encoder_training_elapsed(config, model.train_progress)
            model.text_encoder_lora.requires_grad_(train_text_encoder)

        if model.unet_lora is not None:
            train_unet = config.unet.train and \
                                 not self.stop_unet_training_elapsed(config, model.train_progress)
            model.unet_lora.requires_grad_(train_unet)

        train_embedding = model.train_progress.epoch < args.train_embedding_epochs
        if args.train_embedding and not train_embedding:
            model.text_encoder.get_input_embeddings().requires_grad_(False)

        self._embeddigns_after_optimizer_step(
            model.text_encoder.get_input_embeddings(),
            model.all_text_encoder_original_token_embeds,
            model.text_encoder_untrainable_token_embeds_mask,
        )

    def report_learning_rates(
            self,
            model,
            config,
            scheduler,
            tensorboard
    ):
        lrs = scheduler.get_last_lr()
        names = []
        if config.text_encoder.train:
            names.append("te")
        if config.unet.train:
            names.append("unet")
        assert len(lrs) == len(names)

        lrs = config.optimizer.optimizer.maybe_adjust_lrs(lrs, model.optimizer)

        for name, lr in zip(names, lrs):
            tensorboard.add_scalar(
                f"lr/{name}", lr, model.train_progress.global_step
            )
