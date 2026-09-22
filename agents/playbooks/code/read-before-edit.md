## Read before edit

Edits are checked against what you last read — a stale edit is refused.

- Always `ws_read` a file before `ws_edit` or `ws_apply_patch` in the same run.
- If an edit is refused as stale, re-read the file and rebase your change onto
  what is actually there now. Do not retry the same old text.
- The refusal names who changed the file. If it was another worker, coordinate
  through the lead rather than editing around them.
- For a new file, `ws_write` refuses when the file already exists — read it
  first instead of overwriting blind.
