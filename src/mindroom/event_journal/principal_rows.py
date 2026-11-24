"""The base of every principal-bound store view: one backend and the principal its rows belong to.

Each operation a view exposes is a statement function of one transaction whose
first two parameters are the transaction and the principal. The view declares
which lane runs it, so the read-or-write choice is made once, where the
operation is declared, and the type checker checks every argument against the
function it is passed to.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Concatenate

    from .backend import Backend, Transaction


@dataclass(frozen=True, slots=True)
class PrincipalRows:
    """One principal's rows on the journal's database, each call one transaction."""

    _backend: Backend
    _principal_id: str

    @property
    def principal_id(self) -> str:
        """Return this view's durable principal identity."""
        return self._principal_id

    async def _read[**P, T](
        self,
        operation: Callable[Concatenate[Transaction, str, P], T],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> T:
        """Run one statement function for this principal in a read snapshot."""
        return await self._backend.read(lambda transaction: operation(transaction, self._principal_id, *args, **kwargs))

    async def _write[**P, T](
        self,
        operation: Callable[Concatenate[Transaction, str, P], T],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> T:
        """Run one statement function for this principal on the writer, ordered after every queued write."""
        return await self._backend.write(
            lambda transaction: operation(transaction, self._principal_id, *args, **kwargs),
        )

    async def _recovery_read[**P, T](
        self,
        operation: Callable[Concatenate[Transaction, str, P], T],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> T:
        """Run one statement function for this principal in the read lane reserved for shutdown handoff."""
        return await self._backend.recovery_read(
            lambda transaction: operation(transaction, self._principal_id, *args, **kwargs),
        )
