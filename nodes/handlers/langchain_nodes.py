"""
Native Tool Nodes

Search, lookup, and utility tool nodes implemented as native BaseNodeHandler
subclasses using direct HTTP/SDK calls. Fully compatible with supervision,
variable resolution, and the platform's control flow.
"""
import logging
import httpx
from typing import Any, TYPE_CHECKING

from .base import (
    BaseNodeHandler,
    NodeCategory,
    FieldConfig,
    FieldType,
    HandleDef,
    NodeExecutionResult,
    NodeItem,
)

if TYPE_CHECKING:
    from compiler.schemas import ExecutionContext

logger = logging.getLogger(__name__)


# ============================================================
# FREE TOOLS (No API Key Required)
# ============================================================

class WikipediaNode(BaseNodeHandler):
    """
    Search Wikipedia for information on any topic.
    Uses the public MediaWiki API — no API key required.
    """
    node_type = "wikipedia_tool"
    name = "Wikipedia"
    category = NodeCategory.INTEGRATION.value
    description = "Search Wikipedia for information on any topic"
    icon = "📖"
    color = "#000000"
    static_output_fields = ["result", "title", "url"]

    fields = [
        FieldConfig(
            name="query",
            label="Search Term",
            field_type=FieldType.STRING,
            placeholder="Artificial Intelligence",
            description="The topic to search for on Wikipedia",
            required=True,
        ),
        FieldConfig(
            name="sentences",
            label="Sentences",
            field_type=FieldType.NUMBER,
            default=5,
            required=False,
            description="Number of sentences to return in the summary",
        ),
    ]

    outputs = [
        HandleDef(id="output-0", label="Output"),
    ]

    async def execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext',
    ) -> NodeExecutionResult:
        query = config.get("query", "")
        sentences = int(config.get("sentences", 5))

        if not query:
            return NodeExecutionResult(
                success=False, error="Query is required", output_handle="output-0"
            )

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                # Step 1: Search for the page title
                search_resp = await client.get(
                    "https://en.wikipedia.org/w/api.php",
                    params={
                        "action": "query",
                        "list": "search",
                        "srsearch": query,
                        "srlimit": 1,
                        "format": "json",
                    },
                )
                search_data = search_resp.json()
                results = search_data.get("query", {}).get("search", [])

                if not results:
                    return NodeExecutionResult(
                        success=True,
                        items=[NodeItem(json={"result": f"No Wikipedia articles found for '{query}'", "title": "", "url": ""})],
                        output_handle="output-0",
                    )

                title = results[0]["title"]

                # Step 2: Get the summary extract
                summary_resp = await client.get(
                    "https://en.wikipedia.org/w/api.php",
                    params={
                        "action": "query",
                        "prop": "extracts",
                        "exsentences": sentences,
                        "exlimit": 1,
                        "titles": title,
                        "explaintext": 1,
                        "format": "json",
                    },
                )
                summary_data = summary_resp.json()
                pages = summary_data.get("query", {}).get("pages", {})
                page = next(iter(pages.values()), {})
                extract = page.get("extract", "No content available.")

                url = f"https://en.wikipedia.org/wiki/{title.replace(' ', '_')}"

                return NodeExecutionResult(
                    success=True,
                    items=[NodeItem(json={"result": extract, "title": title, "url": url})],
                    output_handle="output-0",
                )

        except Exception as e:
            return NodeExecutionResult(
                success=False, error=f"Wikipedia error: {str(e)}", output_handle="output-0"
            )


