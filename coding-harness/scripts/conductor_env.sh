#!/usr/bin/env bash
# Shared Conductor environment loading for every shell CLI entrypoint.

load_harness_environment() {
  local env_file="$1"
  local had_server_url="${CONDUCTOR_SERVER_URL+x}"
  local had_auth_key="${CONDUCTOR_AUTH_KEY+x}"
  local had_auth_secret="${CONDUCTOR_AUTH_SECRET+x}"
  local had_auth_token="${CONDUCTOR_AUTH_TOKEN+x}"
  local had_server_type="${CONDUCTOR_SERVER_TYPE+x}"
  local original_server_url="${CONDUCTOR_SERVER_URL-}"
  local original_auth_key="${CONDUCTOR_AUTH_KEY-}"
  local original_auth_secret="${CONDUCTOR_AUTH_SECRET-}"
  local original_auth_token="${CONDUCTOR_AUTH_TOKEN-}"
  local original_server_type="${CONDUCTOR_SERVER_TYPE-}"
  local missing

  # Treat .env as defaults. Explicit process environment always wins, including an
  # explicitly empty value used to disable a setting from .env.
  if [ -f "$env_file" ] && [ "${CODING_HARNESS_ENV_LOADED:-}" != "1" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$env_file"
    set +a
  fi
  [ -z "$had_server_url" ] || export CONDUCTOR_SERVER_URL="$original_server_url"
  [ -z "$had_auth_key" ] || export CONDUCTOR_AUTH_KEY="$original_auth_key"
  [ -z "$had_auth_secret" ] || export CONDUCTOR_AUTH_SECRET="$original_auth_secret"
  [ -z "$had_auth_token" ] || export CONDUCTOR_AUTH_TOKEN="$original_auth_token"
  [ -z "$had_server_type" ] || export CONDUCTOR_SERVER_TYPE="$original_server_type"

  export CONDUCTOR_SERVER_URL="${CONDUCTOR_SERVER_URL:-http://localhost:8080/api}"
  export CODING_HARNESS_ENV_LOADED=1

  if [ -n "${CONDUCTOR_AUTH_KEY:-}" ] && [ -z "${CONDUCTOR_AUTH_SECRET:-}" ]; then
    missing="CONDUCTOR_AUTH_SECRET"
  elif [ -n "${CONDUCTOR_AUTH_SECRET:-}" ] && [ -z "${CONDUCTOR_AUTH_KEY:-}" ]; then
    missing="CONDUCTOR_AUTH_KEY"
  else
    return 0
  fi
  echo "ERROR: $missing must be set when using Conductor key/secret authentication" >&2
  return 2
}
