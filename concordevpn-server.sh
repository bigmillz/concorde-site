#!/usr/bin/env bash
#
# v23 — Hysteria 2 server bootstrap for Ubuntu (DigitalOcean droplet)
#
# Goals, in priority order:
#   1. Saturate the client's line. Nothing here can exceed the ISP's
#      provisioned rate; the job is to reach it on every kind of traffic.
#   2. Leak-proof DNS. The local network operator sees one UDP flow to one
#      IP and nothing else — no queries, no SNI, no plaintext :53.
#   3. Survive DPI (Sandvine et al.) via switchable obfuscation.
#
# Usage on a fresh Ubuntu 22.04/24.04 droplet, as root:
#   ./concordevpn-server.sh install
#   ./concordevpn-server.sh install --obfs --down 110 --up 14
#   ./concordevpn-server.sh install --domain vpn.example.com --email me@example.com
#   ./concordevpn-server.sh uninstall
#
set -euo pipefail

# ---------------------------------------------------------------- constants --
HY_VERSION="app/v2.10.0"
REPO="apernet/hysteria"
BIN_PATH="/usr/local/bin/hysteria"
CFG_DIR="/etc/hysteria"
CFG_FILE="${CFG_DIR}/config.yaml"
CERT_FILE="${CFG_DIR}/cert.pem"
KEY_FILE="${CFG_DIR}/key.pem"
STATE_DIR="/var/lib/hysteria"
V23_DIR="/etc/v23"
CLIENT_DIR="${V23_DIR}/client"
META_FILE="${V23_DIR}/v23.env"
LIB_DIR="/usr/local/lib/v23"
UNIT_FILE="/etc/systemd/system/hysteria-server.service"
SYSCTL_FILE="/etc/sysctl.d/99-v23-quic.conf"
NFT_FILE="${CFG_DIR}/porthop.nft"
NFT_UNIT="/etc/systemd/system/v23-porthop.service"
CLI_PATH="/usr/local/bin/concordevpn"
# the old name keeps working on servers that learned it
CLI_ALIAS="/usr/local/bin/v23"
SVC_USER="hysteria"

# ----------------------------------------------------------------- defaults --
PORT=443
PASSWORD=""
SNI="zoom.us"
MASQ_URL=""
DOMAIN=""
ACME_EMAIL=""
OBFS_PW=""            # non-empty => salamander obfuscation on
OBFS_REQUESTED=0
TROJAN_ENABLED=1      # sing-box Trojan/TLS on tcp/PORT — the client's PRIMARY
TROJAN_PW=""          # generated at install like the hysteria password
SB_VER="1.13.16"      # pinned; matches the first droplet
# VLESS + XTLS-Vision + REALITY (Xray-core), the THIRD transport. Added
# 2026-08-22 for the Japan/Korea trip. Trojan and REALITY are both TLS on
# TCP, but they fail differently: Trojan is a real TLS server with a
# self-signed cert and no website behind it, which an active prober can
# confirm in one connection; REALITY borrows a genuine third-party
# certificate and hands unauthenticated probers the real site, so there is
# nothing to confirm. That is its whole reason to exist here — see
# docs/PLAN.md and the protocol-choice note.
REALITY_ENABLED=1
REALITY_PORT=8443    # 443/tcp belongs to Trojan; see the port note below
REALITY_PORT_SET=0   # 1 once --reality-port is passed explicitly
REALITY_SNI_SET=0    # ditto for --reality-sni
REALITY_PORT_ARG=""
REALITY_SNI_ARG=""
# The borrowed site (--reality-sni). NOT a free choice: the dest must
# complete a TLS 1.3 handshake the way REALITY relays it. www.microsoft.com
# is reachable, serves TLS 1.3 and h2, and STILL fails as a dest — every
# client got "handshake did not complete successfully" (measured 2026-08-25,
# xray 26.3.27, reproduced locally against a fresh keypair). dl.google.com
# and addons.mozilla.org both work. The self-test at the end of
# install_reality is what stops a bad dest reaching a client again.
REALITY_SNI="dl.google.com"
XRAY_VER="26.3.27"   # pinned like the others; newest at time of writing
VLESS_UUID=""
REALITY_PRIV=""
REALITY_PUB=""
REALITY_SID=""
HOP_ENABLED=1
HOP_START=20000
HOP_END=45000
SRV_UP_MBPS=1000
SRV_DOWN_MBPS=1000
CLI_DOWN_MBPS=110     # set to ~90% of the MEASURED line rate, not the plan rate
CLI_UP_MBPS=14
CC_MODE="bbr"    # BBR adapts to any line (100-500+ Mbps); no per-network tuning
TUN_MTU=1400
# A new server is locked down by default; each has an opt-out flag.
FIREWALL=1       # ufw: nothing in but SSH and the VPN's own ports
HARDEN_SSH=1     # key-only SSH (skipped, never forced, if root has no key)
FAIL2BAN=1       # only where OpenSSH lacks its own repeat-failure blocking
AUTO_REBOOT=1    # reboot for security updates at 04:00 in the region's time
ASSUME_YES=0

# ------------------------------------------------------------------- output --
if [[ -t 1 ]]; then
  B=$'\033[1m'; R=$'\033[0m'; G=$'\033[32m'; Y=$'\033[33m'; RD=$'\033[31m'; C=$'\033[36m'
else
  B=""; R=""; G=""; Y=""; RD=""; C=""
fi
say()  { printf '%s==>%s %s\n' "$C$B" "$R" "$*"; }
ok()   { printf '%s  ok%s %s\n' "$G" "$R" "$*"; }
warn() { printf '%swarn%s %s\n' "$Y" "$R" "$*" >&2; }
die()  { printf '%sfail%s %s\n' "$RD" "$R" "$*" >&2; exit 1; }

usage() {
  cat <<'USAGE'
v23 — Hysteria 2 server bootstrap for Ubuntu

  concordevpn-server.sh install [options]
  concordevpn-server.sh uninstall [--yes]

Options:
  --password STR      Auth password (default: generated 32-char random)
  --port N            UDP listen port (default: 443)
  --sni HOST          TLS SNI / masquerade host (default: zoom.us)
  --masquerade URL    Site reverse-proxied for TCP probes (default: https://SNI/)
  --domain HOST       Real Let's Encrypt cert for HOST (replaces SNI spoofing)
  --email ADDR        ACME contact email
  --obfs [PASSWORD]   Salamander obfuscation: makes the flow unfingerprintable
                      random UDP instead of a recognizable QUIC handshake.
                      Best anti-Sandvine setting. Random password if omitted.
  --down N            Client download target, Mbps (default: 110)
  --up N              Client upload target, Mbps (default: 14)
  --cc brutal|bbr     Client congestion control (default: brutal)
  --mtu N             Client TUN MTU (default: 1400; v23-mac.sh measures this)
  --hop A-B           UDP port-hop range (default: 20000-45000)
  --no-hop            Disable port hopping
  --no-firewall       Leave the firewall off (default: on, SSH + VPN ports only)
  --keep-ssh-passwords  Leave SSH password login alone (default: key-only,
                      applied only when root already has an authorized key)
  --no-fail2ban       No fail2ban on an OpenSSH older than 9.8 (newer ones
                      block repeat login failures themselves)
  --no-auto-reboot    Install security updates but never reboot for them
  --hy-version TAG    Hysteria release tag (default: app/v2.10.0)
  --no-reality        Skip the VLESS/REALITY transport (Xray-core)
  --reality-port N    TCP port for REALITY (default: 8443)
  --reality-sni HOST  Site REALITY borrows a certificate from
                      (default: www.microsoft.com). Pick one that is fast
                      from THIS droplet, speaks TLS 1.3 + HTTP/2, and is
                      not blocked where you will be connecting FROM.
  --yes               Skip confirmations
  -h, --help          This message

Add REALITY to a server that is already running (touches nothing else):
  ./concordevpn-server.sh reality

Manage afterwards:  v23 status | client | tune <down> <up> | obfs on|off | rotate
USAGE
}

# ------------------------------------------------------------- arg handling --
ACTION="${1:-install}"
[[ "$ACTION" == "-h" || "$ACTION" == "--help" ]] && { usage; exit 0; }
case "$ACTION" in
  install|uninstall|reality) shift || true ;;
  *) ACTION="install" ;;
esac

while [[ $# -gt 0 ]]; do
  case "$1" in
    --password)    PASSWORD="$2"; shift 2 ;;
    --port)        PORT="$2"; shift 2 ;;
    --sni)         SNI="$2"; shift 2 ;;
    --masquerade)  MASQ_URL="$2"; shift 2 ;;
    --domain)      DOMAIN="$2"; shift 2 ;;
    --email)       ACME_EMAIL="$2"; shift 2 ;;
    --obfs)
      OBFS_REQUESTED=1
      if [[ "${2:-}" == --* || -z "${2:-}" ]]; then shift; else OBFS_PW="$2"; shift 2; fi ;;
    --no-obfs)     OBFS_REQUESTED=0; OBFS_PW=""; shift ;;
    --down)        CLI_DOWN_MBPS="$2"; shift 2 ;;
    --up)          CLI_UP_MBPS="$2"; shift 2 ;;
    --cc)          CC_MODE="$2"; shift 2 ;;
    --mtu)         TUN_MTU="$2"; shift 2 ;;
    --hop)         HOP_START="${2%%-*}"; HOP_END="${2##*-}"; HOP_ENABLED=1; shift 2 ;;
    --no-hop)      HOP_ENABLED=0; shift ;;
    --harden-ssh)  HARDEN_SSH=1; shift ;;          # the default now; kept for old scripts
    --keep-ssh-passwords) HARDEN_SSH=0; shift ;;
    --no-firewall) FIREWALL=0; shift ;;
    --no-fail2ban) FAIL2BAN=0; shift ;;
    --no-auto-reboot) AUTO_REBOOT=0; shift ;;
    --hy-version)  HY_VERSION="$2"; shift 2 ;;
    --no-reality)     REALITY_ENABLED=0; shift ;;
    # remembered separately so the standalone `reality` path can tell an
    # explicit flag from the value it recovered out of an existing config
    --reality-port)   REALITY_PORT="$2"; REALITY_PORT_ARG="$2"
                      REALITY_PORT_SET=1; shift 2 ;;
    --reality-sni)    REALITY_SNI="$2"; REALITY_SNI_ARG="$2"
                      REALITY_SNI_SET=1; shift 2 ;;
    --yes|-y)      ASSUME_YES=1; shift ;;
    -h|--help)     usage; exit 0 ;;
    *)             die "unknown option: $1 (try --help)" ;;
  esac
