"""Wiring shared by the CLI and the MCP server: config, policy, registry, redactor, ingest."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .connectors.base import ConnectorError, Registry, build_registry
from .core.ingest import CaseWorkers, IngestError, Ingestor, Progress, file_sha256
from .core.redact import Redactor, redactor_for_home
from .policy import Policy
from .store import db, query


@dataclass
class App:
    config: Config
    policy: Policy
    registry: Registry
    redactor: Redactor
    ingestor: Ingestor
    workers: CaseWorkers = field(init=False)

    def __post_init__(self) -> None:
        self.workers = CaseWorkers(self.ingestor)

    @classmethod
    def create(cls, config: Config, policy: Policy, progress: Progress | None = None) -> App:
        try:
            registry = build_registry(config.rule_dirs, allow_code_connectors=policy.allow_code_connectors)
        except ConnectorError as e:
            from .config import ConfigError

            raise ConfigError(str(e)) from e
        redactor = redactor_for_home(config.home)
        return cls(config, policy, registry, redactor, Ingestor(config, policy, registry, redactor, progress))

    def versions(self) -> dict[str, str]:
        return {p.name: p.version for p in self.registry.plugins()}

    def blocked(self, name: str, path: str) -> bool:
        inner = name.split("!", 1)[1].split("#", 1)[0].rsplit(":", 1)[0] if "!" in name else ""
        return self.policy.source_blocked(name, path, inner) if self.policy.blocked_sources else False

    def connect(self, case: str) -> sqlite3.Connection:
        return db.connect(self.config.case_db(case))

    def list_sources(self, case: str) -> list[query.SourceRow] | None:
        """Sources of a case, without blocked ones; None if the case does not exist."""
        path = self.config.case_db(case)
        if not path.exists():
            return None
        with db.opened(path) as conn:
            return query.sources(conn, self.versions(), self.redactor.rules_hash, self.blocked)

    def relink(self, case: str, source_id: int, new_path: Path) -> list[int]:
        path = self.config.case_db(case)
        if not path.exists():
            raise IngestError(f"case '{case}' does not exist")
        new_path = new_path.expanduser().resolve()
        with db.opened(path) as conn:
            try:
                return query.relink(conn, source_id, new_path, file_sha256)
            except ValueError as e:
                raise IngestError(str(e)) from None