class DuckDuckGoNode(BaseNodeHandler):
    """
    Search the web using DuckDuckGo Instant Answer API.
    Free, no API key required.
    """
    node_type = "duckduckgo_tool"
    name = "DuckDuckGo Search"
    category = NodeCategory.INTEGRATION.value
    description = "Search the web using DuckDuckGo (Free)"
    icon = "🦆"
    color = "#e37151"
    static_output_fields = ["result"]

    fields = [
        FieldConfig(
            name="query",
            label="Search Term",
            field_type=FieldType.STRING,
            placeholder="What is machine learning?",
            description="The topic to search for",
            required=True,
        ),
    ]

    outputs = [
        HandleDef(id="output-0", label="Output"),
    ]

    async def execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext',
    ) -> NodeExecutionResult:
        query = config.get("query", "")

        if not query:
            return NodeExecutionResult(
                success=False, error="Query is required", output_handle="output-0"
            )

        try:
            # Use the duckduckgo_search library for text search results
            from duckduckgo_search import DDGS

            import asyncio
            def _search():
                with DDGS() as ddgs:
                    results = list(ddgs.text(query, max_results=5))
                    if not results:
                        return f"No results found for '{query}'"
                    formatted = []
                    for r in results:
                        formatted.append(f"**{r.get('title', '')}**\n{r.get('body', '')}\nURL: {r.get('href', '')}")
                    return "\n\n---\n\n".join(formatted)

            result = await asyncio.to_thread(_search)

            return NodeExecutionResult(
                success=True,
                items=[NodeItem(json={"result": result})],
                output_handle="output-0",
            )

        except ImportError:
            # Fallback to DuckDuckGo Instant Answer API
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.get(
                        "https://api.duckduckgo.com/",
                        params={"q": query, "format": "json", "no_redirect": 1},
                    )
                    data = resp.json()
                    abstract = data.get("AbstractText", "")
                    related = [t.get("Text", "") for t in data.get("RelatedTopics", [])[:5] if t.get("Text")]
                    
                    if abstract:
                        result = abstract
                    elif related:
                        result = "\n".join(related)
                    else:
                        result = f"No instant answer available for '{query}'"

                    return NodeExecutionResult(
                        success=True,
                        items=[NodeItem(json={"result": result})],
                        output_handle="output-0",
                    )
            except Exception as e:
                return NodeExecutionResult(
                    success=False, error=f"DuckDuckGo error: {str(e)}", output_handle="output-0"
                )

        except Exception as e:
            return NodeExecutionResult(
                success=False, error=f"DuckDuckGo error: {str(e)}", output_handle="output-0"
            )


class ArxivNode(BaseNodeHandler):
    """
    Search for academic papers on Arxiv.
    Uses the public Arxiv API — no API key required.
    """
    node_type = "arxiv_tool"
    name = "Arxiv Search"
    category = NodeCategory.INTEGRATION.value
    description = "Search for academic papers on Arxiv (Free)"
    icon = "📚"
    color = "#b31b1b"
    static_output_fields = ["result", "papers"]

    fields = [
        FieldConfig(
            name="query",
            label="Search Term",
            field_type=FieldType.STRING,
            placeholder="transformer architecture",
            description="The topic or paper to search for",
            required=True,
        ),
        FieldConfig(
            name="max_results",
            label="Max Results",
            field_type=FieldType.NUMBER,
            default=3,
            required=False,
            description="Maximum number of papers to return",
        ),
    ]

    outputs = [
        HandleDef(id="output-0", label="Output"),
    ]

    async def execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext',
    ) -> NodeExecutionResult:
        query = config.get("query", "")
        max_results = int(config.get("max_results", 3))

        if not query:
            return NodeExecutionResult(
                success=False, error="Query is required", output_handle="output-0"
            )

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    "http://export.arxiv.org/api/query",
                    params={
                        "search_query": f"all:{query}",
                        "start": 0,
                        "max_results": max_results,
                        "sortBy": "relevance",
                        "sortOrder": "descending",
                    },
                )

                # Parse Atom XML response
                import xml.etree.ElementTree as ET
                root = ET.fromstring(resp.text)
                ns = {"atom": "http://www.w3.org/2005/Atom"}

                papers = []
                text_parts = []

                for entry in root.findall("atom:entry", ns):
                    title = entry.findtext("atom:title", "", ns).strip().replace("\n", " ")
                    summary = entry.findtext("atom:summary", "", ns).strip().replace("\n", " ")
                    published = entry.findtext("atom:published", "", ns)[:10]
                    
                    # Get PDF link
                    link = ""
                    for l in entry.findall("atom:link", ns):
                        if l.get("title") == "pdf":
                            link = l.get("href", "")
                            break
                    if not link:
                        link_el = entry.find("atom:id", ns)
                        link = link_el.text if link_el is not None else ""

                    authors = [a.findtext("atom:name", "", ns) for a in entry.findall("atom:author", ns)]

                    papers.append({
                        "title": title,
                        "authors": authors,
                        "summary": summary,
                        "published": published,
                        "url": link,
                    })
                    text_parts.append(
                        f"**{title}**\nAuthors: {', '.join(authors)}\nPublished: {published}\n{summary}\nURL: {link}"
                    )

                result = "\n\n---\n\n".join(text_parts) if text_parts else f"No papers found for '{query}'"

                return NodeExecutionResult(
                    success=True,
                    items=[NodeItem(json={"result": result, "papers": papers})],
                    output_handle="output-0",
                )

        except Exception as e:
            return NodeExecutionResult(
                success=False, error=f"Arxiv error: {str(e)}", output_handle="output-0"
            )


