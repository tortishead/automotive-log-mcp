"""Wiring shared by the CLI and the MCP server: config, policy, registry, redactor, ingest."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .connectors.base import ConnectorError, Registry, build_registry
from .core.align import MarkerRule, load_markers
from .core.ingest import CaseWorkers, IngestError, Ingestor, Progress, file_sha256
from .core.redact import Redactor, redactor_for_home
from .detect.engine import RuleSet, load_rules
from .policy import Policy
from .store import db, query


@dataclass
class App:
    config: Config
    policy: Policy
    registry: Registry
    redactor: Redactor
    ingestor: Ingestor
    rules: RuleSet = field(default_factory=lambda: load_rules())
    markers: list[MarkerRule] = field(default_factory=list)
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
        rules = load_rules(config.rule_dirs)  # ConfigError for an invalid rule file
        markers = load_markers(config.rule_dirs)
        redactor = redactor_for_home(config.home)
        ingestor = Ingestor(config, policy, registry, redactor, progress, markers)
        return cls(config, policy, registry, redactor, ingestor, rules, markers)

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

    def visible_source_ids(self, case: str) -> list[int] | None:
        rows = self.list_sources(case)
        return None if rows is None else [s.id for s in rows]

    def list_artifacts(self, case: str, type: str | None = None) -> list[query.ArtifactRow] | None:
        """Artifacts of a case from sources the policy does not block; None if no such case."""
        visible = self.visible_source_ids(case)
        if visible is None:
            return None
        with db.opened(self.config.case_db(case)) as conn:
            return query.artifacts(conn, type, visible)

    def read_artifact(self, case: str, cite: str) -> tuple[query.ArtifactRow, list[tuple[int, str]] | None]:
        """An artifact (with its section list) and, if the citation names a section or lines, those
        lines. Raises ValueError for a malformed citation or an unknown artifact, section or line."""
        c = query.parse_art_citation(cite)
        visible = self.visible_source_ids(case)
        if visible is None:
            raise ValueError(f"case '{case}' does not exist")
        with db.opened(self.config.case_db(case)) as conn:
            a = query.artifact(conn, c.id)
            if a is None or a.source_id not in visible:
                raise ValueError(f"no artifact art:{c.id} in case '{case}'")
            if c.section is None and c.first is None:
                return a, None
            return a, query.artifact_lines(conn, a, c)

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
