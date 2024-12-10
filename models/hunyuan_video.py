from pathlib import Path
import sys
import argparse
import os.path
sys.path.insert(0, os.path.abspath('submodules/HunyuanVideo'))

import safetensors
import torch
from torch import nn
import torch.nn.functional as F
from accelerate import init_empty_weights
from accelerate.utils import set_module_tensor_to_device
from loguru import logger

from models.base import BasePipeline, PreprocessMediaFile
from utils.common import AUTOCAST_DTYPE
from hyvideo.config import add_network_args, add_extra_models_args, add_denoise_schedule_args, add_inference_args, sanity_check_args
from hyvideo.modules import load_model
from hyvideo.vae import load_vae
from hyvideo.constants import PRECISION_TO_TYPE, PROMPT_TEMPLATE
from hyvideo.text_encoder import TextEncoder
from hyvideo.inference import HunyuanVideoSampler
from hyvideo.modules.attenion import get_cu_seqlens


FRAMERATE = 24
# In diffusion-pipe, we already converted the dtype to an object. But Hunyuan scripts want the string version in a lot of places.
TYPE_TO_PRECISION = {v: k for k, v in PRECISION_TO_TYPE.items()}


def load_state_dict(args, model, pretrained_model_path, dtype):
    load_key = args.load_key
    dit_weight = Path(args.dit_weight)

    if dit_weight is None:
        model_dir = pretrained_model_path / f"t2v_{args.model_resolution}"
        files = list(model_dir.glob("*.pt"))
        if len(files) == 0:
            raise ValueError(f"No model weights found in {model_dir}")
        if str(files[0]).startswith("pytorch_model_"):
            model_path = dit_weight / f"pytorch_model_{load_key}.pt"
            bare_model = True
        elif any(str(f).endswith("_model_states.pt") for f in files):
            files = [f for f in files if str(f).endswith("_model_states.pt")]
            model_path = files[0]
            if len(files) > 1:
                logger.warning(
                    f"Multiple model weights found in {dit_weight}, using {model_path}"
                )
            bare_model = False
        else:
            raise ValueError(
                f"Invalid model path: {dit_weight} with unrecognized weight format: "
                f"{list(map(str, files))}. When given a directory as --dit-weight, only "
                f"`pytorch_model_*.pt`(provided by HunyuanDiT official) and "
                f"`*_model_states.pt`(saved by deepspeed) can be parsed. If you want to load a "
                f"specific weight file, please provide the full path to the file."
            )
    else:
        if dit_weight.is_dir():
            files = list(dit_weight.glob("*.pt"))
            if len(files) == 0:
                raise ValueError(f"No model weights found in {dit_weight}")
            if str(files[0]).startswith("pytorch_model_"):
                model_path = dit_weight / f"pytorch_model_{load_key}.pt"
                bare_model = True
            elif any(str(f).endswith("_model_states.pt") for f in files):
                files = [f for f in files if str(f).endswith("_model_states.pt")]
                model_path = files[0]
                if len(files) > 1:
                    logger.warning(
                        f"Multiple model weights found in {dit_weight}, using {model_path}"
                    )
                bare_model = False
            else:
                raise ValueError(
                    f"Invalid model path: {dit_weight} with unrecognized weight format: "
                    f"{list(map(str, files))}. When given a directory as --dit-weight, only "
                    f"`pytorch_model_*.pt`(provided by HunyuanDiT official) and "
                    f"`*_model_states.pt`(saved by deepspeed) can be parsed. If you want to load a "
                    f"specific weight file, please provide the full path to the file."
                )
        elif dit_weight.is_file():
            model_path = dit_weight
            bare_model = "unknown"
        else:
            raise ValueError(f"Invalid model path: {dit_weight}")

    if not model_path.exists():
        raise ValueError(f"model_path not exists: {model_path}")
    logger.info(f"Loading torch model {model_path}...")
    state_dict = torch.load(model_path, map_location=lambda storage, loc: storage, weights_only=True, mmap=True)

    if bare_model == "unknown" and ("ema" in state_dict or "module" in state_dict):
        bare_model = False
    if bare_model is False:
        if load_key in state_dict:
            state_dict = state_dict[load_key]
        else:
            raise KeyError(
                f"Missing key: `{load_key}` in the checkpoint: {model_path}. The keys in the checkpoint "
                f"are: {list(state_dict.keys())}."
            )

    base_dtype = torch.bfloat16
    params_to_keep = {"norm", "bias", "time_in", "vector_in", "guidance_in", "txt_in", "img_in"}
    for name, param in model.named_parameters():
        dtype_to_use = base_dtype if any(keyword in name for keyword in params_to_keep) else dtype
        set_module_tensor_to_device(model, name, device='cpu', dtype=dtype_to_use, value=state_dict[name])
    return model


