"""FastAPI dependencies.

Only one dependency exists: the container. Resolving services from
``request.app.state`` means the test suite can build an application with fake
Claude/Google clients and every route picks them up automatically -- no monkeypatching
of module globals.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from app.core.container import AppContainer


def get_container(request: Request) -> AppContainer:
    container = getattr(request.app.state, "container", None)
    if container is None:  # pragma: no cover - only possible with a wiring bug
        raise RuntimeError("The application container was not attached to app.state.")
    return container


ContainerDep = Annotated[AppContainer, Depends(get_container)]