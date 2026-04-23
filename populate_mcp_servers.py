"""
Populate MCP Servers
====================
Seeds the database with pre-configured, system-wide MCP server connectors.

These are global templates (user=NULL) visible to every user.  Each user
provides their own credentials via the platform's Credential vault; the
credential_env_map / credential_header_map fields wire those secrets into
the MCP process at runtime.

Servers are created **disabled** by default so they don't attempt
connections until the user explicitly enables them and has saved the
required credentials.

Usage:
    python populate_mcp_servers.py
"""
import os
import sys
import django

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "workflow_backend.settings")
django.setup()

from django.db import transaction
from mcp_integration.models import MCPServer


MCP_SERVERS = [
    # =====================================================================
    #  1. GitHub  —  Repository management, issues, PRs, file operations
    # =====================================================================
    {
        "name": "GitHub",
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github"],
        "env": {},
        "required_credential_types": ["github"],
        "credential_env_map": {
            "GITHUB_PERSONAL_ACCESS_TOKEN": "github:token"
        },
        "credential_header_map": {},
        "setup_notes": (
            "Provides tools to search repositories, read/write files, manage issues & pull requests, "
            "create branches, and push commits.\n\n"
            "Requires a GitHub Personal Access Token (classic or fine-grained) with scopes: "
            "repo, read:org, read:user.\n\n"
            "Add your token under Settings → Credentials → GitHub Token."
        ),
    },

    # =====================================================================
    #  2. Filesystem  —  Secure local file operations
    # =====================================================================
    {
        "name": "Filesystem",
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/allowed/directory"],
        "env": {},
        "required_credential_types": [],
        "credential_env_map": {},
        "credential_header_map": {},
        "setup_notes": (
            "Secure file operations: read, write, create, list, search, and move files "
            "within allowed directories.\n\n"
            "⚠️ IMPORTANT: Edit the 'args' field and replace '/path/to/allowed/directory' "
            "with the actual directory path(s) you want the agent to access. You can specify "
            "multiple directories as separate entries in the args array.\n\n"
            "No credentials required — access is controlled by the directory whitelist."
        ),
    },

    # =====================================================================
    #  3. Memory  —  Persistent knowledge graph for agents
    # =====================================================================
    {
        "name": "Memory",
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-memory"],
        "env": {},
        "required_credential_types": [],
        "credential_env_map": {},
        "credential_header_map": {},
        "setup_notes": (
            "A knowledge graph-based persistent memory system for AI agents. "
            "Stores entities, relations, and observations that persist across conversations.\n\n"
            "No credentials required. Data is stored locally on the server."
        ),
    },

    # =====================================================================
    #  4. Brave Search  —  Web search via Brave API
    # =====================================================================
    {
        "name": "Brave Search",
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-brave-search"],
        "env": {},
        "required_credential_types": [],
        "credential_env_map": {},
        "credential_header_map": {},
        "setup_notes": (
            "Web and local search powered by the Brave Search API.\n\n"
            "Requires a Brave Search API key. Since there is no built-in 'Brave' credential type, "
            "add your API key directly to the 'Environment Vars' field:\n"
            '  {"BRAVE_API_KEY": "your-key-here"}\n\n'
            "Get a free API key at https://brave.com/search/api/"
        ),
    },

    # =====================================================================
    #  5. Fetch  —  Web content fetching and conversion
    # =====================================================================
    {
        "name": "Fetch",
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-fetch"],
        "env": {},
        "required_credential_types": [],
        "credential_env_map": {},
        "credential_header_map": {},
        "setup_notes": (
            "Fetches web pages and converts them to markdown or plain text for LLM consumption. "
            "Supports HTML, JSON, and plain text with automatic content extraction.\n\n"
            "No credentials required."
        ),
    },

    # =====================================================================
    #  6. Slack  —  Channel management, messaging, and search
    # =====================================================================
    {
        "name": "Slack",
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-slack"],
        "env": {},
        "required_credential_types": ["slack"],
        "credential_env_map": {
            "SLACK_BOT_TOKEN": "slack:token"
        },
        "credential_header_map": {},
        "setup_notes": (
            "Full Slack workspace integration: list channels, post messages, reply to threads, "
            "add reactions, search messages, and manage channel membership.\n\n"
            "Requires a Slack Bot User OAuth Token (xoxb-...) with scopes: "
            "channels:history, channels:read, chat:write, reactions:write, users:read.\n\n"
            "Optionally set SLACK_TEAM_ID in Environment Vars if you have multiple workspaces.\n\n"
            "Add your token under Settings → Credentials → Slack Token."
        ),
    },

    # =====================================================================
    #  7. Google Drive  —  File search, read, and management
    # =====================================================================
    {
        "name": "Google Drive",
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-google-drive"],
        "env": {},
        "required_credential_types": [],
        "credential_env_map": {},
        "credential_header_map": {},
        "setup_notes": (
            "Search, read, and manage files in Google Drive. Supports Docs, Sheets, "
            "and other Google Workspace files.\n\n"
            "⚠️ Requires Google OAuth2 service account credentials.\n"
            "Set the following in Environment Vars:\n"
            '  {"GOOGLE_APPLICATION_CREDENTIALS": "/path/to/service-account.json"}\n\n'
            "Follow the setup guide at https://github.com/modelcontextprotocol/servers"
        ),
    },

    # =====================================================================
    #  8. Google Maps  —  Geocoding, directions, places, and elevation
    # =====================================================================
    {
        "name": "Google Maps",
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-google-maps"],
        "env": {},
        "required_credential_types": ["gemini-api"],
        "credential_env_map": {
            "GOOGLE_MAPS_API_KEY": "gemini-api:api_key"
        },
        "credential_header_map": {},
        "setup_notes": (
            "Geocoding, reverse geocoding, route directions, elevation data, and "
            "Places search/details powered by the Google Maps Platform API.\n\n"
            "Requires a Google Cloud API key with Maps APIs enabled.\n"
            "If you have a separate Maps API key, override it in Environment Vars instead:\n"
            '  {"GOOGLE_MAPS_API_KEY": "your-maps-key-here"}\n\n'
            "Currently mapped to use your Gemini API key — ensure Maps APIs are enabled "
            "on the same GCP project."
        ),
    },

    # =====================================================================
    #  9. PostgreSQL  —  Read-only database introspection and querying
    # =====================================================================
    {
        "name": "PostgreSQL",
        "type": "stdio",
        "command": "npx",
        "args": [
            "-y", "@modelcontextprotocol/server-postgres",
            "postgresql://user:password@localhost:5432/dbname"
        ],
        "env": {},
        "required_credential_types": [],
        "credential_env_map": {},
        "credential_header_map": {},
        "setup_notes": (
            "Read-only access to PostgreSQL databases. Provides schema introspection "
            "and SQL query execution tools.\n\n"
            "⚠️ IMPORTANT: Edit the 'args' field and replace the connection string\n"
            "  postgresql://user:password@localhost:5432/dbname\n"
            "with your actual PostgreSQL connection URI.\n\n"
            "For security, all queries run in a READ ONLY transaction by default."
        ),
    },

    # =====================================================================
    # 10. SQLite  —  Local database introspection and querying
    # =====================================================================
    {
        "name": "SQLite",
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-sqlite", "/path/to/database.db"],
        "env": {},
        "required_credential_types": [],
        "credential_env_map": {},
        "credential_header_map": {},
        "setup_notes": (
            "Query and analyze local SQLite databases. Provides tools for schema inspection, "
            "SQL execution, data analysis, and business intelligence memo generation.\n\n"
            "⚠️ Edit the 'args' field and replace '/path/to/database.db' with the actual "
            "path to your SQLite database file."
        ),
    },

    # =====================================================================
    # 11. Puppeteer  —  Browser automation and web scraping
    # =====================================================================
    {
        "name": "Puppeteer",
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-puppeteer"],
        "env": {},
        "required_credential_types": [],
        "credential_env_map": {},
        "credential_header_map": {},
        "setup_notes": (
            "Browser automation via Puppeteer: navigate pages, click elements, fill forms, "
            "take screenshots, and execute JavaScript in the browser.\n\n"
            "No credentials required. Puppeteer will download Chromium automatically on first run.\n"
            "Note: requires Node.js and sufficient disk space for the Chromium binary."
        ),
    },

    # =====================================================================
    # 12. Sequential Thinking  —  Dynamic reasoning chains
    # =====================================================================
    {
        "name": "Sequential Thinking",
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-sequential-thinking"],
        "env": {},
        "required_credential_types": [],
        "credential_env_map": {},
        "credential_header_map": {},
        "setup_notes": (
            "A reasoning tool that enables dynamic, reflective problem-solving through "
            "sequential thought chains. Supports branching, revision, and hypothesis generation.\n\n"
            "No credentials required. Useful for complex multi-step analysis tasks."
        ),
    },

    # =====================================================================
    # 13. Notion  —  Page and database management
    # =====================================================================
    {
        "name": "Notion",
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "@notionhq/notion-mcp-server"],
        "env": {},
        "required_credential_types": ["notion"],
        "credential_env_map": {
            "OPENAPI_MCP_HEADERS": "notion:api_key"
        },
        "credential_header_map": {},
        "setup_notes": (
            "Official Notion MCP server: search pages, read/update page content, "
            "query databases, create pages, and manage blocks.\n\n"
            "Requires a Notion Internal Integration Token.\n"
            "1. Create an integration at https://www.notion.so/my-integrations\n"
            "2. Share the relevant pages/databases with your integration\n"
            "3. Add the token under Settings → Credentials → Notion API\n\n"
            "Note: The OPENAPI_MCP_HEADERS env var is set to your Notion API key. "
            "If the server expects a JSON header format, override the env var directly:\n"
            '  {"OPENAPI_MCP_HEADERS": "{\\"Authorization\\": \\"Bearer ntn_xxx\\"}"}'
        ),
    },

    # =====================================================================
    # 14. Tavily Search  —  AI-optimized web search
    # =====================================================================
    {
        "name": "Tavily Search",
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "tavily-mcp@latest"],
        "env": {},
        "required_credential_types": ["tavily"],
        "credential_env_map": {
            "TAVILY_API_KEY": "tavily:apiKey"
        },
        "credential_header_map": {},
        "setup_notes": (
            "AI-optimized web search, content extraction, and site mapping. "
            "Provides cleaner, more relevant results than traditional search for LLM use.\n\n"
            "Requires a Tavily API key from https://tavily.com\n\n"
            "Add your key under Settings → Credentials → Tavily API."
        ),
    },

    # =====================================================================
    # 15. Firecrawl  —  Advanced web scraping and crawling
    # =====================================================================
    {
        "name": "Firecrawl",
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "firecrawl-mcp"],
        "env": {},
        "required_credential_types": ["firecrawl"],
        "credential_env_map": {
            "FIRECRAWL_API_KEY": "firecrawl:apiKey"
        },
        "credential_header_map": {},
        "setup_notes": (
            "Advanced web scraping, crawling, and content extraction. "
            "Turns any website into clean, LLM-ready markdown. Supports JavaScript rendering, "
            "sitemaps, and batch scraping.\n\n"
            "Requires a Firecrawl API key from https://firecrawl.dev\n\n"
            "Add your key under Settings → Credentials → Firecrawl API."
        ),
    },

    # =====================================================================
    # 16. Exa Search  —  Neural / semantic web search
    # =====================================================================
    {
        "name": "Exa Search",
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "exa-mcp-server"],
        "env": {},
        "required_credential_types": [],
        "credential_env_map": {},
        "credential_header_map": {},
        "setup_notes": (
            "Neural / semantic web search powered by Exa. "
            "Finds content based on meaning, not just keywords.\n\n"
            "Requires an Exa API key. Add it directly to Environment Vars:\n"
            '  {"EXA_API_KEY": "your-key-here"}\n\n'
            "Get a key at https://exa.ai"
        ),
    },

    # =====================================================================
    # 17. Discord  —  Bot messaging and channel management
    # =====================================================================
    {
        "name": "Discord",
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "@mcp/discord-server"],
        "env": {},
        "required_credential_types": ["discord_bot"],
        "credential_env_map": {
            "DISCORD_BOT_TOKEN": "discord_bot:bot_token"
        },
        "credential_header_map": {},
        "setup_notes": (
            "Discord bot integration: send messages, read channel history, "
            "manage channels, and interact with Discord servers.\n\n"
            "Requires a Discord Bot Token with Message Content Intent enabled.\n\n"
            "Add your token under Settings → Credentials → Discord Bot Token."
        ),
    },

    # =====================================================================
    # 18. Telegram  —  Bot messaging
    # =====================================================================
    {
        "name": "Telegram",
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "@mcp/telegram-server"],
        "env": {},
        "required_credential_types": ["telegram"],
        "credential_env_map": {
            "TELEGRAM_BOT_TOKEN": "telegram:bot_token"
        },
        "credential_header_map": {},
        "setup_notes": (
            "Telegram Bot integration: send messages, read updates, manage chats.\n\n"
            "Requires a Telegram Bot Token from @BotFather.\n\n"
            "Add your token under Settings → Credentials → Telegram Bot."
        ),
    },
]


