import os
import django

# Set up Django environment
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'workflow_backend.settings')
django.setup()

from nodes.models import AIProvider, AIModel

def populate():
    providers = [
        # =====================================================================
        # OPENROUTER — Unified AI Gateway
        # =====================================================================
        {
            'name': 'OpenRouter',
            'slug': 'openrouter',
            'description': 'Unified AI Gateway. Access 600+ models with a single API key.',
            'icon': '🌐',
            'models': [
                # --- FREE MODELS (Feb 2026) ---
                {'name': '🆓 Auto-Router (Best Free)', 'value': 'openrouter/free', 'is_free': True},
                {'name': '🆓 Arcee Trinity Large 400B (Free)', 'value': 'arcee-ai/trinity-large-preview:free', 'is_free': True},
                {'name': '🆓 StepFun Step 3.5 Flash 196B (Free)', 'value': 'stepfun/step-3.5-flash:free', 'is_free': True},
                {'name': '🆓 GLM 4.5 Air (Free)', 'value': 'z-ai/glm-4.5-air:free', 'is_free': True},
                {'name': '🆓 NVIDIA Nemotron 3 Nano 30B (Free)', 'value': 'nvidia/nemotron-3-nano-30b-a3b:free', 'is_free': True},
                {'name': '🆓 DeepSeek R1 0528 (Free, Latest)', 'value': 'deepseek/deepseek-r1-0528:free', 'is_free': True},
                {'name': '🆓 Qwen3 235B A22B Thinking (Free)', 'value': 'qwen/qwen3-235b-a22b-thinking-2507', 'is_free': True},
                {'name': '🆓 Llama 3.3 70B (Free)', 'value': 'meta-llama/llama-3.3-70b-instruct:free', 'is_free': True},
                {'name': '🆓 Gemini 2.0 Flash (Free)', 'value': 'google/gemini-2.0-flash-001:free', 'is_free': True},
                {'name': '🆓 Gemma 3 27B (Free)', 'value': 'google/gemma-3-27b-it:free', 'is_free': True},
                
                # --- TOP PAID / FRONTIER ---
                {'name': '💰 MiniMax M2.5 (🥇 Best Coding)', 'value': 'minimax/minimax-m2.5', 'is_free': False},
                {'name': '💰 MoonshotAI Kimi K2.5 (Visual Coding)', 'value': 'moonshotai/kimi-k2.5', 'is_free': False},
                {'name': '💰 Google Gemini 3 Flash Preview', 'value': 'google/gemini-3-flash-preview', 'is_free': False},
                {'name': '💰 DeepSeek V3.2 (GPT-5 class)', 'value': 'deepseek/deepseek-v3.2', 'is_free': False},
                {'name': '💰 xAI Grok 4.1 Fast (2M ctx)', 'value': 'x-ai/grok-4.1-fast', 'is_free': False},
                {'name': '💰 Anthropic Claude Opus 4.6 (Best for Agents)', 'value': 'anthropic/claude-opus-4.6', 'is_free': False},
                {'name': '💰 Anthropic Claude Sonnet 4.5', 'value': 'anthropic/claude-sonnet-4.5', 'is_free': False},
                {'name': '💰 Anthropic Claude Haiku 4.5', 'value': 'anthropic/claude-haiku-4.5', 'is_free': False},
                {'name': '💰 OpenAI o1 (Reasoning)', 'value': 'openai/o1', 'is_free': False},
                {'name': '💰 OpenAI o3-mini', 'value': 'openai/o3-mini', 'is_free': False},
                {'name': '💰 OpenAI GPT-4o', 'value': 'openai/gpt-4o', 'is_free': False},
                {'name': '💰 OpenAI GPT-4o Mini', 'value': 'openai/gpt-4o-mini', 'is_free': False},
            ]
        },
        # =====================================================================
        # OLLAMA — Private local models
        # =====================================================================
        {
            'name': 'Ollama (Local)',
            'slug': 'ollama',
            'description': 'Run private, local AI models on your own hardware.',
            'icon': '🦙',
            'models': [
                {'name': '🆓 DeepSeek R1 8B (Recommended)', 'value': 'deepseek-r1:8b', 'is_free': True},
                {'name': '🆓 DeepSeek R1 32B', 'value': 'deepseek-r1:32b', 'is_free': True},
                {'name': '🆓 DeepSeek R1 1.5B (Edge)', 'value': 'deepseek-r1:1.5b', 'is_free': True},
                {'name': '🆓 Llama 3.3 70B', 'value': 'llama3.3:latest', 'is_free': True},
                {'name': '🆓 Llama 3.2 3B', 'value': 'llama3.2:latest', 'is_free': True},
                {'name': '🆓 Llama 3.2 1B', 'value': 'llama3.2:1b', 'is_free': True},
                {'name': '🆓 Qwen 2.5 Coder 32B', 'value': 'qwen2.5-coder:32b', 'is_free': True},
                {'name': '🆓 Qwen 2.5 7B', 'value': 'qwen2.5:latest', 'is_free': True},
                {'name': '🆓 Mistral Nemo 12B', 'value': 'mistral-nemo:latest', 'is_free': True},
                {'name': '🆓 Phi-4 (Microsoft)', 'value': 'phi4:latest', 'is_free': True},
            ]
        },
        # =====================================================================
        # OPENAI — Native API
        # =====================================================================
        {
            'name': 'OpenAI',
            'slug': 'openai',
            'description': 'Direct connection to OpenAI API.',
            'icon': '🤖',
            'models': [
                {'name': '💰 o1', 'value': 'o1', 'is_free': False},
                {'name': '💰 o1-mini', 'value': 'o1-mini', 'is_free': False},
                {'name': '💰 o3-mini', 'value': 'o3-mini', 'is_free': False},
                {'name': '💰 gpt-4o', 'value': 'gpt-4o', 'is_free': False},
                {'name': '💰 gpt-4o-mini', 'value': 'gpt-4o-mini', 'is_free': False},
                {'name': '💰 gpt-4-turbo', 'value': 'gpt-4-turbo', 'is_free': False},
            ]
        },
        # =====================================================================
        # GOOGLE GEMINI — Native API
        # =====================================================================
        {
            'name': 'Google Gemini',
            'slug': 'gemini',
            'description': 'Google AI Studio models with massive context windows.',
            'icon': '✨',
            'models': [
                {'name': '🆓 Gemini 3 Flash Preview (Free Tier, 1M ctx)', 'value': 'gemini-3-flash-preview', 'is_free': True},
                {'name': '🆓 Gemini 2.5 Flash (Free Tier)', 'value': 'gemini-2.5-flash', 'is_free': True},
                {'name': '💰 Gemini 2.5 Flash-Lite (Fastest)', 'value': 'gemini-2.5-flash-lite', 'is_free': False},
                {'name': '💰 Gemini 2.5 Pro (Best reasoning)', 'value': 'gemini-2.5-pro', 'is_free': False},
            ]
        },
        # =====================================================================
        # ANTHROPIC — Native API
        # =====================================================================
        {
            'name': 'Anthropic',
            'slug': 'anthropic',
            'description': 'Claude series by Anthropic. Best for coding agents and long tasks.',
            'icon': '🎭',
            'models': [
                {'name': '💰 Claude Opus 4.6 (Best for Agents)', 'value': 'claude-opus-4-6', 'is_free': False},
                {'name': '💰 Claude Sonnet 4.6 (Intelligence)', 'value': 'claude-sonnet-4-6', 'is_free': False},
                {'name': '💰 Claude Haiku 4.5 (Fastest)', 'value': 'claude-haiku-4-5', 'is_free': False},
                {'name': '💰 Claude 3.5 Sonnet (Legacy)', 'value': 'claude-3-5-sonnet-20241022', 'is_free': False},
            ]
        },
        # =====================================================================
        # PERPLEXITY — Native API
        # =====================================================================
        {
            'name': 'Perplexity',
            'slug': 'perplexity',
            'description': 'Real-time web search and citation enabled AI.',
            'icon': '🔍',
            'models': [
                {'name': '💰 Sonar Pro (Advanced Search)', 'value': 'sonar-pro', 'is_free': False},
                {'name': '💰 Sonar (Lightweight Search)', 'value': 'sonar', 'is_free': False},
                {'name': '💰 Sonar Reasoning Pro (CoT)', 'value': 'sonar-reasoning-pro', 'is_free': False},
                {'name': '💰 Sonar Deep Research (Exhaustive)', 'value': 'sonar-deep-research', 'is_free': False},
            ]
        },
        # =====================================================================
        # DEEPSEEK — Native API
        # =====================================================================
        {
            'name': 'DeepSeek',
            'slug': 'deepseek',
            'description': 'Official DeepSeek API. Best for reasoning and coding tasks.',
            'icon': '🧠',
            'models': [
                {'name': '💰 DeepSeek Chat (V3)', 'value': 'deepseek-chat', 'is_free': False},
                {'name': '💰 DeepSeek Reasoner (R1)', 'value': 'deepseek-reasoner', 'is_free': False},
            ]
        }
    ]

    print("\n" + "="*80)
    print("🚀 Synchronizing AI Models Database (Curated Feb 2026 Edition)...")
    print("="*80 + "\n")

    # Clear existing to ensure fresh state
    AIModel.objects.all().delete()
    AIProvider.objects.all().delete()

    for p_data in providers:
        p_models = p_data.pop('models')
        provider, created = AIProvider.objects.get_or_create(slug=p_data['slug'], defaults=p_data)
        
        print(f"✅ Provider: {provider.name}")
        
        for m_data in p_models:
            AIModel.objects.create(
                provider=provider,
                name=m_data['name'],
                value=m_data['value'],
                is_free=m_data['is_free']
            )
            badge = "🆓" if m_data['is_free'] else "💰"
            print(f"   ├─ {badge} {m_data['name']}")
        print("   └─ Done.\n")
        
    print("="*80)
    print(f"✅ Successfully synchronized all models.")
    print("="*80)

if __name__ == '__main__':
    populate()
