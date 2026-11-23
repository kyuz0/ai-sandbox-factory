"""Configure the AWS profile that supplies a sandbox's restricted role session."""

saved_action = 'aws-access'


def status(config):
    from ....aws_ui import profile_label
    grant = config.get('aws_access', {})
    return 'Profile · ' + profile_label(grant) if grant else 'Off'


def draft_editor(draft, connector):
    from ....aws_ui import AWSProfilePicker
    return AWSProfilePicker(draft.get('aws_access'))


def apply_draft(draft, value):
    draft['aws_access'] = value


def hint(config):
    from ...context import Hint
    from ....aws_access import permission_values
    access = config.get('aws_access')
    if not access:
        return None
    grants = []
    for row in access['grants']:
        for action in permission_values(row['Action']):
            for resource in permission_values(row['Resource']):
                # Resource patterns are data even when they contain whitespace.
                grants.append(f'{action} on {resource!r}')
    return Hint(
        'AWS',
        f'Use AWS CLI or Boto3 for role {access["role"]} in {access["region"]}. '
        'The host sign-in stays on the user’s computer. A separate container signs requests with temporary role credentials. AWS also enforces IAM policies.',
        list(dict.fromkeys(grants)),
    )
