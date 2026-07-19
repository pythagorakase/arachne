#!/usr/bin/env bash
# Create and validate the private TLS identity used on Arachne's loopback hop.

set -euo pipefail
umask 077

: "${HOME:?HOME must be set}"
arachne_state_root=${XDG_STATE_HOME:-${HOME}/.local/state}
arachne_tls_dir=${ARACHNE_TLS_DIR:-${arachne_state_root}/arachne-tls}
arachne_openssl=${OPENSSL_BIN:-openssl}
arachne_ca_key=${arachne_tls_dir}/ca-key.pem
arachne_ca_cert=${arachne_tls_dir}/ca-cert.pem
arachne_server_key=${arachne_tls_dir}/server-key.pem
arachne_server_cert=${arachne_tls_dir}/server-cert.pem
arachne_trust_bundle=${arachne_tls_dir}/trust-bundle.pem

if [[ -n ${ARACHNE_SYSTEM_CA_BUNDLE:-} ]]; then
  arachne_system_bundle=$ARACHNE_SYSTEM_CA_BUNDLE
else
  arachne_system_bundle=
  for arachne_candidate in \
    /etc/ssl/cert.pem \
    /etc/ssl/certs/ca-certificates.crt \
    /etc/pki/tls/certs/ca-bundle.crt \
    /etc/ssl/ca-bundle.pem; do
    if [[ -s "$arachne_candidate" ]]; then
      arachne_system_bundle=$arachne_candidate
      break
    fi
  done
fi
if [[ -z "$arachne_system_bundle" || ! -f "$arachne_system_bundle" || \
      ! -r "$arachne_system_bundle" || ! -s "$arachne_system_bundle" ]]; then
  printf 'Arachne: no readable system CA bundle; set ARACHNE_SYSTEM_CA_BUNDLE\n' >&2
  exit 1
fi
if ! command -v "$arachne_openssl" >/dev/null 2>&1; then
  printf 'Arachne: openssl is not available: %s\n' "$arachne_openssl" >&2
  exit 1
fi

arachne_mode() {
  stat -c '%a' "$1" 2>/dev/null || stat -f '%Lp' "$1"
}

