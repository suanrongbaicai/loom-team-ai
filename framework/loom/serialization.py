"""Stable JSON text shared by platform records and approval comparisons."""
import json


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
