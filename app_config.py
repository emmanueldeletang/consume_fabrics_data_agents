import json
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


class ConfigError(RuntimeError):
    pass


def _required_env(*names: str) -> str:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    env_names = ", ".join(names)
    raise ConfigError(f"Missing required environment variable. Set one of: {env_names}")


def _load_agents_config(path: Path) -> dict[str, dict]:
    if not path.exists():
        raise ConfigError(
            f"Agents config file not found: {path}. "
            "Create it from config/agents.example.json and set AGENTS_CONFIG_PATH if needed."
        )

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid JSON in agents config file {path}: {exc}") from exc

    if not isinstance(data, dict) or not data:
        raise ConfigError("Agents config must be a non-empty JSON object.")

    required_fields = {"name", "description", "capabilities", "sample_questions", "base_url"}
    for key, value in data.items():
        if not isinstance(value, dict):
            raise ConfigError(f"Agent '{key}' must be a JSON object.")
        missing = required_fields - set(value.keys())
        if missing:
            raise ConfigError(f"Agent '{key}' is missing required fields: {sorted(missing)}")

        base_url = str(value.get("base_url", "")).strip()
        if not base_url:
            raise ConfigError(f"Agent '{key}' has empty 'base_url'.")
        if "<" in base_url or ">" in base_url:
            raise ConfigError(
                f"Agent '{key}' has placeholder tokens in base_url: {base_url}. "
                "Replace placeholders with a real Fabric Data Agent endpoint."
            )
        if not base_url.startswith("https://api.fabric.microsoft.com/v1/workspaces/"):
            raise ConfigError(
                f"Agent '{key}' has invalid base_url. Expected Fabric endpoint, got: {base_url}"
            )

    return data


def load_app_config() -> dict:
    tenant_id = _required_env("FABRIC_TENANT_ID", "TENANT_ID")
    client_id = _required_env("FABRIC_ADMIN_CLIENT_ID", "ADMIN_CLIENT_ID")
    client_secret = _required_env("FABRIC_ADMIN_CLIENT_SECRET", "ADMIN_CLIENT_SECRET")

    fabric_scope = os.getenv("FABRIC_SCOPE", "https://api.fabric.microsoft.com/.default")
    api_version = os.getenv("FABRIC_API_VERSION", "2024-05-01-preview")

    agents_path = Path(os.getenv("AGENTS_CONFIG_PATH", "config/agents.json"))
    agents = _load_agents_config(agents_path)

    default_agent = os.getenv("DEFAULT_AGENT_KEY") or next(iter(agents.keys()))
    if default_agent not in agents:
        raise ConfigError(
            f"DEFAULT_AGENT_KEY '{default_agent}' is not present in {agents_path}. "
            f"Available agents: {', '.join(agents.keys())}"
        )

    return {
        "tenant_id": tenant_id,
        "fabric_scope": fabric_scope,
        "api_version": api_version,
        "admin_client_id": client_id,
        "admin_client_secret": client_secret,
        "agents": agents,
        "default_agent": default_agent,
    }