# ============================================================
# PREMIUM TOOLS (API Key Required)
# ============================================================

class TavilySearchNode(BaseNodeHandler):
    """
    Search the web using Tavily.
    Uses the tavily-python SDK directly.
    """
    node_type = "tavily_search"
    name = "Tavily Search"
    category = NodeCategory.INTEGRATION.value
    description = "Search the web using Tavily"
    icon = "🔍"
    color = "#4c8bf5"
    static_output_fields = ["result", "answer", "results"]

    fields = [
        FieldConfig(
            name="credential",
            label="Tavily API Key",
            field_type=FieldType.CREDENTIAL,
            credential_type="tavily",
            description="Select your Tavily credential",
        ),
        FieldConfig(
            name="query",
            label="Search Term",
            field_type=FieldType.STRING,
            placeholder="What is the capital of France?",
            description="The topic to search for",
            required=True,
        ),
        FieldConfig(
            name="search_depth",
            label="Search Depth",
            field_type=FieldType.SELECT,
            options=["basic", "advanced"],
            default="basic",
        ),
        FieldConfig(
            name="max_results",
            label="Max Results",
            field_type=FieldType.NUMBER,
            default=5,
        ),
        FieldConfig(
            name="include_answer",
            label="Include Answer",
            field_type=FieldType.BOOLEAN,
            default=False,
        ),
        FieldConfig(
            name="include_raw_content",
            label="Include Raw Content",
            field_type=FieldType.BOOLEAN,
            default=False,
        ),
    ]

    outputs = [
        HandleDef(id="output-0", label="Output"),
    ]

    async def execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext',
    ) -> NodeExecutionResult:
        credential_id = config.get("credential")
        query = config.get("query", "")
        search_depth = config.get("search_depth", "basic")
        max_results = int(config.get("max_results", 5))
        
        # Standardize boolean inputs (handles "yes", "true", "1", "on")
        def _as_bool(val: Any) -> bool:
            if isinstance(val, bool): return val
            return str(val).lower() in ("true", "yes", "1", "on")

        include_answer = _as_bool(config.get("include_answer", False))
        include_raw_content = _as_bool(config.get("include_raw_content", False))

        if not query:
            return NodeExecutionResult(
                success=False, error="Query is required", output_handle="output-0"
            )

        # Smart Extraction: If the query looks like a long LLM response with a specific search query line
        if "**Search Query:**" in query:
            import re
            match = re.search(r"\*\*Search Query:\*\*\s*\"?(.*?)\"?(?:\n|$)", query)
            if match:
                query = match.group(1).strip()
        
        # Truncate to Tavily's hard limit of 400 characters to prevent API errors
        if len(query) > 400:
            query = query[:400]
            logger.warning(f"Tavily query was truncated to 400 characters: {query}")

        creds = await context.get_credential(credential_id) if credential_id else None
        api_key = (creds.get("apiKey") or creds.get("api_key")) if creds else None

        if not api_key:
            return NodeExecutionResult(
                success=False, error="Tavily API key not configured", output_handle="output-0"
            )

        try:
            from tavily import TavilyClient
            import asyncio

            def _search():
                client = TavilyClient(api_key=api_key)
                return client.search(
                    query=query,
                    search_depth=search_depth,
                    max_results=max_results,
                    include_answer=include_answer,
                    include_raw_content=include_raw_content,
                )

            response = await asyncio.to_thread(_search)

            # Format results as readable text
            results_list = response.get("results", [])
            text_parts = []
            for r in results_list:
                text_parts.append(
                    f"**{r.get('title', '')}**\n{r.get('content', '')}\nURL: {r.get('url', '')}\nScore: {r.get('score', '')}"
                )

            result_text = "\n\n---\n\n".join(text_parts) if text_parts else f"No results for '{query}'"
            answer = response.get("answer", "")

            return NodeExecutionResult(
                success=True,
                items=[NodeItem(json={
                    "result": result_text,
                    "answer": answer,
                    "results": results_list,
                })],
                output_handle="output-0",
            )

        except Exception as e:
            return NodeExecutionResult(
                success=False, error=f"Tavily error: {str(e)}", output_handle="output-0"
            )


