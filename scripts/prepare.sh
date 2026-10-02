#!/bin/sh
# One-shot host preparation. Run from anywhere, as root on the Docker host (Unraid terminal), BEFORE `docker compose up`:
#   git clone <repo> /mnt/user/appdata/vault-stack && cd /mnt/user/appdata/vault-stack && ./scripts/prepare.sh && docker compose up -d --build
# Creates .env (from .env.example, if missing), the host folders, and secrets/vault_token (only if missing; never printed,
# never overwritten), and gives the container user (PUID:PGID, default 99:100) ownership. Safe to re-run.
set -eu
cd "$(dirname "$0")/.."
[ -f .env ] || { cp .env.example .env; echo "created .env: edit VAULT_IP and VAULT_MAC before 'docker compose up'"; }
# read settings from .env without executing it
get() { sed -n "s/^$1=//p" .env | tail -n 1 | tr -d '"' ; }
PUID=$(get PUID); PGID=$(get PGID)
PUID=${PUID:-99}; PGID=${PGID:-100}
VAULT_DIR=$(get VAULT_DIR);     VAULT_DIR=${VAULT_DIR:-/mnt/user/data/Documents/Vault}
APPDATA_DIR=$(get APPDATA_DIR); APPDATA_DIR=${APPDATA_DIR:-/mnt/user/appdata/basic-memory}
BACKUP_DIR=$(get BACKUP_DIR);   BACKUP_DIR=${BACKUP_DIR:-$APPDATA_DIR/backup}

mkdir -p "$VAULT_DIR" "$APPDATA_DIR/state" "$BACKUP_DIR"
chown -R "$PUID:$PGID" "$APPDATA_DIR" "$BACKUP_DIR"
chown "$PUID:$PGID" "$VAULT_DIR"          # top level only; files inside keep their owner (the container writes with umask 000)
mkdir -p secrets
if [ -d secrets/vault_token ]; then rmdir secrets/vault_token 2>/dev/null || { echo "secrets/vault_token is a non-empty directory; remove it by hand" >&2; exit 1; }; fi
if [ ! -s secrets/vault_token ]; then
  (umask 077; openssl rand -hex 32 > secrets/vault_token)
  echo "created secrets/vault_token (not shown)"
fi
chown "$PUID:$PGID" secrets/vault_token
chmod 400 secrets/vault_token
echo "prepared: vault=$VAULT_DIR appdata=$APPDATA_DIR backup=$BACKUP_DIR owner=$PUID:$PGID"
