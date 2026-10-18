"""What Graphiti is told when it extracts an episode: the entity types and the extraction instructions.

Graphiti reads each type model's docstring as the type's description. With its generic ``Entity`` type excluded, the
extractor must pick one of these types for a node or leave the node out, so what stays out of the graph rests on the
docstrings and the instructions, not on a filter. This module imports only pydantic, so it loads without the optional
Graphiti dependency.
"""

from __future__ import annotations

from pydantic import BaseModel


class Person(BaseModel):
    """A human being. Handles, Matrix IDs, and email addresses are names of a person, not separate entities."""


class Agent(BaseModel):
    """An AI assistant or bot that takes part in conversations, named by its name, never by its Matrix ID."""


class Organization(BaseModel):
    """A company, institution, or other group of people, never a single person or a web address."""


class Location(BaseModel):
    """A physical place, such as a city, a country, or a building, never a file path, URL, or chat room."""


class Event(BaseModel):
    """Something that happens at a time, such as a meeting, a trip, or an incident, never a commit, release tag, or branch."""


class Project(BaseModel):
    """A piece of work with a goal that people or agents carry out together, never a repository path or URL."""


class Product(BaseModel):
    """Software, a service, a device, or a vehicle, never a file, URL, command, or configuration key."""


class Document(BaseModel):
    """A named report, article, contract, issue, or pull request, never a file path or source file."""


class Account(BaseModel):
    """A financial or online account, never a person's handle, Matrix ID, or email address, which name a Person."""


ENTITY_TYPES: dict[str, type[BaseModel]] = {
    "Person": Person,
    "Agent": Agent,
    "Organization": Organization,
    "Location": Location,
    "Event": Event,
    "Project": Project,
    "Product": Product,
    "Document": Document,
    "Account": Account,
}

# Graphiti's generic type; excluding it removes it from the types the extractor chooses from, so a node is one of
# ENTITY_TYPES or left out.
EXCLUDED_ENTITY_TYPES = ["Entity"]

_INSTRUCTIONS = """\
Name each person, agent, organization, and project by its full name when the text gives one, and use that same name every time.
Never create entities for file paths, source files, URLs, environment variables, configuration keys, commit hashes, branch names, command lines, or log files; mention them inside the facts that use them.
Date each fact by when it happened or held, as the text states or implies, never by when it was discussed. When a document states dates, use them."""


def extraction_instructions() -> str:
    """Return the instructions Graphiti appends to its entity and fact extraction prompts."""
    return _INSTRUCTIONS