class SerpApiNode(BaseNodeHandler):
    """
    Search Google using SerpAPI.
    Uses direct HTTP calls to the SerpAPI endpoint.
    """
    node_type = "serpapi_tool"
    name = "Google (SerpAPI)"
    category = NodeCategory.INTEGRATION.value
    description = "Search Google using SerpAPI (Requires API Key)"
    icon = "🔍"
    color = "#4285F4"
    static_output_fields = ["result", "organic_results"]

    fields = [
        FieldConfig(
            name="credential",
            label="SerpAPI Credential",
            field_type=FieldType.CREDENTIAL,
            credential_type="serpapi",
            description="Select your SerpAPI credential",
        ),
        FieldConfig(
            name="query",
            label="Search Term",
            field_type=FieldType.STRING,
            placeholder="latest news on AI",
            description="The topic to search for",
            required=True,
        ),
        FieldConfig(
            name="num_results",
            label="Number of Results",
            field_type=FieldType.NUMBER,
            default=5,
            required=False,
        ),
    ]

    outputs = [
        HandleDef(id="output-0", label="Output"),
    ]

    async def execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext',
    ) -> NodeExecutionResult:
        credential_id = config.get("credential")
        query = config.get("query", "")
        num_results = int(config.get("num_results", 5))

        if not query:
            return NodeExecutionResult(
                success=False, error="Query is required", output_handle="output-0"
            )

        creds = await context.get_credential(credential_id) if credential_id else None
        api_key = (creds.get("apiKey") or creds.get("api_key")) if creds else None

        if not api_key:
            return NodeExecutionResult(
                success=False, error="SerpAPI key not configured", output_handle="output-0"
            )

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    "https://serpapi.com/search",
                    params={
                        "q": query,
                        "api_key": api_key,
                        "engine": "google",
                        "num": num_results,
                    },
                )

                if resp.status_code != 200:
                    return NodeExecutionResult(
                        success=False,
                        error=f"SerpAPI error ({resp.status_code}): {resp.text}",
                        output_handle="output-0",
                    )

                data = resp.json()
                organic = data.get("organic_results", [])

                text_parts = []
                for r in organic:
                    text_parts.append(
                        f"**{r.get('title', '')}**\n{r.get('snippet', '')}\nURL: {r.get('link', '')}"
                    )

                result = "\n\n---\n\n".join(text_parts) if text_parts else f"No results for '{query}'"

                return NodeExecutionResult(
                    success=True,
                    items=[NodeItem(json={"result": result, "organic_results": organic})],
                    output_handle="output-0",
                )

        except Exception as e:
            return NodeExecutionResult(
                success=False, error=f"SerpAPI error: {str(e)}", output_handle="output-0"
            )


