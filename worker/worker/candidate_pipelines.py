"""Queued inference adapters for benchmark-only candidate architectures."""

from __future__ import annotations

from contextlib import nullcontext
from typing import Any, Callable

from worker.manifests import Manifest

Callback = Callable[[Any, int, Any, dict[str, Any]], dict[str, Any]]


class MossAdapter:
    def __init__(self, pipe: Any, *, device: str, rung: str, torch: Any):
        self.pipe = pipe
        self.encoder = pipe.text_encoder
        self.tokenizer = pipe.tokenizer
        pipe.text_encoder = None
        pipe.tokenizer = None
        self.device = device
        self.rung = rung
        self.torch = torch
        self._closed = False

    def __call__(self, *, prompt: str, num_inference_steps: int = 50,
                 guidance_scale: float = 3.0, width: int = 1024, height: int = 1024,
                 generator: Any = None, callback_on_step_end: Callback | None = None,
                 negative_prompt: str = "", **_: Any) -> Any:
        width = width or 1024
        height = height or 1024
        pipe = self.pipe
        with self.torch.inference_mode():
            embeddings, mask = _moss_encode(self.tokenizer, self.encoder, prompt, 4096, self.torch)
            negative = negative_mask = None
            if guidance_scale != 1.0:
                negative, negative_mask = _moss_encode(
                    self.tokenizer, self.encoder, negative_prompt, 4096, self.torch,
                )
        if self.rung == "full":
            pipe.to(self.device)
        else:
            pipe.transformer.to(self.device)
            pipe.vae.to("cpu")
        pipe.vae.enable_tiling()
        original_step = pipe.scheduler.step
        index = 0

        def step(*args: Any, **kwargs: Any) -> Any:
            nonlocal index
            result = original_step(*args, **kwargs)
            if self.rung == "model_offload" and index + 1 == num_inference_steps:
                pipe.transformer.to("cpu")
                pipe.vae.to(self.device)
            if callback_on_step_end is not None:
                callback_on_step_end(self, index, args[1], {"latents": result[0]})
            index += 1
            return result

        pipe.scheduler.step = step
        try:
            result = pipe(
                prompt=None, prompt_embeds=embeddings, prompt_attention_mask=mask,
                negative_prompt_embeds=negative, negative_prompt_attention_mask=negative_mask,
                num_inference_steps=num_inference_steps, guidance_scale=guidance_scale,
                width=width, height=height, generator=generator, max_sequence_length=4096,
            )
            return result
        finally:
            pipe.scheduler.step = original_step
            if self.rung == "model_offload":
                pipe.transformer.to("cpu")
                pipe.vae.to("cpu")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.rung == "model_offload":
            self.pipe.transformer.to("cpu")
            self.pipe.vae.to("cpu")
        del self.pipe, self.encoder, self.tokenizer


def _moss_encode(tokenizer: Any, encoder: Any, prompt: str, max_length: int,
                 torch: Any) -> tuple[Any, Any]:
    rows = []
    for value in [prompt]:
        formatted = tokenizer.apply_chat_template(
            [{"role": "user", "content": value}], tokenize=False,
            add_generation_prompt=True, enable_thinking=True,
        )
        inputs = tokenizer(
            formatted, return_tensors="pt", truncation=True, max_length=max_length,
        ).to("cpu")
        output = encoder(**inputs, use_cache=False, return_dict=True)
        rows.append(output.last_hidden_state[0])
    length = max(row.shape[0] for row in rows)
    embeds = rows[0].new_zeros(len(rows), length, rows[0].shape[-1])
    mask = torch.zeros((len(rows), length), dtype=torch.bool)
    for i, row in enumerate(rows):
        embeds[i, :row.shape[0]] = row
        mask[i, :row.shape[0]] = True
    return embeds, mask


