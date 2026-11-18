"""Scenarios shared by Pi RPC and Pendant's shipped worker."""


def cases_for(approval, inner):
    auto = approval in ('automatic', 'assisted')
    assisted = approval == 'assisted'
    manual = approval == 'manual'
    reviewed_bash = 'bash_escalate' if inner else 'bash'
    cases = {
        'ordinary': dict(tool='bash', verdict='allow', answers=['approve'] if manual and not inner else [],
                         review_count=int(auto and not inner), writes=1),
        'protected': dict(tool='read', verdict='allow', answers=[], review_count=0, writes=0),
    }
    if manual and inner:
        cases['escalation'] = dict(tool='bash_escalate', verdict='allow', answers=['approve'], review_count=0, writes=1)
    if approval == 'off':
        cases['wrapped'] = dict(tool='bash', verdict='deny', answers=[], review_count=0, writes=1)
        cases['external'] = dict(tool='read', verdict='deny', answers=[], review_count=0, writes=0)
    if auto:
        for name, verdict, answers, count, writes in (
            ('allow', 'allow', [], 1, 1),
            ('reject', 'deny', ['deny'] if assisted else [], 1, 0),
            ('defer', 'defer', ['approve'] if assisted else [], 1, int(assisted)),
            ('unavailable', 'provider_error', [], 1, 0),
        ):
            cases[name] = dict(tool=reviewed_bash, verdict=verdict, answers=answers, review_count=count, writes=writes)
    if assisted:
        for name, verdict, answers, count, writes in (
            ('cancel', 'deny', ['cancel'], 1, 0),
            ('abort', 'deny', ['abort'], 1, 0),
            ('critical', 'critical', ['approve', 'APPROVE'], 1, 1),
            ('critical_refused', 'critical', ['approve', 'wrong'], 1, 0),
            ('repeat', 'deny', ['approve', 'deny'], 2, 1),
            ('long', 'deny', ['deny'], 1, 0),
        ):
            cases[name] = dict(tool=reviewed_bash, verdict=verdict, answers=answers, review_count=count, writes=writes)
    return cases