def vae_encode(tensor, vae):
    # tensor values already in range [-1, 1] here
    latents = vae.encode(tensor).latent_dist.sample()
    return latents * vae.config.scaling_factor


class HunyuanVideoPipeline(BasePipeline):
    name = 'hunyuan-video'
    checkpointable_layers = ['DoubleBlock', 'SingleBlock']
    adapter_target_modules = ['MMDoubleStreamBlock', 'MMSingleStreamBlock']

    def __init__(self, config):
        self.config = config
        self.model_config = self.config['model']

        dtype = self.model_config['dtype']

        parser = argparse.ArgumentParser()
        parser = add_network_args(parser)
        parser = add_extra_models_args(parser)
        parser = add_denoise_schedule_args(parser)
        parser = add_inference_args(parser)
        args = parser.parse_args([])
        args.model_base = self.model_config['diffusers_path']
        args.dit_weight = os.path.join(args.model_base, 'hunyuan-video-t2v-720p/transformers/mp_rank_00_model_states.pt')

        self.args = sanity_check_args(args)

        vae, _, s_ratio, t_ratio = load_vae(
            self.args.vae,
            TYPE_TO_PRECISION[dtype],
            vae_path=os.path.join(self.args.model_base, 'hunyuan-video-t2v-720p/vae'),
            logger=logger,
            device='cpu',
        )
        vae_kwargs = {"s_ratio": s_ratio, "t_ratio": t_ratio}
        # Enabled by default in inference scripts, so we should probably train with it.
        #vae.enable_tiling()

        # Text encoder
        if self.args.prompt_template_video is not None:
            crop_start = PROMPT_TEMPLATE[self.args.prompt_template_video].get(
                "crop_start", 0
            )
            self.max_text_length_video = self.args.text_len + crop_start
        if self.args.prompt_template is not None:
            crop_start = PROMPT_TEMPLATE[self.args.prompt_template].get("crop_start", 0)
            self.max_text_length_image = self.args.text_len + crop_start

        prompt_template = (
            PROMPT_TEMPLATE[self.args.prompt_template]
            if self.args.prompt_template is not None
            else None
        )

        prompt_template_video = (
            PROMPT_TEMPLATE[self.args.prompt_template_video]
            if self.args.prompt_template_video is not None
            else None
        )

        text_encoder = TextEncoder(
            text_encoder_type=self.args.text_encoder,
            max_length=self.max_text_length_video,
            text_encoder_path=os.path.join(self.args.model_base, 'text_encoder'),
            text_encoder_precision=TYPE_TO_PRECISION[dtype],
            tokenizer_type=self.args.tokenizer,
            prompt_template=prompt_template,
            prompt_template_video=prompt_template_video,
            hidden_state_skip_layer=self.args.hidden_state_skip_layer,
            apply_final_norm=self.args.apply_final_norm,
            reproduce=self.args.reproduce,
            logger=logger,
            device='cpu',
        )

        text_encoder_2 = TextEncoder(
            text_encoder_type=self.args.text_encoder_2,
            max_length=self.args.text_len_2,
            text_encoder_path=os.path.join(self.args.model_base, 'text_encoder_2'),
            text_encoder_precision=TYPE_TO_PRECISION[dtype],
            tokenizer_type=self.args.tokenizer_2,
            reproduce=self.args.reproduce,
            logger=logger,
            device='cpu',
        )

        self.inference_pipeline = HunyuanVideoSampler(
            args=self.args,
            vae=vae,
            vae_kwargs=vae_kwargs,
            text_encoder=text_encoder,
            text_encoder_2=text_encoder_2,
            model=None,
            use_cpu_offload=False,
            device='cpu',
            logger=logger,
        )
        self.diffusers_pipeline = self.inference_pipeline.pipeline

    # delay loading transformer to save RAM
    def load_diffusion_model(self):
        transformer_dtype = self.model_config.get('transformer_dtype', self.model_config['dtype'])
        # Device needs to be cuda here or we get an error. We initialize the model with empty weights so it doesn't matter, and
        # then directly load the weights onto CPU right after.
        factor_kwargs = {"device": 'cuda', "dtype": transformer_dtype}
        in_channels = self.args.latent_channels
        out_channels = self.args.latent_channels
        with init_empty_weights():
            transformer = load_model(
                self.args,
                in_channels=in_channels,
                out_channels=out_channels,
                factor_kwargs=factor_kwargs,
            )
        transformer = load_state_dict(self.args, transformer, self.args.model_base, transformer_dtype)
        self.inference_pipeline.model = transformer
        self.diffusers_pipeline.transformer = transformer
        self.transformer.train()
        # We'll need the original parameter name for saving, and the name changes once we wrap modules for pipeline parallelism,
        # so store it in an attribute here. Same thing below if we're training a lora and creating lora weights.
        for name, p in self.transformer.named_parameters():
            p.original_name = name

    def __getattr__(self, name):
        return getattr(self.diffusers_pipeline, name)

    def get_vae(self):
        return self.vae

    def get_text_encoders(self):
        return (self.text_encoder, self.text_encoder_2)

    def save_adapter(self, save_dir, peft_state_dict):
        self.peft_config.save_pretrained(save_dir)
        # ComfyUI format.
        peft_state_dict = {'diffusion_model.'+k: v for k, v in peft_state_dict.items()}
        safetensors.torch.save_file(peft_state_dict, save_dir / 'adapter_model.safetensors', metadata={'format': 'pt'})

    def save_model(self, save_dir, diffusers_sd):
        raise NotImplementedError()

    def get_preprocess_media_file_fn(self):
        return PreprocessMediaFile(
            self.config,
            support_video=True,
            framerate=FRAMERATE,
            round_height=8,
            round_width=8,
            round_frames=4,
        )

    def get_call_vae_fn(self, vae):
        def fn(tensor):
            return {'latents': vae_encode(tensor.to(vae.device, vae.dtype), vae)}
        return fn

    def get_call_text_encoder_fn(self, text_encoder):
        if text_encoder == self.text_encoder:
            text_encoder_idx = 1
        elif text_encoder == self.text_encoder_2:
            text_encoder_idx = 2
        else:
            raise RuntimeError()
        def fn(caption, is_video):
            # args are lists
            prompt_embeds, prompt_attention_masks = [], []
            # need to use a loop because is_video might be different for each one
            for caption, is_video in zip(caption, is_video):
                if is_video:
                    # This is tricky. The text encoder will crop off the prompt correctly based on the data_type, but the offical code only sets the max
                    # length (which needs to be set accordingly to the prompt) once. So we have to do it here each time.
                    if text_encoder_idx == 1:
                        text_encoder.max_length = self.max_text_length_video
                    data_type = 'video'
                else:
                    if text_encoder_idx == 1:
                        text_encoder.max_length = self.max_text_length_image
                    data_type = 'image'
                (
                    prompt_embed,
                    negative_prompt_embed,
                    prompt_mask,
                    negative_prompt_mask,
                ) = self.encode_prompt(
                    caption,
                    device=next(text_encoder.parameters()).device,
                    num_videos_per_prompt=1,
                    do_classifier_free_guidance=False,
                    text_encoder=text_encoder,
                    data_type=data_type,
                )
                prompt_embeds.append(prompt_embed)
                prompt_attention_masks.append(prompt_mask)
            prompt_embeds = torch.cat(prompt_embeds)
            prompt_attention_masks = torch.cat(prompt_attention_masks)
            if text_encoder_idx == 1:
                return {'prompt_embeds_1': prompt_embeds, 'prompt_attention_mask_1': prompt_attention_masks}
            elif text_encoder_idx == 2:
                return {'prompt_embeds_2': prompt_embeds}
            else:
                raise RuntimeError()
        return fn

    def prepare_inputs(self, inputs, timestep_quantile=None):
        latents = inputs['latents'].float()
        prompt_embeds_1 = inputs['prompt_embeds_1']
        prompt_attention_mask_1 = inputs['prompt_attention_mask_1']
        prompt_embeds_2 = inputs['prompt_embeds_2']

        bs, channels, num_frames, h, w = latents.shape

        guidance_expand = torch.tensor(
            [self.model_config.get('guidance', 1.0)] * bs,
            dtype=torch.float32,
        ) * 1000

        timestep_sample_method = self.model_config.get('timestep_sample_method', 'logit_normal')

        if timestep_sample_method == 'logit_normal':
            dist = torch.distributions.normal.Normal(0, 1)
        elif timestep_sample_method == 'uniform':
            dist = torch.distributions.uniform.Uniform(0, 1)
        else:
            raise NotImplementedError()

        if timestep_quantile is not None:
            t = dist.icdf(torch.full((bs,), timestep_quantile, device=latents.device))
        else:
            t = dist.sample((bs,)).to(latents.device)

        if timestep_sample_method == 'logit_normal':
            sigmoid_scale = self.model_config.get('sigmoid_scale', 1.0)
            t = t * sigmoid_scale
            t = torch.sigmoid(t)

        x_1 = latents
        x_0 = torch.randn_like(x_1)
        t_expanded = t.view(-1, 1, 1, 1, 1)
        x_t = (1 - t_expanded) * x_1 + t_expanded * x_0
        target = x_0 - x_1

        video_length = (num_frames - 1) * 4 + 1
        video_height = h * 8
        video_width = w * 8
        freqs_cos, freqs_sin = self.inference_pipeline.get_rotary_pos_embed(
            video_length, video_height, video_width
        )
        freqs_cos = freqs_cos.expand(bs, -1, -1)
        freqs_sin = freqs_sin.expand(bs, -1, -1)

        # timestep input to model needs to be in range [0, 1000]
        t = t * 1000

        return (
            x_t,
            t,
            prompt_embeds_1,
            prompt_attention_mask_1,
            prompt_embeds_2,
            freqs_cos,
            freqs_sin,
            guidance_expand,
            target,
        )

    def to_layers(self):
        transformer = self.transformer
        layers = [InitialLayer(transformer)]
        for block in transformer.double_blocks:
            layers.append(DoubleBlock(block))
        layers.append(concatenate_hidden_states)
        for block in transformer.single_blocks:
            layers.append(SingleBlock(block))
        layers.append(OutputLayer(transformer))
        return layers


