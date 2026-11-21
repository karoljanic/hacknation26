"""Corpus ingestion: source catalogue -> collectors -> documents and passages.

Stages communicate only through the database, so each can run on its own:
``sync_catalog`` (catalogue file -> ``source``), ``IngestionService`` (collector
output -> ``document`` + ``passage``) and ``services.embedding`` (rows without
vectors -> vectors).
"""
