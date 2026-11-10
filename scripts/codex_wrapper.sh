#!/usr/bin/env bash
set -euo pipefail

real_codex="${PACE_REAL_CODEX:-codex}"
auth_mode="${PACE_CODEX_AUTH_MODE:-api_key}"
base_url="${PACE_CODEX_BASE_URL:-}"
provider_name="${PACE_CODEX_PROVIDER_NAME:-pace}"
wire_api="${PACE_CODEX_WIRE_API:-responses}"
requires_openai_auth="${PACE_CODEX_REQUIRES_OPENAI_AUTH:-false}"

config_args=()
if [[ "$auth_mode" == "api_key" ]]; then
  if [[ -z "$base_url" || -z "${PACE_CODEX_API_KEY:-}" ]]; then
    echo "Codex API-key mode requires a nonempty base URL and API key." >&2
    exit 2
  fi
  if [[ "$wire_api" != "responses" ]]; then
    echo "Codex requires wire_api=responses and a Responses API endpoint." >&2
    exit 2
  fi
  config_args+=(
    -c 'model_reasoning_summary="none"'
    -c "model_provider=\"$provider_name\""
    -c "model_providers.$provider_name.name=\"$provider_name\""
    -c "model_providers.$provider_name.base_url=\"$base_url\""
    -c "model_providers.$provider_name.env_key=\"PACE_CODEX_API_KEY\""
    -c "model_providers.$provider_name.wire_api=\"$wire_api\""
    -c "model_providers.$provider_name.requires_openai_auth=$requires_openai_auth"
  )
elif [[ "$auth_mode" == "existing_codex_login" ]]; then
  exec "$real_codex" "$@"
else
  echo "Unsupported Codex authentication mode: $auth_mode" >&2
  exit 2
fi

forwarded_args=()
provider_config_inserted=false
for argument in "$@"; do
  forwarded_args+=("$argument")
  if [[ "$argument" == "exec" && "$provider_config_inserted" == false ]]; then
    forwarded_args+=("${config_args[@]}")
    provider_config_inserted=true
  fi
done

if [[ "$provider_config_inserted" == false ]]; then
  if [[ "$#" -eq 0 ]]; then
    exec "$real_codex" "${config_args[@]}"
  fi
  forwarded_args=("${config_args[@]}" "${forwarded_args[@]}")
fi

exec "$real_codex" "${forwarded_args[@]}"
