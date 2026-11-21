# Run AWS CLI commands in a sandbox

On the computer running Factory, install AWS CLI v2 and sign in with the profile you use for your account. For an IAM Identity Center login, configure the profile once with `aws configure sso` and sign in with `aws sso login --profile my-work-profile`.

Use a dedicated IAM role for sandbox work. Its trust policy must allow your host identity to call `sts:AssumeRole`. Your identity must have permission to assume it. Give the role the actions and resources the sandbox may need. Factory applies a second, per-sandbox policy each time it obtains a session. AWS enforces both policies. [AWS role sessions](https://docs.aws.amazon.com/STS/latest/APIReference/API_AssumeRole.html)

1. During creation, open **Agent tools → AWS access → Configure…**. For an existing sandbox, open **Settings → AWS access**.
2. Select a saved AWS access profile, or choose **New…** and give it a name. Enter the host AWS CLI profile name, the dedicated role ARN and the AWS region.
3. Enter one action and resource per line. For example, `ec2:DescribeRegions *` lets Pi list regions. For a resource-scoped action, enter the resource ARN or ARN pattern. Repeat an action on another line to add another resource.
4. Save the profile and choose **Use profile**. Try `aws ec2 describe-regions` if you granted that action. In Python, `boto3.client("ec2").describe_regions()` uses the same permissions.

The first connection installs AWS CLI and Boto3. If a sandbox already has a container,
Factory asks before replacing it. Project files, the sandbox home folder and saved
chats are kept. Active work stops, and files stored only in the old container, such
as files in `/tmp`, are removed. A stopped sandbox stays stopped. For an optional
backup of Pi or FrostPi conversations, choose **Actions… → Export chat history…**.
If Factory reports that an older Pi sandbox needs a runtime update, use
**Actions… → Update sources and recreate…** before connecting AWS.

Open **Library → AWS access** to edit or duplicate a profile. A duplicate has its own name and permissions. After you save an edit, select the sandboxes that should receive it and choose **Apply to selected**. Unchecked sandboxes keep their saved permissions. Choose **Skip** to save only to the library, or **Apply saved update…** to update more sandboxes later. Running sandboxes receive the new permissions immediately. Stopped sandboxes use them at their next start.

Open **Settings → AWS access** to assign a different profile or disconnect. **Disconnect** removes the signer. Assigning a profile again reuses the AWS tools in the current sandbox container. Stopping the sandbox stops signing and renewal. When your host sign-in expires, sign in again on the host so Factory can renew the session.

## Credentials and permissions

The host obtains the role session. A separate container holds its credentials in memory and signs requests for that sandbox. Pi's CLI receives a local endpoint and placeholder credentials. Each allowed action can be used by any process in that sandbox. Include the effect of granting access to secret data, changing IAM permissions, or delegating work to another AWS role when choosing permissions.

Use individual IAM actions and resource ARNs or `*`. The sandbox grant supports simple action and resource rules. Put conditions and other restrictions in the role policy. The host handles STS operations. This connection supports standard regional HTTPS service endpoints with ordinary Signature Version 4 requests. Presigned URLs, chunk-signed uploads, interactive streaming protocols and custom service endpoints require other mechanisms.

For noninteractive setup, supply `--aws-access-file PATH` to `create`. The JSON file contains settings only.

```json
{
  "profile": "my-work-profile",
  "role": "arn:aws:iam::123456789012:role/SandboxResearch",
  "region": "eu-west-1",
  "grants": [
    {"Action": "ec2:DescribeRegions", "Resource": "*"}
  ]
}
```
