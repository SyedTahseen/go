#!/bin/bash

SSH_DIR="$HOME/.ssh"
CONFIG_FILE="$SSH_DIR/config"

mkdir -p "$SSH_DIR"
chmod 700 "$SSH_DIR"

touch "$CONFIG_FILE"
chmod 600 "$CONFIG_FILE"

# ==========================================
# FUNCTION TO ADD VPS
# ==========================================

add_vps() {

    SERVER_NAME="$1"
    USERNAME="$2"
    SERVER_IP="$3"
    PRIVATE_KEY="$4"

    KEY_FILE="$SSH_DIR/${SERVER_NAME}_key"

    # Save private key
    echo "$PRIVATE_KEY" > "$KEY_FILE"
    chmod 600 "$KEY_FILE"

    # Remove old config if exists
    sed -i "/Host $SERVER_NAME/,+5d" "$CONFIG_FILE"

    # Add SSH config
    cat >> "$CONFIG_FILE" <<EOF

Host $SERVER_NAME
    HostName $SERVER_IP
    User $USERNAME
    IdentityFile $KEY_FILE
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null

EOF

    echo "[ADDED] $SERVER_NAME"
}

# ==========================================
# VPS 1
# ==========================================

add_vps \
"instance1" \
"ubuntu" \
"141.148.43.81" \
'-----BEGIN RSA PRIVATE KEY-----
MIIEowIBAAKCAQEArtAZ2S8/odHZyppwg/QxC9M4Qkp+IFbFK1Io00UwDx1EOLI3
TzcSpevfhfLg4gOKnEJjes+SfttUcZJzAHIjiZhHRQcIfYdskuEtycTrn3GM7gx9
gTfJPfUrRkE3tkzSdXLlz1DIUUvp4KMcgxdtp9gapB1+kENOcdQWOsLppiNXdxME
66R5lPPx3ZaEwGoc6fIbehpsER5FbFtwLu838D2qzz2v8D8VTZBaFpeseUAy0zNm
3l/kKzPnL4kGRdiEF/EdsJIOSpJEbaVJuekysjBXPr+nTLcO+/zYfuUo3j+7QTSU
4UKs5XoZ3lXiPlc++5hWH3HFBvIaUZOMVS+W+QIDAQABAoIBAACWaVBx044xkCze
a1KfcdUQTcpy8KWBaNif7aT31JnxolppWlCfHPT4f6J7Cp0lZJfo2zaCCKGXhQxo
Nd74jRVVf85I20RVW2GjxFdIxERG9hQbNN8Wu/MS3EykplFm4lAzSAHCy8jyePKl
hzy8LDsjr8OLNukvI84WNoOHgfKvHIc145KdbC2mRnsS46MvnluUjEZf69Y3nTEe
X1fO+fcZyyRhwgr9FHs1o6YGhRj/vUI6vOxAFFIsGPTxGWdMwTppJ7Tx3dL/FlTb
RQ/ReDD1Pj7mh8vOdK1X6KFrqVQuB7Q+B6yaARBtSbhXt5BgyetbHreBqmwjVERw
HDASKKcCgYEA5uON5mMUIe1YZedNURLP6k0R5YI2iOqbIXK0pRR/v2HiIsvZxrpJ
b/iHz12Uk+g7RUCp1NsOyla4Wb8DVQVU6wLb6FXsoboz4T76it/rJbPn0+HMtekP
1z1UHyZ6z9FndLMLW9oPdftgbpHFYumcDGI9HA2ZGa6R
owsg7uEZdlhe3IO1Ovc6Qclca+Sq9UOaeRhFPXWB9PYx8BMz1bEd
-----END RSA PRIVATE KEY-----'



# add_vps \
# "vps1" \
# "root" \
# "1.2.3.4" \
# 'PASTE YOUR RSA KEY'


# ==========================================
# SHOW ALL SERVERS
# ==========================================

echo ""
echo "========================================"
echo "ALL SSH SERVERS ON THIS UBUNTU SERVER"
echo "========================================"

awk '
/^Host / {
    host=$2
}
/HostName / {
    ip=$2
}
/User / {
    user=$2
    printf "Server: %-15s IP: %-15s User: %s\n", host, ip, user
}
' "$CONFIG_FILE"

echo ""
echo "Quick Connect Examples:"
echo "ssh instance1"
echo "ssh instance2"