done

[[ "$CC_MODE" == "brutal" || "$CC_MODE" == "bbr" ]] || die "--cc must be 'brutal' or 'bbr'"
[[ "$TUN_MTU" =~ ^[0-9]+$ ]] && (( TUN_MTU >= 1200 && TUN_MTU <= 1500 )) \
  || die "--mtu must be 1200-1500"
[[ -n "$DOMAIN" ]] && SNI="$DOMAIN"
[[ -z "$MASQ_URL" ]] && MASQ_URL="https://${SNI}/"

# ---------------------------------------------------------------- uninstall --
do_uninstall() {
  [[ $EUID -eq 0 ]] || die "must run as root"
  if [[ $ASSUME_YES -eq 0 ]]; then
    printf 'This removes the hysteria service, configs, certs and client files.\nType %sREMOVE%s to continue: ' "$B" "$R"
    read -r reply
    [[ "$reply" == "REMOVE" ]] || die "aborted"
  fi
  systemctl disable --now hysteria-server.service 2>/dev/null || true
  systemctl disable --now sing-box.service 2>/dev/null || true
  systemctl disable --now xray.service 2>/dev/null || true
  systemctl disable --now v23-porthop.service 2>/dev/null || true
  nft delete table inet v23hop 2>/dev/null || true
  rm -f "$UNIT_FILE" "$NFT_UNIT" "$SYSCTL_FILE" "$CLI_PATH" "$BIN_PATH"
  rm -f /etc/systemd/system/sing-box.service /etc/systemd/system/xray.service
  rm -f /usr/local/bin/sing-box /usr/local/bin/xray
  rm -rf /etc/sing-box /var/lib/sing-box /usr/local/etc/xray /var/log/xray
  userdel xray 2>/dev/null || true
  rm -f /etc/systemd/journald.conf.d/99-v23.conf
  rm -f /etc/sysctl.d/98-concordevpn-recovery.conf /etc/apt/apt.conf.d/52concordevpn-reboot \
        /etc/fail2ban/jail.d/concordevpn.local
  rm -f /etc/systemd/system/{hysteria-server,sing-box,xray}.service.d/10-concordevpn-restart.conf
  rm -rf "$CFG_DIR" "$V23_DIR" "$STATE_DIR" "$LIB_DIR"
  systemctl daemon-reload
  systemctl restart systemd-journald 2>/dev/null || true
  sysctl --system >/dev/null 2>&1 || true
  userdel "$SVC_USER" 2>/dev/null || true
  ok "concordevpn removed (the firewall, SSH settings and swap are left in place)"
  exit 0
}
[[ "$ACTION" == "uninstall" ]] && do_uninstall

# ------------------------------------------------- vless + reality (TCP) --
# The third transport, and the only one that survives ACTIVE probing.
# Trojan and REALITY both look like TLS on TCP, but a prober that connects
# to the Trojan port gets a real TLS server presenting a self-signed cert
# for a site it does not actually host — one connection confirms it.
# REALITY answers an unauthenticated prober by relaying the handshake to
# the GENUINE ${REALITY_SNI}, certificate and all, so there is nothing to
# confirm; only a client holding the x25519 public key + shortId is let in.
#
# PORT: 443/tcp is already Trojan's and a TCP port cannot be shared, so
# REALITY listens on ${REALITY_PORT}. That is the one thing weaker than a
# textbook REALITY deployment (real HTTPS lives on 443), and xray says so
# itself at startup: "REALITY: Listening on non-443 ports may get your IP
# blocked by the GFW" (seen live on v26.3.27 — it is a warning, config is
# still OK). That warning is about CHINA specifically; for the US, Japan
# and Korea it does not apply, which is why 8443 is the right trade here —
# Trojan is the proven everyday line and moving it would cut off every
# client already in the field. If you ever need REALITY on 443 (travelling
# into China), reinstall with:  --port 8443 --reality-port 443
# which swaps them, and re-copy the client config because Trojan moves.
# Validate the REALITY port before anything is written. Called from BOTH the
# preflight and the standalone `reality` path — which exits long before
# preflight runs, and is the path most likely to be handed 443, since it is
# the SAFE way to touch a live box. $1 is the port trojan/hysteria hold (the
# CLI default on install, the recorded V23_PORT when adding to a live box).
validate_reality_port() {
  local busy="${1:-$PORT}"
  [[ "$REALITY_PORT" =~ ^[0-9]+$ ]] && (( REALITY_PORT >= 1 && REALITY_PORT <= 65535 )) \
    || die "--reality-port must be a port number, got '${REALITY_PORT}'"
  if [[ -n "$busy" ]] && (( REALITY_PORT == busy )); then
    die "--reality-port ${REALITY_PORT} collides with tcp/${busy}, which trojan
   already holds. To put reality on 443, move the others first:
     ./concordevpn-server.sh install --port 8443 --reality-port 443
   (that is a full reinstall — it rotates every password, so re-copy configs)"
  fi
}

# Open the REALITY port. The standalone `reality` path exits long before the
# main firewall section, so without this the subcommand would report success
# on a box where ufw silently drops every packet to 8443 — and the symptom
# (fallback quietly staying on trojan) looks exactly like a client-side key
# mistake. Only ever ADDS a rule; never enables ufw, never touches the rest.
open_reality_port() {
  if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "^Status: active"; then
    ufw allow "${REALITY_PORT}/tcp" >/dev/null 2>&1 || true
    ufw reload >/dev/null 2>&1 || true
    ok "ufw: opened TCP ${REALITY_PORT}"
  else
    warn "ufw inactive — if this droplet uses a DigitalOcean Cloud Firewall,"
    warn "add an inbound rule for TCP ${REALITY_PORT} or reality cannot be reached."
  fi
}

# Rewrite just the REALITY keys in v23.env, leaving every other line alone.
# (write_meta rebuilds the file wholesale from shell vars — wrong here.)
meta_set_reality() {
  [[ -f "$META_FILE" ]] || return 0
  local tmp; tmp="$(mktemp)"
  grep -vE '^V23_(VLESS_UUID|REALITY_PBK|REALITY_SID|REALITY_SNI|REALITY_PORT)=' \
    "$META_FILE" > "$tmp" 2>/dev/null || true
  cat >> "$tmp" <<METAR
V23_VLESS_UUID=${VLESS_UUID}
V23_REALITY_PBK=${REALITY_PUB}
V23_REALITY_SID=${REALITY_SID}
V23_REALITY_SNI=${REALITY_SNI}
V23_REALITY_PORT=${REALITY_PORT}
METAR
  install -m 0600 "$tmp" "$META_FILE"
  rm -f "$tmp"
}

