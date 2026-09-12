"""Public receipts must not expose native file handles as downloadable artifacts."""
from __future__ import annotations

import hashlib


def public_receipt(value):
    """Copy a receipt, retaining provenance without advertising an internal file URI.

    The original handle stays in task_actions for auditing and is still required on
    native file inputs. It is not a browser URL or a new output attachment.
    """
    if isinstance(value, dict):
        result = {key: public_receipt(item) for key, item in value.items() if key != 'file_id'}
        reference = value.get('file_id')
        if isinstance(reference, str) and reference:
            result['source_reference_sha256'] = hashlib.sha256(reference.encode('utf-8')).hexdigest()
        return result
    if isinstance(value, list):
        return [public_receipt(item) for item in value]
    return value
