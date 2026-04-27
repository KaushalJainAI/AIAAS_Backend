import requests
import json
import logging
from django.conf import settings
from typing import Dict, List, Any

logger = logging.getLogger(__name__)

class OpenRouterService:
    BASE_URL = "https://openrouter.ai/api/v1"
    
    @classmethod
    def _get_headers(cls):
        return {
            "Authorization": f"Bearer {settings.OPEN_ROUTER_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://better-n8n.com", # Optional, for OpenRouter rankings
            "X-Title": "Better n8n Imagine",
        }

    @classmethod
    def fetch_models(cls) -> Dict[str, List[Dict[str, Any]]]:
        """
        Fetch models from OpenRouter and categorize them by modality.
        """
        try:
            # 1. Fetch regular models
            response = requests.get(f"{cls.BASE_URL}/models", headers=cls._get_headers(), timeout=10)
            response.raise_for_status()
            all_models = response.json().get("data", [])

            # 2. Fetch video models specifically (they have extra metadata)
            video_response = requests.get(f"{cls.BASE_URL}/videos/models", headers=cls._get_headers(), timeout=10)
            video_models_data = {}
            if video_response.status_code == 200:
                video_models_data = {m['id']: m for m in video_response.json().get("data", [])}

            capabilities = {
                "image": [],
                "video": [],
                "audio": []
            }

            for model in all_models:
                modalities = model.get("architecture", {}).get("modality", "")
                # OpenRouter sometimes uses output_modalities or other fields
                output_modalities = model.get("output_modalities", [])
                
                model_id = model.get("id")
                model_name = model.get("name")
                
                # Image Generation
                if "image" in output_modalities:
                    capabilities["image"].append({
                        "id": model_id,
                        "name": model_name,
                        "description": model.get("description", ""),
                        "resolutions": ["1024x1024", "1344x768", "768x1344"], # Default common ones
                        "aspect_ratios": ["1:1", "16:9", "9:16", "4:3", "3:4"],
                        "parameters": model.get("supported_parameters", [])
                    })

                # Video Generation
                if "video" in output_modalities:
                    v_meta = video_models_data.get(model_id, {})
                    capabilities["video"].append({
                        "id": model_id,
                        "name": model_name,
                        "description": model.get("description", ""),
                        "resolutions": v_meta.get("supported_resolutions", ["720p", "1080p"]),
                        "aspect_ratios": v_meta.get("supported_aspect_ratios", ["16:9", "9:16", "1:1"]),
                        "durations": [5, 10], # Typical for OpenRouter video models
                        "parameters": model.get("supported_parameters", [])
                    })

                # Audio (TTS)
                # Note: OpenRouter TTS might not always show 'audio' in output_modalities 
                # but we can check for common TTS models or look at the architecture
                if "audio" in output_modalities or "tts" in model_id.lower():
                    capabilities["audio"].append({
                        "id": model_id,
                        "name": model_name,
                        "description": model.get("description", ""),
                        "voices": ["alloy", "echo", "fable", "onyx", "nova", "shimmer"], # Standard OpenAI voices
                        "parameters": model.get("supported_parameters", [])
                    })

            return capabilities
        except Exception as e:
            logger.error(f"Error fetching OpenRouter models: {e}")
            return {"image": [], "video": [], "audio": []}

    @classmethod
    def generate_image(cls, prompt: str, model: str, config: Dict[str, Any]) -> Dict[str, Any]:
        """
        Generate an image using the chat/completions endpoint.
        """
        payload = {
            "model": model,
            "messages": [
                {"role": "user", "content": prompt}
            ],
            "modalities": ["image"],
            "image_config": {
                "aspect_ratio": config.get("aspect_ratio", "1:1"),
                "image_size": config.get("image_size", "1K")
            }
        }
        
        # Add negative prompt if provided
        if config.get("negative_prompt"):
            payload["messages"].insert(0, {"role": "system", "content": f"Negative prompt: {config['negative_prompt']}"})

        try:
            response = requests.post(
                f"{cls.BASE_URL}/chat/completions",
                headers=cls._get_headers(),
                json=payload,
                timeout=60
            )
            response.raise_for_status()
            data = response.json()
            
            choices = data.get("choices", [])
            if not choices:
                return {"error": "No response from model"}
            
            message = choices[0].get("message", {})
            images = message.get("images", [])
            
            if not images:
                return {"error": "No images generated"}
            
            # Return the first image URL (base64)
            return {
                "status": "completed",
                "url": images[0].get("image_url", {}).get("url") or images[0].get("url")
            }
        except Exception as e:
            logger.error(f"OpenRouter Image Generation Error: {e}")
            return {"error": str(e)}

    @classmethod
    def generate_video(cls, prompt: str, model: str, config: Dict[str, Any]) -> Dict[str, Any]:
        """
        Submit a video generation job.
        """
        payload = {
            "model": model,
            "prompt": prompt,
            "resolution": config.get("resolution", "720p"),
            "aspect_ratio": config.get("aspect_ratio", "16:9"),
            "duration": config.get("duration", 5),
        }
        
        try:
            response = requests.post(
                f"{cls.BASE_URL}/videos",
                headers=cls._get_headers(),
                json=payload,
                timeout=30
            )
            response.raise_for_status()
            data = response.json()
            
            return {
                "status": "pending",
                "job_id": data.get("id"),
                "polling_url": data.get("polling_url")
            }
        except Exception as e:
            logger.error(f"OpenRouter Video Generation Error: {e}")
            return {"error": str(e)}

    @classmethod
    def poll_video_status(cls, job_id: str) -> Dict[str, Any]:
        """
        Check the status of a video generation job.
        """
        try:
            response = requests.get(
                f"{cls.BASE_URL}/videos/{job_id}",
                headers=cls._get_headers(),
                timeout=10
            )
            response.raise_for_status()
            data = response.json()
            
            status = data.get("status")
            if status == "completed":
                urls = data.get("unsigned_urls", [])
                return {
                    "status": "completed",
                    "url": urls[0] if urls else None
                }
            elif status == "failed":
                return {
                    "status": "failed",
                    "error": data.get("error", "Unknown error")
                }
            
            return {"status": "pending"}
        except Exception as e:
            logger.error(f"OpenRouter Video Polling Error: {e}")
            return {"status": "error", "error": str(e)}

    @classmethod
    def generate_audio(cls, text: str, model: str, config: Dict[str, Any]) -> Dict[str, Any]:
        """
        Generate audio using the TTS endpoint.
        """
        payload = {
            "model": model,
            "input": text,
            "voice": config.get("voice", "alloy"),
            "speed": config.get("speed", 1.0)
        }
        
        try:
            response = requests.post(
                f"{cls.BASE_URL}/audio/speech",
                headers=cls._get_headers(),
                json=payload,
                timeout=30
            )
            response.raise_for_status()
            
            # OpenRouter returns raw bytes for audio. 
            # We might want to convert to base64 for the frontend.
            import base64
            audio_base64 = base64.b64encode(response.content).decode("utf-8")
            
            return {
                "status": "completed",
                "url": f"data:audio/mpeg;base64,{audio_base64}"
            }
        except Exception as e:
            logger.error(f"OpenRouter Audio Generation Error: {e}")
            return {"error": str(e)}