install_reality() {
  say "Installing Xray-core ${XRAY_VER} (VLESS/REALITY TCP ${REALITY_PORT})"
  XR_ARCH="64"; [[ "$(uname -m)" == "aarch64" ]] && XR_ARCH="arm64-v8a"
  XR_TMP="$(mktemp -d)"
  if curl -fsSL --retry 3 -o "${XR_TMP}/xray.zip" \
      "https://github.com/XTLS/Xray-core/releases/download/v${XRAY_VER}/Xray-linux-${XR_ARCH}.zip"; then
    command -v unzip >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq unzip; }
    unzip -oq "${XR_TMP}/xray.zip" -d "${XR_TMP}"
    install -m 0755 "${XR_TMP}/xray" /usr/local/bin/xray
    install -d -m 0755 /usr/local/share/xray
    for geo in geoip.dat geosite.dat; do
      [[ -f "${XR_TMP}/${geo}" ]] && install -m 0644 "${XR_TMP}/${geo}" /usr/local/share/xray/
    done
    rm -rf "$XR_TMP"
    ok "xray $(/usr/local/bin/xray version | head -1 | awk '{print $2}') installed"
  else
    rm -rf "$XR_TMP"
    die "could not download Xray-core ${XRAY_VER}"
  fi

  # Keys. `xray x25519` renamed its output labels between releases — it used
  # to print "Private key:"/"Public key:" and on 26.3.27 prints
  # "PrivateKey:"/"Password (PublicKey):" — so match on the WORD, never on a
  # fixed line number. (Both forms verified against the real binary.)
  [[ -n "$VLESS_UUID" ]] || VLESS_UUID="$(/usr/local/bin/xray uuid)"
  # ONLY the private key decides whether to generate. It used to be
  # "-z PRIV || -z PUB", which meant a failed public-key DERIVATION threw away
  # a perfectly good recovered private key and minted a new identity — after
  # do_reality had already printed "clients keep working". Every client in the
  # field would have stopped authenticating, silently. Never regenerate on the
  # strength of the half we can always recompute.
  if [[ -z "$REALITY_PRIV" ]]; then
    XR_KEYS="$(/usr/local/bin/xray x25519)"
    REALITY_PRIV="$(printf '%s\n' "$XR_KEYS" | grep -iE 'private' | head -1 | sed 's/.*: *//')"
    REALITY_PUB="$(printf '%s\n' "$XR_KEYS" | grep -iE 'password|public' | head -1 | sed 's/.*: *//')"
  elif [[ -z "$REALITY_PUB" ]]; then
    # have the private half, missing the public one: derive, and FAIL LOUDLY
    # rather than quietly rotating the identity out from under the clients
    REALITY_PUB="$(/usr/local/bin/xray x25519 -i "$REALITY_PRIV" \
      | grep -iE 'password|public' | head -1 | sed 's/.*: *//')" || true
    [[ -n "$REALITY_PUB" ]] || die \
      "recovered the reality private key but could not derive its public half.
   Refusing to continue: generating a new keypair here would silently cut off
   every client already provisioned. Check 'xray x25519 -i <key>' by hand."
  fi
  [[ -n "$REALITY_PRIV" && -n "$REALITY_PUB" ]] \
    || die "could not parse 'xray x25519' output — check the format and this parser"

  # Reachability is necessary but NOWHERE NEAR sufficient — www.microsoft.com
  # passes this and still fails as a dest. The self-test after startup is the
  # check that actually matters; this one just gives a clearer early message.
  if ! curl -fsS --max-time 8 -o /dev/null "https://${REALITY_SNI}/" 2>/dev/null; then
    warn "cannot reach https://${REALITY_SNI} from this droplet — REALITY needs"
    warn "to relay probers there, so pick a --reality-sni this box can fetch."
  fi

  id xray >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin xray
  mkdir -p /usr/local/etc/xray
  # CONTAINMENT, explained here rather than inside the file: the routing rule
  # below is what stops this exit being an open relay into the VPC, and it has
  # to hold for HOSTNAMES, not just bare IPs.
  #   * sniffing with routeOnly:true feeds the sniffed name to the ROUTER
  #     without rewriting the connection's destination. WITHOUT routeOnly the
  #     sniffed name REPLACES the IP target, so an ip: rule only ever matches
  #     literals.
  #   * domainStrategy IPIfNonMatch makes the router resolve a name that
  #     matched no domain rule, then re-test the ip: rules against the answer.
  # Together: http://<name-that-resolves-to-169.254.169.254>/ is blocked
  # instead of reaching DigitalOcean's metadata service, which serves droplet
  # credentials over plain HTTP to anything that can open a socket. Under the
  # AsIs + no-routeOnly pair this replaced, it was reachable.
  #
  # This stays a shell comment ON PURPOSE. Xray tolerates // comments in its
  # config, but do_reality recovers the existing identity with python's
  # json.load, which does NOT — writing the rationale into the file made key
  # recovery throw, which silently rotated the keypair and cut off every
  # client. Keep /usr/local/etc/xray/config.json strict JSON.
  cat > /usr/local/etc/xray/config.json <<XRCFG
{
  "log": { "loglevel": "warning", "dnsLog": false },
  "inbounds": [
    {
      "tag": "vless-reality",
      "listen": "::",
      "port": ${REALITY_PORT},
      "protocol": "vless",
      "settings": {
        "clients": [ { "id": "${VLESS_UUID}", "flow": "xtls-rprx-vision" } ],
        "decryption": "none"
      },
      "streamSettings": {
        "network": "tcp",
        "security": "reality",
        "realitySettings": {
          "show": false,
          "dest": "${REALITY_SNI}:443",
          "xver": 0,
          "serverNames": [ "${REALITY_SNI}" ],
          "privateKey": "${REALITY_PRIV}",
          "shortIds": [ "${REALITY_SID}" ]
        }
      },
      "sniffing": { "enabled": true, "destOverride": [ "http", "tls", "quic" ],
                    "routeOnly": true }
    }
  ],
  "outbounds": [
    { "tag": "direct", "protocol": "freedom" },
    { "tag": "block", "protocol": "blackhole" }
  ],
  "routing": {
    "domainStrategy": "IPIfNonMatch",
    "rules": [
      { "type": "field",
        "ip": [ "geoip:private", "169.254.0.0/16", "::1/128", "fc00::/7", "fe80::/10" ],
        "outboundTag": "block" },
      { "type": "field", "protocol": [ "bittorrent" ], "outboundTag": "block" }
    ]
  }
}
XRCFG
  # The private key lives in here, so nobody but the service may read it.
  chown root:xray /usr/local/etc/xray/config.json
  chmod 0640 /usr/local/etc/xray/config.json

  # Runs as its own user, not root: there is no shared cert to read. The
  # ambient capability is what lets --reality-port 443 actually work — an
  # unprivileged process cannot bind below 1024, and without this the
  # documented "promote REALITY to 443" swap would die at the is-active
  # check with Trojan already moved off 443, leaving BOTH TCP transports
  # down. Harmless on 8443; the bounding set keeps it to that one capability.
  cat > /etc/systemd/system/xray.service <<XRUNIT
[Unit]
Description=ConcordeVPN — VLESS/XTLS-Vision/REALITY (Xray-core)
Documentation=https://xtls.github.io/
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=xray
Group=xray
ExecStart=/usr/local/bin/xray run -config /usr/local/etc/xray/config.json
Restart=on-failure
RestartSec=3s
LimitNOFILE=1048576
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
PrivateDevices=true
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
DevicePolicy=closed

[Install]
WantedBy=multi-user.target
XRUNIT
  systemctl daemon-reload
  systemctl enable xray.service >/dev/null 2>&1
  # RESTART, not just enable --now: on a box where xray is already running
  # (every re-run of this subcommand) "enable --now" is a no-op, so a changed
  # dest or port silently keeps serving the OLD config. That is exactly how a
  # config change appeared to succeed while every client got "server name
  # mismatch" — caught by the self-test below.
  systemctl restart xray.service >/dev/null 2>&1
  sleep 2
  if systemctl is-active --quiet xray.service; then
    ok "xray running (VLESS/REALITY) on TCP/${REALITY_PORT}, borrowing ${REALITY_SNI}"
    # PROVE IT CARRIES TRAFFIC. "xray is active" means only that the process
    # started; a dest that cannot serve as a REALITY target still listens
    # happily and rejects every real client with "handshake did not complete
    # successfully". That is exactly what shipped once — www.microsoft.com is
    # reachable, does TLS 1.3 + h2, and is still unusable here. So: stand up a
    # throwaway client against our own port and push one request through it.
    say "Self-test: pushing a request through REALITY"
    XR_PUB_TEST="$(/usr/local/bin/xray x25519 -i "$REALITY_PRIV" \
      | grep -iE 'password|public' | head -1 | sed 's/.*: *//')"
    XR_TDIR="$(mktemp -d)"
    cat > "${XR_TDIR}/c.json" <<XRTEST
{ "log": {"loglevel": "warning"},
  "inbounds": [{"port": 10891, "listen": "127.0.0.1", "protocol": "socks",
                "settings": {"udp": false}}],
  "outbounds": [{"protocol": "vless",
    "settings": {"vnext": [{"address": "127.0.0.1", "port": ${REALITY_PORT},
      "users": [{"id": "${VLESS_UUID}", "encryption": "none",
                 "flow": "xtls-rprx-vision"}]}]},
    "streamSettings": {"network": "tcp", "security": "reality",
      "realitySettings": {"serverName": "${REALITY_SNI}", "fingerprint": "chrome",
        "publicKey": "${XR_PUB_TEST}", "shortId": "${REALITY_SID}"}}}] }
XRTEST
    /usr/local/bin/xray run -c "${XR_TDIR}/c.json" >"${XR_TDIR}/c.log" 2>&1 &
    XR_TPID=$!
    sleep 3
    XR_SEEN="$(curl -s --socks5-hostname 127.0.0.1:10891 --max-time 20 \
      https://api.ipify.org 2>/dev/null || true)"
    kill "$XR_TPID" 2>/dev/null || true
    rm -rf "$XR_TDIR"
    if [[ -n "$XR_SEEN" ]]; then
      ok "self-test passed — reality carried traffic, exit ${XR_SEEN}"
    else
      warn "xray is running but REALITY did not carry a test request."
      warn "Almost always the borrowed site: it must complete a TLS 1.3"
      warn "handshake the way REALITY relays it. Reachable is NOT enough —"
      warn "www.microsoft.com passes a curl and still fails as a dest."
      warn "Known-good: dl.google.com, addons.mozilla.org."
      die  "re-run with:  ./concordevpn-server.sh reality --reality-sni dl.google.com"
    fi
    # The uuid and public key only exist once xray has run, long after the
    # first write_meta. Update ONLY those keys — write_meta rebuilds the whole
    # file from shell variables, which in the standalone `reality` path are
    # empty, so calling it here would blank the hysteria and trojan passwords.
    meta_set_reality
  else
    journalctl -u xray.service -n 30 --no-pager || true
    die "xray failed to start (log above)"
  fi
}

# Adding REALITY to a server that is ALREADY RUNNING. This exists because
# `install` is not safe to re-run on a live box: it generates fresh hysteria,
# obfs and trojan passwords (it never reads the existing v23.env back), so it
# would silently rotate every credential and cut off every client. This path
# touches nothing but Xray.
do_reality() {
  [[ $EUID -eq 0 ]] || die "must run as root"
  [[ -r "$META_FILE" ]] || die "no ${META_FILE} — run './concordevpn-server.sh install' first"
  # Reuse what is already provisioned: re-running must not invalidate clients.
  # An UNREADABLE config here is the dangerous case, not the harmless one — a
  # config we cannot parse looks identical to "no config", and falling through
  # would mint a new identity and cut off every client. So: parse failures are
  # reported and refused, never swallowed. (Tolerating // comments because
  # xray does is exactly how this went wrong once already.)
  REALITY_RECOVERED=0
  if [[ -e /usr/local/etc/xray/config.json ]]; then
    XR_REC="$(python3 - <<'PYX'
import json, re, sys
p = "/usr/local/etc/xray/config.json"
try:
    raw = open(p).read()
except Exception as exc:
    sys.exit("cannot read %s: %s" % (p, exc))
try:
    c = json.loads(raw)
except Exception:
    # be liberal about what we accept back: strip // and /* */ comments,
    # which xray tolerates and earlier builds of this script wrote
    stripped = re.sub(r"/\*.*?\*/", "", raw, flags=re.S)
    stripped = re.sub(r"(?m)^\s*//.*$", "", stripped)
    try:
        c = json.loads(stripped)
    except Exception as exc:
        sys.exit("%s is present but unparseable (%s)" % (p, exc))
try:
    i = c["inbounds"][0]
    r = i["streamSettings"]["realitySettings"]
    print("VLESS_UUID=%s" % i["settings"]["clients"][0]["id"])
    print("REALITY_PRIV=%s" % r["privateKey"])
    print("REALITY_SID=%s" % r["shortIds"][0])
    print("REALITY_PORT=%s" % i["port"])
    print("REALITY_SNI=%s" % r["serverNames"][0])
except Exception as exc:
    sys.exit("%s has no reality inbound to reuse (%s)" % (p, exc))
PYX
)" || die "refusing to continue: ${XR_REC:-could not read the existing xray config}.
   Generating a new identity here would cut off every client already
   provisioned. Fix or move that file, then re-run."
    eval "$XR_REC"
    REALITY_RECOVERED=1
    [[ -n "$REALITY_PRIV" ]] && ok "found an existing reality identity — keeping it"
  fi
  # An explicit --reality-port wins over whatever the old config said; without
  # one, the recovered value stands. Validate whichever we end up with: this
  # path never reaches the preflight checks.
  (( REALITY_PORT_SET == 1 )) && REALITY_PORT="$REALITY_PORT_ARG"
  (( REALITY_SNI_SET == 1 )) && REALITY_SNI="$REALITY_SNI_ARG"
  validate_reality_port "$(sed -n 's/^V23_PORT=//p' "$META_FILE" | head -1)"
  [[ -n "$REALITY_SID" ]] || REALITY_SID="$(head -c 4 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  install_reality
  open_reality_port
  SERVER_ADDR="$(sed -n 's/^V23_SERVER=//p' "$META_FILE" | head -1)"
  cat <<REALSUM

