"""`python -m auditkit.ui.child …`: the command line as the local page runs it, for live runs and
replays. It is `cli.main` with the server's environment, which read `./.env` when the server
started; the child does not read it again. So a live run uses the models and the cap the page
estimated and confirmed, even if `.env` changed since (restart the server to read a change)."""

from auditkit.cli import main

raise SystemExit(main())
