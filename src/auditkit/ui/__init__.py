"""The local page (`eval --ui`): a server on 127.0.0.1 that shows runs and packs and starts the
command line's own runs and replays.

`views` reads (data files only, no pack code), `jobs` runs the command line (`child`) as a child
process, `app` routes and guards requests with no socket, `server` is the thin HTTP adapter.
"""