${B}${G}reality is up on ${SERVER_ADDR}.${R}  tcp ${REALITY_PORT}, borrowing ${REALITY_SNI}

Add these to ~/.concordevpn/env on the Mac (suffix per site, e.g. _SGP):
  V23_VLESS_UUID=${VLESS_UUID}
  V23_REALITY_PBK=${REALITY_PUB}
  V23_REALITY_SID=${REALITY_SID}
  V23_REALITY_SNI=${REALITY_SNI}
  V23_REALITY_PORT=${REALITY_PORT}

Then in ConcordeVPN: gear ▸ Reinstall / repair, to regenerate the profile.
Nothing else on this server was touched — hysteria, trojan and every
password are exactly as they were.
REALSUM
  exit 0
}
[[ "$ACTION" == "reality" ]] && do_reality



# ---------------------------------------------------------------- preflight --
say "Preflight"
[[ $EUID -eq 0 ]] || die "must run as root (sudo ./concordevpn-server.sh install)"
[[ -r /etc/os-release ]] || die "cannot read /etc/os-release"
. /etc/os-release
[[ "${ID:-}" == "ubuntu" || "${ID_LIKE:-}" == *debian* ]] \
  || warn "built for Ubuntu/Debian; found '${ID:-unknown}' — continuing"

case "$(uname -m)" in
  x86_64|amd64) ARCH="amd64" ;;
  aarch64|arm64) ARCH="arm64" ;;
  *) die "unsupported architecture: $(uname -m)" ;;
esac

# Throughput here is CPU-bound on AEAD, so report whether this droplet can
# actually push the rate being asked of it.
NCPU="$(nproc)"
MEM_MB="$(( $(awk '/MemTotal/{print $2}' /proc/meminfo) / 1024 ))"
if grep -qE '^flags.*\baes\b' /proc/cpuinfo 2>/dev/null || [[ "$ARCH" == "arm64" ]]; then
  AES_HW="yes"
else
  AES_HW="no"
fi
ok "Ubuntu ${VERSION_ID:-?} · ${ARCH} · ${NCPU} vCPU · ${MEM_MB} MB RAM · AES accel: ${AES_HW}"
[[ "$AES_HW" == "no" ]] && warn "no AES-NI: expect throughput to top out well under 200 Mbps"
if (( NCPU < 2 )); then
  warn "1 vCPU handles roughly 300-600 Mbps of QUIC — fine for a ~123 Mbps line,"
  warn "but resize the droplet before expecting more."
fi

if [[ -z "$PASSWORD" ]]; then
  PASSWORD="$(head -c 24 /dev/urandom | base64 | tr -d '/+=' | head -c 32)"
  ok "generated auth password"
fi
if (( OBFS_REQUESTED == 1 )) && [[ -z "$OBFS_PW" ]]; then
  OBFS_PW="$(head -c 24 /dev/urandom | base64 | tr -d '/+=' | head -c 32)"
  ok "generated obfuscation password"
fi
if (( TROJAN_ENABLED == 1 )) && [[ -z "$TROJAN_PW" ]]; then
  TROJAN_PW="$(head -c 24 /dev/urandom | base64 | tr -d '/+=' | head -c 32)"
  ok "generated trojan password"
fi
if (( REALITY_ENABLED == 1 )); then validate_reality_port "$PORT"; fi
if (( REALITY_ENABLED == 1 )) && [[ -z "$REALITY_SID" ]]; then
  # shortId is any even-length hex up to 16 chars; 8 is the usual choice
  REALITY_SID="$(head -c 4 /dev/urandom | od -An -tx1 | tr -d ' \n')"
fi

PUBLIC_IP="$(curl -fsS --max-time 3 http://169.254.169.254/metadata/v1/interfaces/public/0/ipv4/address 2>/dev/null || true)"
[[ -z "$PUBLIC_IP" ]] && PUBLIC_IP="$(curl -fsS --max-time 5 https://api.ipify.org 2>/dev/null || true)"
[[ -z "$PUBLIC_IP" ]] && PUBLIC_IP="SERVER_IP_HERE"
DO_REGION="$(curl -fsS --max-time 3 http://169.254.169.254/metadata/v1/region 2>/dev/null || echo unknown)"
SERVER_ADDR="${DOMAIN:-$PUBLIC_IP}"
ok "address ${SERVER_ADDR} · region ${DO_REGION}"

# ---------------------------------------------------------------- packages  --
say "Installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
  curl ca-certificates openssl nftables ethtool >/dev/null
ok "curl, openssl, nftables, ethtool"

# ---------------------------------------------------------------- kernel    --
# QUIC runs in userspace, so throughput is gated by UDP socket buffers and how
# fast the kernel can hand packets up. Stock buffers (~208 KB) are a hard wall.
say "Tuning kernel for high-rate QUIC"
cat > "$SYSCTL_FILE" <<'SYSCTL'
# v23 — QUIC / Hysteria 2 tuning
net.core.rmem_max = 33554432
net.core.wmem_max = 33554432
net.core.rmem_default = 4194304
net.core.wmem_default = 4194304
net.core.netdev_max_backlog = 32768
net.core.somaxconn = 32768
net.core.default_qdisc = fq
net.ipv4.udp_rmem_min = 16384
net.ipv4.udp_wmem_min = 16384
net.ipv4.udp_mem = 786432 1048576 26777216
net.ipv4.tcp_congestion_control = bbr
net.ipv4.tcp_mtu_probing = 1
net.ipv4.tcp_fastopen = 3
net.ipv4.tcp_slow_start_after_idle = 0
net.ipv4.ip_forward = 1
net.ipv6.conf.all.forwarding = 1
net.ipv4.ip_local_port_range = 10240 65000
fs.file-max = 1048576
SYSCTL
modprobe tcp_bbr 2>/dev/null || true
sysctl -q --system >/dev/null 2>&1 || warn "some sysctl keys rejected by this kernel"
ok "socket buffers raised to 32 MB, fq + bbr active"

# NIC offloads: best-effort, purely a pps win. Never fatal.
NIC="$(ip -o -4 route show default 2>/dev/null | awk '{print $5; exit}')"
if [[ -n "$NIC" ]] && command -v ethtool >/dev/null 2>&1; then
  ethtool -K "$NIC" gro on gso on tso on >/dev/null 2>&1 || true
  ip link set "$NIC" txqueuelen 10000 >/dev/null 2>&1 || true
  ok "offloads enabled on ${NIC}"
fi

# ---------------------------------------------------------------- binary    --
say "Installing Hysteria ${HY_VERSION} (${ARCH})"
TAG_ENC="${HY_VERSION//\//%2F}"
BASE="https://github.com/${REPO}/releases/download/${TAG_ENC}"
ASSET="hysteria-linux-${ARCH}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

curl -fsSL --retry 3 -o "${TMP}/${ASSET}" "${BASE}/${ASSET}" \
  || die "download failed: ${BASE}/${ASSET}"
curl -fsSL --retry 3 -o "${TMP}/hashes.txt" "${BASE}/hashes.txt" \
  || die "could not fetch hashes.txt — refusing to install an unverified binary"
EXPECTED="$(awk -v a="build/${ASSET}" '$2==a{print $1}' "${TMP}/hashes.txt")"
[[ -n "$EXPECTED" ]] || die "no checksum for ${ASSET} in hashes.txt"
ACTUAL="$(sha256sum "${TMP}/${ASSET}" | cut -d' ' -f1)"
[[ "$EXPECTED" == "$ACTUAL" ]] || die "checksum mismatch: expected ${EXPECTED}, got ${ACTUAL}"
ok "sha256 verified ${ACTUAL:0:16}…"
install -m 0755 "${TMP}/${ASSET}" "$BIN_PATH"

# ---------------------------------------------------------------- layout   --
id -u "$SVC_USER" >/dev/null 2>&1 || \
  useradd --system --no-create-home --shell /usr/sbin/nologin "$SVC_USER"
install -d -m 0750 -o root -g "$SVC_USER" "$CFG_DIR"
install -d -m 0750 -o "$SVC_USER" -g "$SVC_USER" "$STATE_DIR"
install -d -m 0700 -o root -g root "$V23_DIR" "$CLIENT_DIR"
install -d -m 0755 -o root -g root "$LIB_DIR"

# ---------------------------------------------------------------- TLS      --
PIN_SHA256=""
if [[ -n "$DOMAIN" ]]; then
  say "ACME mode for ${DOMAIN}"
  warn "DNS for ${DOMAIN} must already resolve to ${PUBLIC_IP}, TCP/80 reachable"
else
  say "Generating self-signed cert for ${SNI}"
  openssl ecparam -genkey -name prime256v1 -out "$KEY_FILE" 2>/dev/null
  openssl req -new -x509 -days 3650 -key "$KEY_FILE" -out "$CERT_FILE" \
    -subj "/CN=${SNI}" -addext "subjectAltName=DNS:${SNI},DNS:*.${SNI}" 2>/dev/null
  chown root:"$SVC_USER" "$CERT_FILE" "$KEY_FILE"
  chmod 0640 "$CERT_FILE" "$KEY_FILE"
  # Pinning gives real server authentication without insecure=true.
  PIN_SHA256="$(openssl x509 -in "$CERT_FILE" -outform der 2>/dev/null \
    | sha256sum | cut -d' ' -f1 | tr 'a-f' 'A-F' | sed 's/../&:/g; s/:$//')"
  ok "cert pin ${PIN_SHA256:0:29}…"
fi

