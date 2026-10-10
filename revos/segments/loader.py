"""Load and validate a pack folder. Failures name the file and the field."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from revos.segments.schema import (
    CaseStudy,
    CaseStudyLibrary,
    EnrichmentConfig,
    Messaging,
    PackMeta,
    PolicyPack,
    RateCard,
    ScoringConfig,
    StagesConfig,
    check_template,
)

# template file -> variables it must use
TEMPLATES = {
    "templates/io.md.j2": ("advertiser_name", "line_items", "total", "rate_card_label"),
    "templates/agreement.md.j2": ("advertiser_name", "rate_card_label"),
}


class PackError(Exception):
    """One or more problems in a pack. `problems` is a list of (file, field, message)."""

    def __init__(self, problems: list[tuple[str, str, str]]) -> None:
        self.problems = problems
        super().__init__("\n".join(
            f"{file}: {field}: {message}" if field else f"{file}: {message}"
            for file, field, message in problems
        ))


@dataclass(frozen=True)
class Pack:
    directory: Path
    meta: PackMeta
    scoring: ScoringConfig
    enrichment: EnrichmentConfig
    stages: StagesConfig
    policy: PolicyPack
    messaging: Messaging
    case_studies: tuple[CaseStudy, ...]
    rate_card: RateCard
    templates: dict[str, str]     # "io", "agreement"

    @property
    def id(self) -> str:
        return self.meta.id


def _label(directory: Path, name: str) -> str:
    return f"{directory.name}/{name}"


def _read_yaml(directory: Path, name: str) -> Any:
    label = _label(directory, name)
    path = directory / name
    if not path.is_file():
        raise PackError([(label, "", "file is missing")])
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise PackError([(label, "", f"not valid YAML: {exc}")]) from exc
    if not isinstance(data, dict):
        raise PackError([(label, "", "must contain a mapping of settings at the top level")])
    return data


def _validate[M: BaseModel](model: type[M], data: Any, directory: Path, name: str) -> M:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        label = _label(directory, name)
        problems = []
        for err in exc.errors():
            field = ".".join(str(part) for part in err["loc"]) or "(whole file)"
            message = err["msg"].removeprefix("Value error, ")
            got = err.get("input")
            if (err["type"] != "missing" and isinstance(got, str | int | float | bool)
                    and str(got) not in message):
                message += f" (got {got!r})"
            problems.append((label, field, message))
        raise PackError(problems) from exc


def load_pack(directory: Path | str) -> Pack:
    directory = Path(directory)
    if not directory.is_dir():
        raise PackError([(str(directory), "", "pack folder not found")])

    meta = _validate(PackMeta, _read_yaml(directory, "pack.yaml"), directory, "pack.yaml")
    if meta.id != directory.name:
        raise PackError([(_label(directory, "pack.yaml"), "id",
                          f"'{meta.id}' must match the folder name '{directory.name}'")])

    scoring = _validate(ScoringConfig, _read_yaml(directory, "icp.yaml"), directory, "icp.yaml")
    enrichment = _validate(EnrichmentConfig, _read_yaml(directory, "enrichment.yaml"), directory,
                           "enrichment.yaml")
    unknown = [(_label(directory, "enrichment.yaml"), f"extractors.{i}.field",
                f"'{e.field}' is not declared in icp.yaml lead_fields")
               for i, e in enumerate(enrichment.extractors) if e.field not in scoring.lead_fields]
    if unknown:
        raise PackError(unknown)
    stages = _validate(StagesConfig, _read_yaml(directory, "stages.yaml"), directory, "stages.yaml")
    policy = _validate(PolicyPack, _read_yaml(directory, "policy.yaml"), directory, "policy.yaml")
    messaging = _validate(Messaging, _read_yaml(directory, "messaging.yaml"), directory,
                          "messaging.yaml")
    library = _validate(CaseStudyLibrary, _read_yaml(directory, "case_studies.yaml"), directory,
                        "case_studies.yaml")
    rate_card = _validate(RateCard, _read_yaml(directory, "rate_card.yaml"), directory,
                          "rate_card.yaml")

    templates: dict[str, str] = {}
    for name, required in TEMPLATES.items():
        path = directory / name
        if not path.is_file():
            raise PackError([(_label(directory, name), "", "file is missing")])
        try:
            templates[name.split("/")[1].split(".")[0]] = check_template(path.read_text(), required)
        except ValueError as exc:
            raise PackError([(_label(directory, name), "", str(exc))]) from exc

    return Pack(directory, meta, scoring, enrichment, stages, policy, messaging, tuple(library.case_studies),
                rate_card, templates)


def discover(packs_dir: Path | str) -> list[Path]:
    root = Path(packs_dir)
    return sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))


def load_all(packs_dir: Path | str) -> dict[str, Pack]:
    return {p.name: load_pack(p) for p in discover(packs_dir)}
