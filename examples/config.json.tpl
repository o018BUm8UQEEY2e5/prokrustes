{{
  "service": "{name:/[a-z][a-z0-9-]{1,20}/}",
  "version": "{version:/[0-9]{1,2}\.[0-9]{1,2}\.[0-9]{1,2}/}",
  "replicas": {replicas:/[1-9][0-9]?/},
  "enabled": {enabled:/true|false/},
  "region": {region:/us-east-1|us-west-2|eu-west-1|ap-south-1/},
  "created": "{created:/[0-9]{4}-[0-9]{2}-[0-9]{2}/}",
  "owner": {{"name": "{owner:/[A-Z][a-z]{1,11} [A-Z][a-z]{1,11}/}", "email": "{email:/[a-z.]{1,12}@[a-z]{2,10}\.[a-z]{2,6}/}"}},
  "limits": {{
    "cpu": "{cpu:/[0-9]{1}\.[0-9]{2}/}",
    "memory_mb": {memory:/[1-9][0-9]{2,4}/},
    "timeout_s": {timeout:/[1-9][0-9]{0,2}/}
  }},
  "notes": "{notes:/[A-Za-z][A-Za-z ,.]{4,40}/}"
}}
