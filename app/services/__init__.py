"""Application services: business rules, orchestration and side-effect control.

Dependency direction is one-way: ``api -> services -> integrations/db -> core``.
Services never import from ``app.api``, and integrations never import services.
"""