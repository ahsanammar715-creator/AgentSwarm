"""Configuration: which model plays which role, what it costs, and swarm limits.

Resolution order (later wins): built-in defaults -> ~/.agentswarm.toml ->
<repo>/agentswarm.toml -> environment variables.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path


@dataclass
class ModelConfig:
    model: str
    base_url: str
    api_key_env: str
    # USD per 1M tokens. Defaults are placeholders: set real prices in agentswarm.toml.
    input_price: float = 0.0
    cached_input_price: float = 0.0
    output_price: float = 0.0
    reasoning_effort: str | None = None
    max_tokens: int | None = None
    timeout: float = 300.0

    @property
    def api_key(self) -> str | None:
        return os.environ.get(self.api_key_env)


def _default_foreman() -> ModelConfig:
    return ModelConfig(
        model="gpt-6.1",
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
        input_price=5.0,
        cached_input_price=0.5,
        output_price=20.0,
        reasoning_effort="high",
    )


def _default_worker() -> ModelConfig:
    return ModelConfig(
        model="deepseek-chat",
        base_url="https://api.deepseek.com/v1",
        api_key_env="DEEPSEEK_API_KEY",
        input_price=0.27,
        cached_input_price=0.07,
        output_price=1.10,
    )


@dataclass
class SwarmConfig:
    foreman: ModelConfig = field(default_factory=_default_foreman)
    lead: ModelConfig = field(default_factory=_default_worker)
    worker: ModelConfig = field(default_factory=_default_worker)
    # Model that checks each task once, when it is finished. Defaults to the foreman.
    reviewer: ModelConfig | None = None
    # Optional vision model used by design mode to critique screenshots.
    # Falls back to the foreman when unset (vision is rare and short, so it's cheap).
    vision: ModelConfig | None = None

    workers: int = 4
    max_steps_per_task: int = 40
    max_task_attempts: int = 2
    per_task_review: bool = True  # the expensive model checks every finished task
    max_task_reviews: int = 2  # how many times a task may be sent back before it counts as failed
    review_rounds: int = 2  # how many times the foreman may send a cleanup team back in
    use_worktree: bool = True
    budget_usd: float | None = None  # hard stop when total spend crosses this
    shell_timeout: int = 180
    design_viewports: tuple = ((390, 844), (1440, 900))
    design_min_iterations: int = 2


_MODEL_KEYS = {f.name for f in fields(ModelConfig)}
_ROLES = ("foreman", "lead", "worker", "reviewer", "vision")


def _apply_model(cfg: SwarmConfig, role: str, data: dict) -> None:
    current = getattr(cfg, role) or _default_foreman()
    merged = {**current.__dict__, **{k: v for k, v in data.items() if k in _MODEL_KEYS}}
    setattr(cfg, role, ModelConfig(**merged))


def _apply(cfg: SwarmConfig, data: dict) -> None:
    for role in _ROLES:
        if isinstance(data.get(role), dict):
            _apply_model(cfg, role, data[role])
    swarm = data.get("swarm", {})
    for key, value in swarm.items():
        if hasattr(cfg, key) and key not in _ROLES:
            if key == "design_viewports":
                value = tuple(tuple(v) for v in value)
            setattr(cfg, key, value)


def _apply_env(cfg: SwarmConfig) -> None:
    env = os.environ
    for role in _ROLES:
        prefix = f"SWARM_{role.upper()}_"
        data = {}
        if env.get(prefix + "MODEL"):
            data["model"] = env[prefix + "MODEL"]
        if env.get(prefix + "BASE_URL"):
            data["base_url"] = env[prefix + "BASE_URL"]
        if env.get(prefix + "API_KEY_ENV"):
            data["api_key_env"] = env[prefix + "API_KEY_ENV"]
        if data:
            _apply_model(cfg, role, data)
    if env.get("SWARM_WORKERS"):
        cfg.workers = int(env["SWARM_WORKERS"])
    if env.get("SWARM_BUDGET_USD"):
        cfg.budget_usd = float(env["SWARM_BUDGET_USD"])
    if env.get("SWARM_NO_WORKTREE"):
        cfg.use_worktree = False


def load_config(repo: Path | None = None) -> SwarmConfig:
    cfg = SwarmConfig()
    candidates = [Path.home() / ".agentswarm.toml"]
    if repo is not None:
        candidates.append(Path(repo) / "agentswarm.toml")
    for path in candidates:
        if path.is_file():
            with path.open("rb") as fh:
                _apply(cfg, tomllib.load(fh))
    _apply_env(cfg)
    return cfg
