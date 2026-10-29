

def parse_decision(kind, raw):
    raise NotImplementedError('not implemented: parse_decision')


def build_prompt(kind, context):
    raise NotImplementedError('not implemented: build_prompt')


def decide(kind, context, transport, mode='shadow'):
    raise NotImplementedError('not implemented: decide')


def resolve_action(kind, rules_action, decision, mode='shadow'):
    raise NotImplementedError('not implemented: resolve_action')


def log_decision(path, kind, context_summary, rules_action, decision, mode):
    raise NotImplementedError('not implemented: log_decision')
