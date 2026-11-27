"""Exa AI-powered web search and research toolkit."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Literal, NamedTuple

from mindroom.logging_config import get_logger
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.declarations import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolFileAccess,
    ToolStatus,
    bool_config_field,
)
from mindroom.tool_system.registration import register_tool_with_metadata
from mindroom.tools.native_helpers import require_enabled_action, restrict_actions

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.tool_system.tool_function import ToolFunction

logger = get_logger(__name__)

_RESEARCH_MODEL_EFFORT: dict[str, str] = {
    "exa-research": "auto",
    "exa-research-pro": "high",
}

_LookupAction = Literal["search", "contents", "similar", "answer"]
_LOOKUP_ACTION_FIELDS: dict[str, str] = {
    "search": "query num_results category",
    "contents": "urls",
    "similar": "url num_results",
    "answer": "query text",
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

        toggles: dict[_LookupAction, bool] = {
            "search": all or enable_search,
            "contents": all or enable_get_contents,
            "similar": all or enable_find_similar,
            "answer": all or enable_answer,
        }
        self._lookup_actions: tuple[_LookupAction, ...] = tuple(
            action for action, enabled in toggles.items() if enabled
        )

        tools: list[Callable[..., Any]] = []
        if self._lookup_actions:
            tools.append(self.exa_lookup)
        if all or enable_research:
            tools.append(self.research)

        super().__init__(name="exa", tools=tools)

    def offered_function(self, function: ToolFunction) -> ToolFunction:
        """Narrow ``exa_lookup`` schema and parameters to enabled actions."""
        if function.name == "exa_lookup":
            return restrict_actions(function, self._lookup_actions, action_fields=_LOOKUP_ACTION_FIELDS)
        return function

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
                text = result.text[: self.text_length_limit] if self.text_length_limit else result.text
                result_dict["text"] = text
            if summary := getattr(result, "summary", None):
                result_dict["summary"] = summary
            if highlights := getattr(result, "highlights", None):
                result_dict["highlights"] = highlights
            parsed.append(result_dict)
        return json.dumps(parsed, indent=4, ensure_ascii=False)

    def _content_options(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "summary": self.summary,
            "livecrawl": self.livecrawl,
            "highlights": self.highlights or None,
        }

    def _filtered_options(self, num_results: int | None) -> dict[str, Any]:
        effective_num = self.num_results or (5 if num_results is None else num_results)
        return {
            **self._content_options(),
            "include_domains": self.include_domains,
            "exclude_domains": self.exclude_domains,
            "include_text": self.include_text,
            "exclude_text": self.exclude_text,
            "start_crawl_date": self.start_crawl_date,
            "end_crawl_date": self.end_crawl_date,
            "start_published_date": self.start_published_date,
            "end_published_date": self.end_published_date,
            "num_results": effective_num,
        }

    def _run_lookup(
        self,
        event: str,
        log_context: dict[str, Any],
        call: Callable[[], Any],
        *,
        serialize: Callable[[Any], str] | None = None,
    ) -> str:
        try:
            if self.show_results:
                logger.info(event, **log_context)
            raw = call()
            serialized = self._parse_results(raw) if serialize is None else serialize(raw)
            if self.show_results:
                logger.info(f"{event}_results", results=serialized)
        except (TimeoutError, self._timeout_error_cls) as exc:
            logger.exception(f"{event}_timeout", timeout=self.timeout, error=str(exc))
            return f"Error: {exc}"
        except Exception as exc:
            logger.exception(f"{event}_failed")
            return f"Error: {exc}"
        else:
            return serialized

    def exa_lookup(
        self,
        action: _LookupAction,
        query: str | None = None,
        url: str | None = None,
        urls: list[str] | None = None,
        num_results: int | None = None,
        category: str | None = None,
        text: bool = False,
    ) -> str:
        """Query the Exa API and return JSON results.

        - `search`: Web search for `query` (optional `num_results`, default 5; `category`: company, research paper, news, pdf, github, tweet, personal site, linkedin profile, financial report).
        - `contents`: Fetch content for `urls`.
        - `similar`: Find pages similar to `url` (optional `num_results`, default 5).
        - `answer`: LLM answer with citations for `query` (optional `text` to include citation full text).
        """
        if (disabled := require_enabled_action(action, self._lookup_actions)) is not None:
            return f"Error: {disabled}"
        if action == "search":
            return self._run_search(query, num_results=num_results, category=category)
        if action == "contents":
            return (
                "Error: urls is required for contents."
                if not urls
                else self._run_lookup(
                    "exa_get_contents",
                    {"urls": urls},
                    lambda: self.exa.get_contents(urls=urls, **self._content_options()),
                )
            )
        if action == "similar":
            return (
                "Error: url is required for similar."
                if not url
                else self._run_lookup(
                    "exa_find_similar",
                    {"url": url},
                    lambda: self.exa.find_similar_and_contents(url=url, **self._filtered_options(num_results)),
                )
            )
        return self._run_answer(query, text=text)

    def _run_search(self, query: str | None, *, num_results: int | None, category: str | None) -> str:
        if not query:
            return "Error: query is required for search."
        kwargs = {
            **self._filtered_options(num_results),
            "type": self.type,
            "category": self.category or category,
            "user_location": self.user_location,
            "moderation": self.moderation,
        }
        kwargs = {key: value for key, value in kwargs.items() if value is not None}
        return self._run_lookup(
            "exa_search",
            {"query": query},
            lambda: self.exa.search_and_contents(query, **kwargs),
        )

    def _run_answer(self, query: str | None, *, text: bool) -> str:
        if not query:
            return "Error: query is required for answer."
        if self.model and self.model not in {"exa", "exa-pro"}:
            msg = "Model must be either 'exa' or 'exa-pro'"
            raise ValueError(msg)
        answer_kwargs: dict[str, Any] = {"text": text}
        if self.model is not None:
            answer_kwargs["model"] = self.model

        def _serialize_answer(answer: Any) -> str:  # noqa: ANN401
            return json.dumps(
                {
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
                },
                indent=4,
            )

        return self._run_lookup(
            "exa_answer",
            {"query": query},
            lambda: self.exa.answer(query=query, **answer_kwargs),
            serialize=_serialize_answer,
        )

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
        """Perform deep research on `instructions` with optional `output_schema` JSON schema and return JSON data and citations."""
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

            terminal_run = self.exa.agent.runs.poll_until_finished(run_id)
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
        bool_config_field("enable_search", "Enable Search"),
        bool_config_field("enable_get_contents", "Enable Get Contents"),
        bool_config_field("enable_find_similar", "Enable Find Similar"),
        bool_config_field("enable_answer", "Enable Answer"),
        bool_config_field("enable_research", "Enable Research", default=False),
        bool_config_field("all", "All", default=False),
        bool_config_field("text", "Text"),
        ConfigField(
            name="text_length_limit",
            label="Text Length Limit",
            type="number",
            required=False,
            default=1000,
        ),
        bool_config_field("summary", "Summary", default=False),
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
        bool_config_field("moderation", "Moderation", default=None),
        bool_config_field("highlights", "Highlights", default=False),
        bool_config_field("show_results", "Show Results", default=False),
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
    function_names=("exa_lookup", "research"),
    action_scoped_grants=("exa_lookup",),
)
def exa_tools() -> type[Toolkit]:
    """Return Exa tools for AI-powered web search and research."""
    _require_exa()
    return _ExaTools
