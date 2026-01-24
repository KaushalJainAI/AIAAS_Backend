import logging

class SensitiveDataFilter(logging.Filter):
    """
    Scrub sensitive data from log records.
    Redacts keys like password, api_key, token, secret.
    """
    SENSITIVE_KEYS = {
        'password', 'api_key', 'apikey', 'access_token', 'refresh_token', 
        'secret', 'client_secret', 'token', 'private_key', 'key'
    }

    def filter(self, record):
        # 1. Scrub message string if it's a string
        if isinstance(record.msg, str):
            record.msg = self._scrub_string(record.msg)
            
        # 2. Scrub args if present
        if record.args:
            if isinstance(record.args, dict):
                 # record.args is the mapping itself
                 record.args = {
                     k: (self._scrub_string(v) if isinstance(v, str) else v)
                     for k, v in record.args.items()
                 }
            elif isinstance(record.args, (list, tuple)):
                # record.args is a sequence (standard)
                record.args = tuple(
                    self._scrub_string(arg) if isinstance(arg, str) else arg 
                    for arg in record.args
                )
            
        return True

    def _scrub_string(self, text):
        # Naive keyword checking, could be improved with regex
        # This prevents "password='123'" typical patterns in logs
        # But for arbitrary text it's hard. 
        # Here we primarily catch structured logs or dict-like strings?
        # Actually, python logging often does string interpolation.
        
        # Simple heuristic: if we see 'password' and then some value, mask it?
        # That's dangerous to guess. 
        # Better: if this filter is used, we assume the log MIGHT contain sensitive data.
        # But scrubbing arbitrary strings is hard.
        
        # Maybe the intention is to scrub DICTIONARIES passed as args?
        # If record.msg is "Data: %s" and arg is a dict.
        
        # Let's focus on known patterns or request dictionaries.
        return text # Placeholder if we can't safely scrub strings.

    # Better approach: Scrub specific attributes of the record if they are dicts
    # But standard logging stores messsage as string.
    
    # Let's try to scrub if the message LOOKS like a dict representation
    # OR if the user implementation logs dicts.
    
    # REVISED STRATEGY based on typical Django usage:
    # Users might log `logger.info(f"User data: {data_dict}")`
    # We can try to regex replace `(password|token|...)\s*[:=]\s*['"]?([^'",\s}]+)['"]?`
    
    def _scrub_string(self, text):
        import re
        # Pattern to catch key=value or key: value or key': 'value'
        # keys: password, token, key, secret
        pattern = r"((?:password|api_key|apikey|access_token|refresh_token|secret|client_secret|token|private_key|key)[\"']?\s*[:=]\s*[\"']?)([^\"',\}\s]+)([\"']?)"
        
        return re.sub(pattern, r"\1***REDACTED***\3", text, flags=re.IGNORECASE)