# ---------------------------------------------------------------- metadata --
# Single source of truth. The generators below read only this file, so the v23
# CLI can change settings and regenerate without ever hand-editing configs.
write_meta() {
  cat > "$META_FILE" <<META
V23_SERVER=${SERVER_ADDR}
V23_PORT=${PORT}
V23_SNI=${SNI}
V23_PASSWORD=${PASSWORD}
V23_OBFS_PW=${OBFS_PW}
V23_TROJAN_PW=${TROJAN_PW}
V23_VLESS_UUID=${VLESS_UUID}
V23_REALITY_PBK=${REALITY_PUB}
V23_REALITY_SID=${REALITY_SID}
V23_REALITY_SNI=${REALITY_SNI}
V23_REALITY_PORT=${REALITY_PORT}
V23_DOMAIN=${DOMAIN}
V23_EMAIL=${ACME_EMAIL:-admin@${SNI}}
V23_MASQ=${MASQ_URL}
V23_CERT=${CERT_FILE}
V23_KEY=${KEY_FILE}
V23_PIN=${PIN_SHA256}
V23_HOP=${HOP_ENABLED}
V23_HOP_START=${HOP_START}
V23_HOP_END=${HOP_END}
V23_DOWN=${CLI_DOWN_MBPS}
V23_UP=${CLI_UP_MBPS}
V23_SRV_UP=${SRV_UP_MBPS}
V23_SRV_DOWN=${SRV_DOWN_MBPS}
V23_CC=${CC_MODE}
V23_MTU=${TUN_MTU}
META
  chmod 0600 "$META_FILE"
}
write_meta
ok "metadata written to ${META_FILE}"

# ------------------------------------------------------- server generator  --
cat > "${LIB_DIR}/genserver.sh" <<'GENSRV'
#!/usr/bin/env bash
# Regenerate /etc/hysteria/config.yaml from /etc/v23/v23.env
set -euo pipefail
. /etc/v23/v23.env
CFG=/etc/hysteria/config.yaml

if [[ -n "${V23_DOMAIN}" ]]; then
  TLS_BLOCK="acme:
  domains:
    - ${V23_DOMAIN}
  email: ${V23_EMAIL}
  dir: /var/lib/hysteria/acme"
else
  TLS_BLOCK="tls:
  cert: ${V23_CERT}
  key: ${V23_KEY}"
fi

if [[ -n "${V23_OBFS_PW}" ]]; then
  # Salamander wraps every QUIC packet so there is no TLS handshake, no SNI
  # and no QUIC header for a classifier to fingerprint. Non-matching packets
  # are dropped without reply, so active probes learn nothing.
  OBFS_BLOCK="obfs:
  type: salamander
  salamander:
    password: ${V23_OBFS_PW}"
else
  OBFS_BLOCK="# obfs off: presents a genuine TLS/QUIC handshake with SNI ${V23_SNI}"
fi

# TCP masquerade listeners only when nothing else owns the port (see below)
if [[ "${V23_TROJAN_PW:-}" == "" ]]; then
  MASQ_TCP_BLOCK="  listenHTTP: :80
  listenHTTPS: :${V23_PORT}
  forceHTTPS: true"
else
  MASQ_TCP_BLOCK="  # tcp/${V23_PORT} belongs to sing-box (Trojan/TLS)"
fi

cat > "$CFG" <<CONF
# v23 Hysteria 2 server — generated $(date -u +%Y-%m-%dT%H:%M:%SZ)
# Edit /etc/v23/v23.env and run 'v23 apply' instead of editing this file.
listen: :${V23_PORT}

${TLS_BLOCK}

auth:
  type: password
  password: ${V23_PASSWORD}

${OBFS_BLOCK}

# Answer TCP probes like an ordinary HTTPS host rather than a closed port.
# NOTE: the TCP listeners are conditional. When sing-box serves Trojan on
# tcp/PORT (the default), IT owns that port and its real TLS handshake with
# the pinned cert is a better masquerade than a reverse proxy — and two
# processes cannot bind the same port ("address already in use", hit live
# on the Singapore build 2026-08-22). Only a trojan-less server opens them.
masquerade:
  type: proxy
  proxy:
    url: ${V23_MASQ}
    rewriteHost: true
${MASQ_TCP_BLOCK}

# No server bandwidth block on purpose, and the client IS listened to.
# Hysteria decides per connection: a client that declares a rate gets Brutal
# at that rate, a client that declares nothing gets BBR. That is exactly the
# split the app wants - Performance and Ultra Performance declare a measured
# rate, Balanced, Stealth and Ultra Stealth declare none and stay on BBR.
#
# Deliberately NO 'bandwidth:' block (unlike the config before 2026-08-15):
# a fixed server ceiling is a guess that caps a fast line, whereas the
# client's number comes from an actual measurement of the line in front of
# it. V23_SRV_UP/V23_SRV_DOWN stay unused for that reason.
#
# This was 'true' from 2026-08-15 to 2026-09-18, which silently discarded
# every rate the app measured: Performance's turbo tuning and the Ultra
# controller were both writing numbers nothing read. Measured on the live
# NYC exit before and after - a client asking for a 1 Mbps cap got 13.96
# Mbps with it true, and 0.97 with it false, while a client declaring
# nothing got ~14 either way.
ignoreClientBandwidth: false

# Big QUIC windows are what let one flow fill a fat pipe.
quic:
  initStreamReceiveWindow: 26843545
  maxStreamReceiveWindow: 26843545
  initConnReceiveWindow: 67108864
  maxConnReceiveWindow: 67108864
  maxIdleTimeout: 30s
  maxIncomingStreams: 1024
  disablePathMTUDiscovery: false

# No 'resolver:' block on purpose. Hysteria's own DoH/DoT resolver silently
# falls back to the system resolver when it fails, with nothing logged, which
# makes DNS behaviour impossible to reason about — and it bypasses any
# filtering configured on the host. Leaving it out means every lookup goes
# through systemd-resolved, so whatever you point that at (NextDNS, etc.)
# actually applies to proxied traffic.

# Refuse to proxy into the droplet's own networks. Without this, any client
# can read 169.254.169.254 and lift the droplet's cloud credentials.
acl:
  inline:
    - reject(127.0.0.0/8)
    - reject(::1/128)
    - reject(169.254.0.0/16)
    - reject(fe80::/10)
    - reject(10.0.0.0/8)
    - reject(172.16.0.0/12)
    - reject(192.168.0.0/16)
    - reject(fc00::/7)
    - direct(all)

udpIdleTimeout: 60s

# Required for 'v23-mac.sh bench' (hysteria speedtest). Without it the server
# tries to resolve the magic host "@SpeedTest" as a real name and the test
# fails. Only authenticated clients can reach it.
speedTest: true
CONF
chown root:hysteria "$CFG"
chmod 0640 "$CFG"
GENSRV
chmod 0755 "${LIB_DIR}/genserver.sh"

# ------------------------------------------------------- client generator  --
cat > "${LIB_DIR}/genclient.sh" <<'GENCLI'
#!/usr/bin/env bash
# Regenerate macOS client configs in /etc/v23/client from /etc/v23/v23.env
set -euo pipefail
. /etc/v23/v23.env
OUT=/etc/v23/client
mkdir -p "$OUT"

# --- TLS: pin the exact self-signed cert rather than disabling verification.
if [[ -n "${V23_PIN}" && -r "${V23_CERT}" ]]; then
  CERT_JSON="$(awk 'NF{printf "%s          \"%s\"", (n++ ? ",\n" : ""), $0} END{printf "\n"}' "${V23_CERT}")"
  TLS_JSON="        \"enabled\": true,
        \"insecure\": false,
        \"server_name\": \"${V23_SNI}\",
        \"certificate\": [
${CERT_JSON}
        ],
        \"alpn\": [\"h3\"]"
else
  TLS_JSON="        \"enabled\": true,
        \"insecure\": false,
        \"server_name\": \"${V23_SNI}\",
        \"alpn\": [\"h3\"]"
fi

# --- Brutal targets, or omit entirely to fall back to BBR.
if [[ "${V23_CC}" == "brutal" ]]; then
  BW_JSON="      \"up_mbps\": ${V23_UP},
      \"down_mbps\": ${V23_DOWN},"
  BW_YAML="bandwidth:
  up: ${V23_UP} mbps
  down: ${V23_DOWN} mbps"
else
  BW_JSON=""
  BW_YAML="# bandwidth omitted -> BBR"
fi

# --- Obfuscation must match the server exactly or nothing connects.
if [[ -n "${V23_OBFS_PW}" ]]; then
  OBFS_JSON="      \"obfs\": { \"type\": \"salamander\", \"password\": \"${V23_OBFS_PW}\" },"
  OBFS_YAML="obfs:
  type: salamander
  salamander:
    password: ${V23_OBFS_PW}"
else
  OBFS_JSON=""
  OBFS_YAML="# obfs off"
fi

emit() {   # $1=outfile  $2=server_port block  $3=extra outbound lines
  cat > "$1" <<JSON
{
  "log": { "level": "warn", "timestamp": true },

  "dns": {
    "servers": [
      { "type": "https", "tag": "dns-proxy", "server": "1.1.1.1", "detour": "proxy" },
      { "type": "local", "tag": "dns-local" }
    ],
    "final": "dns-proxy",
    "strategy": "prefer_ipv4",
    "independent_cache": true
  },

  "inbounds": [
    {
      "type": "tun",
      "tag": "tun-in",
      "interface_name": "utun42",
      "address": ["172.19.0.1/30", "fdfe:dcba:9876::1/126"],
      "mtu": ${V23_MTU},
      "auto_route": true,
      "strict_route": true,
      "endpoint_independent_nat": true,
      "stack": "system"
    }
  ],

  "outbounds": [
    {
      "type": "hysteria2",
      "tag": "proxy",
      "server": "${V23_SERVER}",
${2}
${3}${OBFS_JSON}
${BW_JSON}
      "password": "${V23_PASSWORD}",
      "tls": {
${TLS_JSON}
      }
    },
    { "type": "direct", "tag": "direct" }
  ],

  "route": {
    "rules": [
      { "port": 53, "action": "hijack-dns" },
      { "ip_is_private": true, "outbound": "direct" }
    ],
    "default_domain_resolver": "dns-local",
    "auto_detect_interface": true
  }
}
JSON
}

emit "${OUT}/v23_mac_tun.json" "      \"server_port\": ${V23_PORT}," ""