class InitialLayer(nn.Module):
    def __init__(self, transformer):
        super().__init__()
        # Prevent registering the whole Transformer.
        self.transformer = [transformer]
        # Explicitly register these modules.
        self.time_in = self.transformer[0].time_in
        self.vector_in = self.transformer[0].vector_in
        self.guidance_embed = self.transformer[0].guidance_embed
        self.guidance_in = self.transformer[0].guidance_in
        self.img_in = self.transformer[0].img_in
        self.text_projection = self.transformer[0].text_projection
        self.txt_in = self.transformer[0].txt_in

    @torch.autocast('cuda', dtype=AUTOCAST_DTYPE)
    def forward(self, inputs):
        for item in inputs:
            if torch.is_floating_point(item):
                item.requires_grad_(True)

        x, t, text_states, text_mask, text_states_2, freqs_cos, freqs_sin, guidance, target = inputs

        _, _, ot, oh, ow = x.shape
        tt, th, tw = (
            ot // self.transformer[0].patch_size[0],
            oh // self.transformer[0].patch_size[1],
            ow // self.transformer[0].patch_size[2],
        )
        unpatchify_args = torch.tensor([tt, th, tw], device=x.device)

        # diffusion-pipe makes all input tensors have a batch dimension, but Hunyuan code wants these to not have batch dim
        assert freqs_cos.ndim == 3
        freqs_cos, freqs_sin = freqs_cos[0], freqs_sin[0]

        img = x
        txt = text_states

        # Prepare modulation vectors.
        vec = self.time_in(t)

        # text modulation
        vec = vec + self.vector_in(text_states_2)

        # guidance modulation
        if self.guidance_embed:
            if guidance is None:
                raise ValueError(
                    "Didn't get guidance strength for guidance distilled model."
                )

            # our timestep_embedding is merged into guidance_in(TimestepEmbedder)
            vec = vec + self.guidance_in(guidance)

        # Embed image and text.
        img = self.img_in(img)
        if self.text_projection == "linear":
            txt = self.txt_in(txt)
        elif self.text_projection == "single_refiner":
            txt = self.txt_in(txt, t, text_mask if self.transformer[0].use_attention_mask else None)
        else:
            raise NotImplementedError(
                f"Unsupported text_projection: {self.text_projection}"
            )

        txt_seq_len = txt.shape[1]
        img_seq_len = img.shape[1]

        # Compute cu_squlens and max_seqlen for flash attention
        cu_seqlens = get_cu_seqlens(text_mask, img_seq_len)

        # Everything passed between layers needs to be a CUDA tensor for Deepspeed pipeline parallelism.
        txt_seq_len = torch.tensor(txt_seq_len, device=img.device)
        img_seq_len = torch.tensor(img_seq_len, device=img.device)
        max_seqlen = img_seq_len + txt_seq_len

        return img, txt, vec, cu_seqlens, max_seqlen, freqs_cos, freqs_sin, txt_seq_len, img_seq_len, unpatchify_args, target


