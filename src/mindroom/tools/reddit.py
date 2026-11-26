"""Reddit browsing, posting, and community interaction toolkit."""

from __future__ import annotations

from typing import Any, Literal

from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.declarations import ConfigField, SetupType, ToolCategory, ToolFileAccess, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata
from mindroom.tools.native_helpers import error_json, json_dumps, require_non_empty, safe_path_segment

_ReadAction = Literal["user_info", "top_posts", "subreddit_info", "trending_subreddits", "subreddit_stats"]
_WriteAction = Literal["create_post", "reply_to_post", "reply_to_comment"]
_TimeFilter = Literal["all", "day", "hour", "month", "week", "year"]
_REDDIT_URL = "https://www.reddit.com"
_REDDIT_OAUTH_URL = "https://oauth.reddit.com"
_REDDIT_SHORT_URL = "https://redd.it"


def _link_flair_templates(sub_obj: Any) -> list[dict[str, Any]]:  # noqa: ANN401
    flair_mgr = sub_obj.flair.link_templates
    if hasattr(flair_mgr, "user_selectable"):
        try:
            return [dict(item) for item in flair_mgr.user_selectable()]
        except Exception:  # noqa: S110 - fall back to moderator link_templates listing
            pass
    return [dict(item) for item in flair_mgr]


def _flair_label(template: dict[str, Any]) -> str:
    return str(template.get("flair_text") or template.get("text") or "")


def _flair_template_id(template: dict[str, Any]) -> str | None:
    raw = template.get("flair_template_id") or template.get("id")
    return str(raw) if raw else None


def _extract_reddit_id(value: str, field_name: str, *, post_url: bool = False) -> str:
    cleaned = require_non_empty(value, field_name).rstrip("/")
    parts = [segment for segment in cleaned.split("/") if segment]
    if not parts:
        msg = f"Invalid {field_name}."
        raise ValueError(msg)
    if post_url and "comments" in parts:
        idx = parts.index("comments")
        if idx + 1 < len(parts):
            return safe_path_segment(parts[idx + 1], field_name)
    return safe_path_segment(parts[-1], field_name)