arachne_assert_private_file() {
  local arachne_path=$1 arachne_permissions
  if [[ ! -f "$arachne_path" || -L "$arachne_path" || ! -O "$arachne_path" ]]; then
    printf 'Arachne: TLS state must be an owner-controlled regular file: %s\n' \
      "$arachne_path" >&2
    return 1
  fi
  arachne_permissions=$(arachne_mode "$arachne_path") || return 1
  if (( (8#$arachne_permissions & 077) != 0 )); then
    printf 'Arachne: TLS state has group/other permissions: %s (%s)\n' \
      "$arachne_path" "$arachne_permissions" >&2
    return 1
  fi
}

arachne_validate() {
  local arachne_check_trust=${1:-true}
  local arachne_expected arachne_cert_pub arachne_key_pub
  local arachne_ca_cert_pub arachne_ca_key_pub arachne_dir_permissions
  if [[ ! -d "$arachne_tls_dir" || -L "$arachne_tls_dir" || \
        ! -O "$arachne_tls_dir" ]]; then
    printf 'Arachne: TLS directory is not owner-controlled: %s\n' \
      "$arachne_tls_dir" >&2
    return 1
  fi
  arachne_dir_permissions=$(arachne_mode "$arachne_tls_dir") || return 1
  if (( (8#$arachne_dir_permissions & 077) != 0 )); then
    printf 'Arachne: TLS directory has group/other permissions: %s (%s)\n' \
      "$arachne_tls_dir" "$arachne_dir_permissions" >&2
    return 1
  fi
  for arachne_path in "$arachne_ca_key" "$arachne_ca_cert" \
    "$arachne_server_key" "$arachne_server_cert"; do
    arachne_assert_private_file "$arachne_path" || return 1
  done
  "$arachne_openssl" pkey -in "$arachne_ca_key" -noout -check >/dev/null 2>&1 || return 1
  "$arachne_openssl" pkey -in "$arachne_server_key" -noout -check >/dev/null 2>&1 || return 1
  "$arachne_openssl" x509 -in "$arachne_ca_cert" -noout -checkend 86400 >/dev/null || return 1
  "$arachne_openssl" x509 -in "$arachne_server_cert" -noout -checkend 86400 >/dev/null || return 1
  "$arachne_openssl" verify -CAfile "$arachne_ca_cert" \
    "$arachne_ca_cert" >/dev/null || return 1
  "$arachne_openssl" verify -CAfile "$arachne_ca_cert" \
    -verify_hostname localhost "$arachne_server_cert" >/dev/null || return 1
  "$arachne_openssl" verify -CAfile "$arachne_ca_cert" \
    -verify_ip 127.0.0.1 "$arachne_server_cert" >/dev/null || return 1
  arachne_cert_pub=$("$arachne_openssl" x509 -in "$arachne_server_cert" -pubkey -noout | \
    "$arachne_openssl" pkey -pubin -outform DER | "$arachne_openssl" dgst -sha256)
  arachne_key_pub=$("$arachne_openssl" pkey -in "$arachne_server_key" -pubout -outform DER | \
    "$arachne_openssl" dgst -sha256)
  [[ "$arachne_cert_pub" == "$arachne_key_pub" ]] || return 1
  arachne_ca_cert_pub=$("$arachne_openssl" x509 -in "$arachne_ca_cert" -pubkey -noout | \
    "$arachne_openssl" pkey -pubin -outform DER | "$arachne_openssl" dgst -sha256)
  arachne_ca_key_pub=$("$arachne_openssl" pkey -in "$arachne_ca_key" -pubout -outform DER | \
    "$arachne_openssl" dgst -sha256)
  [[ "$arachne_ca_cert_pub" == "$arachne_ca_key_pub" ]] || return 1
  [[ "$arachne_check_trust" == false ]] && return 0
  arachne_assert_private_file "$arachne_trust_bundle" || return 1
  arachne_expected=$(mktemp "${TMPDIR:-/tmp}/arachne-trust.XXXXXX")
  trap 'rm -f "$arachne_expected"' RETURN
  { cat "$arachne_system_bundle"; printf '\n'; cat "$arachne_ca_cert"; } >"$arachne_expected"
  cmp -s "$arachne_expected" "$arachne_trust_bundle" || return 1
  rm -f "$arachne_expected"
  trap - RETURN
}

arachne_refresh_trust_bundle() {
  local arachne_replacement
  if [[ -e "$arachne_trust_bundle" || -L "$arachne_trust_bundle" ]]; then
    arachne_assert_private_file "$arachne_trust_bundle" || return 1
  fi
  arachne_replacement=$(mktemp "${arachne_tls_dir}/.trust-bundle.XXXXXX")
  trap 'rm -f "$arachne_replacement"' RETURN
  { cat "$arachne_system_bundle"; printf '\n'; cat "$arachne_ca_cert"; } \
    >"$arachne_replacement"
  chmod 600 "$arachne_replacement"
  if [[ -f "$arachne_trust_bundle" ]] && \
      cmp -s "$arachne_replacement" "$arachne_trust_bundle"; then
    rm -f "$arachne_replacement"
  else
    mv -f "$arachne_replacement" "$arachne_trust_bundle"
  fi
  trap - RETURN
}

if [[ -e "$arachne_tls_dir" || -L "$arachne_tls_dir" ]]; then
  if ! arachne_validate false || ! arachne_refresh_trust_bundle || \
      ! arachne_validate true; then
    printf 'Arachne: existing TLS state is partial, stale, or invalid: %s\n' \
      "$arachne_tls_dir" >&2
    exit 1
  fi
  exit 0
fi

mkdir -p "$(dirname "$arachne_tls_dir")"
arachne_staging=$(mktemp -d "${arachne_tls_dir}.new.XXXXXX")
trap 'rm -rf "$arachne_staging"' EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
arachne_config=${arachne_staging}/openssl.cnf
cat >"$arachne_config" <<'EOF'
[req]
prompt = no
distinguished_name = subject
[subject]
CN = localhost
[v3_ca]
basicConstraints = critical,CA:TRUE
keyUsage = critical,keyCertSign,cRLSign
subjectKeyIdentifier = hash
[v3_server]
basicConstraints = critical,CA:FALSE
keyUsage = critical,digitalSignature,keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = DNS:localhost,IP:127.0.0.1,IP:::1
EOF

"$arachne_openssl" req -new -x509 -newkey rsa:3072 -nodes -sha256 \
  -days 3650 -subj '/CN=Arachne Local Backend CA' \
  -config "$arachne_config" -extensions v3_ca \
  -keyout "${arachne_staging}/ca-key.pem" \
  -out "${arachne_staging}/ca-cert.pem" >/dev/null 2>&1
"$arachne_openssl" req -new -newkey rsa:3072 -nodes -sha256 \
  -subj '/CN=localhost' -config "$arachne_config" -reqexts v3_server \
  -keyout "${arachne_staging}/server-key.pem" \
  -out "${arachne_staging}/server.csr" >/dev/null 2>&1
"$arachne_openssl" x509 -req -sha256 -days 825 -set_serial 1 \
  -in "${arachne_staging}/server.csr" \
  -CA "${arachne_staging}/ca-cert.pem" \
  -CAkey "${arachne_staging}/ca-key.pem" \
  -extfile "$arachne_config" -extensions v3_server \
  -out "${arachne_staging}/server-cert.pem" >/dev/null 2>&1
{ cat "$arachne_system_bundle"; printf '\n'; cat "${arachne_staging}/ca-cert.pem"; } \
  >"${arachne_staging}/trust-bundle.pem"
rm -f "${arachne_staging}/server.csr" "$arachne_config"
chmod 700 "$arachne_staging"
chmod 600 "${arachne_staging}"/*.pem
mv "$arachne_staging" "$arachne_tls_dir"
trap - EXIT HUP INT TERM

if ! arachne_validate false || ! arachne_refresh_trust_bundle || \
    ! arachne_validate true; then
  printf 'Arachne: generated TLS state failed validation: %s\n' "$arachne_tls_dir" >&2
  exit 1
fi
