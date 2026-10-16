"""`providers.yaml`: which provider and model serve each pipeline task (spec §9.3)."""
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

# P2a ships the Ollama provider and the in-process ASR ("local"); cloud providers arrive in P2b.
KNOWN_PROVIDERS = frozenset({"ollama", "local"})
TASKS = ("caption", "asr", "embed", "chapter_summary", "recap", "chat")
# Tasks that send only text (chapter summaries, the day recap, chat answers).
TEXT_TASKS = ("chapter_summary", "recap", "chat")
LOCAL_ASR_MODELS = frozenset({"faster-whisper/large-v3-turbo-int8"})


class ProvidersConfigError(Exception):
    """providers.yaml is missing, unreadable or invalid."""


class Privacy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allow_cloud_images: bool = False
    allow_cloud_audio: bool = False


class TaskRoute(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str
    fallback: TaskRoute | None = None

    def chain(self) -> list[TaskRoute]:
        """This route followed by its fallbacks, in order."""
        routes: list[TaskRoute] = []
        route: TaskRoute | None = self
        while route is not None:
            routes.append(route)
            route = route.fallback
        return routes


class ProvidersConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    privacy: Privacy = Privacy()
    tasks: dict[str, TaskRoute]

    @model_validator(mode="after")
    def _check(self) -> ProvidersConfig:
        missing = [t for t in TASKS if t not in self.tasks]
        unknown = sorted(set(self.tasks) - set(TASKS))
        if missing or unknown:
            raise ValueError(f"tasks must be exactly {', '.join(TASKS)} (missing {missing}, unknown {unknown})")
        for task, route in self.tasks.items():
            for step in route.chain():
                if step.provider not in KNOWN_PROVIDERS:
                    raise ValueError(f"task {task}: unknown provider {step.provider!r}")
                if (step.provider == "local") != (task == "asr"):
                    raise ValueError(f"task {task}: provider 'local' serves asr only, and asr must be local")
                if task == "asr" and step.model not in LOCAL_ASR_MODELS:
                    raise ValueError(f"task asr: unsupported model {step.model!r}")
        return self

    def ollama_models(self) -> list[str]:
        """Every Ollama model named by any route or fallback, without duplicates, in task order."""
        models: list[str] = []
        for task in TASKS:
            for step in self.tasks[task].chain():
                if step.provider == "ollama" and step.model not in models:
                    models.append(step.model)
        return models

    def ollama_text_models(self) -> frozenset[str]:
        """Ollama models that serve a text task (chapters, recap, chat), fallbacks included."""
        return frozenset(
            step.model for task in TEXT_TASKS for step in self.tasks[task].chain() if step.provider == "ollama"
        )


DEFAULT_CONFIG = ProvidersConfig(
    tasks={
        "caption": TaskRoute(provider="ollama", model="minicpm-v4.6:1b"),
        "asr": TaskRoute(provider="local", model="faster-whisper/large-v3-turbo-int8"),
        "embed": TaskRoute(provider="ollama", model="qwen3-embedding:0.6b"),
        "chapter_summary": TaskRoute(provider="ollama", model="qwen3:4b"),
        "recap": TaskRoute(provider="ollama", model="qwen3:8b"),
        "chat": TaskRoute(provider="ollama", model="qwen3:8b"),
    }
)


def load_providers(path: Path | None) -> ProvidersConfig:
    """Read and validate ``path``; no path means the built-in defaults (all local)."""
    if path is None:
        return DEFAULT_CONFIG
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ProvidersConfigError(f"cannot read {path}: {exc}") from exc
    try:
        return ProvidersConfig.model_validate(data)
    except ValidationError as exc:
        raise ProvidersConfigError(f"invalid {path}: {exc}") from exc
