name: {name:/[a-z][a-z0-9-]{1,20}/}
version: {version:/[0-9]{1,2}\.[0-9]{1,2}\.[0-9]{1,2}/}
image: {repo:/[a-z][a-z0-9-]{1,18}/}/{name:/[a-z][a-z0-9-]{1,20}/}:{tag:/[A-Za-z0-9][A-Za-z0-9._-]{1,14}/}
replicas: {replicas:/[1-9][0-9]?/}

ports:
  - name: http
    containerPort: {container_port:/[1-9][0-9]{0,4}/}
    protocol: TCP
  - name: metrics
    containerPort: {metrics_port:/[1-9][0-9]{0,4}/}
    protocol: TCP

env:
  - name: LOG_LEVEL
    value: {log:/debug|info|warn|error/}
  - name: FEATURE_FLAGS
    value: "{flags:/[a-z]{3}(,[a-z]{3}){1,5}/}"
  - name: REQUEST_ID
    value: {reqid:/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/}

resources:
  requests:
    cpu: "{cpu_request:/[0-9]{1}\.[0-9]{2}/}"
    memory: {memory_request:/[1-9][0-9]{2,4}/}Mi
  limits:
    cpu: "{cpu_limit:/[0-9]{1}\.[0-9]{2}/}"
    memory: {memory_limit:/[1-9][0-9]{2,4}/}Mi

annotations:
  checksum/config: {checksum:/[0-9a-f]{8}/}
  owner: {owner:/[a-z]{2,12} [a-z]{2,12}/}