if [[ "${V23_HOP}" == "1" ]]; then
  emit "${OUT}/v23_mac_tun_hop.json" \
    "      \"server_ports\": [\"${V23_HOP_START}:${V23_HOP_END}\"]," \
    "      \"hop_interval\": \"30s\",
"
fi

# Native client. Use this for 'hysteria speedtest', which measures the tunnel
# itself with no TUN or routing in the path.
HY_SERVER="${V23_SERVER}:${V23_PORT}"
[[ "${V23_HOP}" == "1" ]] && HY_SERVER="${V23_SERVER}:${V23_PORT},${V23_HOP_START}-${V23_HOP_END}"
{
  echo "server: ${HY_SERVER}"
  echo "auth: ${V23_PASSWORD}"
  echo
  echo "tls:"
  echo "  sni: ${V23_SNI}"
  if [[ -n "${V23_PIN}" ]]; then
    echo "  pinSHA256: ${V23_PIN}"
    echo "  insecure: true"
  fi
  echo
  echo "${OBFS_YAML}"
  echo
  echo "${BW_YAML}"
  echo
  echo "quic:"
  echo "  initStreamReceiveWindow: 26843545"
  echo "  maxStreamReceiveWindow: 26843545"
  echo "  initConnReceiveWindow: 67108864"
  echo "  maxConnReceiveWindow: 67108864"
  echo
  echo "fastOpen: true"
  echo
  echo "socks5:"
  echo "  listen: 127.0.0.1:1080"
  echo "http:"
  echo "  listen: 127.0.0.1:8080"
} > "${OUT}/v23_hysteria.yaml"

# Share URI. Cannot carry a pinned cert, hence insecure=1 here.
PW_ESC="$(printf '%s' "${V23_PASSWORD}" | sed 's/@/%40/g; s/:/%3A/g')"
URI="hy2://${PW_ESC}@${V23_SERVER}:${V23_PORT}/?sni=${V23_SNI}&insecure=1"
[[ -n "${V23_OBFS_PW}" ]] && URI="${URI}&obfs=salamander&obfs-password=${V23_OBFS_PW}"
[[ "${V23_HOP}" == "1" ]] && URI="${URI}&mport=${V23_HOP_START}-${V23_HOP_END}"
printf '%s#v23\n' "$URI" > "${OUT}/v23.uri"

chmod 0600 "${OUT}"/*
GENCLI
chmod 0755 "${LIB_DIR}/genclient.sh"

say "Generating server + client configs"
"${LIB_DIR}/genserver.sh"
"${LIB_DIR}/genclient.sh"
ok "configs generated"

# ---------------------------------------------------------------- systemd  --
say "Installing systemd unit"
cat > "$UNIT_FILE" <<UNIT
[Unit]
Description=ConcordeVPN — Hysteria 2 (UDP transport)
Documentation=https://v2.hysteria.network/
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SVC_USER}
Group=${SVC_USER}
ExecStart=${BIN_PATH} server --config ${CFG_FILE}
Environment=HYSTERIA_LOG_LEVEL=warn
WorkingDirectory=${STATE_DIR}
Restart=on-failure
RestartSec=3s
LimitNOFILE=1048576
LimitNPROC=1048576
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictSUIDSGID=true
ReadWritePaths=${STATE_DIR}
DevicePolicy=closed

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now hysteria-server.service >/dev/null 2>&1
sleep 2
if systemctl is-active --quiet hysteria-server.service; then
  ok "hysteria-server running on UDP/${PORT}"
else
  journalctl -u hysteria-server.service -n 30 --no-pager || true
  die "hysteria-server failed to start (log above)"
fi

# ------------------------------------------------------------ trojan (TCP) --
# The TCP half of the pair, and the client's PRIMARY transport: sing-box
# serving Trojan over TLS on tcp/443 with the SAME self-signed cert as
# hysteria, so one pinned fingerprint authenticates both. Hysteria alone
# dies on networks that block or shape UDP — this is what carries those.
# (Provisioned by hand on the first droplet in 2026; codified here 2026-08-22
# so the stack is actually reproducible. NOT VLESS/REALITY — plain Trojan.)
if [[ $TROJAN_ENABLED -eq 1 ]]; then
  say "Installing sing-box ${SB_VER} (Trojan/TCP ${PORT})"
  SB_ARCH="amd64"; [[ "$(uname -m)" == "aarch64" ]] && SB_ARCH="arm64"
  SB_TGZ="sing-box-${SB_VER}-linux-${SB_ARCH}.tar.gz"
  SB_TMP="$(mktemp -d)"
  if curl -fsSL --retry 3 -o "${SB_TMP}/${SB_TGZ}" \
      "https://github.com/SagerNet/sing-box/releases/download/v${SB_VER}/${SB_TGZ}"; then
    tar -xzf "${SB_TMP}/${SB_TGZ}" -C "$SB_TMP"
    install -m 0755 "${SB_TMP}"/sing-box-*/sing-box /usr/local/bin/sing-box
    rm -rf "$SB_TMP"
    ok "sing-box $(/usr/local/bin/sing-box version | head -1 | awk '{print $3}') installed"
  else
    rm -rf "$SB_TMP"
    die "could not download sing-box ${SB_VER}"
  fi

  mkdir -p /etc/sing-box /var/lib/sing-box
  cat > /etc/sing-box/config.json <<SBCFG
{
  "log": { "level": "warn", "timestamp": true },
  "inbounds": [
    {
      "type": "trojan",
      "tag": "trojan-in",
      "listen": "::",
      "listen_port": ${PORT},
      "users": [ { "name": "v23", "password": "${TROJAN_PW}" } ],
      "tls": {
        "enabled": true,
        "server_name": "${SNI}",
        "certificate_path": "${CERT_FILE}",
        "key_path": "${KEY_FILE}"
      }
    }
  ],
  "outbounds": [ { "type": "direct", "tag": "direct" } ],
  "route": {
    "rules": [
      { "ip_is_private": true, "action": "reject" },
      { "ip_cidr": ["169.254.0.0/16"], "action": "reject" }
    ]
  }
}
SBCFG
  chmod 0600 /etc/sing-box/config.json
  # sing-box runs as root to bind :443 and read the shared cert; the route
  # rules above are what stop it being an open relay into the VPC.
  cat > /etc/systemd/system/sing-box.service <<'SBUNIT'
[Unit]
Description=ConcordeVPN — Trojan/TLS (TCP transport)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/bin/sing-box run -c /etc/sing-box/config.json
Restart=on-failure
RestartSec=3s
LimitNOFILE=1048576
AmbientCapabilities=CAP_NET_BIND_SERVICE
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/sing-box
DevicePolicy=closed

[Install]
WantedBy=multi-user.target
SBUNIT
  systemctl daemon-reload
  systemctl enable --now sing-box.service >/dev/null 2>&1
  sleep 2
  if systemctl is-active --quiet sing-box.service; then
    ok "sing-box running (Trojan) on TCP/${PORT}"
  else
    journalctl -u sing-box.service -n 30 --no-pager || true
    die "sing-box failed to start (log above)"
  fi
fi

if [[ $REALITY_ENABLED -eq 1 ]]; then install_reality; fi

# ------------------------------------------------------------ port hopping --
# The one real lever against UDP shaping: some middleboxes rate-limit per
# 5-tuple or per destination port. Spraying across a range defeats that.
if [[ $HOP_ENABLED -eq 1 ]]; then
  say "Enabling UDP port hopping ${HOP_START}-${HOP_END} -> ${PORT}"
  cat > "$NFT_FILE" <<NFT
#!/usr/sbin/nft -f
table inet v23hop {}
delete table inet v23hop
table inet v23hop {
  chain prerouting {
    type nat hook prerouting priority dstnat; policy accept;
    udp dport ${HOP_START}-${HOP_END} redirect to :${PORT}
  }
}
NFT
  chmod 0644 "$NFT_FILE"
  cat > "$NFT_UNIT" <<UNIT
[Unit]
Description=ConcordeVPN — UDP port-hopping redirect
After=network-online.target nftables.service
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/nft -f ${NFT_FILE}
ExecStop=/usr/sbin/nft delete table inet v23hop

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  if systemctl enable --now v23-porthop.service >/dev/null 2>&1; then
    ok "nftables redirect active"
  else
    warn "port hopping failed to load; clients should use port ${PORT} only"
    HOP_ENABLED=0
    write_meta
    "${LIB_DIR}/genclient.sh"
  fi
fi

# ---------------------------------------------------------------- firewall --
# A new server takes nothing but SSH and the VPN's own ports: ufw, default
# deny inbound, IPv4 and IPv6. SSH is allowed on every port sshd really
# listens on (sshd -T reads the drop-ins too) BEFORE the firewall comes up,
# and ufw keeps established connections, so the session running this is
# never cut. TCP 80 only where something uses it: the decoy site of a server
# without Trojan, or certificate renewal for --domain. The UDP hop range is
# redirected to the Hysteria port before the firewall sees it, but is opened
# too so a firewall in front of this one can mirror these rules. A ufw that
# is already on keeps its own defaults and just gains these rules.
# sshd's effective settings, read once: under pipefail a failing sshd -T in
# a pipeline would end the install, and `| grep -q` can SIGPIPE it
SSHD_T="$(sshd -T 2>/dev/null || true)"
SSH_PORTS="$(awk '$1 == "port" {print $2}' <<<"$SSHD_T" | sort -un | tr '\n' ' ')"
[[ -z "${SSH_PORTS// }" ]] && SSH_PORTS="$( (awk '/^[[:space:]]*Port[[:space:]]+[0-9]+/{print $2}' /etc/ssh/sshd_config 2>/dev/null || true) | tr '\n' ' ')"
[[ -z "${SSH_PORTS// }" ]] && SSH_PORTS="22"
SSH_PORTS="${SSH_PORTS% }"
NEED_80=0
[[ $TROJAN_ENABLED -ne 1 || -n "$DOMAIN" ]] && NEED_80=1
FW_ACTIVE=0
command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "^Status: active" && FW_ACTIVE=1
fw_allow() { ufw allow "$1" >/dev/null 2>&1 || warn "ufw: could not allow $1"; }
if [[ $FIREWALL -eq 1 || $FW_ACTIVE -eq 1 ]]; then
  say "Firewall"
  if ! command -v ufw >/dev/null 2>&1; then
    apt-get install -y -qq --no-install-recommends ufw >/dev/null || die "could not install ufw"
  fi
  for p in $SSH_PORTS; do fw_allow "${p}/tcp"; done
  fw_allow "${PORT}/udp"
  fw_allow "${PORT}/tcp"
  [[ $REALITY_ENABLED -eq 1 ]] && fw_allow "${REALITY_PORT}/tcp"
  [[ $NEED_80 -eq 1 ]] && fw_allow 80/tcp
  [[ $HOP_ENABLED -eq 1 ]] && fw_allow "${HOP_START}:${HOP_END}/udp"
  if [[ $FW_ACTIVE -eq 0 ]]; then
    ufw default deny incoming >/dev/null
    ufw default allow outgoing >/dev/null
    # blocked scans would log strangers' addresses; the log-privacy section
    # below keeps nothing it does not need
    ufw logging off >/dev/null 2>&1 || true
    ufw --force enable >/dev/null || die "could not enable ufw"
    ok "firewall on: SSH ${SSH_PORTS}, TCP ${PORT}$([[ $REALITY_ENABLED -eq 1 ]] && echo ", TCP ${REALITY_PORT}")$([[ $NEED_80 -eq 1 ]] && echo ", TCP 80"), UDP ${PORT}$([[ $HOP_ENABLED -eq 1 ]] && echo ", UDP ${HOP_START}-${HOP_END}"); everything else dropped"
  else
    ufw reload >/dev/null 2>&1 || true
    ok "ufw was already on: rules added"
  fi
