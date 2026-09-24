# `esign/`: documents out for signature

Behind the `request_signature` and `signature_status` tools
(`chat/tools/esign.py`). **Off by default** (`ESIGN_ENGINE=none`).

## Files

| File | What it does |
|---|---|
| `models.py` | `SignatureRequest`: a document sent for signing, and its status |
| `provider.py` | The e-sign provider, chosen by `ESIGN_ENGINE` |
| `views.py`, `urls.py` | The webhook the provider calls when a document is signed or declined |