class OpenWeatherMapNode(BaseNodeHandler):
    """
    Fetch weather using the OpenWeatherMap API.
    """
    node_type = "openweathermap_tool"
    name = "OpenWeatherMap"
    category = NodeCategory.INTEGRATION.value
    description = "Fetch weather using OpenWeatherMap (Requires API Key)"
    icon = "⛅"
    color = "#eb6e4b"
    static_output_fields = ["result", "temperature", "description", "humidity"]

    fields = [
        FieldConfig(
            name="credential",
            label="OpenWeatherMap Credential",
            field_type=FieldType.CREDENTIAL,
            credential_type="openweathermap",
            description="Select your OpenWeatherMap credential",
        ),
        FieldConfig(
            name="query",
            label="Location",
            field_type=FieldType.STRING,
            placeholder="London, UK",
            description="The city or location to get the weather for",
            required=True,
        ),
        FieldConfig(
            name="units",
            label="Units",
            field_type=FieldType.SELECT,
            options=["metric", "imperial", "standard"],
            default="metric",
            required=False,
        ),
    ]

    outputs = [
        HandleDef(id="output-0", label="Output"),
    ]

    async def execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext',
    ) -> NodeExecutionResult:
        credential_id = config.get("credential")
        query = config.get("query", "")
        units = config.get("units", "metric")

        if not query:
            return NodeExecutionResult(
                success=False, error="Location is required", output_handle="output-0"
            )

        creds = await context.get_credential(credential_id) if credential_id else None
        api_key = (creds.get("apiKey") or creds.get("api_key")) if creds else None

        if not api_key:
            return NodeExecutionResult(
                success=False, error="OpenWeatherMap API key not configured", output_handle="output-0"
            )

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    "https://api.openweathermap.org/data/2.5/weather",
                    params={
                        "q": query,
                        "appid": api_key,
                        "units": units,
                    },
                )

                if resp.status_code != 200:
                    error_data = resp.json()
                    return NodeExecutionResult(
                        success=False,
                        error=f"OpenWeatherMap error: {error_data.get('message', resp.text)}",
                        output_handle="output-0",
                    )

                data = resp.json()
                weather_desc = data.get("weather", [{}])[0].get("description", "")
                temp = data.get("main", {}).get("temp", "")
                feels_like = data.get("main", {}).get("feels_like", "")
                humidity = data.get("main", {}).get("humidity", "")
                wind_speed = data.get("wind", {}).get("speed", "")
                city = data.get("name", query)

                unit_label = "°C" if units == "metric" else ("°F" if units == "imperial" else "K")
                result = (
                    f"Weather in {city}: {weather_desc}. "
                    f"Temperature: {temp}{unit_label} (feels like {feels_like}{unit_label}). "
                    f"Humidity: {humidity}%. Wind: {wind_speed} m/s."
                )

                return NodeExecutionResult(
                    success=True,
                    items=[NodeItem(json={
                        "result": result,
                        "temperature": temp,
                        "description": weather_desc,
                        "humidity": humidity,
                        "raw": data,
                    })],
                    output_handle="output-0",
                )

        except Exception as e:
            return NodeExecutionResult(
                success=False, error=f"OpenWeatherMap error: {str(e)}", output_handle="output-0"
            )


