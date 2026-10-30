"""llm_advisor.py -- glue between arr-webhook seams and llm_decide: mode, transport, hard rules, logging."""

import json
import os
import urllib.request

import llm_decide

DEFAULT_MODEL = 'qwen3.6-35b-a3b-vl-mtp-mxfp8'
DEFAULT_LOG = '/data/llm_decisions.jsonl'


def http_transport(url, model, timeout=20):
    def transport(prompt):
        import urllib.request
        data = {
            'model': model,
            'messages': [{'role': 'user', 'content': prompt}],
            'temperature': 0.2,
            'max_tokens': 1024,
            'chat_template_kwargs': {'enable_thinking': False},
        }
        body = json.dumps(data).encode('utf-8')
        req = urllib.request.Request(url, data=body, headers={'Content-Type': 'application/json'})
        key = os.environ.get('LLM_DECIDE_KEY')
        if key:
            req.add_header('Authorization', 'Bearer ' + key)
        resp = urllib.request.urlopen(req, timeout=timeout)
        result = json.loads(resp.read())
        return result['choices'][0]['message']['content']
    return transport


def advise(kind, facts, rules_action, mode=None, transport=None, log_path=None, record=None, env=None):
    """Return the action the caller should take."""
    try:
        if env is None:
            env = os.environ

        # Determine mode from env or explicit parameter
        if mode is None:
            mode = (env.get('LLM_DECISIONS', 'off') or '').strip().lower()
        if mode not in ('shadow', 'enforce'):
            mode = 'off'

        # When off: return rules_action at once (no transport call, no log, no record)
        if mode == 'off':
            return rules_action

        # Build transport if needed
        if transport is None:
            url = env.get('LLM_DECIDE_URL')
            if url:
                transport = http_transport(
                    url,
                    env.get('LLM_DECIDE_MODEL', DEFAULT_MODEL),
                    float(env.get('LLM_DECIDE_TIMEOUT', '20'))
                )

        # Get LLM decision
        decision = llm_decide.decide(kind, facts, transport, mode)

        # Apply hard rules when decision is not None
        if decision is not None:
            action, rule = llm_decide.apply_hard_rules(kind, decision['action'], facts)
            if rule is not None:
                decision = dict(decision, action=action,
                                reason='[hard rule %s] %s' % (rule, decision['reason']))

        # Log the decision
        llm_decide.log_decision(
            log_path or env.get('LLM_DECISIONS_LOG', DEFAULT_LOG),
            kind,
            json.dumps(facts, default=str, sort_keys=True)[:500],
            rules_action,
            decision,
            mode
        )

        # Call record callback if provided
        if record is not None:
            try:
                record('llm-decision', {
                    'kind': kind,
                    'mode': mode,
                    'rules_action': rules_action,
                    'llm_action': decision['action'] if decision else None
                })
            except Exception:
                pass

        # Resolve final action
        return llm_decide.resolve_action(kind, rules_action, decision, mode)

    except Exception:
        return rules_action