class _IrisPromptEncoder:
    def __init__(self, encoder: Any):
        self.encoder = encoder
        self._empty: Any = None

    def encode(self, prompts: list[str]) -> Any:
        return self.encoder.encode(prompts)

    def null(self, negative_prompt: str = "") -> Any:
        if negative_prompt:
            return self.encoder.encode([negative_prompt])
        if self._empty is None:
            self._empty = self.encoder.encode([""])
        return self._empty

    def to(self, device: str) -> _IrisPromptEncoder:
        self.encoder.to(device)
        return self


class IrisAdapter:
    def __init__(self, model: Any, encoder: Any, *, device: str, rung: str, torch: Any,
                 generate: Callable[..., Any]):
        self.model = model
        self.encoder = encoder
        self.device = device
        self.rung = rung
        self.torch = torch
        self.generate = generate
        self._closed = False

    def __call__(self, *, prompt: str, num_inference_steps: int = 100,
                 guidance_scale: float = 3.0, width: int = 1024, height: int = 1024,
                 generator: Any = None, callback_on_step_end: Callback | None = None,
                 negative_prompt: str = "", **_: Any) -> Any:
        width = width or 1024
        height = height or 1024
        torch = self.torch
        self.model.to(self.device)
        if self.rung == "full":
            self.encoder.to(self.device)
        calls = 0

        def hook(_module: Any, _inputs: Any, output: Any) -> Any:
            nonlocal calls
            calls += 1
            if callback_on_step_end is not None:
                callback_on_step_end(self, calls - 1, calls - 1, {"sample": output})
            return output

        handle = self.model.register_forward_hook(hook)
        try:
            autocast = torch.autocast("cuda", dtype=torch.bfloat16) if self.device.startswith("cuda") else nullcontext()
            with autocast:
                images = self.generate(
                    self.model, self.encoder, [prompt], height, width,
                    steps=num_inference_steps, order=2, cfg_scale=guidance_scale,
                    negative_prompt=negative_prompt, generator=generator,
                    device=self.device, num_train_timesteps=1000, prediction="v",
                )
            pixels = ((images[0].detach().float().cpu().clamp(-1, 1) + 1) * 127.5)
            pixels = pixels.round().to(torch.uint8).permute(1, 2, 0).numpy()
            from PIL import Image
            return _Output([Image.fromarray(pixels)])
        finally:
            handle.remove()
            if self.rung == "model_offload":
                self.model.to("cpu")
                self.encoder.to("cpu")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.model.to("cpu")
        self.encoder.to("cpu")
        del self.model, self.encoder


class _Output:
    def __init__(self, images: list[Any]):
        self.images = images


class Flux3Adapter:
    def __init__(self, pipe: Any, manager: Any, *, device: str, rung: str, torch: Any):
        self.pipe, self.manager = pipe, manager
        self.device, self.rung, self.torch = device, rung, torch
        self._closed = False

    def __call__(self, *, prompt: str, num_inference_steps: int = 16,
                 guidance_scale: float = 0.0, width: int = 1024, height: int = 1024,
                 generator: Any = None, callback_on_step_end: Callback | None = None,
                 negative_prompt: str | None = None, **_: Any) -> Any:
        width = width or 1024
        height = height or 1024
        if guidance_scale != 0:
            raise ValueError("Flux3Distilled does not support guidance")
        if negative_prompt:
            raise ValueError("Flux3Distilled does not support negative prompts")
        if self.rung == "full":
            self.pipe.to(self.device)
        self.pipe.vae.enable_tiling()
        scheduler = self.pipe.scheduler
        original_step = scheduler.step
        index = 0

        def step(*args: Any, **kwargs: Any) -> Any:
            nonlocal index
            result = original_step(*args, **kwargs)
            if callback_on_step_end is not None:
                callback_on_step_end(self, index, args[1], {"latents": result[0]})
            index += 1
            return result

        scheduler.step = step
        try:
            images = self.pipe(
                prompt=prompt, width=width, height=height,
                num_inference_steps=num_inference_steps, generator=generator,
                max_sequence_length=512, output="images",
            )
            return _Output(images if isinstance(images, list) else [images])
        finally:
            scheduler.step = original_step

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.manager.disable_auto_cpu_offload()
        del self.pipe