class WolframAlphaNode(BaseNodeHandler):
    """
    Query Wolfram Alpha for computational intelligence.
    """
    node_type = "wolfram_alpha_tool"
    name = "WolframAlpha"
    category = NodeCategory.INTEGRATION.value
    description = "Query Wolfram Alpha for computational intelligence (Requires APP ID)"
    icon = "🧮"
    color = "#ff7e00"
    static_output_fields = ["result"]

    fields = [
        FieldConfig(
            name="credential",
            label="WolframAlpha Credential",
            field_type=FieldType.CREDENTIAL,
            credential_type="wolfram_alpha",
            description="Select your WolframAlpha App ID credential",
        ),
        FieldConfig(
            name="query",
            label="Query",
            field_type=FieldType.STRING,
            placeholder="integral of x^2",
            description="The query for Wolfram Alpha (e.g. math problem)",
            required=True,
        ),
    ]

    outputs = [
        HandleDef(id="output-0", label="Output"),
    ]

    async def execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext',
    ) -> NodeExecutionResult:
        credential_id = config.get("credential")
        query = config.get("query", "")

        if not query:
            return NodeExecutionResult(
                success=False, error="Query is required", output_handle="output-0"
            )

        creds = await context.get_credential(credential_id) if credential_id else None
        app_id = (creds.get("appId") or creds.get("app_id")) if creds else None

        if not app_id:
            return NodeExecutionResult(
                success=False, error="WolframAlpha App ID not configured", output_handle="output-0"
            )

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    "https://api.wolframalpha.com/v1/result",
                    params={
                        "appid": app_id,
                        "i": query,
                    },
                )

                if resp.status_code == 501:
                    return NodeExecutionResult(
                        success=True,
                        items=[NodeItem(json={"result": "Wolfram Alpha could not understand or compute an answer for this query."})],
                        output_handle="output-0",
                    )

                if resp.status_code != 200:
                    return NodeExecutionResult(
                        success=False,
                        error=f"WolframAlpha error ({resp.status_code}): {resp.text}",
                        output_handle="output-0",
                    )

                return NodeExecutionResult(
                    success=True,
                    items=[NodeItem(json={"result": resp.text})],
                    output_handle="output-0",
                )

        except Exception as e:
            return NodeExecutionResult(
                success=False, error=f"WolframAlpha error: {str(e)}", output_handle="output-0"
            )


class BingSearchNode(BaseNodeHandler):
    """
    Search the web using Bing Search API v7.
    """
    node_type = "bing_search_tool"
    name = "Bing Search"
    category = NodeCategory.INTEGRATION.value
    description = "Search the web using Bing Search API (Requires API Key)"
    icon = "🔍"
    color = "#00809d"
    static_output_fields = ["result", "web_pages"]

    fields = [
        FieldConfig(
            name="credential",
            label="Bing Search Credential",
            field_type=FieldType.CREDENTIAL,
            credential_type="bing_search",
            description="Select your Bing Search credential",
        ),
        FieldConfig(
            name="query",
            label="Search Term",
            field_type=FieldType.STRING,
            placeholder="latest tech news",
            description="The topic to search for",
            required=True,
        ),
        FieldConfig(
            name="count",
            label="Number of Results",
            field_type=FieldType.NUMBER,
            default=5,
            required=False,
        ),
    ]

    outputs = [
        HandleDef(id="output-0", label="Output"),
    ]

    async def execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext',
    ) -> NodeExecutionResult:
        credential_id = config.get("credential")
        query = config.get("query", "")
        count = int(config.get("count", 5))

        if not query:
            return NodeExecutionResult(
                success=False, error="Query is required", output_handle="output-0"
            )

        creds = await context.get_credential(credential_id) if credential_id else None
        api_key = (creds.get("apiKey") or creds.get("api_key")) if creds else None

        if not api_key:
            return NodeExecutionResult(
                success=False, error="Bing Search API key not configured", output_handle="output-0"
            )

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    "https://api.bing.microsoft.com/v7.0/search",
                    headers={"Ocp-Apim-Subscription-Key": api_key},
                    params={"q": query, "count": count},
                )

                if resp.status_code != 200:
                    return NodeExecutionResult(
                        success=False,
                        error=f"Bing Search error ({resp.status_code}): {resp.text}",
                        output_handle="output-0",
                    )

                data = resp.json()
                web_pages = data.get("webPages", {}).get("value", [])

                text_parts = []
                for page in web_pages:
                    text_parts.append(
                        f"**{page.get('name', '')}**\n{page.get('snippet', '')}\nURL: {page.get('url', '')}"
                    )

                result = "\n\n---\n\n".join(text_parts) if text_parts else f"No results for '{query}'"

                return NodeExecutionResult(
                    success=True,
                    items=[NodeItem(json={"result": result, "web_pages": web_pages})],
                    output_handle="output-0",
                )

        except Exception as e:
            return NodeExecutionResult(
                success=False, error=f"Bing Search error: {str(e)}", output_handle="output-0"
            )
