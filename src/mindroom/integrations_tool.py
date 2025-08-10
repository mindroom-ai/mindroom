"""Integration tools for various external services."""

import json
from pathlib import Path
from typing import Any

import requests
from agno.tools import Toolkit

# Base path for credentials
CREDS_PATH = Path(__file__).parent.parent.parent


def get_service_credentials(service: str) -> dict[str, Any]:
    """Get stored credentials for a service."""
    creds_file = CREDS_PATH / f"{service}_credentials.json"
    if not creds_file.exists():
        return {}

    try:
        with open(creds_file) as f:
            data: dict[str, Any] = json.load(f)
            return data
    except Exception:
        return {}


# IMDb - Real implementation with OMDb API
def search_imdb(query: str, type: str = "movie") -> str:
    """Search IMDb for movies or TV shows.

    Args:
        query: Movie/show title to search
        type: Type of content ('movie', 'series', 'episode')

    Returns:
        String with movie/show information
    """
    creds = get_service_credentials("imdb")
    if not creds or "api_key" not in creds:
        return "IMDb not configured. Please configure IMDb (OMDB API) through the MindRoom widget first."

    try:
        response = requests.get(
            "http://www.omdbapi.com/",
            params={
                "apikey": creds["api_key"],
                "s": query,
                "type": type,
            },
        )
        data = response.json()

        if data.get("Response") == "False":
            return f"No results found for '{query}'"

        results = []
        for item in data.get("Search", [])[:5]:
            results.append(f"• {item['Title']} ({item['Year']}) - {item['Type']}")

        return f"IMDb search results for '{query}':\n" + "\n".join(results)
    except Exception as e:
        return f"Error searching IMDb: {str(e)}"


def get_imdb_details(title: str) -> str:
    """Get detailed information about a movie or show.

    Args:
        title: Exact title of movie/show

    Returns:
        Detailed information string
    """
    creds = get_service_credentials("imdb")
    if not creds or "api_key" not in creds:
        return "IMDb not configured. Please configure IMDb (OMDB API) through the MindRoom widget first."

    try:
        response = requests.get(
            "http://www.omdbapi.com/",
            params={
                "apikey": creds["api_key"],
                "t": title,
                "plot": "full",
            },
        )
        data = response.json()

        if data.get("Response") == "False":
            return f"No details found for '{title}'"

        details = [
            f"Title: {data.get('Title')}",
            f"Year: {data.get('Year')}",
            f"Rating: {data.get('imdbRating')}/10",
            f"Runtime: {data.get('Runtime')}",
            f"Genre: {data.get('Genre')}",
            f"Director: {data.get('Director')}",
            f"Actors: {data.get('Actors')}",
            f"Plot: {data.get('Plot')}",
        ]

        return "\n".join(details)
    except Exception as e:
        return f"Error getting IMDb details: {str(e)}"


# Spotify - Real implementation
def get_spotify_current() -> str:
    """Get currently playing track on Spotify.

    Returns:
        String with current track information
    """
    creds = get_service_credentials("spotify")
    if not creds or "access_token" not in creds:
        return "Spotify not connected. Please connect Spotify through the MindRoom widget first."

    try:
        from spotipy import Spotify  # type: ignore[import-not-found]

        sp = Spotify(auth=creds["access_token"])
        current = sp.current_playback()

        if not current or not current.get("item"):
            return "No track currently playing on Spotify"

        track = current["item"]
        artist_names = ", ".join([a["name"] for a in track["artists"]])

        return (
            f"Currently playing on Spotify:\n"
            f"• Track: {track['name']}\n"
            f"• Artist: {artist_names}\n"
            f"• Album: {track['album']['name']}\n"
            f"• Playing: {'Yes' if current['is_playing'] else 'Paused'}"
        )
    except Exception as e:
        return f"Error getting Spotify current track: {str(e)}"


# Placeholder functions for services not yet implemented
# These return clear messages that the service is not yet available
def search_amazon(query: str, max_results: int = 5) -> str:
    """Amazon search is not yet implemented."""
    return "Amazon integration is not yet implemented. This feature is coming soon."


def search_walmart(query: str, max_results: int = 5) -> str:
    """Walmart search is not yet implemented."""
    return "Walmart integration is not yet implemented. This feature is coming soon."


def send_telegram(chat_id: str, message: str) -> str:
    """Telegram is not yet implemented."""
    return "Telegram integration is not yet implemented. This feature is coming soon."


def search_reddit(query: str, subreddit: str | None = None, limit: int = 5) -> str:
    """Reddit search is not yet implemented."""
    return "Reddit integration is not yet implemented. This feature is coming soon."


def list_dropbox_files(path: str = "/") -> str:
    """Dropbox is not yet implemented."""
    return "Dropbox integration is not yet implemented. This feature is coming soon."


def search_github_repos(query: str, limit: int = 5) -> str:
    """GitHub search is not yet implemented."""
    return "GitHub integration is not yet implemented. This feature is coming soon."


def get_facebook_page(page_id: str) -> str:
    """Facebook is not yet implemented."""
    return "Facebook integration is not yet implemented. This feature is coming soon."


class IntegrationsTools(Toolkit):
    """Toolkit for external service integrations."""

    def __init__(self) -> None:
        super().__init__(name="integrations")

    def search_amazon(self, query: str, max_results: int = 5) -> str:
        """Search Amazon for products."""
        return search_amazon(query, max_results)

    def search_imdb(self, query: str, type: str = "movie") -> str:
        """Search IMDb for movies or TV shows."""
        return search_imdb(query, type)

    def get_imdb_details(self, title: str) -> str:
        """Get detailed information about a movie or show."""
        return get_imdb_details(title)

    def get_spotify_current(self) -> str:
        """Get currently playing track on Spotify."""
        return get_spotify_current()

    def search_walmart(self, query: str, max_results: int = 5) -> str:
        """Search Walmart for products."""
        return search_walmart(query, max_results)

    def send_telegram(self, chat_id: str, message: str) -> str:
        """Send a message via Telegram bot."""
        return send_telegram(chat_id, message)

    def search_reddit(self, query: str, subreddit: str | None = None, limit: int = 5) -> str:
        """Search Reddit for posts."""
        return search_reddit(query, subreddit, limit)

    def list_dropbox_files(self, path: str = "/") -> str:
        """List files in Dropbox folder."""
        return list_dropbox_files(path)

    def search_github_repos(self, query: str, limit: int = 5) -> str:
        """Search GitHub repositories."""
        return search_github_repos(query, limit)

    def get_facebook_page(self, page_id: str) -> str:
        """Get information about a Facebook page."""
        return get_facebook_page(page_id)