class _RedditTools(Toolkit):
    """Browse Reddit users/subreddits and publish posts or replies via `praw`."""

    def __init__(
        self,
        reddit_instance: Any = None,  # noqa: ANN401
        client_id: str | None = None,
        client_secret: str | None = None,
        user_agent: str | None = None,
        username: str | None = None,
        password: str | None = None,
        allowed_subreddits: list[str] | None = None,
    ) -> None:
        if isinstance(allowed_subreddits, str):
            msg = "allowed_subreddits must be a list of subreddit names, not a string"
            raise TypeError(msg)
        self.allowed_subreddits: set[str] | None = (
            {sub.strip().lower() for sub in allowed_subreddits} if allowed_subreddits is not None else None
        )
        self.client_id = client_id.strip() if isinstance(client_id, str) and client_id.strip() else None
        self.client_secret = client_secret.strip() if isinstance(client_secret, str) and client_secret.strip() else None
        self.user_agent = (
            user_agent.strip() if isinstance(user_agent, str) and user_agent.strip() else "RedditTools v1.0"
        )
        self.username = username.strip() if isinstance(username, str) and username.strip() else None
        self.password = password.strip() if isinstance(password, str) and password.strip() else None
        self.reddit: Any = reddit_instance
        if self.reddit is None and self.client_id and self.client_secret:
            import praw

            has_user_auth = bool(self.username and self.password)
            kwargs: dict[str, Any] = {
                "site_name": "DEFAULT",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "user_agent": self.user_agent,
                "username": self.username if has_user_auth else None,
                "password": self.password if has_user_auth else None,
                "refresh_token": None,
                "redirect_uri": None,
                "reddit_url": _REDDIT_URL,
                "oauth_url": _REDDIT_OAUTH_URL,
                "short_url": _REDDIT_SHORT_URL,
                "check_for_updates": False,
            }
            self.reddit = praw.Reddit(**kwargs)
            if not has_user_auth:
                self.reddit.read_only = True
        super().__init__(name="reddit", tools=[self.reddit_read, self.reddit_write])

    def _check_user_auth(self) -> bool:
        if self.reddit is None or not (self.username and self.password):
            return False
        try:
            self.reddit.user.me()
        except Exception:
            return False
        else:
            return True

    def _validate_allowed_subreddit(self, subreddit: str) -> str | None:
        if self.allowed_subreddits is None or subreddit.lower() in self.allowed_subreddits:
            return None
        return f"Error: r/{subreddit} is not in the allowed_subreddits scope"

    def _read_dispatch(
        self,
        action: _ReadAction,
        username: str | None,
        subreddit: str | None,
        time_filter: _TimeFilter,
        limit: int,
    ) -> dict[str, Any]:
        if action == "user_info":
            user = self.reddit.redditor(safe_path_segment(require_non_empty(username, "username"), "username"))
            result: dict[str, Any] = {
                "name": user.name,
                "comment_karma": user.comment_karma,
                "link_karma": user.link_karma,
                "is_mod": user.is_mod,
                "is_gold": user.is_gold,
                "is_employee": user.is_employee,
                "created_utc": user.created_utc,
            }
        elif action == "top_posts":
            sub = safe_path_segment(require_non_empty(subreddit, "subreddit"), "subreddit")
            posts = self.reddit.subreddit(sub).top(time_filter=time_filter, limit=max(1, limit))
            result = {
                "top_posts": [
                    {
                        "id": p.id,
                        "title": p.title,
                        "score": p.score,
                        "url": p.url,
                        "selftext": p.selftext,
                        "author": str(p.author),
                        "permalink": p.permalink,
                        "created_utc": p.created_utc,
                        "subreddit": str(p.subreddit),
                        "subreddit_name_prefixed": p.subreddit_name_prefixed,
                    }
                    for p in posts
                ],
            }
        elif action == "subreddit_info":
            sub = self.reddit.subreddit(safe_path_segment(require_non_empty(subreddit, "subreddit"), "subreddit"))
            flairs = [label for template in _link_flair_templates(sub) if (label := _flair_label(template))]
            result = {
                "display_name": sub.display_name,
                "title": sub.title,
                "description": sub.description,
                "subscribers": sub.subscribers,
                "created_utc": sub.created_utc,
                "over18": sub.over18,
                "available_flairs": flairs,
                "public_description": sub.public_description,
                "url": sub.url,
            }
        elif action == "trending_subreddits":
            result = {"trending_subreddits": [s.display_name for s in self.reddit.subreddits.popular(limit=5)]}
        else:
            sub = self.reddit.subreddit(safe_path_segment(require_non_empty(subreddit, "subreddit"), "subreddit"))
            result = {
                "subreddit_stats": {
                    "display_name": sub.display_name,
                    "subscribers": sub.subscribers,
                    "active_users": sub.active_user_count,
                    "description": sub.description,
                    "created_utc": sub.created_utc,
                    "over18": sub.over18,
                    "public_description": sub.public_description,
                },
            }
        return result

    def reddit_read(
        self,
        action: _ReadAction,
        username: str | None = None,
        subreddit: str | None = None,
        time_filter: _TimeFilter = "week",
        limit: int = 10,
    ) -> str:
        """Read Reddit user profiles, posts, and subreddit metadata.

        Actions:
        - `user_info`: Profile & karma for `username`.
        - `top_posts`: Top posts in `subreddit` (`time_filter`, `limit`).
        - `subreddit_info`: Metadata & available flairs for `subreddit`.
        - `trending_subreddits`: Top 5 popular subreddits.
        - `subreddit_stats`: Subscriber & active user counts for `subreddit`.
        """
        if self.reddit is None:
            return error_json("Reddit API credentials (client_id, client_secret) are required.")
        try:
            return json_dumps(self._read_dispatch(action, username, subreddit, time_filter, limit))
        except Exception as exc:
            return error_json(str(exc))

    def _create_post(
        self,
        subreddit: str | None,
        title: str | None,
        content: str | None,
        flair: str | None,
        *,
        is_self: bool,
    ) -> str:
        clean_sub = safe_path_segment(require_non_empty(subreddit, "subreddit"), "subreddit")
        if scope_err := self._validate_allowed_subreddit(clean_sub):
            return scope_err
        clean_title = require_non_empty(title, "title")
        clean_content = require_non_empty(content, "content")
        sub_obj = self.reddit.subreddit(clean_sub)
        resolved_flair_id: str | None = None
        if flair:
            templates = _link_flair_templates(sub_obj)
            matched = next(
                (t for t in templates if flair in {_flair_label(t), _flair_template_id(t)}),
                None,
            )
            if matched is None:
                available = [label for t in templates if (label := _flair_label(t))]
                return f"Invalid flair. Available flairs: {', '.join(available)}"
            resolved_flair_id = _flair_template_id(matched)
            if resolved_flair_id is None:
                return f"Flair '{flair}' does not have a valid template ID."
        submission = (
            sub_obj.submit(title=clean_title, selftext=clean_content, flair_id=resolved_flair_id)
            if is_self
            else sub_obj.submit(title=clean_title, url=clean_content, flair_id=resolved_flair_id)
        )
        return json_dumps(
            {
                "post": {
                    "id": submission.id,
                    "title": submission.title,
                    "url": submission.url,
                    "permalink": submission.permalink,
                    "created_utc": submission.created_utc,
                    "author": str(submission.author),
                    "flair": submission.link_flair_text,
                },
            },
        )

    def _reply(
        self,
        target_id: str | None,
        content: str | None,
        subreddit: str | None,
        *,
        is_comment: bool,
    ) -> str:
        field_name = "comment_id" if is_comment else "post_id"
        item_id = _extract_reddit_id(require_non_empty(target_id, field_name), field_name, post_url=not is_comment)
        clean_content = require_non_empty(content, "content")
        target = self.reddit.comment(id=item_id) if is_comment else self.reddit.submission(id=item_id)
        if not is_comment:
            _ = target.title
            _ = target.author
        target_sub = target.subreddit.display_name
        if subreddit and target_sub.lower() != subreddit.lower():
            kind = "Comment" if is_comment else "Post"
            return f"Error: {kind} ID belongs to r/{target_sub}, not r/{subreddit}"
        if scope_err := self._validate_allowed_subreddit(target_sub):
            return scope_err
        reply = target.reply(body=clean_content)
        submission_id = target.submission.id if is_comment else target.id
        return json_dumps(
            {
                "reply": {
                    "id": reply.id,
                    "body": reply.body,
                    "score": reply.score,
                    "permalink": reply.permalink,
                    "created_utc": reply.created_utc,
                    "author": str(reply.author),
                    "parent_id": reply.parent_id,
                    "submission_id": submission_id,
                    "subreddit": str(reply.subreddit),
                },
            },
        )

    def reddit_write(
        self,
        action: _WriteAction,
        subreddit: str | None = None,
        title: str | None = None,
        content: str | None = None,
        post_id: str | None = None,
        comment_id: str | None = None,
        flair: str | None = None,
        is_self: bool = True,
    ) -> str:
        """Create Reddit posts or reply to posts and comments.

        Actions:
        - `create_post`: Submit a post in `subreddit` (`title`, `content`, optional `flair`, `is_self`).
        - `reply_to_post`: Reply to `post_id` (ID or URL) with `content` (optional `subreddit` check).
        - `reply_to_comment`: Reply to `comment_id` (ID or URL) with `content` (optional `subreddit` check).
        """
        if self.reddit is None:
            return error_json("Reddit API credentials (client_id, client_secret) are required.")
        if not self._check_user_auth():
            return "User authentication required for posting. Please provide username and password."
        try:
            if action == "create_post":
                return self._create_post(subreddit, title, content, flair, is_self=is_self)
            if action == "reply_to_post":
                return self._reply(post_id, content, subreddit, is_comment=False)
            return self._reply(comment_id, content, subreddit, is_comment=True)
        except Exception as exc:
            return error_json(str(exc))


