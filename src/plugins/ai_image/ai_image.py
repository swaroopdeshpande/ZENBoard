"""
AI Image - ZENBoard
Generates an image from a text prompt using Hugging Face's Inference
Providers router - specifically the fal-ai provider, which is free-tier
friendly and (unlike HF's own retired "hf-inference" hosting) actually has
these models live. No OpenAI billing required. Prompt randomization, if
enabled, uses OpenRouter's free-tier chat models via the same key
ai_text.py already uses - entirely optional, the plugin works with just a
Hugging Face token.

Note on HF's Inference Providers: HF stopped hosting most text-to-image
models on their own "hf-inference" backend and now brokers requests to
third-party providers (fal-ai, together, replicate, ...) through one
router with your HF token. Each provider has its own request/response
shape - fal-ai's is {"prompt": ...} in, {"images": [{"url": ...}]} out
(a URL to fetch, not raw bytes), which is what's implemented below.
"""

import logging
import random
from io import BytesIO
from urllib.parse import quote

import requests
from PIL import Image

from plugins.base_plugin.base_plugin import BasePlugin

logger = logging.getLogger(__name__)

HF_ROUTER_URL = "https://router.huggingface.co/fal-ai/{provider_path}"
POLLINATIONS_URL = "https://image.pollinations.ai/prompt/{prompt}"

# display model -> provider config. Two tiers on offer:
#   fal-ai (via HF token): best/most reliable quality (FLUX), capped by
#     HF's small monthly free credit.
#   pollinations: genuinely free and uncapped, no key needed, but doesn't
#     let you pin a specific backend model - style/quality varies by request.
IMAGE_MODELS = {
    "black-forest-labs/FLUX.1-schnell": {"provider": "fal-ai", "path": "fal-ai/flux/schnell"},
    "stabilityai/stable-diffusion-xl-base-1.0": {"provider": "fal-ai", "path": "fal-ai/fast-sdxl"},
    "pollinations/flux": {"provider": "pollinations"},
}
DEFAULT_IMAGE_MODEL = "black-forest-labs/FLUX.1-schnell"

# Free OpenRouter chat model used only for the optional "randomize prompt"
# feature (same key/mechanism as ai_text.py).
RANDOMIZE_MODEL = "openai/gpt-oss-20b:free"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

HTTP_TIMEOUT = 60