else
  warn "firewall skipped (--no-firewall). Allow: UDP ${PORT}$([[ $HOP_ENABLED -eq 1 ]] && echo " and UDP ${HOP_START}-${HOP_END}"), TCP ${PORT}$([[ $REALITY_ENABLED -eq 1 ]] && echo ", TCP ${REALITY_PORT}")$([[ $NEED_80 -eq 1 ]] && echo ", TCP 80"), SSH ${SSH_PORTS}"
fi

# ------------------------------------------------------------- log privacy --
say "Reducing log retention"
install -d -m 0755 /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/99-v23.conf <<'JRN'
# v23: RAM-only logs, short retention — nothing durable to hand over.
[Journal]
Storage=volatile
RuntimeMaxUse=32M
MaxRetentionSec=1h
ForwardToSyslog=no
JRN
systemctl restart systemd-journald >/dev/null 2>&1 || warn "journald restart failed"
ok "journald volatile, 1h retention"

# -------------------------------------------------------------- ssh harden --
# Key-only: a password can be guessed, a key cannot. Never applied when root
# has no authorized key - that would lock the owner out.
if [[ $HARDEN_SSH -eq 1 ]]; then
  say "Hardening SSH"
  if [[ -s /root/.ssh/authorized_keys ]]; then
    install -d -m 0755 /etc/ssh/sshd_config.d
    cat > /etc/ssh/sshd_config.d/99-v23.conf <<'SSHD'
PasswordAuthentication no
PermitRootLogin prohibit-password
KbdInteractiveAuthentication no
SSHD
    if sshd -t 2>/dev/null; then
      systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true
      ok "SSH is key-only"
    else
      rm -f /etc/ssh/sshd_config.d/99-v23.conf
      warn "sshd rejected the key-only settings - left as it was"
    fi
  else
    warn "no root authorized_keys - SSH passwords left on rather than risk a lockout"
  fi
fi

# ------------------------------------------------------ repeat-failure block --
# An address that keeps failing to log in gets refused. OpenSSH 9.8 and later
# do it themselves (PerSourcePenalties, on by default - Ubuntu 26.04 ships
# 10.2), and there fail2ban would be protection in name only: its stock sshd
# filter does not see OpenSSH 10's log lines, which come from "sshd-session".
# So fail2ban is installed only where sshd lacks the built-in, reading the
# journal (logs here never reach /var/log/auth.log) and never banning a
# private address - SSH over the VPN itself arrives from one.
if [[ $FAIL2BAN -eq 1 ]]; then
  if grep -q '^persourcepenalties ' <<<"$SSHD_T"; then
    ok "OpenSSH refuses repeat login failures itself (PerSourcePenalties)"
  else
    say "Installing fail2ban (this OpenSSH has no built-in blocking)"
    if apt-get install -y -qq --no-install-recommends fail2ban python3-systemd >/dev/null; then
      install -d -m 0755 /etc/fail2ban/jail.d
      cat > /etc/fail2ban/jail.d/concordevpn.local <<F2B
[DEFAULT]
backend = systemd
banaction = nftables-multiport
banaction_allports = nftables-allports
ignoreip = 127.0.0.1/8 ::1 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 fc00::/7
bantime = 1h
bantime.increment = true
bantime.maxtime = 1w
findtime = 10m
maxretry = 5

[sshd]
enabled = true
port = ${SSH_PORTS// /,}
F2B
      systemctl enable fail2ban >/dev/null 2>&1 || true
      systemctl restart fail2ban >/dev/null 2>&1 || true
      sleep 2
      if fail2ban-client status sshd >/dev/null 2>&1; then
        ok "fail2ban: 5 failures in 10 min = banned for an hour, longer if they come back"
      else
        warn "fail2ban did not start its sshd jail - check 'journalctl -u fail2ban'"
      fi
    else
      warn "fail2ban could not be installed"
    fi
  fi
fi

# ---------------------------------------------------------- security updates --
# Ubuntu installs security updates by itself, but a new kernel or C library
# only takes effect after a reboot that nothing schedules: a server can run a
# kernel that is patched on disk and vulnerable in memory for months (one of
# ours went 51 days). So when an update needs it, reboot at 04:00 in the
# region's own time zone, when the fewest people are connected. Every service
# here starts at boot, and ConcordeVPN holds traffic and reconnects by itself.
say "Security updates"
apt-get install -y -qq --no-install-recommends unattended-upgrades >/dev/null 2>&1 || true
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'AUP'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
AUP
if [[ $AUTO_REBOOT -eq 1 ]]; then
  # 04:00 local, as UTC (droplets run on UTC); unknown regions use the
  # server's own clock
  case "${DO_REGION:-unknown}" in
    nyc*|tor*|ric*|atl*) REBOOT_AT="09:00" ;;
    mem*|mkc*)           REBOOT_AT="10:00" ;;
    sfo*)                REBOOT_AT="12:00" ;;
    lon*)                REBOOT_AT="03:00" ;;
    ams*|fra*)           REBOOT_AT="02:00" ;;
    blr*)                REBOOT_AT="22:30" ;;
    sgp*)                REBOOT_AT="20:00" ;;
    syd*)                REBOOT_AT="17:00" ;;
    *)                   REBOOT_AT="04:00" ;;
  esac
  cat > /etc/apt/apt.conf.d/52concordevpn-reboot <<AUR
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-WithUsers "true";
Unattended-Upgrade::Automatic-Reboot-Time "${REBOOT_AT}";
AUR
  ok "security updates daily; when one needs a reboot, it happens at ${REBOOT_AT} (server clock)"
else
  rm -f /etc/apt/apt.conf.d/52concordevpn-reboot
  ok "security updates daily; reboots left to you (--no-auto-reboot)"
fi

# ----------------------------------------------------------- crash recovery --
# Nobody watches a server set up with a few clicks. A kernel crash reboots it
# in 10 seconds instead of leaving it frozen until someone finds DigitalOcean's
# power button (the default is to hang forever). A VPN service that crashes is
# restarted every time: systemd's default gives up after five failures in ten
# seconds, which leaves the server up and serving nothing. A server with under
# 2 GB of memory gets a 1 GB swap file, so a memory spike slows it down rather
# than the kernel killing the VPN to make room. (Droplets have no watchdog
# device, so a hardware watchdog is not an option.)
say "Crash recovery"
cat > /etc/sysctl.d/98-concordevpn-recovery.conf <<'RCV'
kernel.panic = 10
kernel.panic_on_oops = 1
RCV
sysctl -q -p /etc/sysctl.d/98-concordevpn-recovery.conf >/dev/null 2>&1 \
  || warn "could not set kernel.panic"
# drop-ins, not edits: the units are the upstream ones. All three are written
# now - Xray is installed later, by `reality`, and picks its drop-in up then
for u in hysteria-server sing-box xray; do
  install -d -m 0755 "/etc/systemd/system/${u}.service.d"
  cat > "/etc/systemd/system/${u}.service.d/10-concordevpn-restart.conf" <<'RST'
[Unit]
StartLimitIntervalSec=0

[Service]
Restart=always
RestartSec=3
RST
done
systemctl daemon-reload
MEM_MB=$(awk '/^MemTotal:/ {print int($2 / 1024)}' /proc/meminfo)
if (( MEM_MB < 2000 )) && [[ -z "$(swapon --show --noheadings 2>/dev/null || true)" ]]; then
  if [[ ! -e /swapfile ]] && { fallocate -l 1G /swapfile 2>/dev/null \
       || dd if=/dev/zero of=/swapfile bs=1M count=1024 status=none; }; then
    if chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile; then
      grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
      echo 'vm.swappiness = 10' > /etc/sysctl.d/98-concordevpn-swap.conf
      sysctl -q vm.swappiness=10 >/dev/null 2>&1 || true
      ok "1 GB swap file (this server has ${MEM_MB} MB of memory)"
    else
      warn "swap file could not be enabled"
    fi
  fi
fi
ok "a kernel crash reboots in 10 s; the VPN services restart every time"

# ------------------------------------------------------------------ v23 CLI --
cat > "$CLI_PATH" <<'CLI'
#!/usr/bin/env bash
# v23 — management CLI. All state lives in /etc/v23/v23.env.
set -euo pipefail
META=/etc/v23/v23.env
OUT=/etc/v23/client
LIB=/usr/local/lib/v23
[[ -r "$META" ]] || { echo "concordevpn is not installed"; exit 1; }
. "$META"

need_root() { [[ $EUID -eq 0 ]] || { echo "run as root"; exit 1; }; }
set_meta() { sed -i "s|^$1=.*|$1=$2|" "$META"; }
apply_all() {
  "$LIB/genserver.sh"; "$LIB/genclient.sh"
  systemctl restart hysteria-server.service
  echo "applied — server restarted"
}

case "${1:-status}" in
  show)
    # the six lines ConcordeVPN's first-run screen asks for, in the names it
    # uses, plus the REALITY line — paste the whole block into the app
    cat <<S