def populate():
    print("\n" + "=" * 80)
    print("[MCP] Populating MCP Server Connectors...")
    print("=" * 80 + "\n")

    created_count = 0
    updated_count = 0

    with transaction.atomic():
        for server_data in MCP_SERVERS:
            name = server_data["name"]

            defaults = {
                "type": server_data["type"],
                "command": server_data.get("command"),
                "args": server_data.get("args", []),
                "url": server_data.get("url"),
                "env": server_data.get("env", {}),
                "required_credential_types": server_data.get("required_credential_types", []),
                "credential_env_map": server_data.get("credential_env_map", {}),
                "credential_header_map": server_data.get("credential_header_map", {}),
                "setup_notes": server_data.get("setup_notes", ""),
                "enabled": server_data.get("enabled", False),
                "user": None,  # System-wide (global template)
            }

            obj, created = MCPServer.objects.update_or_create(
                name=name,
                user__isnull=True,  # Only match system-wide entries
                defaults=defaults,
            )

            if created:
                created_count += 1
                print(f"  [+] Created:  {name} ({server_data['type']})")
            else:
                updated_count += 1
                print(f"  [~] Updated:  {name} ({server_data['type']})")

    print(f"\n{'-' * 60}")
    print(f"  Created: {created_count}  |  Updated: {updated_count}  |  Total: {len(MCP_SERVERS)}")
    print(f"{'-' * 60}")
    print("\n[OK] MCP Server population complete.")
    print("     Servers are DISABLED by default. Enable them in the MCP Servers dashboard")
    print("     after adding the required credentials.\n")


if __name__ == "__main__":
    populate()
