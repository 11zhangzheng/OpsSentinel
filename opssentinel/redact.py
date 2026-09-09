import re


def redact(value, secrets=()):
    if isinstance(value, dict):
        return {k: ("[REDACTED]" if re.search(r"token|password|secret|api.?key|authorization", k, re.I)
                    else redact(v, secrets)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, secrets) for v in value]
    if not isinstance(value, str):
        return value
    for secret in secrets:
        if secret:
            value = value.replace(secret, "[REDACTED]")
    value = re.sub(r"(?i)(bearer\s+)[^\s\"']+", r"\1[REDACTED]", value)
    value = re.sub(r"(?i)((?:password|token|api[_-]?key|secret)\s*[=:]\s*)[^\s,;]+", r"\1[REDACTED]", value)
    return value
