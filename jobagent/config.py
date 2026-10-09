"""Loading and validating the user's config files.

Error messages name the field and the problem, never the submitted value.
"""

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

TOKEN_RE = re.compile(r"^[a-z0-9_-]{1,100}$")
DEFAULT_OLLAMA_URL = "http://localhost:11434"


class ConfigError(Exception):
    """A problem with the user's config that the user can fix."""


def _listify(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    return [str(v).strip() for v in value if str(v).strip()]


class LLMConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["none", "ollama", "openai_compatible"] = "none"
    model: str = ""
    base_url: str = ""
    top_n: int = Field(default=30, ge=1, le=200)

    @model_validator(mode="after")
    def _check(self) -> "LLMConfig":
        if self.provider == "none":
            return self
        if not self.model.strip():
            raise ValueError("llm.model is required when llm.provider is set")
        if self.provider == "ollama" and not self.base_url:
            self.base_url = DEFAULT_OLLAMA_URL
        if self.provider == "openai_compatible" and not self.base_url:
            raise ValueError("llm.base_url is required for provider openai_compatible")
        if not self.base_url.startswith(("http://", "https://")):
            raise ValueError("llm.base_url must start with http:// or https://")
        return self


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = ""
    summary: str = ""
    target_roles: list[str] = Field(min_length=1)
    skills: list[str] = Field(default_factory=list)
    must_have_any: list[str] = Field(default_factory=list)
    avoid_in_title: list[str] = Field(default_factory=list)
    avoid_companies: list[str] = Field(default_factory=list)
    experience_years: int = Field(default=0, ge=0, le=60)
    locations: list[str] = Field(default_factory=list)
    countries: list[str] = Field(default_factory=list)
    resume_file: str = "config/resume.md"
    min_score: int = Field(default=35, ge=0, le=100)
    llm: LLMConfig = Field(default_factory=LLMConfig)

    @field_validator(
        "target_roles",
        "skills",
        "must_have_any",
        "avoid_in_title",
        "avoid_companies",
        "locations",
        "countries",
        mode="before",
    )
    @classmethod
    def _as_clean_list(cls, value: Any) -> list[str]:
        return _listify(value)

    @field_validator("countries")
    @classmethod
    def _country_codes(cls, value: list[str]) -> list[str]:
        codes = [v.upper() for v in value]
        if any(len(c) != 2 or not c.isalpha() for c in codes):
            raise ValueError("countries must be two-letter codes such as IN or US")
        return codes

    @property
    def allows_remote(self) -> bool:
        return any(loc.lower() == "remote" for loc in self.locations)

    @property
    def city_names(self) -> list[str]:
        return [loc for loc in self.locations if loc.lower() != "remote"]


class BoardConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    token: str
    company: str
    enabled: bool = True

    @field_validator("token")
    @classmethod
    def _token_shape(cls, value: str) -> str:
        value = str(value).strip().lower()
        if not TOKEN_RE.match(value):
            raise ValueError("token may only contain letters, digits, '_' and '-'")
        return value

    @field_validator("source")
    @classmethod
    def _source_lower(cls, value: str) -> str:
        return str(value).strip().lower()


class CompaniesFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    boards: list[BoardConfig] = Field(default_factory=list)

    @field_validator("boards", mode="before")
    @classmethod
    def _none_is_empty(cls, value: Any) -> Any:
        return [] if value is None else value


class Paths:
    """All locations the tool reads and writes, relative to one project folder."""

    def __init__(self, home: Path) -> None:
        self.home = home.resolve()
        self.config_dir = self.home / "config"
        self.profile = self.config_dir / "profile.yaml"
        self.companies = self.config_dir / "companies.yaml"
        self.data_dir = self.home / "data"
        self.db = self.data_dir / "jobagent.db"
        self.reports_dir = self.home / "reports"
        self.env = self.home / ".env"


def _format_errors(exc: ValidationError, source: str) -> str:
    lines = [f"Problems in {source}:"]
    for err in exc.errors():
        where = ".".join(str(p) for p in err["loc"]) or "(whole file)"
        lines.append(f"  - {where}: {err['msg']}")
    return "\n".join(lines)


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"{path.name} not found. Run `jobagent init` first.")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path.name} is not valid YAML: {type(exc).__name__}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name} must contain key: value pairs at the top level.")
    return data


def load_profile(paths: Paths) -> Profile:
    data = _read_yaml(paths.profile)
    try:
        return Profile.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(_format_errors(exc, paths.profile.name)) from exc


def load_boards(paths: Paths) -> list[BoardConfig]:
    data = _read_yaml(paths.companies)
    try:
        return CompaniesFile.model_validate(data).boards
    except ValidationError as exc:
        raise ConfigError(_format_errors(exc, paths.companies.name)) from exc


def read_resume(paths: Paths, profile: Profile) -> str:
    """Resume text, or an empty string if the file is missing."""
    candidate = Path(profile.resume_file)
    path = candidate if candidate.is_absolute() else paths.home / candidate
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines. Real environment variables win."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