# --- paste this into ConcordeVPN's first-run screen ---
V23_SERVER=${V23_SERVER}
V23_TROJAN_PW=${V23_TROJAN_PW}
V23_HY2_PW=${V23_PASSWORD}
V23_OBFS_PW=${V23_OBFS_PW}
V23_CERT_PIN=${V23_PIN}
V23_NEXTDNS_ID=
V23_VLESS_UUID=${V23_VLESS_UUID}
V23_REALITY_PBK=${V23_REALITY_PBK}
V23_REALITY_SID=${V23_REALITY_SID}
V23_REALITY_SNI=${V23_REALITY_SNI}
V23_REALITY_PORT=${V23_REALITY_PORT}
S
    echo "(V23_NEXTDNS_ID is yours to fill in: the profile ID from my.nextdns.io)" >&2
    ;;
  status)
    systemctl status hysteria-server.service --no-pager -l | head -14
    echo
    echo "server     ${V23_SERVER}:${V23_PORT}   sni=${V23_SNI}"
    echo "obfs       $([[ -n "${V23_OBFS_PW}" ]] && echo "salamander (on)" || echo "off")"
    echo "cc         ${V23_CC}  target ${V23_DOWN} down / ${V23_UP} up Mbps"
    echo "client mtu ${V23_MTU}"
    [[ "${V23_HOP}" == "1" ]] && echo "port hop   ${V23_HOP_START}-${V23_HOP_END} -> ${V23_PORT}"
    echo
    if [[ -n "${V23_REALITY_PBK:-}" ]]; then
      echo "reality    tcp ${V23_REALITY_PORT:-8443} sni=${V23_REALITY_SNI:-?} $(systemctl is-active xray.service 2>/dev/null)"
    fi
    ss -lunp 2>/dev/null | grep -E "hysteria|:${V23_PORT}" || echo "(no UDP socket visible)"
    ;;
  log|logs)   journalctl -u hysteria-server.service -f --no-pager ;;
  restart)    need_root; systemctl restart hysteria-server.service; echo restarted ;;
  apply)      need_root; apply_all ;;
  client)
    for f in "$OUT"/*; do echo "───── $f"; cat "$f"; echo; done ;;
  uri)        cat "$OUT/v23.uri" ;;
  reality)
    [[ -n "${V23_REALITY_PBK:-}" ]] || { echo "reality not installed"; exit 1; }
    echo "V23_VLESS_UUID=${V23_VLESS_UUID}"
    echo "V23_REALITY_PBK=${V23_REALITY_PBK}"
    echo "V23_REALITY_SID=${V23_REALITY_SID}"
    echo "V23_REALITY_SNI=${V23_REALITY_SNI}"
    echo "V23_REALITY_PORT=${V23_REALITY_PORT:-8443}"
    ;;
  tune)
    # Client-side only; no server restart needed.
    need_root
    [[ $# -eq 3 ]] || { echo "usage: v23 tune <down_mbps> <up_mbps>"; exit 1; }
    set_meta V23_DOWN "$2"; set_meta V23_UP "$3"
    "$LIB/genclient.sh"
    echo "targets now $2 down / $3 up Mbps — re-copy the config to your Mac"
    ;;
  mtu)
    need_root
    [[ $# -eq 2 ]] || { echo "usage: v23 mtu <1200-1500>"; exit 1; }
    set_meta V23_MTU "$2"; "$LIB/genclient.sh"
    echo "client MTU now $2 — re-copy the config to your Mac"
    ;;
  obfs)
    need_root
    case "${2:-}" in
      on)
        pw="$(head -c 24 /dev/urandom | base64 | tr -d '/+=' | head -c 32)"
        set_meta V23_OBFS_PW "$pw"; apply_all
        echo "salamander obfuscation ON — re-copy the config to your Mac (it will"
        echo "not connect until both ends match)"
        ;;
      off)
        set_meta V23_OBFS_PW ""; apply_all
        echo "obfuscation OFF — re-copy the config to your Mac"
        ;;
      *) echo "usage: concordevpn obfs on|off"; exit 1 ;;
    esac
    ;;
  rotate)
    need_root
    new="$(head -c 24 /dev/urandom | base64 | tr -d '/+=' | head -c 32)"
    set_meta V23_PASSWORD "$new"; apply_all
    echo "new password: ${new}"
    ;;
  speedtest)
    # Parallel streams: one TCP flow understates a fast line by 2x or more.
    echo "droplet -> internet, 4 parallel streams:"
    tmp="$(mktemp -d)"
    for i in 1 2 3 4; do
      curl -fsS -o /dev/null -w '%{speed_download}\n' --max-time 30 \
        "https://speed.cloudflare.com/__down?bytes=25000000" >"${tmp}/${i}" 2>/dev/null &
    done
    wait
    cat "${tmp}"/* 2>/dev/null \
      | awk '{s+=$1} END{ if (NR) printf "  %.1f Mbps aggregate\n", s*8/1e6;
                          else print "  measurement failed (endpoint unreachable)" }'
    rm -rf "$tmp"
    echo
    echo "If this far exceeds your client result, the droplet is not the bottleneck."
    echo "For the end-to-end tunnel number, run on the Mac:  ./v23-mac.sh bench"
    ;;
  *)
    cat <<'H'
concordevpn <command>
  show               the block to paste into ConcordeVPN's first-run screen
  status             service state and current settings
  log                follow the server log
  client             print every generated client config
  uri                print the hy2:// share URI
  tune <down> <up>   set client Brutal targets in Mbps
  mtu <n>            set client TUN MTU
  obfs on|off        toggle salamander obfuscation (both ends must match)
  rotate             new auth password
  apply              regenerate configs from /etc/v23/v23.env and restart
  restart            restart the server
  speedtest          check the droplet's own line
H
    ;;
esac
CLI
chmod 0755 "$CLI_PATH"
ln -sfn "${CLI_PATH}" "${CLI_ALIAS}" 2>/dev/null || true
ok "installed ${CLI_PATH}"
echo
say "Paste this block into ConcordeVPN's first-run screen (again later: concordevpn show)"
"${CLI_PATH}" show

# ---------------------------------------------------------------- summary  --
cat <<SUMMARY

${B}${G}v23 is up.${R}

  server        ${B}${SERVER_ADDR}${R}
  udp port      ${PORT}$([[ $HOP_ENABLED -eq 1 ]] && echo "  (+ hop range ${HOP_START}-${HOP_END})")
  sni / masq    ${SNI}  (TCP probes get a reverse proxy of ${MASQ_URL})
  password      ${B}${PASSWORD}${R}
  obfuscation   $([[ -n "$OBFS_PW" ]] && echo "${B}salamander ON${R}  pw ${OBFS_PW}" || echo "off — enable later with 'v23 obfs on'")
  cc            ${CC_MODE}$([[ "$CC_MODE" == "brutal" ]] && echo "  asking for ${CLI_DOWN_MBPS} down / ${CLI_UP_MBPS} up Mbps")
  client mtu    ${TUN_MTU}
$([[ -n "$PIN_SHA256" ]] && printf '  cert pin      %s\n' "${PIN_SHA256}")
$([[ $REALITY_ENABLED -eq 1 ]] && cat <<REAL

  ${B}vless/reality${R}  tcp ${REALITY_PORT}, borrowing ${B}${REALITY_SNI}${R}
    uuid        ${B}${VLESS_UUID}${R}
    public key  ${B}${REALITY_PUB}${R}
    short id    ${B}${REALITY_SID}${R}
  Put these in ~/.concordevpn/env on the Mac — suffix them per site the same
  way as the rest (V23_VLESS_UUID_SGP=… for a second exit):
    V23_VLESS_UUID=${VLESS_UUID}
    V23_REALITY_PBK=${REALITY_PUB}
    V23_REALITY_SID=${REALITY_SID}
    V23_REALITY_SNI=${REALITY_SNI}
    V23_REALITY_PORT=${REALITY_PORT}
  ConcordeVPN drops the VLESS line from its profile when these are absent, so
  a Mac that has not been given them keeps working on Trojan + Hysteria.
REAL
)
${B}Next: run the Mac side.${R} It measures your real line rate and path MTU, then
sets the Brutal targets from the measurement instead of guesswork:

  ./v23-mac.sh setup root@${SERVER_ADDR}     # fetches the configs itself
  ./v23-mac.sh up && ./v23-mac.sh leaktest

${B}${Y}What is and isn't achievable here${R}

Your line is the ceiling. A ~123/16 Fios result is a policer at the BNG and a
token bucket at the OLT, both upstream of everything you control, so no tunnel
reaches 1.2 Gbps over it. "As fast as possible" means reaching that ceiling on
${B}every${R} kind of traffic, which is a real win where a shaper was previously
holding specific traffic below it.

The speed work that actually matters, in order of effect:
  1. Brutal targets set to ~90% of measured rate. Overshooting makes it slower,
     because Brutal ignores loss and will happily blast into a policer.
  2. Path MTU sized correctly — wrong MTU means fragmentation on every packet.
  3. 32 MB UDP socket buffers on both ends (done here; v23-mac.sh does the Mac).
  4. Enough droplet CPU: ${NCPU} vCPU, AES accel ${AES_HW}.

${B}${G}DNS privacy${R}

This is fully solved, and it was the real gap in the old config. The client
hijacks everything to port 53 into sing-box's own resolver, which answers over
DoH ${B}inside${R} the tunnel. The server then resolves over DoH too. Result:
your local network sees one UDP flow to one IP — no queries, no hostnames, no
SNI of anything you visit. Verify with:  ./v23-mac.sh leaktest

${B}${G}Sandvine${R}

Run ${B}v23 obfs on${R}. Salamander removes the TLS handshake, the SNI and the
QUIC header, so there is no protocol fingerprint left to match — it becomes
uniform random UDP. That beats signature-based classification, which is how
Sandvine identifies Hysteria/WireGuard/OpenVPN. Caveat worth knowing: a few
networks deprioritize UDP they cannot classify, so if throughput drops after
enabling it, run 'v23 obfs off' and you are back to looking like QUIC to Zoom.
Both ends must match, so re-copy the client config after toggling.

${B}Manage:${R}  v23 status | client | tune <down> <up> | obfs on|off | rotate

SUMMARY
