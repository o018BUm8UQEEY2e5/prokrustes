[Unit]
Description={desc:/[A-Za-z][A-Za-z0-9 ]{3,24}/}
After=network-online.target
Wants=network-online.target

[Service]
Type=notify
Environment=APP_ENV={env:/development|staging|production/}
Environment=LOG_LEVEL={level:/trace|debug|info|warn|error/}
ExecStart=/usr/bin/{name:/[a-z][a-z0-9-]{1,15}/} \
    --config /etc/{name:/[a-z][a-z0-9-]{1,15}/}/{name:/[a-z][a-z0-9-]{1,15}/}.toml \
    --listen {addr:/[a-z][a-z0-9-]{1,20}\.[a-z]{2,6}/}:{port:/[1-9][0-9]{0,4}/} \
    --workers {workers:/[1-9][0-9]?/} \
    --grace-period {grace:/[1-9][0-9]{0,2}/}

Restart=on-failure
RestartSec={backoff:/[1-9][0-9]?/}
TimeoutStopSec={stop:/[1-9][0-9]{0,2}/}s
User={user:/[a-z][a-z0-9-]{1,15}/}
Group={user:/[a-z][a-z0-9-]{1,15}/}

NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/{name:/[a-z][a-z0-9-]{1,15}/}

[Install]
WantedBy=multi-user.target
