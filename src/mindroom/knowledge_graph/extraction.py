"""What Graphiti is told when it extracts an episode: the entity types and the extraction instructions.

Graphiti reads each type model's docstring as the type's description and extracts only nodes it can place in one of
the types; a node that fits none falls back to its generic ``Entity`` type, which MindRoom excludes. This module
imports only pydantic, so it loads without the optional Graphiti dependency.
"""

from __future__ import annotations

from pydantic import BaseModel


class Person(BaseModel):
    """A human being. Handles, Matrix IDs, and email addresses are names of a person, not separate entities."""


class Agent(BaseModel):
    """An AI assistant or bot that takes part in conversations."""


class Organization(BaseModel):
    """A company, team, institution, or other group of people."""


class Location(BaseModel):
    """A place, such as a city, a country, a building, or a room."""


class Event(BaseModel):
    """Something that happens at a time, such as a meeting, a release, a trip, or an incident."""


class Project(BaseModel):
    """A piece of work with a goal that people or agents carry out together."""


class Product(BaseModel):
    """Software, a service, a device, or a vehicle."""


class Document(BaseModel):
    """A named report, article, contract, issue, or pull request, never a file path or source file."""


class Account(BaseModel):
    """A financial or online account."""


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

# Graphiti's generic type for a node that fits none of ENTITY_TYPES; dropping it keeps paths, URLs, and the like out.
EXCLUDED_ENTITY_TYPES = ["Entity"]

_INSTRUCTIONS = """\
Name each person, agent, organization, and project by its full name when the text gives one, and use that same name every time.
Never create entities for file paths, source files, URLs, environment variables, configuration keys, commit hashes, branch names, command lines, or log files; mention them inside the facts that use them.
Date each fact by when it happened or held, as the text states or implies, never by when it was discussed. When a document states dates, use them."""


def extraction_instructions() -> str:
    """Return the instructions Graphiti appends to its entity and fact extraction prompts."""
    return _INSTRUCTIONS