def load_candidate_pipeline(manifest: Manifest, *, rung: str, device: str) -> Any:
    if manifest.pipeline == "MOSSImage":
        return _load_moss(manifest, rung, device)
    if manifest.pipeline == "Iris3B":
        return _load_iris(manifest, rung, device)
    if manifest.pipeline == "Flux3Distilled":
        return _load_flux3(manifest, rung, device)
    raise ValueError(f"unsupported candidate pipeline {manifest.pipeline!r}")


def _load_moss(manifest: Manifest, rung: str, device: str) -> MossAdapter:
    import torch
    from diffusers import DiffusionPipeline

    pin = _revision(manifest)
    pipe = DiffusionPipeline.from_pretrained(
        manifest.source, revision=pin, custom_pipeline="pipeline_moss_image",
        custom_revision=pin, trust_remote_code=True,
        torch_dtype={"default": torch.bfloat16, "vae": torch.float32},
    )
    pipe.set_progress_bar_config(disable=True)
    pipe.text_encoder.to("cpu")
    pipe.vae.to("cpu")
    return MossAdapter(pipe, device=device, rung=rung, torch=torch)


def _load_iris(manifest: Manifest, rung: str, device: str) -> IrisAdapter:
    import torch
    from huggingface_hub import snapshot_download
    from iris3b.config import inference_config
    from iris3b.models.dit import IrisDiT
    from iris3b.sampling import generate, load_for_inference
    from iris3b.text.qwen3_vl import Qwen3VLTextEncoder

    model_dir = snapshot_download(
        repo_id=manifest.source, revision=_revision(manifest),
        allow_patterns=["config.yaml", "model.safetensors"],
    )
    raw, weights = load_for_inference(model_dir)
    cfg = inference_config(raw)
    cfg.text_encoder.pretrained = snapshot_download(
        repo_id="Qwen/Qwen3-VL-4B-Instruct", revision="ebb281ec70b05090aa6165b016eac8ec08e71b17",
        allow_patterns=["config.json", "generation_config.json", "tokenizer.json",
                        "tokenizer_config.json", "special_tokens_map.json", "vocab.json",
                        "merges.txt", "added_tokens.json", "*.safetensors",
                        "model.safetensors.index.json"],
    )
    with torch.device("meta"):
        model = IrisDiT(cfg.model)
    model.load_state_dict(weights, strict=True, assign=True)
    del weights
    model.to(dtype=getattr(torch, manifest.dtype or "bfloat16"))
    model.eval().requires_grad_(False)
    encoder_device = "cpu" if rung == "model_offload" else device
    encoder = _IrisPromptEncoder(Qwen3VLTextEncoder(cfg.text_encoder, device=encoder_device))
    return IrisAdapter(model, encoder, device=device, rung=rung, torch=torch, generate=generate)


def _load_flux3(manifest: Manifest, rung: str, device: str) -> Flux3Adapter:
    import torch
    from diffusers import ComponentsManager, Flux3DistilledAutoBlocks, Flux3DistilledModularPipeline

    blocks: Any = Flux3DistilledAutoBlocks()
    blocks.sub_blocks.pop("prompt_upsampler")
    manager: Any = ComponentsManager()
    pipe: Any = Flux3DistilledModularPipeline(
        blocks=blocks, pretrained_model_name_or_path=manifest.source,
        revision=_revision(manifest), components_manager=manager,
    )
    pipe.set_progress_bar_config(disable=True)
    pipe.load_components(
        dtype={"vae": torch.float32, "default": torch.bfloat16}, revision=_revision(manifest),
    )
    if rung == "model_offload":
        manager.enable_auto_cpu_offload(
            device=device,
            offload_strategy=lambda hooks, **_kwargs: hooks,
        )
    else:
        pipe.to(device)
    return Flux3Adapter(pipe, manager, device=device, rung=rung, torch=torch)


def _revision(manifest: Manifest) -> str:
    if not manifest.source_revision:
        raise ValueError(f"{manifest.id} requires a pinned source_revision")
    return manifest.source_revision