class DoubleBlock(nn.Module):
    def __init__(self, block):
        super().__init__()
        self.block = block

    @torch.autocast('cuda', dtype=AUTOCAST_DTYPE)
    def forward(self, inputs):
        img, txt, vec, cu_seqlens, max_seqlen, freqs_cos, freqs_sin, txt_seq_len, img_seq_len, unpatchify_args, target = inputs
        img, txt = self.block(img, txt, vec, cu_seqlens, cu_seqlens, max_seqlen.item(), max_seqlen.item(), (freqs_cos, freqs_sin))
        return img, txt, vec, cu_seqlens, max_seqlen, freqs_cos, freqs_sin, txt_seq_len, img_seq_len, unpatchify_args, target


def concatenate_hidden_states(inputs):
    img, txt, vec, cu_seqlens, max_seqlen, freqs_cos, freqs_sin, txt_seq_len, img_seq_len, unpatchify_args, target = inputs
    x = torch.cat((img, txt), 1)
    return x, vec, cu_seqlens, max_seqlen, freqs_cos, freqs_sin, txt_seq_len, img_seq_len, unpatchify_args, target


class SingleBlock(nn.Module):
    def __init__(self, block):
        super().__init__()
        self.block = block

    @torch.autocast('cuda', dtype=AUTOCAST_DTYPE)
    def forward(self, inputs):
        x, vec, cu_seqlens, max_seqlen, freqs_cos, freqs_sin, txt_seq_len, img_seq_len, unpatchify_args, target = inputs
        x = self.block(x, vec, txt_seq_len.item(), cu_seqlens, cu_seqlens, max_seqlen.item(), max_seqlen.item(), (freqs_cos, freqs_sin))
        return x, vec, cu_seqlens, max_seqlen, freqs_cos, freqs_sin, txt_seq_len, img_seq_len, unpatchify_args, target

class OutputLayer(nn.Module):
    def __init__(self, transformer):
        super().__init__()
        # Prevent registering the whole Transformer.
        self.transformer = [transformer]
        # Explicitly register these modules.
        self.final_layer = self.transformer[0].final_layer

    @torch.autocast('cuda', dtype=AUTOCAST_DTYPE)
    def forward(self, inputs):
        x, vec, cu_seqlens, max_seqlen, freqs_cos, freqs_sin, txt_seq_len, img_seq_len, unpatchify_args, target = inputs
        img = x[:, :img_seq_len.item(), ...]

        # ---------------------------- Final layer ------------------------------
        img = self.final_layer(img, vec)  # (N, T, patch_size ** 2 * out_channels)

        tt, th, tw = (arg.item() for arg in unpatchify_args)
        output = self.transformer[0].unpatchify(img, tt, th, tw)

        with torch.autocast('cuda', enabled=False):
            output = output.to(torch.float32)
            target = target.to(torch.float32)
            loss = F.mse_loss(output, target)
        return loss