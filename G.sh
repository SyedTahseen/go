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
    PASSWORD="$5"   # optional

    # Remove old config if exists
    sed -i "/Host $SERVER_NAME/,+6d" "$CONFIG_FILE"

    if [ -n "$PRIVATE_KEY" ]; then
        # Key-based server (original behavior)
        KEY_FILE="$SSH_DIR/${SERVER_NAME}_key"
        echo "$PRIVATE_KEY" > "$KEY_FILE"
        chmod 600 "$KEY_FILE"

        cat >> "$CONFIG_FILE" <<EOF

Host $SERVER_NAME
    HostName $SERVER_IP
    User $USERNAME
    IdentityFile $KEY_FILE
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null

EOF
        echo "[ADDED] $SERVER_NAME (key-based)"

    elif [ -n "$PASSWORD" ]; then
        # Password-based server using sshpass
        cat >> "$CONFIG_FILE" <<EOF

Host $SERVER_NAME
    HostName $SERVER_IP
    User $USERNAME
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null
    ProxyCommand sshpass -p '$PASSWORD' ssh -o StrictHostKeyChecking=no -W %h:%p $USERNAME@$SERVER_IP

EOF
        echo "[ADDED] $SERVER_NAME (password-based)"
    else
        echo "ERROR: Either PRIVATE_KEY or PASSWORD must be provided!"
    fi
}

# ==========================================
# VPS 1 (key-based)
# ==========================================
add_vps \
"instance1" \
"ubuntu" \
"141.148.43.81" \
'-----BEGIN RSA PRIVATE KEY-----
MIIEowIBAAKCAQEArtAZ2S8/odHZyppwg/QxC9M4Qkp+IFbFK1Io00UwDx1EOLI3
...
-----END RSA PRIVATE KEY-----'

# ==========================================
# VPS 2 (password-based)
# ==========================================
add_vps \
"instance2" \
"stahseen" \
"s10.serv00.com" \
"" \
"LJ4yZ!&LXD8uQl$YKzH("

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
