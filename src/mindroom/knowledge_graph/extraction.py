"""What Graphiti is told when it extracts an episode: the entity types and the extraction instructions.

Graphiti reads each type model's docstring as the type's description, and with its generic ``Entity`` type excluded it
asks the extractor to give every node one of these types or to leave it out. That steers the model; it is not a filter.
An edge endpoint the model names without listing it as a node still becomes an untyped ``Entity`` node, because Graphiti
never checks the exclusion for it, and MindRoom does not clean such untyped entities up yet. This module imports only
pydantic, so it loads without the optional Graphiti dependency.
"""

from __future__ import annotations

from pydantic import BaseModel


class Person(BaseModel):
    """A human being. A handle, Matrix ID, or email address names whoever it belongs to, a person or an agent, and is never an entity of its own."""


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
    """A financial or online account, never a handle, Matrix ID, or email address, which names whoever it belongs to."""


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

# Graphiti's generic type; excluding it removes it from the types the extractor is asked to choose from. Graphiti does
# not check it for an edge endpoint the model names without listing it as a node, so such a node stays untyped.
EXCLUDED_ENTITY_TYPES = ["Entity"]

# Appended to Graphiti's node and edge extraction prompts.
EXTRACTION_INSTRUCTIONS = """\
Name each person, agent, organization, and project by its full name when the text gives one, and use that same name every time.
Never create entities for file paths, source files, URLs, environment variables, configuration keys, commit hashes, branch names, command lines, or log files; mention them inside the facts that use them.
Date each fact by when it happened or held, as the text states or implies, never by when it was discussed. When a document states dates, use them."""
