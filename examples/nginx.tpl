upstream api-{name:/[a-z]{2,8}(-[a-z]{2,8}){0,2}/} {{
    zone api;
    server 10.0.{/[0-9]{1,3}/}.{/[0-9]{1,3}/}:{port:/[1-9][0-9]{0,4}/};
    keepalive {keepalive:/[1-9][0-9]{0,3}/};
}}

server {{
    listen {listen_port:/[1-9][0-9]{0,4}/} ssl;
    server_name {host:/[a-z][a-z0-9-]{1,30}\.[a-z]{2,6}/};

    ssl_certificate     /etc/ssl/certs/{name:/[a-z]{2,8}(-[a-z]{2,8}){0,2}/}.pem;
    ssl_certificate_key /etc/ssl/private/{name:/[a-z]{2,8}(-[a-z]{2,8}){0,2}/}.key;
    ssl_protocols       TLSv1.{tls:/[23]/};

    location = /healthz {{
        access_log off;
        return 200 "{status:/ok|healthy/}\n";
    }}

    location / {{
        proxy_pass http://api-{name:/[a-z]{2,8}(-[a-z]{2,8}){0,2}/};
        proxy_set_header X-Request-Id {trace:/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/};
        proxy_read_timeout {timeout:/[1-9][0-9]{0,2}/}s;
    }}
}}
