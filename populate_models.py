import os
import django

# Set up Django environment
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'workflow_backend.settings')
django.setup()

from nodes.models import AIProvider, AIModel

def populate():
    providers = [
        {
            'name': 'OpenRouter',
            'slug': 'openrouter',
            'description': '100+ models (GPT-4, Claude, etc.)',
            'icon': '🌐',
            'models': [
                {'name': 'Auto Select', 'value': 'openrouter/auto', 'is_free': False},
                {'name': 'Free Models Only', 'value': 'openrouter/free', 'is_free': True},
                {'name': 'Llama 3.3 70B (Free)', 'value': 'meta-llama/llama-3.3-70b-instruct:free', 'is_free': True},
                {'name': 'Llama 3.1 405B (Free)', 'value': 'meta-llama/llama-3.1-405b-instruct:free', 'is_free': True},
                {'name': 'Gemini 2.0 Flash (Free)', 'value': 'google/gemini-2.0-flash-exp:free', 'is_free': True},
                {'name': 'Gemma 3 27B (Free)', 'value': 'google/gemma-3-27b-it:free', 'is_free': True},
                {'name': 'Gemma 3 12B (Free)', 'value': 'google/gemma-3-12b-it:free', 'is_free': True},
                {'name': 'Gemma 3 4B (Free)', 'value': 'google/gemma-3-4b-it:free', 'is_free': True},
                {'name': 'DeepSeek R1 (Free)', 'value': 'deepseek/deepseek-r1-0528:free', 'is_free': True},
                {'name': 'DeepSeek Chat (Free)', 'value': 'deepseek/deepseek-chat:free', 'is_free': True},
                {'name': 'Qwen 3 Coder 480B (Free)', 'value': 'qwen/qwen3-coder-480b:free', 'is_free': True},
                {'name': 'Qwen 2.5 VL 7B (Free)', 'value': 'qwen/qwen2.5-vl-7b-instruct:free', 'is_free': True},
                {'name': 'Mistral Small 3.1 (Free)', 'value': 'mistralai/mistral-small-3.1:free', 'is_free': True},
                {'name': 'Mistral 7B (Free)', 'value': 'mistralai/mistral-7b-instruct:free', 'is_free': True},
                {'name': 'Devstral 2 (Free)', 'value': 'mistralai/devstral-2512:free', 'is_free': True},
                {'name': 'Nemotron 3 Nano (Free)', 'value': 'nvidia/nemotron-3-nano-30b:free', 'is_free': True},
                {'name': 'Nemotron Nano VL (Free)', 'value': 'nvidia/nemotron-nano-vl-12b:free', 'is_free': True},
                {'name': 'Trinity Large (Free)', 'value': 'arcee-ai/trinity-large:free', 'is_free': True},
                {'name': 'Trinity Mini (Free)', 'value': 'arcee-ai/trinity-mini:free', 'is_free': True},
                {'name': 'Hermes 3 405B (Free)', 'value': 'nousresearch/hermes-3-405b:free', 'is_free': True},
                {'name': 'Step 3.5 Flash (Free)', 'value': 'stepfun/step-3.5-flash:free', 'is_free': True},
                {'name': 'Solar Pro 3 (Free)', 'value': 'upstage/solar-pro-3:free', 'is_free': True},
                {'name': 'LFM 2.5 1.2B Thinking (Free)', 'value': 'liquid-ai/lfm-2.5-1.2b-thinking:free', 'is_free': True},
                {'name': 'LFM 2.5 1.2B Instruct (Free)', 'value': 'liquid-ai/lfm-2.5-1.2b-instruct:free', 'is_free': True},
                {'name': 'MiMo V2 Flash (Free)', 'value': 'xiaomi/mimo-v2-flash:free', 'is_free': True},
                {'name': 'GLM 4.5 Air (Free)', 'value': 'z-ai/glm-4.5-air:free', 'is_free': True},
                {'name': 'GPT-4o', 'value': 'openai/gpt-4o', 'is_free': False},
                {'name': 'Claude 3.5 Sonnet', 'value': 'anthropic/claude-3.5-sonnet', 'is_free': False},
                {'name': 'DeepSeek R1 (Paid)', 'value': 'deepseek/deepseek-r1', 'is_free': False},
            ]
        },
        {
            'name': 'OpenAI',
            'slug': 'openai',
            'description': 'GPT-4o, GPT-4',
            'icon': '🤖',
            'models': [
                {'name': 'GPT-4o', 'value': 'gpt-4o', 'is_free': False},
                {'name': 'GPT-4o Mini', 'value': 'gpt-4o-mini', 'is_free': False},
                {'name': 'GPT-4 Turbo', 'value': 'gpt-4-turbo', 'is_free': False},
                {'name': 'GPT-4', 'value': 'gpt-4', 'is_free': False},
                {'name': 'GPT-3.5 Turbo', 'value': 'gpt-3.5-turbo', 'is_free': False},
            ]
        },
        {
            'name': 'Google Gemini',
            'slug': 'gemini',
            'description': 'Gemini 2.0, 1.5',
            'icon': '✨',
            'models': [
                {'name': 'Gemini 2.5 Pro', 'value': 'gemini-2.5-pro', 'is_free': False},
                {'name': 'Gemini 2.5 Flash', 'value': 'gemini-2.5-flash', 'is_free': False},
                {'name': 'Gemini 2.0 Flash', 'value': 'gemini-2.0-flash', 'is_free': False},
                {'name': 'Gemini 2.0 Flash Lite', 'value': 'gemini-2.0-flash-lite', 'is_free': False},
                {'name': 'Gemini 1.5 Pro', 'value': 'gemini-1.5-pro', 'is_free': False},
                {'name': 'Gemini 1.5 Flash', 'value': 'gemini-1.5-flash', 'is_free': False},
            ]
        },
        {
            'name': 'Ollama (Local)',
            'slug': 'ollama',
            'description': 'Run locally',
            'icon': '🦙',
            'models': [
                {'name': 'Llama 3.2', 'value': 'llama3.2', 'is_free': True},
                {'name': 'Llama 3.1', 'value': 'llama3.1', 'is_free': True},
                {'name': 'Mistral', 'value': 'mistral', 'is_free': True},
                {'name': 'Code Llama', 'value': 'codellama', 'is_free': True},
                {'name': 'Phi-3', 'value': 'phi3', 'is_free': True},
                {'name': 'Gemma 2', 'value': 'gemma2', 'is_free': True},
            ]
        },
        {
            'name': 'Perplexity',
            'slug': 'perplexity',
            'description': 'Web search AI',
            'icon': '🔍',
            'models': [
                {'name': 'Sonar', 'value': 'sonar', 'is_free': False},
                {'name': 'Sonar Pro', 'value': 'sonar-pro', 'is_free': False},
                {'name': 'Sonar Reasoning', 'value': 'sonar-reasoning', 'is_free': False},
            ]
        },
    ]

    for p_data in providers:
        p_models = p_data.pop('models')
        provider, created = AIProvider.objects.get_or_create(slug=p_data['slug'], defaults=p_data)
        if not created:
            for key, value in p_data.items():
                setattr(provider, key, value)
            provider.save()
        
        for m_data in p_models:
            AIModel.objects.update_or_create(
                value=m_data['value'],
                defaults={
                    'name': m_data['name'],
                    'provider': provider,
                    'is_free': m_data['is_free']
                }
            )
    
    print("Successfully populated AI models and providers.")

if __name__ == '__main__':
    populate()
