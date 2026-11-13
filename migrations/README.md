# migrations/

Alembic migrations live here.

**Not yet initialised.** For the MVP the schema is created from the SQLAlchemy
ORM metadata on startup (`Database.create_all`). Before the schema stabilises in
production, initialise Alembic and cut the first revision:

```bash
uv run alembic init migrations
# point migrations/env.py at app.database.models:Base.metadata and Settings.database_url
uv run alembic revision --autogenerate -m "initial schema"
uv run alembic upgrade head
```

After that, production deploys run `alembic upgrade head` and `create_all`
becomes test-only.
