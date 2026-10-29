"""llm_advisor.py -- glue between arr-webhook seams and llm_decide: mode, transport, hard rules, logging."""

import json
import os
import urllib.request

import llm_decide

DEFAULT_MODEL = 'qwen3.6-35b-a3b-vl-mtp-mxfp8'
DEFAULT_LOG = '/data/llm_decisions.jsonl'


def http_transport(url, model, timeout=20):
    raise NotImplementedError('not implemented: http_transport')


def advise(kind, facts, rules_action, mode=None, transport=None, log_path=None, record=None, env=None):
    raise NotImplementedError('not implemented: advise')
