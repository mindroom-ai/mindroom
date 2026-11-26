"""Exa AI-powered web search and research toolkit."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Literal, NamedTuple

from mindroom.logging_config import get_logger
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.declarations import ConfigField, SetupType, ToolCategory, ToolFileAccess, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from collections.abc import Callable

logger = get_logger(__name__)

_RESEARCH_MODEL_EFFORT: dict[str, str] = {
    "exa-research": "auto",
    "exa-research-pro": "high",
}


class _ExaImports(NamedTuple):
    exa_cls: type[Any]
    timeout_error_cls: type[Exception]


def _require_exa() -> _ExaImports:
    """Import and return ``exa_py.Exa`` and its timeout error, raising a helpful ImportError when absent."""
    try:
        from exa_py import Exa, ExaTimeoutError
    except ImportError as exc:
        msg = "`exa_py` not installed. Please install using `pip install exa_py`"
        raise ImportError(msg) from exc
    return _ExaImports(exa_cls=Exa, timeout_error_cls=ExaTimeoutError)


class _ExaTools(Toolkit):
    """Search the web, fetch page contents, find similar links, and run research via Exa."""

    def __init__(
        self,
        enable_search: bool = True,
        enable_get_contents: bool = True,
        enable_find_similar: bool = True,
        enable_answer: bool = True,
        enable_research: bool = False,
        all: bool = False,  # noqa: A002
        text: bool = True,
        text_length_limit: int = 1000,
        summary: bool = False,
        api_key: str | None = None,
        num_results: int | None = None,
        livecrawl: str | None = None,
        start_crawl_date: str | None = None,
        end_crawl_date: str | None = None,
        start_published_date: str | None = None,
        end_published_date: str | None = None,
        type: str | None = None,  # noqa: A002
        category: str | None = None,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
        include_text: list[str] | None = None,
        exclude_text: list[str] | None = None,
        user_location: str | None = None,
        moderation: bool | None = None,
        highlights: bool = False,
        show_results: bool = False,
        model: str | None = None,
        timeout: int = 30,
        research_model: Literal["exa-research", "exa-research-pro"] = "exa-research",
    ) -> None:
        exa_imports = _require_exa()
        self._timeout_error_cls = exa_imports.timeout_error_cls
        self.api_key = api_key
        if not self.api_key:
            logger.error("exa_api_key_missing")

        self.exa = exa_imports.exa_cls(self.api_key, timeout=timeout)
        self.show_results = show_results
        self.timeout = timeout
        self.text = text
        self.text_length_limit = text_length_limit
        self.summary = summary
        self.num_results = num_results
        self.livecrawl = livecrawl
        self.start_crawl_date = start_crawl_date
        self.end_crawl_date = end_crawl_date
        self.start_published_date = start_published_date
        self.end_published_date = end_published_date
        self.type = type
        self.category = category
        self.include_domains = include_domains
        self.exclude_domains = exclude_domains
        self.include_text = include_text
        self.exclude_text = exclude_text
        self.user_location = user_location
        self.moderation = moderation
        self.highlights = highlights
        self.model = model
        self.research_model = research_model

        tools: list[Callable[..., Any]] = []
        if all or enable_search:
            tools.append(self.search_exa)
        if all or enable_get_contents:
            tools.append(self.get_contents)
        if all or enable_find_similar:
            tools.append(self.find_similar)
        if all or enable_answer:
            tools.append(self.exa_answer)
        if all or enable_research:
            tools.append(self.research)

        super().__init__(name="exa", tools=tools)

    def _parse_results(self, exa_results: Any) -> str:  # noqa: ANN401
        parsed = []
        for result in exa_results.results:
            result_dict: dict[str, Any] = {"url": result.url}
            if result.title:
                result_dict["title"] = result.title
            if result.author:
                result_dict["author"] = result.author
            if result.published_date:
                result_dict["published_date"] = result.published_date
            if result.text:
                text = result.text
                if self.text_length_limit:
                    text = text[: self.text_length_limit]
                result_dict["text"] = text
            summary = getattr(result, "summary", None)
            if summary:
                result_dict["summary"] = summary
            highlights = getattr(result, "highlights", None)
            if highlights:
                result_dict["highlights"] = highlights
            parsed.append(result_dict)
        return json.dumps(parsed, indent=4, ensure_ascii=False)

    def search_exa(self, query: str, num_results: int = 5, category: str | None = None) -> str:
        """Use this function to search the web using Exa for a query.

        Args:
            query: The query to search for.
            num_results: Number of results to return. Defaults to 5.
            category: The category to filter search results. Options are "company", "research paper", "news",
                "pdf", "github", "tweet", "personal site", "linkedin profile", "financial report".

        Returns:
            The search results in JSON format.

        """
        try:
            if self.show_results:
                logger.info("exa_search", query=query)
            search_kwargs: dict[str, Any] = {
                "text": self.text,
                "summary": self.summary,
                "livecrawl": self.livecrawl,
                "num_results": self.num_results or num_results,
                "start_crawl_date": self.start_crawl_date,
                "end_crawl_date": self.end_crawl_date,
                "start_published_date": self.start_published_date,
                "end_published_date": self.end_published_date,
                "type": self.type,
                "category": self.category or category,
                "include_domains": self.include_domains,
                "exclude_domains": self.exclude_domains,
                "include_text": self.include_text,
                "exclude_text": self.exclude_text,
                "user_location": self.user_location,
                "moderation": self.moderation,
                "highlights": self.highlights or None,
            }
            search_kwargs = {key: value for key, value in search_kwargs.items() if value is not None}

            exa_results = self.exa.search_and_contents(query, **search_kwargs)
            parsed_results = self._parse_results(exa_results)
            if self.show_results:
                logger.info("exa_search_results", results=parsed_results)
        except (TimeoutError, self._timeout_error_cls) as exc:
            logger.exception("exa_search_timeout", timeout=self.timeout, error=str(exc))
            return f"Error: {exc}"
        except Exception as exc:
            logger.exception("exa_search_failed")
            return f"Error: {exc}"
        else:
            return parsed_results

    def get_contents(self, urls: list[str]) -> str:
        """Retrieve detailed content from specific URLs using the Exa API.

        Args:
            urls: A list of URLs from which to fetch content.

        Returns:
            The search results in JSON format.

        """
        query_kwargs: dict[str, Any] = {
            "text": self.text,
            "summary": self.summary,
            "livecrawl": self.livecrawl,
            "highlights": self.highlights or None,
        }
        try:
            if self.show_results:
                logger.info("exa_get_contents", urls=urls)
            exa_results = self.exa.get_contents(urls=urls, **query_kwargs)
            parsed_results = self._parse_results(exa_results)
            if self.show_results:
                logger.info("exa_get_contents_results", results=parsed_results)
        except (TimeoutError, self._timeout_error_cls) as exc:
            logger.exception("exa_get_contents_timeout", timeout=self.timeout, error=str(exc))
            return f"Error: {exc}"
        except Exception as exc:
            logger.exception("exa_get_contents_failed")
            return f"Error: {exc}"
        else:
            return parsed_results

    def find_similar(self, url: str, num_results: int = 5) -> str:
        """Find similar links to a given URL using the Exa API.

        Args:
            url: The URL for which to find similar links.
            num_results: The number of similar links to return. Defaults to 5.

        Returns:
            The search results in JSON format.

        """
        query_kwargs: dict[str, Any] = {
            "text": self.text,
            "summary": self.summary,
            "livecrawl": self.livecrawl,
            "include_domains": self.include_domains,
            "exclude_domains": self.exclude_domains,
            "include_text": self.include_text,
            "exclude_text": self.exclude_text,
            "highlights": self.highlights or None,
            "start_crawl_date": self.start_crawl_date,
            "end_crawl_date": self.end_crawl_date,
            "start_published_date": self.start_published_date,
            "end_published_date": self.end_published_date,
            "num_results": self.num_results or num_results,
        }
        try:
            if self.show_results:
                logger.info("exa_find_similar", url=url)
            exa_results = self.exa.find_similar_and_contents(url=url, **query_kwargs)
            parsed_results = self._parse_results(exa_results)
            if self.show_results:
                logger.info("exa_find_similar_results", results=parsed_results)
        except (TimeoutError, self._timeout_error_cls) as exc:
            logger.exception("exa_find_similar_timeout", timeout=self.timeout, error=str(exc))
            return f"Error: {exc}"
        except Exception as exc:
            logger.exception("exa_find_similar_failed")
            return f"Error: {exc}"
        else:
            return parsed_results

    def exa_answer(self, query: str, text: bool = False) -> str:
        """Get an LLM answer to a question informed by Exa search results.

        Args:
            query: The question or query to answer.
            text: Include full text from citation. Default is False.

        Returns:
            The answer results in JSON format with both generated answer and sources.

        """
        if self.model and self.model not in {"exa", "exa-pro"}:
            msg = "Model must be either 'exa' or 'exa-pro'"
            raise ValueError(msg)
        try:
            if self.show_results:
                logger.info("exa_answer", query=query)
            answer_kwargs: dict[str, Any] = {
                "model": self.model,
                "text": text,
            }
            answer_kwargs = {key: value for key, value in answer_kwargs.items() if value is not None}

            answer = self.exa.answer(query=query, **answer_kwargs)
            result = {
                "answer": answer.answer,
                "citations": [
                    {
                        "id": citation.id,
                        "url": citation.url,
                        "title": citation.title,
                        "published_date": citation.published_date,
                        "author": citation.author,
                        "text": citation.text if text else None,
                    }
                    for citation in answer.citations
                ],
            }
            serialized = json.dumps(result, indent=4)
            if self.show_results:
                logger.info("exa_answer_result", result=serialized)
        except (TimeoutError, self._timeout_error_cls) as exc:
            logger.exception("exa_answer_timeout", timeout=self.timeout, error=str(exc))
            return f"Error: {exc}"
        except Exception as exc:
            logger.exception("exa_answer_failed")
            return f"Error: {exc}"
        else:
            return serialized

    @staticmethod
    def _serialize_research_run(terminal_run: Any) -> dict[str, Any]:  # noqa: ANN401
        if terminal_run.status == "failed":
            error_detail = (
                terminal_run.error.message
                if terminal_run.error is not None and terminal_run.error.message
                else f"Research run {terminal_run.id} failed"
            )
            return {"error": error_detail}
        if terminal_run.status == "cancelled":
            return {"error": f"Research run {terminal_run.id} was cancelled"}

        output = terminal_run.output
        data = (
            output.structured
            if output is not None and output.structured is not None
            else (output.text if output is not None else None)
        )
        citations: dict[str, list[dict[str, Any]]] = {}
        if output is not None and output.grounding:
            for entry in output.grounding:
                citations.setdefault(entry.field, []).extend(
                    {"url": citation.url, "title": citation.title} for citation in entry.citations
                )
        return {
            "id": terminal_run.id,
            "status": terminal_run.status,
            "data": data,
            "citations": citations,
        }

    def research(self, instructions: str, output_schema: dict[str, Any] | None = None) -> str:
        """Perform deep research on a topic.

        Args:
            instructions: Research instructions.
            output_schema: JSON schema for structured output. If not provided, the API will auto-infer a schema.

        Returns:
            JSON formatted research results including data and citations.

        """
        try:
            logger.debug("exa_research_create_run", instructions=instructions, has_schema=output_schema is not None)
            create_kwargs: dict[str, Any] = {
                "query": instructions,
                "effort": _RESEARCH_MODEL_EFFORT.get(self.research_model, self.research_model),
            }
            if output_schema is not None:
                create_kwargs["output_schema"] = output_schema

            run = self.exa.agent.runs.create(**create_kwargs)
            run_id = run.id
            if self.show_results:
                logger.info("exa_research_run_created", run_id=run_id)

            poll_kwargs = {"timeout_ms": int(self.timeout * 1000)} if self.timeout is not None else {}
            terminal_run = self.exa.agent.runs.poll_until_finished(run_id, **poll_kwargs)
            result = self._serialize_research_run(terminal_run)
            if self.show_results and "error" not in result:
                logger.info("exa_research_completed", run_id=run_id)
        except (TimeoutError, self._timeout_error_cls):
            error_msg = "Research task timed out"
            logger.exception("exa_research_timeout")
            return json.dumps({"error": error_msg}, indent=4)
        except Exception as exc:
            error_msg = f"Research failed: {exc}"
            logger.exception("exa_research_failed", error=str(exc))
            return json.dumps({"error": error_msg}, indent=4)
        else:
            return json.dumps(result, indent=4)


@register_tool_with_metadata(
    name="exa",
    file_access=ToolFileAccess.NONE,
    display_name="Exa",
    description="Advanced AI-powered web search engine for research and content discovery",
    category=ToolCategory.RESEARCH,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="FaSearch",
    icon_color="text-blue-600",
    config_fields=[
        ConfigField(
            name="enable_search",
            label="Enable Search",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_get_contents",
            label="Enable Get Contents",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_find_similar",
            label="Enable Find Similar",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_answer",
            label="Enable Answer",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_research",
            label="Enable Research",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="all",
            label="All",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="text",
            label="Text",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="text_length_limit",
            label="Text Length Limit",
            type="number",
            required=False,
            default=1000,
        ),
        ConfigField(
            name="summary",
            label="Summary",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="api_key",
            env=("EXA_API_KEY",),
            label="API Key",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="num_results",
            label="Num Results",
            type="number",
            required=False,
            default=None,
        ),
        ConfigField(
            name="livecrawl",
            label="Livecrawl",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="start_crawl_date",
            label="Start Crawl Date",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="end_crawl_date",
            label="End Crawl Date",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="start_published_date",
            label="Start Published Date",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="end_published_date",
            label="End Published Date",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="type",
            label="Type",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="category",
            label="Category",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="include_domains",
            label="Include Domains",
            type="string[]",
            required=False,
            default=None,
        ),
        ConfigField(
            name="exclude_domains",
            label="Exclude Domains",
            type="string[]",
            required=False,
            default=None,
        ),
        ConfigField(
            name="include_text",
            label="Include Text",
            type="string[]",
            required=False,
            default=None,
        ),
        ConfigField(
            name="exclude_text",
            label="Exclude Text",
            type="string[]",
            required=False,
            default=None,
        ),
        ConfigField(
            name="user_location",
            label="User Location",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="moderation",
            label="Moderation",
            type="boolean",
            required=False,
            default=None,
        ),
        ConfigField(
            name="highlights",
            label="Highlights",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="show_results",
            label="Show Results",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="model",
            label="Model",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="timeout",
            label="Timeout",
            type="number",
            required=False,
            default=30,
        ),
        ConfigField(
            name="research_model",
            label="Research Model",
            type="text",
            required=False,
            default="exa-research",
        ),
    ],
    dependencies=["exa_py"],
    docs_url="https://docs.mindroom.chat/tools/web-search/#exa",
    function_names=("exa_answer", "find_similar", "get_contents", "research", "search_exa"),
)
def exa_tools() -> type[Toolkit]:
    """Return Exa tools for AI-powered web search and research."""
    _require_exa()
    return _ExaTools