class AIImage(BasePlugin):
    def generate_settings_template(self):
        template_params = super().generate_settings_template()
        template_params['api_key'] = {
            # Not hard-required: the Pollinations model needs no key at
            # all. Only needed if you pick FLUX/SDXL via fal-ai.
            "required": False,
            "service": "Hugging Face (only needed for FLUX/SDXL, not Pollinations)",
            "expected_key": "HUGGINGFACE_API_KEY"
        }
        return template_params

    def generate_image(self, settings, device_config):
        logger.info("=== AI Image Plugin: Starting image generation ===")

        text_prompt = settings.get("textPrompt", "")
        image_model = settings.get('imageModel', DEFAULT_IMAGE_MODEL)

        if image_model not in IMAGE_MODELS:
            logger.error(f"Invalid image model: {image_model}")
            raise RuntimeError("Invalid Image Model provided.")

        # Pollinations needs no key at all; fal-ai (via HF) does.
        hf_key = None
        if IMAGE_MODELS[image_model]["provider"] == "fal-ai":
            hf_key = device_config.load_env_key("HUGGINGFACE_API_KEY")
            if not hf_key:
                logger.error("Hugging Face API Key not configured")
                raise RuntimeError(
                    "Hugging Face API Key not configured. Get a free token at "
                    "huggingface.co/settings/tokens and add it as HUGGINGFACE_API_KEY. "
                    "Or pick the Pollinations model, which needs no key."
                )

        randomize_prompt = settings.get('randomizePrompt') == 'true'
        orientation = device_config.get_config("orientation")

        logger.info(f"Settings: model={image_model}, orientation={orientation}")
        logger.debug(f"Original prompt: '{text_prompt}'")

        if randomize_prompt:
            openrouter_key = device_config.load_env_key("OPEN_AI_SECRET")
            if openrouter_key:
                try:
                    text_prompt = self.fetch_image_prompt(openrouter_key, text_prompt)
                    logger.info(f"Randomized prompt: '{text_prompt}'")
                except Exception as e:
                    logger.warning(f"Prompt randomization failed, using original prompt: {e}")
            else:
                logger.warning(
                    "Randomize requested but OPEN_AI_SECRET (OpenRouter key) not set - "
                    "using the prompt as typed."
                )

        image = None
        try:
            image = self.fetch_image(hf_key, text_prompt, image_model, orientation)
            if image:
                logger.info(f"AI image generated successfully: {image.size[0]}x{image.size[1]}")
        except Exception as e:
            logger.error(f"Failed to generate image via Hugging Face: {e}")
            raise RuntimeError(f"Image generation failed: {e}")

        logger.info("=== AI Image Plugin: Image generation complete ===")

        # Apply display margins (safe area). Cover-and-crop instead of the
        # old fit-and-letterbox: fills the full safe area edge-to-edge
        # (matches the reference-quality look) rather than leaving gray
        # padding bars when the generated image's aspect ratio doesn't
        # exactly match the panel's - which read as "stretched"/off even
        # though the image itself was never distorted, just under-filled.
        if image:
            safe_area = self.get_safe_area(device_config)
            is_vertical = device_config.get_config("orientation") == "vertical"
            full_w, full_h = (480, 800) if is_vertical else (800, 480)
            background = Image.new('RGB', (full_w, full_h), color='white')

            usable_w = safe_area['usable_width']
            usable_h = safe_area['usable_height']
            start_x = safe_area['start_x']
            start_y = safe_area['start_y']

            if image.mode != 'RGB':
                image = image.convert('RGB')

            image = self._cover_crop(image, usable_w, usable_h)

            background.paste(image, (start_x, start_y))
            return background
        return image

    @staticmethod
    def _cover_crop(image, target_w, target_h):
        """Resize to fully cover (target_w, target_h), preserving aspect
        ratio (no stretching), then center-crop the overflow. Always fills
        the box completely, unlike thumbnail()'s fit-inside-with-padding."""
        src_w, src_h = image.size
        scale = max(target_w / src_w, target_h / src_h)
        new_w, new_h = round(src_w * scale), round(src_h * scale)
        image = image.resize((new_w, new_h), Image.Resampling.LANCZOS)

        left = (new_w - target_w) // 2
        top = (new_h - target_h) // 2
        return image.crop((left, top, left + target_w, top + target_h))

    # ------------------------------------------------------------------
    # Hugging Face Inference API
    # ------------------------------------------------------------------

    def fetch_image(self, hf_key, prompt, model, orientation="horizontal"):
        logger.info(f"Generating image for prompt: {prompt}, model: {model}")

        prompt += (
            ". The image should fully occupy the entire canvas without any frames, "
            "borders, or cropped areas. No blank spaces or artificial framing. "
            "Focus on simplicity, bold shapes, and strong contrast to enhance clarity "
            "and visual appeal. Avoid excessive detail or complex gradients, ensuring "
            "the design works well with flat, vibrant colors."
        )

        config = IMAGE_MODELS.get(model)
        if not config:
            raise RuntimeError(f"Unknown image model: {model}")

        width, height = (1024, 576) if orientation == "horizontal" else (576, 1024)

        if config["provider"] == "pollinations":
            return self._fetch_pollinations(prompt, width, height)
        return self._fetch_fal_ai(hf_key, prompt, config["path"], width, height)

    @staticmethod
    def _fetch_fal_ai(hf_key, prompt, provider_path, width, height):
        payload = {
            "prompt": prompt,
            "image_size": {"width": width, "height": height},
        }
        headers = {"Authorization": f"Bearer {hf_key}"}
        url = HF_ROUTER_URL.format(provider_path=provider_path)

        resp = requests.post(url, headers=headers, json=payload, timeout=HTTP_TIMEOUT)
        if resp.status_code != 200:
            raise RuntimeError(f"fal-ai (via Hugging Face) error {resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        images = data.get("images") or []
        if not images or not images[0].get("url"):
            raise RuntimeError(f"No image returned: {data}")

        img_resp = requests.get(images[0]["url"], timeout=HTTP_TIMEOUT)
        img_resp.raise_for_status()
        return Image.open(BytesIO(img_resp.content))

    @staticmethod
    def _fetch_pollinations(prompt, width, height):
        """No key, no cap. Backend model varies per request (pollinations
        picks whichever is fastest/available), so style/quality isn't as
        consistent as pinning fal-ai/FLUX directly - the trade-off for
        being genuinely free and unlimited."""
        url = POLLINATIONS_URL.format(prompt=quote(prompt))
        params = {
            "width": width,
            "height": height,
            "nologo": "true",
            "model": "flux",
            # random seed so repeated/similar prompts don't hit a cached
            # identical image every refresh
            "seed": random.randint(0, 2**31 - 1),
        }
        resp = requests.get(url, params=params, timeout=HTTP_TIMEOUT)
        if resp.status_code != 200:
            raise RuntimeError(f"Pollinations error {resp.status_code}: {resp.text[:300]}")
        return Image.open(BytesIO(resp.content))

    # ------------------------------------------------------------------
    # Optional prompt randomization via OpenRouter (free tier)
    # ------------------------------------------------------------------

    @staticmethod
    def fetch_image_prompt(openrouter_key, from_prompt=None):
        logger.info("Getting random image prompt via OpenRouter...")

        system_content = (
            "You are a creative assistant generating extremely random and unique image prompts. "
            "Avoid common themes. Focus on unexpected, unconventional, and bizarre combinations "
            "of art style, medium, subjects, time periods, and moods. No repetition. Prompts "
            "should be 20 words or less and specify random artist, movie, tv show or time period "
            "for the theme. Do not provide any headers or repeat the request, just provide the "
            "updated prompt in your response."
        )
        user_content = (
            "Give me a completely random image prompt, something unexpected and creative! "
            "Let's see what your AI mind can cook up!"
        )
        if from_prompt and from_prompt.strip():
            system_content = (
                "You are a creative assistant specializing in generating highly descriptive "
                "and unique prompts for creating images. When given a short or simple image "
                "description, your job is to rewrite it into a more detailed, imaginative, "
                "and descriptive version that captures the essence of the original while "
                "making it unique and vivid. Avoid adding irrelevant details but feel free "
                "to include creative and visual enhancements. Avoid common themes. Focus on "
                "unexpected, unconventional, and bizarre combinations of art style, medium, "
                "subjects, time periods, and moods. Do not provide any headers or repeat the "
                "request, just provide your updated prompt in the response. Prompts "
                "should be 20 words or less and specify random artist, movie, tv show or time "
                "period for the theme."
            )
            user_content = (
                f"Original prompt: \"{from_prompt}\"\n"
                "Rewrite it to make it more detailed, imaginative, and unique while staying "
                "true to the original idea. Include vivid imagery and descriptive details. "
                "Avoid changing the subject of the prompt."
            )

        resp = requests.post(
            OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {openrouter_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": RANDOMIZE_MODEL,
                "messages": [
                    {"role": "system", "content": system_content},
                    {"role": "user", "content": user_content},
                ],
                "temperature": 1,
            },
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        prompt = data["choices"][0]["message"]["content"].strip()
        logger.info(f"Generated random image prompt: {prompt}")
        return prompt