@register_tool_with_metadata(
    name="reddit",
    file_access=ToolFileAccess.NONE,
    display_name="Reddit",
    description="Social media platform for browsing, posting, and interacting with Reddit communities",
    category=ToolCategory.SOCIAL,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="SiReddit",
    icon_color="text-orange-500",
    config_fields=[
        ConfigField(
            name="client_id",
            env=("REDDIT_CLIENT_ID",),
            label="Client ID",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="client_secret",
            env=("REDDIT_CLIENT_SECRET",),
            label="Client Secret",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="user_agent",
            env=("REDDIT_USER_AGENT",),
            label="User Agent",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="username",
            env=("REDDIT_USERNAME",),
            label="Username",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="password",
            env=("REDDIT_PASSWORD",),
            label="Password",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="allowed_subreddits",
            label="Allowed Subreddits",
            type="string[]",
            required=False,
            default=None,
            description="Restrict posts and replies to these subreddits (case-insensitive); reads are unaffected",
        ),
    ],
    dependencies=["praw"],
    docs_url="https://docs.mindroom.chat/tools/messaging-and-social/#reddit",
    function_names=("reddit_read", "reddit_write"),
    action_scoped_grants=("reddit_read", "reddit_write"),
)
def reddit_tools() -> type[Toolkit]:
    """Return Reddit tools for social media interaction."""
    return _RedditTools
